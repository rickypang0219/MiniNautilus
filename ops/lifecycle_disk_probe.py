#!/usr/bin/env python3
"""Measure actor disk-floor response on a task-owned 2.5–4GiB mounted filesystem.

Refuses the repository/root filesystem. Uses unwritten extents, never fills the
host root. Mount creation/removal is an operator step. Offline fake venue only.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))
from mininautilus.audit import IncrementalAudit
from mininautilus.bridge import Engine
from mininautilus.evidence import verified_identity
from mininautilus.live_actor import ActorSession, FailClosed
from mininautilus.offline import FakeLiveAdapter
from mininautilus.sma import SmaStrategy


def validate_mount(path):
    path=path.resolve()
    if not os.path.ismount(path) or path.stat().st_dev in (ROOT.stat().st_dev, Path('/').stat().st_dev):
        raise ValueError('a separate, task-owned mounted filesystem is required')
    v=os.statvfs(path);capacity=v.f_blocks*v.f_frsize
    if not 2.5*1024**3 <= capacity <= 4*1024**3:
        raise ValueError('isolated fixture capacity must be 2.5–4GiB')
    return max(2*1024**3, capacity//5)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mount',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt',type=Path,default=ROOT/'target/runtime-v2-build.json')
    a=p.parse_args();floor=validate_mount(a.mount)
    identity=verified_identity(ROOT,a.binary,a.receipt)
    if identity['dirty']:raise ValueError('clean source required')
    run=a.mount/'disk-probe';run.mkdir(exist_ok=False)
    config=run/'config.json';config.write_text(json.dumps(dict(max_abs_position=1,max_order_qty=1,
        max_order_notional=10**9,request_timeout_ms=30000,market_stale_ms=3000,private_stale_ms=30000,max_signal_lag=10)))
    ready=threading.Event();stop=threading.Event();observations=[];crossing={};fill_errors=[]
    filler=run/'reserved-extents';notes=(run/'lifecycle.jsonl').open('x')
    def note(kind,**data):
        row=dict(kind=kind,observed_ns=time.monotonic_ns());row.update(data)
        notes.write(json.dumps(row)+'\n');notes.flush()
        if kind=='risk_admission' and data['enabled']:ready.set()
        if kind=='risk_admission' and not data['enabled'] and data['reason']=='disk_headroom_exhausted':
            observations.append(row['observed_ns'])
    def reserve():
        if not ready.wait(5):fill_errors.append('runner_not_ready');return
        try:
            with filler.open('xb') as f:
                offset=0
                while not stop.is_set():
                    before=time.monotonic_ns()
                    os.posix_fallocate(f.fileno(),offset,8*1024**2);offset+=8*1024**2
                    v=os.statvfs(run);free=v.f_bavail*v.f_frsize
                    if free<floor:
                        crossing.update(pre_allocation_ns=before,post_allocation_ns=time.monotonic_ns(),free_bytes=free,reserved_bytes=offset)
                        f.flush();os.fsync(f.fileno());return
        except Exception as error:fill_errors.append(type(error).__name__)
    adapter=FakeLiveAdapter(pace=.01,candles=10000,forget_retired=True)
    error=None
    with Engine(run/'events.jsonl',config=config,binary=a.binary,extra_args=['--sync','outbox']) as engine:
        origin,initial=time.monotonic(),engine.state['now']
        audit=IncrementalAudit(1,long_only=True);audit.check(engine.state,None)
        session=ActorSession(engine,adapter,SmaStrategy(3,8,1,'1s',long_only=True),identity=('fake','dedicated','TEST'),
            now=lambda:initial+int((time.monotonic()-origin)*1000),note=note,audit=audit,poll_seconds=.01,
            max_orders=10**9,rotate_orders=200,quiescence_seconds=1)
        worker=threading.Thread(target=reserve,daemon=True);worker.start()
        try:asyncio.run(session.run(10))
        except FailClosed as failure:error=str(failure)
        finally:
            stop.set();worker.join(timeout=5)
            session.close(reconcile=False)
        orders_before=len(adapter.submits);time.sleep(.2)
        no_new_risk=len(adapter.submits)==orders_before
        state=dict(engine.state)
    notes.close()
    delay=(observations[0]-crossing['pre_allocation_ns'])/1e6 if observations and crossing else None
    markers=[json.loads(path.read_text()) for path in run.glob('stop-*.json')]
    result=dict(schema='lc_evidence_v2_fast',scope='isolated mounted disk-floor fault; no soak resource claim',
        provenance=identity,floor_bytes=floor,crossing=crossing,gate_observed_ns=observations,
        response_upper_bound_ms=delay,error=error,fill_errors=fill_errors,worker_quiescent=not worker.is_alive(),
        no_new_risk_after_gate=no_new_risk,stop_markers=markers,health=state['health'],position=state['position'])
    with a.output.open('x') as f:json.dump(result,f,indent=2,allow_nan=False)
    passed=(delay is not None and delay<=1000 and error=='disk_headroom_exhausted' and no_new_risk
            and not fill_errors and not worker.is_alive() and any(m.get('journal_flush') is True for m in markers))
    print(json.dumps(dict(response_upper_bound_ms=delay,passed=passed,error=error)))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
