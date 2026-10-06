#!/usr/bin/env python3
"""Real quote feed + adversarial execution scheduling. No exchange orders in this runner.

Targets deliberately oscillate: this tests concurrency semantics, not SMA profitability.
Fills, latency, cancel races and duplicate reports are explicitly synthetic.
"""
import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import heapq
import json
from pathlib import Path
import random
import sys
import threading
import time
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'python'), str(Path(__file__).resolve().parent)]
from mininautilus.binance import BinanceSpot, units
from mininautilus.bridge import Engine
from mininautilus.targets import target_event, target_order, TERMINAL
from sma_spot import audit


class QuoteFeed:
    """Coalescing market actor. Execution and Rust state remain owned by coordinator."""
    def __init__(self, symbol, tick):
        self.symbol, self.tick = symbol, tick
        self.latest = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
    def run(self):
        # A quiet book stream has no liveness proof. Fetch fresh REST snapshots on a
        # separate actor, retaining request-start time so slow I/O cannot look fresh.
        venue=BinanceSpot(self.symbol);venue.tick=self.tick
        update=0
        while not self.stop.is_set():
            observed=time.monotonic()
            try:
                row=venue.quote();update+=1
                with self.lock:self.latest=(row['bid'],row['ask'],observed,update)
                self.error=None
            except Exception as error:self.error=type(error).__name__
            self.stop.wait(.25)
    def quote(self):
        with self.lock:return self.latest
    def close(self):
        self.stop.set();self.thread.join(timeout=3)


class AdversarialVenue:
    """Independent order/fill ledger and delayed-report mailbox."""
    def __init__(self, schedule, rng):
        self.schedule,self.rng=schedule,rng
        self.orders={};self.fills={};self.position=0;self.cash=0
        self.counts=Counter()
    def submit(self,intent,epoch,now):
        oid=intent['id']
        if oid in self.orders:raise RuntimeError('double wire submit')
        self.orders[oid]=dict(intent=deepcopy(intent),filled=0,lifecycle='Accepted')
        self.schedule(now+self.rng.randint(70,200),'report',(epoch,{'Accepted':{'id':oid}}))
        self.schedule(now+self.rng.randint(10,70),'fill',(oid,epoch))
        self.schedule(now+self.rng.randint(80,180),'fill',(oid,epoch))
        self.counts['submits']+=1
    def cancel(self,oid,epoch,now):
        self.schedule(now+self.rng.randint(5,70),'cancel',(oid,epoch))
        self.counts['cancel_requests']+=1
    def fill(self,oid,epoch,now):
        order=self.orders[oid]
        if order['lifecycle'] in TERMINAL:return
        remaining=order['intent']['qty']-order['filled']
        qty=self.rng.randint(1,remaining)
        fill=dict(execution_id=len(self.fills)+1,order_id=oid,qty=qty,price=order['intent']['limit'])
        self.fills[fill['execution_id']]=fill
        order['filled']+=qty
        order['lifecycle']='Filled' if order['filled']==order['intent']['qty'] else 'Partial'
        sign=1 if order['intent']['side']=='Buy' else -1
        self.position+=sign*qty;self.cash-=sign*qty*fill['price']
        report={'Fill':fill};delay=self.rng.randint(20,250)
        self.schedule(now+delay,'report',(epoch,report))
        self.schedule(now+delay+self.rng.randint(1,150),'report',(epoch,report))
        self.counts['duplicate_reports_scheduled']+=1
    def canceled(self,oid,epoch,now):
        order=self.orders[oid]
        if order['lifecycle'] in TERMINAL:
            report={'CancelRejected':{'id':oid}}
        else:
            order['lifecycle']='Canceled'
            report={'Canceled':{'id':oid,'cumulative_filled':order['filled']}}
            if order['filled']:self.counts['cancel_after_partial_fill']+=1
        self.schedule(now+self.rng.randint(5,100),'report',(epoch,report))
    def snapshot(self,state):
        for order in self.orders.values():
            if order['lifecycle'] not in TERMINAL:order['lifecycle']='Canceled'
        return dict(epoch=state['epoch'],watermark=state['venue_seq'],orders=list(self.orders.values()),fills=list(self.fills.values()),position=self.position)


def check_cross_ledger(state,venue,limit):
    audit(state,limit)
    lower=upper=state['position']
    for order in state['orders'].values():
        if order['lifecycle'] not in TERMINAL or order['uncertain']:
            remainder=order['intent']['qty']-order['filled']
            if order['intent']['side']=='Buy':upper+=remainder
            else:lower-=remainder
    if not 0<=lower<=venue.position<=upper<=limit:raise RuntimeError('true venue inventory escaped reserved exposure bounds')
    unseen=0
    for eid,fill in venue.fills.items():
        if str(eid) not in state['fills']:
            sign=1 if venue.orders[fill['order_id']]['intent']['side']=='Buy' else -1
            unseen+=sign*fill['qty']
    if venue.position-state['position']!=unseen:raise RuntimeError('position gap is not explained by undelivered fills')
    if any(venue.fills.get(int(eid))!=fill for eid,fill in state['fills'].items()):raise RuntimeError('phantom or changed engine fill')
    return dict(engine_position=state['position'],venue_position=venue.position,unseen_fill_delta=unseen,lower=lower,upper=upper)


def run(args):
    venue_info=BinanceSpot(args.symbol);venue_info.initialize()
    feed=QuoteFeed(args.symbol,venue_info.tick);feed.thread.start()
    deadline=time.monotonic()+15
    while feed.quote() is None and not feed.error and time.monotonic()<deadline:time.sleep(.05)
    if feed.quote() is None:feed.close();raise RuntimeError('no live quotes')
    args.run_dir.mkdir(parents=True,exist_ok=False)
    config=dict(max_abs_position=args.lots,max_order_qty=args.lots,max_order_notional=10**15,
                request_timeout_ms=160,market_stale_ms=5000,private_stale_ms=3000,max_signal_lag=8)
    (args.run_dir/'config.json').write_text(json.dumps(config))
    (args.run_dir/'session.json').write_text(json.dumps(dict(mode='adversarial-paper',symbol=args.symbol,seed=args.seed,lots=args.lots,seconds=args.seconds,tick=str(venue_info.tick))))
    queue=[];sequence=0;stats=Counter();refusals=Counter();recovery_needed=False
    started=time.monotonic();next_signal=next_intent=next_cancel=0;max_gap=0
    def now():return int((time.monotonic()-started)*1000)
    def schedule(at,kind,payload):
        nonlocal sequence
        sequence+=1;heapq.heappush(queue,(at,sequence,kind,deepcopy(payload)))
    venue=AdversarialVenue(schedule,random.Random(args.seed))
    completed=False
    with Engine(args.run_dir/'events.jsonl',config=args.run_dir/'config.json') as engine, (args.run_dir/'trace.jsonl').open('w') as trace:
        def emit(event):
            nonlocal recovery_needed,max_gap
            effects=engine.send(now(),event)
            comparison=check_cross_ledger(engine.state,venue,args.lots)
            max_gap=max(max_gap,abs(comparison['unseen_fill_delta']))
            stats['checks']+=1
            trace.write(json.dumps(dict(at=now(),event=event,effects=effects,seq=engine.state['seq'],target=engine.state['target'],**comparison))+'\n')
            for effect in effects:
                if 'QueryState' in effect:recovery_needed=True
                if 'Refused' in effect:refusals[effect['Refused']['reason']]+=1
                if 'SignalRefused' in effect:stats['stale_signals_refused']+=1
                if 'SendOrder' in effect:venue.submit(effect['SendOrder'],engine.state['epoch'],now())
                if 'SendCancel' in effect:venue.cancel(effect['SendCancel']['id'],engine.state['epoch'],now())
            return effects
        def recover():
            nonlocal recovery_needed
            emit('Disconnect');emit('Reconnect')
            snapshot=venue.snapshot(engine.state)
            emit({'Reconcile':snapshot})
            if engine.state['health']!='Healthy' or engine.state['cash']!=venue.cash:raise RuntimeError('barrier failed to converge cash/position')
            stats['reconciliations']+=1;recovery_needed=False
        try:
            while now()<args.seconds*1000:
                quote=feed.quote()
                if quote is None or time.monotonic()-quote[2]>5:raise RuntimeError('live quote feed stale')
                emit({'QuoteObserved':dict(bid=quote[0],ask=quote[1],observed_at=max(0,int((quote[2]-started)*1000)))})
                emit({'Heartbeat':{'epoch':engine.state['epoch']}})
                emit('Tick')
                if recovery_needed:recover()
                while queue and queue[0][0]<=now():
                    _,_,kind,payload=heapq.heappop(queue)
                    if kind=='report':
                        epoch,report=payload
                        emit({'Execution':dict(epoch=epoch,venue_seq=engine.state['venue_seq']+1,report=report)})
                        stats['reports_delivered']+=1
                    elif kind=='fill':venue.fill(*payload,now())
                    elif kind=='cancel':venue.canceled(*payload,now())
                    elif kind=='intent':
                        if len(engine.state['orders'])<args.max_orders:emit(payload)
                    elif kind=='signal':emit(payload)
                    check_cross_ledger(engine.state,venue,args.lots)
                    if recovery_needed:recover()
                if now()>=next_signal:
                    command=target_event(engine.state,args.lots if (now()//450)%2==0 else 0,ttl_ms=1000)
                    emit(command);schedule(now()+200,'signal',command) # stale callback may arrive after a new revision
                    stats['target_updates']+=1;next_signal=now()+90
                if now()>=next_cancel:
                    for oid,order in list(engine.state['orders'].items()):
                        if order['lifecycle'] not in TERMINAL and order['pending']!='Cancel':emit({'Cancel':{'id':int(oid)}})
                    next_cancel=now()+75
                if now()>=next_intent and len(engine.state['orders'])<args.max_orders:
                    command=target_order(engine.state)
                    if command:
                        schedule(now()+venue.rng.randint(0,140),'intent',command)
                        schedule(now()+venue.rng.randint(30,160),'intent',command) # duplicate callback, not blind wire retry
                        stats['candidate_intents']+=1
                    next_intent=now()+25
                time.sleep(.002)
            completed=True
        finally:
            try:recover()
            finally:
                emit('Disconnect');feed.close();trace.flush()
                summary=dict(stats,**venue.counts,completed=completed,mode='adversarial-paper',feed_error=feed.error,refusals=dict(refusals),
                             position_lots=engine.state['position'],fills=len(engine.state['fills']),venue_position=venue.position,
                             max_unseen_position_gap=max_gap,elapsed_seconds=round(time.monotonic()-started,3),
                             engine_events=engine.state['seq'],cash_tick_lots=engine.state['cash'])
                (args.run_dir/'venue-ledger.json').write_text(json.dumps(dict(orders=venue.orders,fills=venue.fills,position=venue.position,cash=venue.cash)))
                (args.run_dir/'summary.json').write_text(json.dumps(summary,indent=2))
                print(json.dumps(summary),flush=True)
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path);p.add_argument('--seconds',type=int,default=30)
    p.add_argument('--symbol',default='BTCUSDT');p.add_argument('--lots',type=int,default=20)
    p.add_argument('--max-orders',type=int,default=100);p.add_argument('--seed',type=int,default=7)
    a=p.parse_args()
    if not 0<a.seconds<=600 or not 0<a.lots<=1000 or not 0<a.max_orders<=1000:p.error('bounded positive duration/lots/orders required')
    run(a)
