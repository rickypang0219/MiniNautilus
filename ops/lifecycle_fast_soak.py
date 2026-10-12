#!/usr/bin/env python3
"""900s offline actor lifecycle: 21 faults, controlled writer restart, real journal.

No network. SDK held case is released only after fail-closed, and the harness
waits for the same worker to become quiescent before restart. Permanently blocked
SDK deadline behavior is separately tested; it is never claimed recovered here.
"""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import random
import sys
import threading
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.bridge import Engine,current_journal
from mininautilus.audit import IncrementalAudit
from mininautilus.evidence import verified_identity
from mininautilus.live_actor import ActorSession
from mininautilus.offline import FakeLiveAdapter,Input,PRICES
from mininautilus.sma import SmaStrategy

FAULTS=('late_result','sdk_held','gap','overflow','writer_kill','executor_cancel','old_epoch')

class ObservedVenue(FakeLiveAdapter):
    def __init__(self, log, **kwargs):
        super().__init__(**kwargs)
        self.cash = 0
        self.external_log = log
    def submit(self, intent):
        self.external_log.write(json.dumps(dict(kind='external_submit', monotonic_ns=time.monotonic_ns(), intent=dict(intent)))+'\n'); self.external_log.flush()
        return super().submit(intent)
    def _fill(self, order):
        super()._fill(order)
        fill=self.fills[self.next_execution]
        self.cash -= fill['qty']*fill['price']*(1 if order['intent']['side']=='Buy' else -1)
        self.external_log.write(json.dumps(dict(kind='external_fill', monotonic_ns=time.monotonic_ns(), fill=fill, position=self.position, cash=self.cash))+'\n');self.external_log.flush()

async def execute(args,identity):
    args.run_dir.mkdir(parents=True,exist_ok=False)
    config=args.run_dir/'config.json'
    config.write_text(json.dumps(dict(max_abs_position=1,max_order_qty=1,max_order_notional=10**9,request_timeout_ms=30000,market_stale_ms=3000,private_stale_ms=30000,max_signal_lag=10)))
    external=(args.run_dir/'venue.jsonl').open('x')
    adapter=ObservedVenue(external,pace=.01,prices=PRICES,candles=10**9,forget_retired=True)
    rng=random.Random(args.seed)
    plan=[]
    for repetition in range(3):
        batch=list(FAULTS);rng.shuffle(batch)
        plan.extend(batch)
    start=time.monotonic();deadline=start+args.seconds
    next_fault=start+(2 if args.diagnostic else 60);armed=[];resume=[];notes=[];errors=[];phases=[];source_failures=[]
    journal=args.run_dir/'events.jsonl';log=(args.run_dir/'lifecycle.jsonl').open('x');faultlog=(args.run_dir/'faults.jsonl').open('x')
    # notes are streamed; keep only currently unresolved fault and bounded totals.
    pending=None;generation=0;totals=Counter()
    def record(kind,**data):
        nonlocal pending
        row=dict(kind=kind,monotonic_ns=time.monotonic_ns(),elapsed_seconds=time.monotonic()-start,runner_generation=generation);row.update(data)
        log.write(json.dumps(row,allow_nan=False)+'\n');log.flush()
        if kind=='coordinator_accounting':
            for r in data['source_counts']:
                if r['offered']!=r['admitted']+r['rejected'] or r['admitted']!=r['completed']+r['pending']:source_failures.append(row)
            if data['accounting_overflow']:source_failures.append(row)
        if kind=='risk_admission' and data['enabled']:
            if resume and resume[-1]['enabled_ns'] is None:resume[-1]['enabled_ns']=row['monotonic_ns']
            if pending is not None:
                pending.update(recovered_ns=row['monotonic_ns'],recovery='quiescent reconciliation and eligible quote');faultlog.write(json.dumps(pending)+'\n');faultlog.flush();pending=None
    while time.monotonic()<deadline:
        launch=time.monotonic_ns();generation+=1
        with Engine(journal,config=config,recover=generation>1,binary=args.binary,extra_args=['--sync','outbox','--cold-verify','pages'],env={'MINI_ORDER_TRACE':str(args.run_dir/'requests.jsonl')}) as engine:
            initial,origin=engine.state['now'],time.monotonic()
            now=lambda:initial+int((time.monotonic()-origin)*1000)
            audit=IncrementalAudit(1,long_only=True);audit.check(engine.state,None)
            session=ActorSession(engine,adapter,SmaStrategy(2,3,1,'1s',long_only=True),identity=('fake','dedicated','TEST'),now=now,note=record,audit=audit,max_orders=10**9,poll_seconds=.01,execution_timeout=.5,quiescence_seconds=1,rotate_orders=200,paged_cold=True,reclaim_cold=True,reconcile_retry=lambda attempt:time.sleep(.1))
            if generation>1:session._reconcile()
            resume.append(dict(launch_ns=launch,enabled_ns=None,generation=generation))
            async def injector():
                nonlocal next_fault,pending
                while time.monotonic()<deadline:
                    await asyncio.sleep(.05)
                    c=session.coordinator
                    if len(armed)>=len(plan) or pending is not None or c is None or c.gated or c.closed or not session.availability.enabled or time.monotonic()<next_fault:continue
                    kind=plan[len(armed)];pending=dict(fault_id=len(armed),kind=kind,injected_ns=time.monotonic_ns(),generation=generation);armed.append(dict(pending));next_fault=time.monotonic()+(1 if args.diagnostic else 30)
                    faultlog.write(json.dumps(dict(pending,stage='injected'))+'\n');faultlog.flush()
                    ident=dict(zip(('venue','account','instrument'),session.identity))
                    if kind=='late_result':adapter.delay_next_submit=1.2
                    elif kind=='sdk_held':
                        release=threading.Event();adapter.submit_block=release
                        timer=threading.Timer(3,release.set);timer.daemon=True;timer.start()
                    elif kind=='writer_kill':engine.process.kill()
                    elif kind=='executor_cancel':c.effect_task.cancel()
                    elif kind=='gap':
                        c.offer(Input('fixture-gap','control','Tick',source_seq=1,**ident));c.offer(Input('fixture-gap','control','Tick',source_seq=3,**ident))
                    elif kind=='overflow':
                        for seq in range(300):c.offer(Input('fixture-overflow','control','Tick',source_seq=seq,**ident))
                    else:
                        c.offer(Input('fixture-epoch','control','Tick',epoch=2,source_seq=1,**ident));c.offer(Input('fixture-epoch','control','Tick',epoch=1,source_seq=2,**ident))
            injector_task=asyncio.create_task(injector());failure=None
            try:await session.run(max(0,deadline-time.monotonic()))
            except Exception as e:failure=type(e).__name__;record('runner_failure',error_type=failure,expected_fault=pending['kind'] if pending else None)
            finally:
                injector_task.cancel();await asyncio.gather(injector_task,return_exceptions=True)
                if session.blocking.busy:
                    until=time.monotonic()+5
                    while session.blocking.busy and time.monotonic()<until:await asyncio.sleep(.02)
                    if session.blocking.busy:errors.append('venue_worker_not_quiescent');break
                    adapter.submit_block=None
                permitted=failure is not None and pending is not None and pending['kind'] in ('writer_kill','sdk_held')
                try:reconciled=session.close(reconcile=failure is None)
                except Exception as e:reconciled=False;errors.append(type(e).__name__)
                report=session.report();phases.append(report)
                for key in ('submissions','rotations','fills_applied','market_inputs'):totals[key]+=report[key]
                if failure and not permitted:errors.append(failure)
                if not failure and not reconciled:errors.append('shutdown_reconcile_failed')
                journal=Path(engine.journal)
        if errors:break
        journal=current_journal(journal)
        if failure is None:break
    # Verify final scope even after a controlled writer crash near the deadline.
    with Engine(current_journal(journal),recover=True,binary=args.binary,extra_args=['--sync','outbox','--cold-verify','pages']) as engine:
        state=engine.state;engine.send(state['now'],'Reconnect');engine.send(state['now'],{'Reconcile':adapter.reconcile(engine.state)});position=engine.state['position'];cash=engine.state['cash'];healthy=engine.state['health']=='Healthy'
        final_journal=engine.journal
    log.close();faultlog.close();external.close()
    durable={}
    for line in (args.run_dir/'requests.jsonl').read_text().splitlines():
        row=json.loads(line)
        if row['effect']=='SendOrder':
            if not row['stages']['synced'] or row['durable_seq']!=row['seq']:errors.append('durability_trace_violation')
            durable[row['order_id']]=row
    unproved=[oid for oid in adapter.submits if oid not in durable]
    allowed_gates={'rotation','delivery_uncertain','source_sequence_gap','input_overflow','writer_failed','executor_cancelled','executor_stopped','old_source_epoch','hard_stop'}
    unexpected_gates=sorted({g for phase in phases for g in phase['gates']} - allowed_gates)

    counts=Counter(a['kind'] for a in armed)
    checks=dict(no_unexpected_error=not errors,no_unexpected_gates=not unexpected_gates,durable_submit_trace=not unproved,cash_matches= cash==adapter.cash,venue_position_matches=position==adapter.position,no_blind_resubmission=adapter.resubmissions==0 and len(adapter.submits)==len(set(adapter.submits)),final_reconciled=healthy,source_accounting=not source_failures,all_faults_three_times=all(counts[k]>=3 for k in FAULTS),all_faults_recovered=pending is None,minimum_submissions=totals['submissions']>=5000,minimum_rotations=totals['rotations']>=20,observation_complete=time.monotonic()-start>=args.seconds)
    report=dict(schema='lc_evidence_v2_fast',scope='DIAGNOSTIC' if args.diagnostic else 'FAST_INTEGRATION',seed=args.seed,seconds=args.seconds,checks=checks,passed=all(checks.values()),errors=errors,totals=dict(totals),fault_counts=dict(counts),resumes=resume,position=position,venue_position=adapter.position,cash=cash,venue_cash=adapter.cash,unproved_external_submits=unproved,unexpected_gates=unexpected_gates,final_journal=final_journal,provenance=identity,limitations=['sdk_held is deliberately released after fail-closed; permanently blocked SDK tested separately','100 candles/s producer, SMA2/3 natural fake signals; not the W0 one-order-per-ten-input trace','Detailed resource acceptance requires external collector'],phases=phases)
    (args.run_dir/'summary.json').write_text(json.dumps(report,indent=2,allow_nan=False));print(json.dumps({k:report[k] for k in ('passed','errors','totals','fault_counts','checks')}));return 0 if report['passed'] else 1

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-dir',type=Path,required=True);p.add_argument('--seed',type=int,default=7);p.add_argument('--seconds',type=float,default=900);p.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus');p.add_argument('--diagnostic',action='store_true',help='short smoke; no numerical adoption');p.add_argument('--receipt',type=Path,default=ROOT/'target/runtime-v2-build.json');a=p.parse_args()
    identity=verified_identity(ROOT,a.binary,a.receipt)
    if identity['dirty']:p.error('clean source required')
    if a.seconds!=900 and not a.diagnostic:p.error('acceptance window fixed at 900 seconds; short smoke requires --diagnostic')
    if a.seconds<=0:p.error('positive duration required')
    return asyncio.run(execute(a,identity))
if __name__=='__main__':raise SystemExit(main())
