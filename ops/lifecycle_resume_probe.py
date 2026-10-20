#!/usr/bin/env python3
"""Five fresh Python runners plus Rust children; explicit independent fake wallet.

Runner launch includes interpreter/import/provenance checks. First retired lookup
includes engine startup/root verification and all child logical reads so far.
No global cache dropping, network access or Testnet resume claim.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.bridge import Engine
from mininautilus.evidence import verified_identity
from mininautilus.live_actor import ActorSession
from mininautilus.offline import FakeLiveAdapter
from mininautilus.sma import SmaStrategy

def logical_reads(pid):
 for line in Path(f'/proc/{pid}/io').read_text().splitlines():
  if line.startswith('rchar:'):return int(line.split()[1])
 raise ValueError('child logical read counter unavailable')

def child(a,identity):
 ready=[];phases=[];engine_launch=time.monotonic_ns()
 with Engine(a.journal,recover=True,binary=a.binary,extra_args=['--sync','outbox','--cold-verify','pages']) as engine:
  engine_ready=time.monotonic_ns()
  if engine.state['position']!=a.expected_position or engine.state['orders'] or engine.state['fills']:
   raise ValueError('fixture baseline/hot state mismatch')
  retired_id=engine.state['id_floor']
  if not retired_id:raise ValueError('retired fixture required')
  # This frozen fixture has sequential execution IDs and one-lot fills at100.
  effects=engine.send(engine.state['now'],{'Execution':dict(epoch=engine.state['epoch'],venue_seq=engine.state['venue_seq']+1,report={'Fill':dict(execution_id=retired_id,order_id=retired_id,qty=1,price=100)})})
  first_query=time.monotonic_ns();first_reads=logical_reads(engine.process.pid)
  if effects or engine.state['position']!=a.expected_position or engine.state['fills']:
   raise ValueError('retired duplicate oracle mismatch')
  adapter=FakeLiveAdapter(pace=.01,candles=10000,forget_retired=True)
  adapter.position=a.expected_position;adapter.next_execution=retired_id
  origin,initial=time.monotonic(),engine.state['now']
  def note(kind,**data):
   if kind=='risk_admission' and data['enabled'] and not ready:ready.append(data['monotonic_ns'])
   if kind=='reconciled':phases.append(time.monotonic_ns())
  session=ActorSession(engine,adapter,SmaStrategy(3,8,1,'1s',long_only=True),identity=('fake','dedicated','TEST'),now=lambda:initial+int((time.monotonic()-origin)*1000),note=note,lots=1,max_orders=10**9,poll_seconds=.01)
  session._reconcile();asyncio.run(session.run(.2));reconciled=session.close(reconcile=True)
  if not reconciled or engine.state['position']!=adapter.position:raise ValueError('full runner venue oracle mismatch')
  return dict(launch_ns=a.launch_runner_ns,engine_launch_ns=engine_launch,engine_ready_ns=engine_ready,reconciled_ns=phases[0] if phases else None,eligible_quote_ns=ready[0] if ready else None,engine_startup_ms=(engine_ready-engine_launch)/1e6,full_resume_ms=(ready[0]-a.launch_runner_ns)/1e6 if ready else None,first_lookup_including_startup_ms=(first_query-engine_launch)/1e6,first_lookup_logical_read_bytes=first_reads,position=engine.state['position'],venue_position=adapter.position,runner_pid=__import__('os').getpid(),engine_pid=engine.process.pid,provenance=identity)

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--journal',type=Path,required=True);p.add_argument('--binary',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True);p.add_argument('--output',type=Path);p.add_argument('--expected-position',type=int,required=True);p.add_argument('--reps',type=int,default=5);p.add_argument('--launch-runner-ns',type=int,help=argparse.SUPPRESS);a=p.parse_args()
 identity=verified_identity(ROOT,a.binary,a.receipt)
 if identity['dirty']:p.error('clean source required')
 if a.launch_runner_ns is not None:
  print(json.dumps(child(a,identity),allow_nan=False));return
 if a.reps<5 or a.output is None:p.error('five repetitions and output required')
 original=hashlib.sha256(a.journal.read_bytes()).hexdigest();rows=[]
 for rep in range(a.reps):
  copy=a.journal.with_name(f'fast-resume-{uuid.uuid4().hex}.jsonl');shutil.copyfile(a.journal,copy)
  launch=time.monotonic_ns()
  command=[sys.executable,str(Path(__file__).resolve()),'--journal',str(copy.resolve()),'--binary',str(a.binary.resolve()),'--receipt',str(a.receipt.resolve()),'--expected-position',str(a.expected_position),'--launch-runner-ns',str(launch)]
  result=subprocess.run(command,check=True,capture_output=True,text=True)
  row=json.loads(result.stdout);row.update(repetition=rep,copy=copy.name);rows.append(row)
 if len({r['runner_pid'] for r in rows})!=a.reps:raise ValueError('runner process reused')
 if hashlib.sha256(a.journal.read_bytes()).hexdigest()!=original:raise ValueError('original fixture mutated')
 result=dict(schema='lc_evidence_v2_fast',scope='fresh Python+Rust fake runner full resume',pagecache='ordinary OS cache; no global drops',input_sha256=original,provenance=identity,rows=rows)
 with a.output.open('x') as f:json.dump(result,f,indent=2,allow_nan=False)
 print(json.dumps(rows,allow_nan=False))
if __name__=='__main__':main()
