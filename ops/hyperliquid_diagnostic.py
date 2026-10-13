#!/usr/bin/env python3
"""Bounded Testnet execution/recovery diagnostic, NOT a trading strategy.

All four orders originate in the durable Rust writer. At the first filled order,
kill that writer before applying reports and recover from the original journal.
No mutation is retried. A failure leaves evidence and requires reconciliation.
"""
import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))
from mininautilus.bridge import Engine, next_order_id
from mininautilus.evidence import verified_identity
from mininautilus.hyperliquid import (ActionJournal, Hyperliquid, HyperliquidError,
    account_writer_lock, testnet_credentials, testnet_signer, decimal)
from mininautilus.lifecycle import publish_sidecar
from mininautilus.hyperliquid_live import reconcile_engine, submit_target



def run(args):
    account, agent, key = testnet_credentials()
    identity = verified_identity(ROOT, args.binary, args.receipt)
    if identity['dirty']: raise ValueError('clean source required for Testnet evidence')
    args.run_dir.mkdir(parents=True, exist_ok=False)
    publish_sidecar(args.run_dir/'provenance.json', identity)
    venue = Hyperliquid(account=account, execute=True, signer=testnet_signer(key, agent),
                       max_notional='40', max_loss='10')
    venue.initialize()
    venue.check_account_mode()
    if venue.open_orders() or venue.account_state()['assetPositions']:
        raise HyperliquidError('diagnostic requires a flat dedicated Testnet account')
    with ActionJournal(args.run_dir/'actions.jsonl') as actions:
        venue.actions = actions
        if args.configure_leverage: venue.configure_testnet_leverage()
        venue.check_leverage()
        venue.session = uuid.uuid4().hex[:16]
        venue.baseline_cash = decimal(venue.account_state()['marginSummary']['accountValue'])
        venue.started_ms = int(time.time()*1000)
        lots = venue.instrument.size('0.0002')
        venue.max_position_lots = lots
        # Same metadata as the SMA runner so --resume --reconcile-only can recover a failed drill.
        publish_sidecar(args.run_dir/'session.json', dict(session=venue.session, started_ms=venue.started_ms,
            baseline_cash=str(venue.baseline_cash), parameters=dict(network='testnet', mode='testnet',
            instrument=dict(symbol=venue.symbol, asset=venue.instrument.asset, sz_decimals=venue.instrument.sz_decimals),
            quantity='0.0002', max_notional='40', max_loss='10', fast=3, slow=8, interval='1m', account=account)))
        config=args.run_dir/'config.json'
        publish_sidecar(config, dict(max_abs_position=lots, max_order_qty=2*lots,
            max_order_notional=int(Decimal('40')/(venue.tick*venue.lot)), request_timeout_ms=30000,
            market_stale_ms=3000, private_stale_ms=30000, max_signal_lag=10))
        journal=args.run_dir/'events.jsonl'
        states=[]
        try:
            with Engine(journal, config=config, binary=args.binary, extra_args=['--sync','outbox']) as engine:
                reconcile_engine(engine,venue)
                submit_target(engine,venue,lots)
                # The exchange accepted the order, but Rust has received no ack/fill.
                engine.process.kill(); engine.process.wait()
            with Engine(journal, recover=True, binary=args.binary, extra_args=['--sync','outbox']) as engine:
                if engine.state['health']=='Healthy': raise AssertionError('restart must be gated')
                reconcile_engine(engine,venue)
                if engine.state['position'] != lots: raise AssertionError('lost response recovery position')
                states.append(lots)
                for target in (0,-lots,0):
                    submit_target(engine,venue,target)
                    reconcile_engine(engine,venue)
                    if engine.state['position'] != target: raise AssertionError('target/venue mismatch')
                    states.append(target)
                result=dict(status='PASS', position_path=states, orders=len(engine.state['orders']),
                    fills=len(engine.state['fills']), recovery='Rust SIGKILL after venue fill, before report',
                    final_position=engine.state['position'], final_open_orders=len(venue.open_orders()),
                    account=venue.last_reconciliation, duplicate_send_attempts=0,
                    scope='Testnet synthetic execution diagnostic, not strategy performance')
                publish_sidecar(args.run_dir/'result.json',result)
                print(json.dumps({k:v for k,v in result.items() if k!='account'}))
        except Exception as error:
            publish_sidecar(args.run_dir/'failure.json',dict(status='FAIL',error_type=type(error).__name__,
                reason=str(error) if isinstance(error,HyperliquidError) else 'diagnostic failed',
                recovery='Use hyperliquid_perp.py --resume --reconcile-only with this run directory'))
            raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt',type=Path,default=ROOT/'target/runtime-v2-build.json')
    p.add_argument('--configure-leverage',action='store_true')
    args=p.parse_args()
    try:
        with account_writer_lock(testnet_credentials()[0]): run(args)
    except Exception as error:
        print(json.dumps(dict(status='FAIL',error_type=type(error).__name__)),file=sys.stderr)
        sys.exit(1)
