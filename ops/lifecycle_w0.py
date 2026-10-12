#!/usr/bin/env python3
"""LC-v2 W0: immutable 100Hz alternating ten-bar target trace, no network.

60s warmup + 300s steady measurement. SMA3/8 observes only an eight-bar ring.
Fake SDK delay is 0 or20ms. All writes and rotation maintenance stay in scope.
"""
import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.audit import IncrementalAudit
from mininautilus.bridge import Engine
from mininautilus.evidence import verified_identity
from mininautilus.live_actor import ActorSession
from mininautilus.offline import FakeLiveAdapter
from mininautilus.sma import SmaStrategy

class TraceVenue(FakeLiveAdapter):
    def __init__(self,origin,**kwargs):
        super().__init__(pace=.01,prices=[100]*10+[101]*10,candles=36000,forget_retired=True,**kwargs)
        self.origin=origin;self.overruns=0
    @contextmanager
    def stream(self):
        def rows():
            while self.cursor<36000:
                deadline=self.origin+(self.cursor+1)/100
                remaining=deadline-time.perf_counter()
                if remaining>0:time.sleep(remaining)
                else:self.overruns+=1
                self.cursor+=1;c=self.candle(self.cursor);self.last_close=c.close_ms
                yield c,time.perf_counter_ns()
            while True:time.sleep(.01);yield None
        yield rows()

class TimedActor(ActorSession):
    def __init__(self,*args,durations,steady_start_ns,**kwargs):
        super().__init__(*args,**kwargs);self.durations=durations;self.steady_start_ns=steady_start_ns
    async def _apply_async(self,item):
        effects=await super()._apply_async(item);ready=time.perf_counter_ns()
        for effect in effects:
            if 'SendOrder' in effect:
                effect['_ready_ns']=ready
                if item.received_ns>=self.steady_start_ns:
                    self.durations.write(json.dumps(dict(kind='internal',received_ns=item.received_ns,ready_ns=ready,duration_ns=ready-item.received_ns,order_id=effect['SendOrder']['id']))+'\n');self.durations.flush()
        return effects
    async def _execute(self,effect):
        ready=effect.pop('_ready_ns',None);now=time.perf_counter_ns()
        if ready is not None and ready>=self.steady_start_ns:
            self.durations.write(json.dumps(dict(kind='executor_queue',ready_ns=ready,started_ns=now,duration_ns=now-ready))+'\n');self.durations.flush()
        return await super()._execute(effect)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-dir',type=Path,required=True);p.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus');p.add_argument('--receipt',type=Path,default=ROOT/'target/runtime-v2-build.json');p.add_argument('--cold-format',choices=['legacy','pages'],required=True);p.add_argument('--delay-ms',type=int,choices=[0,20],default=0);a=p.parse_args()
    identity=verified_identity(ROOT,a.binary,a.receipt)
    if identity['dirty']:p.error('clean source required')
    a.run_dir.mkdir(parents=True,exist_ok=False)
    config=a.run_dir/'config.json';config.write_text(json.dumps(dict(max_abs_position=1,max_order_qty=1,max_order_notional=10**9,request_timeout_ms=30000,market_stale_ms=3000,private_stale_ms=30000,max_signal_lag=10)))
    start=time.perf_counter();adapter=TraceVenue(start,submit_delay=a.delay_ms/1000)
    notes=(a.run_dir/'lifecycle.jsonl').open('x');durations=(a.run_dir/'durations.jsonl').open('x');totals=[]
    def note(kind,**row):
        if kind in ('candle','signal'):return
        record=dict(kind=kind,observed_ns=time.perf_counter_ns());record.update(row);notes.write(json.dumps(record)+'\n');notes.flush()
        if kind=='coordinator_accounting':totals.append(row['counts'])
    with Engine(a.run_dir/'events.jsonl',config=config,binary=a.binary,extra_args=['--sync','outbox','--cold-verify','pages' if a.cold_format=='pages' else 'full'],env={'MINI_ORDER_TRACE':str(a.run_dir/'requests.jsonl')}) as engine:
        origin,initial=time.monotonic(),engine.state['now'];audit=IncrementalAudit(1,long_only=True);audit.check(engine.state,None)
        resource_stop=os.environ.get('MINI_LC_RESOURCE_STOP_FILE')
        session=TimedActor(engine,adapter,SmaStrategy(3,8,1,'1s',long_only=True),identity=('fake','dedicated','TEST'),now=lambda:initial+int((time.monotonic()-origin)*1000),note=note,audit=audit,max_orders=10**9,poll_seconds=.005,rotate_orders=200,reclaim_cold=True,paged_cold=a.cold_format=='pages',durations=durations,steady_start_ns=int((start+60)*1e9),stop_requested=(lambda:Path(resource_stop).exists()) if resource_stop else None)
        error=None
        try:asyncio.run(session.run(max(0,start+360-time.perf_counter())))
        except Exception as e:error=type(e).__name__
        reconciled=session.close(reconcile=error is None);report=session.report();position=engine.state['position']
    notes.close();durations.close()
    counts={key:sum(row[key] for row in totals) for key in ('offered','admitted','completed','rejected','pending')}
    result=dict(schema='lc_evidence_v2_fast',profile='W0',warmup_seconds=60,measure_seconds=300,start_perf_counter_ns=int(start*1e9),delay_ms=a.delay_ms,cold_format=a.cold_format,logical_trace_sha256=hashlib.sha256(json.dumps([100]*10+[101]*10).encode()).hexdigest(),logical_trace_definition='36000 contiguous one-second candles; wall deadlines 100Hz; cyclic 20 prices; desired one-lot long/flat changes every10 market inputs after warmup',error=error,shutdown_reconciled=reconciled,position=position,venue_position=adapter.position,resubmissions=adapter.resubmissions,producer_overruns=adapter.overruns,counts=counts,session=report,provenance=identity)
    (a.run_dir/'summary.json').write_text(json.dumps(result,indent=2,allow_nan=False));print(json.dumps({k:result[k] for k in ('error','shutdown_reconciled','counts','producer_overruns')}))
    return 0 if error is None and reconciled and position==adapter.position and adapter.resubmissions==0 else 1
if __name__=='__main__':raise SystemExit(main())
