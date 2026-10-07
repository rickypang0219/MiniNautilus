#!/usr/bin/env python3
"""Bounded Spot Testnet place/cancel/requote experiment with versioned target guards.

Synthetic long/flat targets exercise order management. No production endpoint exists.
Replacement means cancel -> confirm terminal/fills -> recalculate delta -> new order.
"""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
import uuid
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'python'),str(Path(__file__).resolve().parent)]
from mininautilus.binance import BinanceSpot,VenueError,units
from mininautilus.bridge import Engine
from mininautilus.targets import target_event,target_order,TERMINAL
from sma_spot import load_env,validate,audit


def save(path,value):
    with path.open('x') as handle:
        json.dump(value,handle);handle.flush();os.fsync(handle.fileno())


def run(args):
    load_env(args.env_file)
    path=args.run_dir
    if args.recover_only:
        meta=json.loads((path/'session.json').read_text())
        args.symbol=meta['symbol'];args.quantity=Decimal(meta['quantity'])
    else:
        path.mkdir(parents=True,exist_ok=False)
        meta=dict(session=uuid.uuid4().hex[:8],symbol=args.symbol,quantity=str(args.quantity),mode='testnet-churn')
    venue=BinanceSpot(args.symbol,meta['session'],execute=True);venue.initialize()
    lots=units(str(args.quantity),venue.lot)
    if args.recover_only:
        if (meta['tick'],meta['lot'])!=(str(venue.tick),str(venue.lot)):raise ValueError('instrument increments changed')
    else:
        if venue.open_orders():raise VenueError('fresh run requires no open orders')
        meta.update(tick=str(venue.tick),lot=str(venue.lot),baseline_base=str(venue.base_balance()))
        save(path/'session.json',meta)
        save(path/'config.json',dict(max_abs_position=lots,max_order_qty=lots,max_order_notional=int(args.max_notional/(venue.tick*venue.lot)),
                                   request_timeout_ms=30000,market_stale_ms=5000,private_stale_ms=30000,max_signal_lag=10))
        fd=os.open(path,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    invocation=uuid.uuid4().hex
    stats=dict(wire_submits=0,wire_cancels=0,replacements=0,stale_intents_blocked=0,duplicate_reports=0,barriers=0,checks=0)
    completed=False;reconciled=False;start=time.monotonic()
    with Engine(path/'events.jsonl',config=path/'config.json',recover=args.recover_only) as engine,(path/'trace.jsonl').open('a') as trace:
        initial=engine.state['now']
        def now():return initial+int((time.monotonic()-start)*1000)
        def note(kind,**data):
            row=dict(kind=kind,at=now(),invocation=invocation,**data)
            trace.write(json.dumps(row)+'\n');trace.flush()
        if args.recover_only:
            # DurableEngine.recover appends this gate before emitting its startup response.
            note('engine',event='Disconnect',effects=[],position=engine.state['position'],
                 target=engine.state['target'],seq=engine.state['seq'],source='startup_recovery')
        def send(event):
            effects=engine.send(now(),event,**venue.timing(event));audit(engine.state,lots);stats['checks']+=1
            note('engine',event=event,effects=effects,position=engine.state['position'],target=engine.state['target'],seq=engine.state['seq'])
            return effects
        def deliver(report):
            effects=send({'Execution':dict(epoch=engine.state['epoch'],venue_seq=engine.state['venue_seq']+1,report=report)})
            if any('QueryState' in e for e in effects):raise VenueError('private report needs recovery')
        def refresh():
            venue.base_balance() # Real signed heartbeat even with zero active orders.
            send({'Heartbeat':{'epoch':engine.state['epoch']}})
            observed=now();quote=venue.quote()
            send({'QuoteObserved':dict(**quote,observed_at=observed)})
            return quote
        def poll():
            for report in venue.poll(engine.state):
                deliver(report)
                if 'Fill' in report:
                    before=engine.state['position'];deliver(report);stats['duplicate_reports']+=1
                    if engine.state['position']!=before:raise RuntimeError('duplicate fill changed position')
        def barrier():
            # Cancels only this session's orders; independently checks base balance/fees.
            send('Disconnect');send('Reconnect')
            snapshot=venue.reconcile(engine.state,meta['baseline_base'])
            send({'Reconcile':snapshot})
            if engine.state['health']!='Healthy':raise VenueError('reconciliation rejected')
            cash=sum((-1 if o['intent']['side']=='Buy' else 1)*f['qty']*f['price']
                     for f in snapshot['fills'] for o in snapshot['orders'] if o['intent']['id']==f['order_id'])
            if cash!=engine.state['cash']:raise RuntimeError('cash diverged from exchange trades')
            stats['barriers']+=1
            note('barrier',snapshot=snapshot,accounting=venue.last_reconciliation,cash_tick_lots=cash)
        def checked_barrier():
            for attempt in range(3):
                try:barrier();return
                except VenueError as error:
                    note('recovery_failure',attempt=attempt+1,reason=str(error),code=error.code)
                    if attempt==2 or error.code in (-2014,-2015,-1022):raise
                    time.sleep(2**attempt)
        def dispatch(command):
            if not command:return None
            intent=command['SubmitTargeted']['intent'];validate(venue,intent,args.max_notional)
            if intent['side']=='Sell' and venue.available_base()<venue.lot*intent['qty']:
                raise VenueError('insufficient free base including fees; no submit sent')
            if len(engine.state['orders'])>=args.cycles*2:
                note('budget_blocked');return None
            effects=send(command)
            accepted=[e['SendOrder'] for e in effects if 'SendOrder' in e]
            if not accepted:
                note('refused_order',effects=effects);return None
            response=venue.submit(accepted[0]);stats['wire_submits']+=1
            note('wire_submit',intent=accepted[0],venue_order_id=response['orderId'])
            deliver({'Accepted':{'id':intent['id']}})
            return intent['id']
        def cancel(oid):
            if oid is None:return
            effects=send({'Cancel':{'id':oid}})
            for effect in effects:
                if 'SendCancel' in effect:
                    response=venue.cancel(engine.state['orders'][str(oid)]['intent']);stats['wire_cancels']+=1
                    note('wire_cancel',order_id=oid,status=response['status'],executed_quantity=response['executedQty'])
            poll()
            if engine.state['orders'][str(oid)]['lifecycle'] not in TERMINAL:raise VenueError('cancel has not reached terminal barrier')
        try:
            if args.recover_only:checked_barrier()
            else:
                for cycle in range(args.cycles):
                    # Close any session inventory on the next cycle; otherwise open a bounded long.
                    target=0 if engine.state['position'] else lots
                    quote=refresh();send(target_event(engine.state,target,ttl_ms=30000))
                    passive=quote['bid']-args.passive_offset_ticks if target>engine.state['position'] else quote['ask']+args.passive_offset_ticks
                    command=target_order(engine.state,price=passive)
                    old_command=json.loads(json.dumps(command))
                    oid=dispatch(command)
                    time.sleep(.25)
                    poll()
                    # Signal changes while the old order may still fill. Invalidate pending callbacks.
                    send(target_event(engine.state,target,ttl_ms=30000))
                    cancel(oid)
                    if old_command:
                        effects=send(old_command)
                        if any('SendOrder' in e for e in effects):raise RuntimeError('stale callback escaped target guard')
                        if not any('Refused' in e for e in effects):raise RuntimeError('missing stale refusal')
                        stats['stale_intents_blocked']+=1
                    quote=refresh()
                    command=target_order(engine.state)
                    replacement=dispatch(command)
                    if replacement is not None:stats['replacements']+=1
                    for _ in range(5):
                        time.sleep(.25);poll()
                        if replacement is None or engine.state['orders'][str(replacement)]['lifecycle'] in TERMINAL:break
                    cancel(replacement)
                    checked_barrier()
                    print(json.dumps(dict(cycle=cycle+1,position_lots=engine.state['position'],**stats)),flush=True)
            completed=True
        finally:
            try:checked_barrier();reconciled=True
            finally:
                send('Disconnect')
                summary=dict(stats,mode='testnet-churn',completed=completed,reconciled=reconciled,position_lots=engine.state['position'],
                             fills=len(engine.state['fills']),total_orders=len(engine.state['orders']),cash_tick_lots=engine.state['cash'],
                             elapsed_seconds=round(time.monotonic()-start,3))
                save(path/f'summary-{invocation}.json',summary)
                (path/'summary.json').write_text(json.dumps(summary,indent=2))
                note('summary',**summary);print(json.dumps(summary),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path);p.add_argument('--env-file',type=Path,default=Path('.env.testnet'))
    p.add_argument('--symbol',default='BTCUSDT');p.add_argument('--quantity',type=Decimal,default=Decimal('.0002'))
    p.add_argument('--max-notional',type=Decimal,default=Decimal('200'));p.add_argument('--cycles',type=int,default=4)
    p.add_argument('--passive-offset-ticks',type=int,default=100)
    p.add_argument('--execute-testnet',action='store_true');p.add_argument('--recover-only',action='store_true')
    args=p.parse_args()
    if not args.execute_testnet:p.error('--execute-testnet is required; virtual funds only')
    if not 1<=args.cycles<=10 or not 0<=args.passive_offset_ticks<=10000 or not args.quantity.is_finite() or args.quantity<=0 or not args.max_notional.is_finite() or args.max_notional<=0:p.error('invalid bounded experiment limits')
    try:run(args)
    except Exception as error:
        print('Stopped: '+type(error).__name__+'; use --recover-only with this run directory',file=sys.stderr);sys.exit(1)
