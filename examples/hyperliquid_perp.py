#!/usr/bin/env python3
"""Single-perp Hyperliquid migration diagnostic; no mainnet execution path.

SMA tests long/short execution, not the cluster-PCA alpha. Paper touch fills are
optimistic plumbing and its gross PnL is NOT funding/fee-adjusted performance.
"""
import argparse
import asyncio
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))
from mininautilus.audit import IncrementalAudit
from mininautilus.bridge import Engine
from mininautilus.evidence import build_identity, verified_identity
from mininautilus.hyperliquid import Hyperliquid, HyperliquidError, ActionJournal, decimal, testnet_signer, testnet_credentials, account_writer_lock
from mininautilus.hyperliquid_live import HyperliquidActorAdapter, flatten_known_position
from mininautilus.lifecycle import publish_sidecar
from mininautilus.live_actor import ActorSession
from mininautilus.sma import SmaStrategy


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['probe', 'paper', 'testnet'], default='probe')
    p.add_argument('--network', choices=['mainnet', 'testnet'], default='testnet')
    p.add_argument('--symbol', default='BTC')
    p.add_argument('--interval', choices=['1m','3m','5m','15m','30m','1h'], default='1m')
    p.add_argument('--quantity', default='0.0002')
    p.add_argument('--max-notional', default='40')
    p.add_argument('--max-loss', default='10')
    p.add_argument('--fast', type=int, default=3)
    p.add_argument('--slow', type=int, default=8)
    p.add_argument('--seconds', type=float, default=900)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--binary', type=Path, default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt', type=Path)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--flatten', action='store_true', help='After proven reconciliation, close known Testnet exposure')
    p.add_argument('--reconcile-only', action='store_true')
    p.add_argument('--recovery-drill', type=float, action='append', default=[])
    return p


def rss_bytes(pid):
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith('VmRSS:'): return int(line.split()[1])*1024
    raise RuntimeError('RSS unavailable')


async def run_measured(session, seconds, measurements, note):
    """Record runtime separately from startup/final reconciliation, sample combined RSS."""
    start = time.monotonic()
    task = asyncio.create_task(session.run(seconds))
    try:
        while not task.done():
            rss = rss_bytes(os.getpid())+rss_bytes(session.engine.process.pid)
            measurements['rss_peak_bytes'] = max(measurements['rss_peak_bytes'], rss)
            note('resources', elapsed_s=time.monotonic()-start, rss_bytes=rss)
            if rss > 384*1024*1024:
                session.fatal = 'rss_budget_exceeded'
            await asyncio.wait([task], timeout=2)
        await task
    finally:
        measurements['runtime_s'] = time.monotonic()-start
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def run(args):
    if not 0 < args.seconds <= 900: raise ValueError('diagnostic duration must be in (0,900]')
    execute = args.mode == 'testnet'
    if execute and args.network != 'testnet': raise ValueError('no mainnet execution')
    if args.resume and not execute: raise ValueError('resume is Testnet only')
    if args.reconcile_only and not args.resume: raise ValueError('reconcile-only requires --resume')
    account = agent = None
    signer = None
    provenance = None
    if execute:
        account, agent, key = testnet_credentials()
        signer = testnet_signer(key, agent)
        if args.receipt is None: raise ValueError('Testnet requires --receipt for release provenance')
        provenance = verified_identity(ROOT, args.binary, args.receipt)
        if provenance['dirty']: raise ValueError('Testnet acceptance requires clean source')
    elif args.mode == 'paper':
        provenance = build_identity(ROOT, args.binary)
    path = args.run_dir
    if not args.resume: path.mkdir(parents=True, exist_ok=False)
    invocation = uuid.uuid4().hex
    started = time.monotonic()
    venue = Hyperliquid(args.symbol, network=args.network, account=account, execute=execute,
                       signer=signer, agent_address=agent, max_notional=args.max_notional, max_loss=args.max_loss)
    venue.initialize()
    adapter = HyperliquidActorAdapter(venue, args.interval)
    if args.mode == 'probe':
        result = dict(mode='public_probe', network=args.network, instrument=asdict(venue.instrument),
                      quote=venue.quote(), closed_bars=[asdict(b) for b in adapter.warmup(3)])
        with adapter.stream() as stream:
            while not adapter.subscription_confirmed:
                next(stream)
        result.update(status='PASS', elapsed_s=time.monotonic()-started,
                      scope='public REST data and one bounded WS receive; no account or order test')
        publish_sidecar(path/'probe.json', result)
        print(json.dumps(result)); return
    lots = venue.instrument.size(args.quantity)
    venue.max_position_lots = lots
    params = dict(network=args.network, mode=args.mode, instrument=asdict(venue.instrument),
                  quantity=args.quantity, max_notional=args.max_notional, max_loss=args.max_loss,
                  fast=args.fast, slow=args.slow, interval=args.interval, account=account)
    strategy = SmaStrategy(args.fast, args.slow, lots, args.interval, long_only=False)
    if args.resume:
        metadata = json.loads((path/'session.json').read_text())
        if metadata['parameters'] != params: raise ValueError('resume identity/config mismatch')
    else:
        cash = None
        if execute:
            venue.check_account_mode()
            venue.check_leverage()
            state = venue.account_state()
            if venue.open_orders() or any(decimal(p['position']['szi']) for p in state['assetPositions']):
                raise HyperliquidError('fresh session requires flat dedicated account/no open orders')
            cash = str(decimal(state['marginSummary']['accountValue']))
            if decimal(cash) < 100: raise HyperliquidError('Testnet collateral below 100 USDC')
        metadata = dict(session=uuid.uuid4().hex[:16], started_ms=int(time.time()*1000), baseline_cash=cash,
                        parameters=params)
        publish_sidecar(path/'session.json', metadata)
        publish_sidecar(path/'config.json', dict(max_abs_position=lots, max_order_qty=2*lots,
            max_order_notional=int(decimal(args.max_notional)/(venue.tick*venue.lot)),
            request_timeout_ms=30000, market_stale_ms=3000, private_stale_ms=30000, max_signal_lag=10))
    venue.session, venue.started_ms = metadata['session'], metadata['started_ms']
    venue.baseline_cash = None if metadata['baseline_cash'] is None else decimal(metadata['baseline_cash'])
    if provenance: publish_sidecar(path/f'provenance-{invocation}.json', provenance)
    context = ActionJournal(path/'actions.jsonl') if execute else nullcontext(None)
    with context as action_journal, (path/f'observations-{invocation}.jsonl').open('x') as log:
        venue.actions = action_journal
        with Engine(path/'events.jsonl', paper=not execute, recover=args.resume,
                    config=path/'config.json', binary=args.binary, extra_args=['--sync','outbox']) as engine:
            origin, initial = time.monotonic(), engine.state['now']
            def now(): return initial+int((time.monotonic()-origin)*1000)
            def note(kind, **data):
                log.write(json.dumps(dict(kind=kind, **data))+'\n'); log.flush()
            audit = IncrementalAudit(lots, long_only=False)
            audit.check(engine.state, None)
            session = ActorSession(engine, adapter, strategy,
                identity=('hyperliquid-'+args.network, metadata['session'], args.symbol), now=now, note=note,
                audit=audit, lots=lots, paper=not execute, allow_short=True, max_orders=100,
                order_ttl_ms=10000, drills=args.recovery_drill, heartbeat_seconds=5,
                execution_timeout=20, quiescence_seconds=30, gc_retry=False)
            completed = reconciled = False
            measurements = dict(runtime_s=0., rss_peak_bytes=0)
            error_type = None
            try:
                if execute: session._reconcile()
                if not args.reconcile_only: asyncio.run(run_measured(session, args.seconds, measurements, note))
                completed = True
            except Exception as error:
                error_type = type(error).__name__
                raise
            finally:
                try:
                    closed = session.close(reconcile=execute)
                    if args.flatten and execute and closed and not session.fail_closed:
                        flatten_known_position(engine, venue)
                    reconciled = closed
                finally:
                    limits_pass = (args.reconcile_only or abs(measurements['runtime_s']-args.seconds) <= 5)
                    limits_pass &= measurements['rss_peak_bytes'] <= 384*1024*1024
                    limits_pass &= session.high_water['input'] <= 128 and session.high_water['effects'] <= 32
                    summary = dict(session.report(), **measurements, queue_limits=dict(input=128, effects=32), completed=completed, reconciled=reconciled,
                        error_type=error_type, mode=args.mode, network=args.network,
                        elapsed_s=time.monotonic()-started, requested_s=args.seconds,
                        position_lots=engine.state['position'], core_gross_cash_ticks_lots=engine.state.get('cash'),
                        account_reconciliation=venue.last_reconciliation,
                        strategy='SMA execution diagnostic, not PCA',
                        paper_cost_model='optimistic touch, no fees/funding' if not execute else None,
                        binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
                        status='PASS' if completed and reconciled and not session.fail_closed and limits_pass else 'FAIL')
                    publish_sidecar(path/f'summary-{invocation}.json', summary)
                    print(json.dumps(summary))
                    if summary['status'] != 'PASS': raise HyperliquidError('session acceptance failed')


if __name__ == '__main__':
    try:
        args = parser().parse_args()
        lock = account_writer_lock(testnet_credentials()[0]) if args.mode == 'testnet' else nullcontext()
        with lock: run(args)
    except Exception as error:
        # Exceptions from external SDK/schema paths are never printed with credentials/payloads.
        print(json.dumps(dict(status='FAIL', error_type=type(error).__name__,
                              reason='see prerequisites and retained run evidence; no mutation retry')), file=sys.stderr)
        sys.exit(1)
