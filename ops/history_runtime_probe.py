#!/usr/bin/env python3
"""Functional actor/recovery coverage with real retained terminal history.

Historical fills are produced by the existing paper matcher. New execution and
lost ACK use a separate fake venue. No absolute latency or bounded-RSS claim.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'python'),str(ROOT/'ops')]
from mininautilus.audit import IncrementalAudit,full_audit
from mininautilus.bridge import Engine
from mininautilus.evidence import validate_actions,verified_identity
from mininautilus.offline import FakeVenue,Input
from mininautilus.runtime import Coordinator
from system_sample import sample


async def run(args):
    identity=verified_identity(ROOT,args.binary,args.receipt)
    if identity['dirty'] and not args.allow_dirty:raise ValueError('clean tree required')
    args.run_dir.mkdir(parents=True,exist_ok=False)
    (args.run_dir/'provenance.json').write_text(json.dumps(identity,indent=2)+'\n')
    rows=[]
    for history in args.history:
        directory=args.run_dir/str(history);directory.mkdir()
        journal=directory/'events.jsonl';config=directory/'config.json'
        config.write_text(json.dumps(dict(max_abs_position=2,max_order_qty=1,max_order_notional=1000,
            request_timeout_ms=100,market_stale_ms=1000,private_stale_ms=5000,max_signal_lag=100)))
        venue=FakeVenue();began=time.perf_counter_ns()
        with Engine(journal,binary=args.binary,config=config,paper=True,time_mode='historical',extra_args=['--sync','outbox']) as engine:
            for oid in range(1,history+1):
                side='Buy' if oid%2 else 'Sell'
                intent=dict(id=oid,side=side,qty=1,limit=100,based_on_seq=engine.state['seq']+1,valid_until=10**12)
                engine.send_batch(0,[{'Quote':dict(bid=100,ask=100)},{'Submit':intent},
                    {'Trade':dict(taker='Sell' if side=='Buy' else 'Buy',price=100,qty=1)}])
                # Venue facts follow the explicit paper matching fixture, not
                # a reconstruction from Core's recovered orders or fills.
                venue.orders[oid]=dict(intent=intent,filled=1,lifecycle='Filled')
                venue.fills[oid]=dict(execution_id=oid,order_id=oid,qty=1,price=100)
                venue.position+=1 if side=='Buy' else -1
                if oid%10000==0:print(json.dumps(dict(history=history,built=oid)),flush=True)
            assert engine.state['position']==venue.position==0,'use even history sizes'
            assert len(engine.state['fills'])==history
        generation_ns=time.perf_counter_ns()-began;venue.execution_id=history
        pool=ThreadPoolExecutor(max_workers=1);loop=asyncio.get_running_loop();private_seq=0
        with Engine(journal,binary=args.binary,recover=True,time_mode='historical',extra_args=['--sync','outbox'],
                    env={'MINI_ORDER_TRACE':str(directory/'orders.jsonl')}) as engine:
            audit=IncrementalAudit(2);audit.check(engine.state,None)
            def request(payload):
                effects=engine.send(0,payload)
                trace=engine.last_trace
                validate_actions(effects,[dict(row,request_id=trace['request_id']) for row in trace['actions']],request_id=trace['request_id'])
                audit.check(engine.state,engine.last_change)
                return effects
            def reconcile():
                request('Reconnect')
                request({'Reconcile':venue.snapshot(engine.state['epoch'],engine.state['venue_seq'],engine.state['orders'])})
                full_audit(engine.state,2)
                assert engine.state['health']=='Healthy'
                assert engine.state['position']==venue.position
                assert len(engine.state['fills'])==len(venue.fills)
            await loop.run_in_executor(pool,reconcile)
            async def apply(item):
                def write():
                    event=item.payload
                    if item.lane=='private':
                        event={'Execution':dict(epoch=engine.state['epoch'],venue_seq=engine.state['venue_seq']+1,report=event)}
                    effects=request(event)
                    full_audit(engine.state,2)  # Deliberate functional oracle, not the hot path benchmark.
                    return effects
                return await loop.run_in_executor(pool,write)
            async def execute(effect):
                nonlocal private_seq
                for report in await venue.execute(effect):
                    private_seq+=1
                    assert actor.offer(Input('private','private',report,epoch=engine.state['epoch'],source_seq=private_seq))
            async def guard(effect):
                def read():
                    if 'SendCancel' in effect:
                        order=engine.state['orders'][str(effect['SendCancel']['id'])]
                        if order['lifecycle'] in ('Filled','Canceled','Rejected') and not order['uncertain']:return 'terminal_cancel'
                    return 'send'
                return await loop.run_in_executor(pool,read)
            actor=Coordinator(apply,execute,clock_ms=lambda:0,dispatch_guard=guard).start()
            async def commits_drained():
                while actor.counts['pending']:
                    if actor.failure:raise RuntimeError(actor.failure)
                    await asyncio.sleep(0)
            for side in ('Buy','Sell'):
                oid=history+(1 if side=='Buy' else 2)
                actor.offer(Input('control','control',{'Quote':dict(bid=100,ask=100)}))
                await asyncio.wait_for(commits_drained(),15)
                intent=dict(id=oid,side=side,qty=1,limit=100,based_on_seq=engine.state['seq'],valid_until=10**12)
                actor.offer(Input('control','control',{'Submit':intent}))
                async def fill_committed():
                    while len(engine.state['fills'])<oid:
                        if actor.gated:raise RuntimeError(actor.gate_reason)
                        await asyncio.sleep(0)
                await asyncio.wait_for(fill_committed(),15)
            # A very late duplicate from archived terminal history is still
            # recognized, demonstrating why dedup evidence cannot be forgotten.
            if history:
                private_seq+=1
                actor.offer(Input('private','private',{'Fill':venue.fills[1]},epoch=engine.state['epoch'],source_seq=private_seq))
            await actor.finish()
            assert not actor.gated
            assert engine.state['position']==venue.position==0
            assert len(engine.state['fills'])==history+2
            healthy_counts=dict(actor.counts);rss=sample(os.getpid(),engine.process.pid)
            # Send once, accept+fill remotely, lose the response; recover facts,
            # retaining the intent and never retrying the client ID.
            venue.mode='lost_ack';actor=Coordinator(apply,execute,clock_ms=lambda:0,dispatch_guard=guard).start()
            oid=history+3
            intent=dict(id=oid,side='Buy',qty=1,limit=100,based_on_seq=engine.state['seq'],valid_until=10**12)
            actor.offer(Input('control','control',{'Submit':intent}))
            await actor.finish()
            assert actor.gated and actor.gate_reason=='delivery_uncertain'
            assert engine.state['position']==0 and venue.position==1
            calls=venue.calls;uncertain_counts=dict(actor.counts)
            began=time.perf_counter_ns();await loop.run_in_executor(pool,reconcile);reconcile_ns=time.perf_counter_ns()-began
            assert venue.calls==calls and engine.state['position']==1
            final=dict(position=engine.state['position'],orders=len(engine.state['orders']),fills=len(engine.state['fills']),
                       health=engine.state['health'],uncertain=engine.state['orders'][str(oid)]['uncertain'])
        pool.shutdown()
        row=dict(history=history,generation_ns=generation_ns,healthy_counts=healthy_counts,
            uncertain_counts=uncertain_counts,reconcile_ns=reconcile_ns,resources=rss,final=final,
            venue_calls=calls,late_duplicate_dedup=True,blind_retries=0)
        rows.append(row);print(json.dumps(dict(history=history,final=final)),flush=True)
    report=dict(session_exit=0,provenance=identity,rows=rows,scope='Functional real-journal terminal history coverage, full audit oracle per event. Lost ACK reconciliation is quiescent in the same Core process after its Disconnect; process crash/recovery is covered by the separate soak. No latency claim; final exposure is a confirmed fixture fill, not an unresolved order or live trade.')
    (args.run_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--history',type=int,nargs='+',default=[0,1000,100000])
    p.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt',type=Path,default=ROOT/'target/runtime-v2-build.json')
    p.add_argument('--allow-dirty',action='store_true',help='smoke only')
    args=p.parse_args()
    if any(n<0 or n%2 for n in args.history):p.error('nonnegative even history sizes required')
    try:
        asyncio.run(run(args))
    except Exception as error:
        if args.run_dir.exists() and not (args.run_dir/'report.json').exists():
            provenance=args.run_dir/'provenance.json'
            (args.run_dir/'report.json').write_text(json.dumps(dict(session_exit=1,
                failure=type(error).__name__,provenance=json.loads(provenance.read_text()) if provenance.exists() else None))+'\n')
        raise

if __name__=='__main__':main()
