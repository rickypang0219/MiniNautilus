#!/usr/bin/env python3
"""Wall-clock offline actor soak with independent venue facts and controlled faults.

No exchange/network access. Three seeds may run concurrently for reliability
coverage; their timings are NOT controlled performance evidence. Strategy caches
only its fixed SMA window; Core, mirror and fake venue history remain unbounded
and are deliberately reported rather than claimed as memory-bounded.
"""
import argparse
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import sys
import threading
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'python'),str(ROOT/'ops')]
from mininautilus.audit import IncrementalAudit,full_audit
from mininautilus.backtest import Bar,SmaTarget,plan
from mininautilus.bridge import Engine
from mininautilus.evidence import verified_identity,validate_actions,percentile
from mininautilus.offline import FakeVenue,Input
from mininautilus.runtime import Coordinator
from mininautilus.targets import target_event,target_order
from system_sample import sample


async def run(args):
    identity=verified_identity(ROOT,args.binary,args.receipt)
    if identity['dirty'] and not args.allow_dirty: raise ValueError('clean tree required')
    if args.minutes<=0 or args.rate<=0 or args.fault_interval<=0: raise ValueError('positive duration/rate/fault interval required')
    args.run_dir.mkdir(parents=True,exist_ok=False)
    config=args.run_dir/'config.json'
    config.write_text(json.dumps(dict(max_abs_position=2,max_order_qty=2,max_order_notional=10**12,
        request_timeout_ms=30000,market_stale_ms=3000,private_stale_ms=30000,max_signal_lag=10000)))
    (args.run_dir/'provenance.json').write_text(json.dumps(identity,indent=2))
    origin=time.perf_counter_ns();loop=asyncio.get_running_loop()
    pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='core-writer')
    venue=FakeVenue();strategy=SmaTarget(3,8,lots=2,long_only=True)
    rng=random.Random(args.seed);price=100000
    journal=args.run_dir/'events.jsonl';generation=0;private_seq=0
    actor=None;engine=None;audit=None
    view={};peak_python=peak_rust=0
    timings=deque(maxlen=10000);queue_ages=deque(maxlen=10000)
    events=args.run_dir/'events.log.jsonl';faults=[];sessions=[]
    unexpected=[];offered_market=0;missed=0;phase='active';strategy_enabled=True
    fault_types=['lost_ack','source_gap','input_overflow','writer_kill','executor_cancel','late_epoch']
    current_expected=None;next_fault=args.fault_interval;fault_armed_ns=None
    last_sample=0;phase_times={'active':0.,'recovery':0.,'shutdown':0.}
    log=events.open('x');system=(args.run_dir/'system.jsonl').open('x');log_lock=threading.Lock()
    def elapsed(): return (time.perf_counter_ns()-origin)/1e9
    def note(kind,**data):
        with log_lock:
            log.write(json.dumps(dict(kind=kind,elapsed_seconds=elapsed(),generation=generation,phase=phase,**data))+'\n');log.flush()
    def now(): return int(elapsed()*1000)
    def request(event):
        effects=engine.send(max(engine.state['now'],now()),event)
        trace=engine.last_trace
        if trace['request_id']!=engine.request_id: raise RuntimeError('request trace mismatch')
        validate_actions(effects,[dict(row,request_id=trace['request_id']) for row in trace['actions']],request_id=engine.request_id)
        audit.check(engine.state,engine.last_change)
        return effects
    def apply_sync(item):
        nonlocal view
        payload=item.payload
        if item.lane=='private':
            payload={'Execution':dict(epoch=item.epoch,venue_seq=engine.state['venue_seq']+1,report=payload)}
        elif payload=='Heartbeat':payload={'Heartbeat':{'epoch':item.epoch}}
        effects=[]
        if payload=='Flatten':
            effects.extend(request({'QuoteObserved':dict(bid=price,ask=price,observed_at=now())}))
            effects.extend(request(target_event(engine.state,0)))
            command=target_order(engine.state)
            if command:effects.extend(request(command))
        else:
            if isinstance(payload,dict) and 'Quote' in payload:
                payload={'QuoteObserved':dict(**payload['Quote'],observed_at=item.at)}
            effects.extend(request(payload))
            if item.lane=='market':
                bar=Bar(engine.state['now'],item.payload['Quote']['bid'],1,'Sell')
                target=strategy.on_bar(bar,engine.state)
                if strategy_enabled and not actor.gated and engine.state['health']=='Healthy' and not engine.state['killed']:
                    for command in plan(engine.state,bar,target,order_ttl_ms=30000):effects.extend(request(command))
        ready=time.perf_counter_ns()
        if any('SendOrder' in effect for effect in effects):timings.append(ready-item.received_ns)
        view=dict(seq=engine.state['seq'],epoch=engine.state['epoch'],health=engine.state['health'],
            killed=engine.state['killed'],position=engine.state['position'],orders=len(engine.state['orders']),fills=len(engine.state['fills']))
        note('committed_input',source=item.source,source_seq=item.source_seq,at=engine.state['now'],seq=engine.state['seq'])
        return effects
    async def apply(item):return await loop.run_in_executor(pool,apply_sync,item)
    async def execute(effect):
        nonlocal private_seq
        reports=await venue.execute(effect)
        for report in reports:
            private_seq+=1;t=time.perf_counter_ns()
            if not actor.offer(Input('private','private',report,source_seq=private_seq,epoch=view['epoch'],
                                      at=now(),received_ns=t,scheduled_ns=t)):
                raise RuntimeError('private overflow requires reconciliation')
    def observe(kind,**record):
        if kind=='input':queue_ages.append(record['started_ns']-record['item'].received_ns)
        elif kind=='gate':note('gate',reason=record['reason'])
    def open_engine(recover):
        nonlocal engine,audit,view
        engine=Engine(journal,binary=args.binary,config=config if not recover else None,recover=recover,
            time_mode='historical',extra_args=['--sync','outbox'],env={'MINI_ORDER_TRACE':str(args.run_dir/f'orders-{generation}.jsonl')})
        audit=IncrementalAudit(2,long_only=True);audit.check(engine.state,None)
        view=dict(epoch=engine.state['epoch'],health=engine.state['health'],seq=engine.state['seq'])
    def reconcile_sync():
        nonlocal view
        request('Disconnect');request('Reconnect')
        snapshot=venue.snapshot(engine.state['epoch'],engine.state['venue_seq'],engine.state['orders'])
        request({'Reconcile':snapshot})
        full_audit(engine.state,2,long_only=True)
        if engine.state['health']!='Healthy' or engine.state['position']!=venue.position:raise RuntimeError('offline recovery mismatch')
        if len(engine.state['fills'])!=len(venue.fills):raise RuntimeError('recovery lost venue fills')
        view=dict(epoch=engine.state['epoch'],health=engine.state['health'],seq=engine.state['seq'],killed=engine.state['killed'],position=engine.state['position'],orders=len(engine.state['orders']),fills=len(engine.state['fills']))
    def start_actor():
        nonlocal actor,private_seq
        private_seq=0
        actor=Coordinator(apply,execute,observer=observe,execution_timeout=2).start()
    async def stop_actor():
        try:await asyncio.wait_for(actor.finish(),10)
        except BaseException as error:
            await actor.abort()
            if current_expected not in ('writer_kill','executor_cancel'):
                unexpected.append(type(error).__name__)
        sessions.append(dict(generation=generation,counts=dict(actor.counts),input_high_water=actor.high_water,
            effect_high_water=actor.effect_high_water,effects_started=actor.effects_started,
            effects_abandoned=actor.effects_abandoned,gate_reason=actor.gate_reason,
            execution_failures=actor.execution_failures,pending_inputs_at_stop=actor.counts['pending']))
    async def recover_session():
        nonlocal generation,current_expected,phase,fault_armed_ns
        old=phase;phase='recovery';began=elapsed()
        await stop_actor()
        # The writer may complete a cancelled IPC in its worker. Quiesce before close.
        await loop.run_in_executor(pool,lambda:None)
        engine.close();generation+=1
        open_engine(True)
        await loop.run_in_executor(pool,reconcile_sync)
        venue.mode='immediate';venue.barrier=None
        faults.append(dict(fault=current_expected,gate_reason=sessions[-1]['gate_reason'],recovered=True,
                           recovery_seconds=elapsed()-began,position=venue.position))
        note('recovered',fault=current_expected,position=venue.position)
        phase_times['recovery']+=elapsed()-began;phase=old
        current_expected=None;fault_armed_ns=None;start_actor()
    open_engine(False);start_actor();note('started',seed=args.seed,rate=args.rate,minutes=args.minutes)
    n=0;exit_code=0
    try:
        while elapsed()<args.minutes*60:
            due=origin+int(n/args.rate*1e9)
            delay=(due-time.perf_counter_ns())/1e9
            if delay>0:await asyncio.sleep(delay)
            if actor.gated:
                if current_expected is None:unexpected.append('unexpected_gate')
                await recover_session()
            if elapsed()>=next_fault and current_expected is None:
                current_expected=fault_types[len(faults)%len(fault_types)];fault_armed_ns=time.perf_counter_ns()
                next_fault+=args.fault_interval;note('fault_armed',fault=current_expected)
                if current_expected=='lost_ack':venue.mode='lost_ack'
                elif current_expected=='source_gap':
                    actor.offer(Input('private','private',{'Accepted':{'id':1}},source_seq=private_seq+2,epoch=view['epoch']))
                elif current_expected=='input_overflow':
                    for _ in range(150):actor.offer(Input('flood','control','Tick',at=now()))
                elif current_expected=='writer_kill':engine.process.kill()
                elif current_expected=='executor_cancel':
                    venue.barrier=asyncio.Event();venue.started.clear()
                elif current_expected=='late_epoch':
                    actor.offer(Input('private','private',{'Accepted':{'id':1}},source_seq=private_seq+1,epoch=max(0,view['epoch']-1)))
            if current_expected=='executor_cancel' and venue.started.is_set():actor.effect_task.cancel()
            if fault_armed_ns and time.perf_counter_ns()-fault_armed_ns>int(15e9) and not actor.gated:
                unexpected.append('fault_did_not_gate');actor.gate('fault_deadline')
            t=time.perf_counter_ns()
            if t-due>1e9/args.rate:
                skipped=int((t-due)/(1e9/args.rate));missed+=skipped;n+=skipped
            price=max(1,price+rng.randint(-20,20));n+=1;offered_market+=1
            actor.offer(Input('market','market',{'Quote':{'bid':price,'ask':price}},at=now(),source_seq=offered_market,epoch=view['epoch'],received_ns=t,scheduled_ns=due))
            if n%max(1,int(args.rate))==0:
                actor.offer(Input('clock','timer','Tick',at=now(),epoch=view['epoch'],received_ns=t,scheduled_ns=due))
                actor.offer(Input('timer','timer','Heartbeat',at=now(),epoch=view['epoch'],received_ns=t,scheduled_ns=due))
            if elapsed()-last_sample>=5:
                row=sample(os.getpid(),engine.process.pid);row.update(phase=phase,generation=generation,view=dict(view),
                    input_depth=actor.queued,effect_depth=len(actor.effects),window_length=len(strategy.prices),audit_orders=len(audit.orders),audit_fills=len(audit.fills))
                for who in ('python','rust'):
                    rss=row[who]['status'].get('VmRSS') if row[who] else None
                    if rss:
                        if who=='python':peak_python=max(peak_python,int(rss.split()[0]))
                        else:peak_rust=max(peak_rust,int(rss.split()[0]))
                system.write(json.dumps(row)+'\n');system.flush();last_sample=elapsed()
                print(json.dumps(dict(elapsed=int(elapsed()),generation=generation,**view)),flush=True)
        phase='shutdown';began=elapsed();strategy_enabled=False;venue.mode='immediate';venue.barrier=None
        if actor.gated:
            if current_expected is None:current_expected='shutdown_recovery'
            await recover_session()
        await stop_actor();await loop.run_in_executor(pool,reconcile_sync)
        # Explicit fixture-only flattening; never an automatic live liquidation policy.
        start_actor();t=time.perf_counter_ns();actor.offer(Input('shutdown','control','Flatten',at=now(),epoch=engine.state['epoch'],received_ns=t,scheduled_ns=t))
        await stop_actor()
        if engine.state['position']!=0 or venue.position!=0:raise RuntimeError('offline shutdown failed to flatten')
        await loop.run_in_executor(pool,lambda:request('Kill'))
        engine.close();generation+=1;open_engine(True)
        await loop.run_in_executor(pool,reconcile_sync)
        if not engine.state['killed']:raise RuntimeError('kill latch cleared on restart')
        phase_times['shutdown']=elapsed()-began
        final=dict(engine.state)
    except Exception as error:
        exit_code=1;unexpected.append(type(error).__name__);note('failed',error_type=type(error).__name__)
        await actor.abort();await loop.run_in_executor(pool,lambda:None)
        final=dict(engine.state)
    finally:
        if engine:engine.close()
        pool.shutdown(wait=True,cancel_futures=True)
        system.close();log.close()
    phase_times['active']=max(0,elapsed()-phase_times['recovery']-phase_times['shutdown'])
    if unexpected:exit_code=1
    report=dict(session_exit=exit_code,provenance=identity,seed=args.seed,wall_seconds=elapsed(),minutes=args.minutes,
        scope='Offline reliability run; fake fills, parallel host contention, rolling latency samples. No live/absolute performance claim; terminal history is NOT bounded.',
        offered_market=offered_market,missed_deadlines=missed,phase_seconds=phase_times,faults=faults,unexpected=unexpected,
        sessions=sessions,peak_rss_kib=dict(python=peak_python,rust=peak_rust),window_capacity=8,window_length=len(strategy.prices),
        final=dict(position=final['position'],cash=final['cash'],orders=len(final['orders']),fills=len(final['fills']),killed=final['killed'],health=final['health']),
        fake_venue=dict(calls=venue.calls,orders=len(venue.orders),fills=len(venue.fills),position=venue.position),
        rolling_internal_p99_ms=percentile(timings,.99)/1e6 if timings else None,
        rolling_queue_age_p99_ms=percentile(queue_ages,.99)/1e6 if queue_ages else None)
    (args.run_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    (args.run_dir/'venue-final.json').write_text(json.dumps(dict(orders=venue.orders,fills=venue.fills,position=venue.position)))
    print(json.dumps({key:report[key] for key in ('session_exit','wall_seconds','faults','unexpected','final','peak_rss_kib')}),flush=True);return exit_code


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path);p.add_argument('--minutes',type=float,default=90)
    p.add_argument('--rate',type=float,default=10);p.add_argument('--seed',type=int,default=7)
    p.add_argument('--fault-interval',type=float,default=600)
    p.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt',type=Path,default=ROOT/'target/runtime-v2-build.json')
    p.add_argument('--allow-dirty',action='store_true',help='smoke only')
    return asyncio.run(run(p.parse_args()))

if __name__=='__main__':sys.exit(main())
