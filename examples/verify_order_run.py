#!/usr/bin/env python3
"""Independently verify stress trace vs durable journal, fills, targets and cash.

--exchange performs only GETs against Spot Testnet and saves raw order/trade evidence.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'python'),str(Path(__file__).resolve().parent)]
from mininautilus.bridge import ROOT
from mininautilus.binance import BinanceSpot,units
from sma_spot import load_env


def verify(path,exchange=False,env_file=Path('.env.testnet')):
    started=time.monotonic()
    state=json.loads(subprocess.check_output([str(ROOT/'target/debug/mininautilus'),'inspect',str(path/'events.jsonl')]))
    replay_seconds=time.monotonic()-started
    rows=[json.loads(line) for line in (path/'trace.jsonl').read_text().splitlines()]
    events=[r for r in rows if 'event' in r]
    durable=[]
    for line in (path/'events.jsonl').read_text().splitlines():
        payload=json.loads(json.loads(line)['payload'])
        if 'Input' in payload:durable.append(payload['Input'])
    if len(events)!=len(durable):raise ValueError('trace omitted or added durable input events')
    accepted=set();refused=0
    for logged,saved in zip(events,durable):
        if logged['seq']!=saved['seq'] or logged['event']!=saved['event']:raise ValueError('trace/journal event mismatch')
        for effect in logged['effects']:
            if 'Refused' in effect:refused+=1
            if 'SendOrder' not in effect:continue
            intent=effect['SendOrder'];command=logged['event']['SubmitTargeted'];target=logged['target']
            position=logged.get('engine_position',logged.get('position'))
            if intent['id'] in accepted:raise ValueError('same client ID dispatched twice')
            if target['revision']!=command['revision'] or position!=command['expected_position']:raise ValueError('accepted stale revision/position')
            delta=intent['qty']*(1 if intent['side']=='Buy' else -1)
            if delta!=target['position']-position:raise ValueError('accepted order differs from target delta')
            accepted.add(intent['id'])
    meta=json.loads((path/'session.json').read_text())
    if meta['mode']=='adversarial-paper':
        ledger=json.loads((path/'venue-ledger.json').read_text())
        orders=list(ledger['orders'].values());fills=list(ledger['fills'].values())
        venue_position=ledger['position']
    else:
        barriers=[r for r in rows if r.get('kind')=='barrier']
        if not barriers:raise ValueError('no quiescent exchange barrier')
        snapshot=barriers[-1]['snapshot'];orders=snapshot['orders'];fills=snapshot['fills'];venue_position=snapshot['position']
    expected_position=sum(o['filled']*(1 if o['intent']['side']=='Buy' else -1) for o in orders)
    order_map={o['intent']['id']:o for o in orders}
    expected_cash=sum(f['qty']*f['price']*(-1 if order_map[f['order_id']]['intent']['side']=='Buy' else 1) for f in fills)
    if {str(f['execution_id']):f for f in fills}!=state['fills']:raise ValueError('venue/engine fill ledger differs')
    if len(fills)!=len(state['fills']):raise ValueError('duplicate venue fills')
    if state['position']!=venue_position or venue_position!=expected_position:raise ValueError('position mismatch')
    if state['cash']!=expected_cash:raise ValueError('cash mismatch')
    if set(map(int,state['orders']))!=accepted:raise ValueError('accepted order set mismatch')
    result=dict(engine_events=state['seq'],orders=len(accepted),refused_actions=refused,fills=len(fills),
                position_lots=state['position'],cash_tick_lots=state['cash'],replay_seconds=round(replay_seconds,4))
    if exchange:
        if meta['mode']!='testnet-churn':raise ValueError('--exchange requires a Testnet churn run')
        load_env(env_file);venue=BinanceSpot(meta['symbol'],meta['session'],execute=False);venue.initialize()
        if (str(venue.tick),str(venue.lot))!=(meta['tick'],meta['lot']):raise ValueError('instrument units changed')
        raw_orders=[];raw_trades=[];independent_fills=[];fees={};exchange_position=0;exchange_cash=0
        for local in state['orders'].values():
            intent=local['intent'];remote=venue.query(intent);trades=venue.trades(remote['orderId'])
            if remote['status'] not in ('FILLED','CANCELED','EXPIRED','EXPIRED_IN_MATCH','REJECTED'):raise ValueError('exchange order still open')
            qty=sum((Decimal(t['qty']) for t in trades),Decimal(0))
            if qty!=Decimal(remote['executedQty']):raise ValueError('exchange order/history mismatch')
            sign=1 if intent['side']=='Buy' else -1
            for trade in trades:
                if bool(trade['isBuyer'])!=(sign==1) or trade['symbol']!=meta['symbol']:raise ValueError('exchange trade direction/symbol mismatch')
                q,p=units(trade['qty'],venue.lot),units(trade['price'],venue.tick)
                exchange_position+=sign*q;exchange_cash-=sign*q*p
                independent_fills.append(dict(execution_id=trade['id']+1,order_id=intent['id'],qty=q,price=p))
                asset=trade['commissionAsset'];fees[asset]=fees.get(asset,Decimal(0))+Decimal(trade['commission'])
            raw_orders.append(remote);raw_trades.extend(trades)
        if {str(f['execution_id']):f for f in independent_fills}!=state['fills'] or len(independent_fills)!=len(state['fills']):raise ValueError('fresh exchange trades differ from engine fills')
        if exchange_position!=state['position'] or exchange_cash!=state['cash']:raise ValueError('fresh exchange position/cash mismatch')
        delta=venue.base_balance()-Decimal(meta['baseline_base'])
        expected_delta=venue.lot*exchange_position-fees.get(venue.base_asset,Decimal(0))
        if delta!=expected_delta or venue.open_orders():raise ValueError('wallet drift or open orders remain')
        evidence=dict(raw_orders=raw_orders,raw_trades=raw_trades,commissions={k:str(v) for k,v in fees.items()},
                      observed_base_change=str(delta),expected_base_change=str(expected_delta),position_lots=exchange_position,cash_tick_lots=exchange_cash)
        (path/'exchange-audit.json').write_text(json.dumps(evidence,indent=2))
        result.update(exchange_verified=True,exchange_trade_count=len(raw_trades),base_change=str(delta),commissions=evidence['commissions'])
    (path/('exchange-verification.json' if exchange else 'verification.json')).write_text(json.dumps(result,indent=2))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path)
    p.add_argument('--exchange',action='store_true');p.add_argument('--env-file',type=Path,default=Path('.env.testnet'))
    a=p.parse_args();print(json.dumps(verify(a.run_dir,a.exchange,a.env_file)))
