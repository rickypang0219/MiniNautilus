#!/usr/bin/env python3
"""Fresh runner copies, fake venue reconciliation, strategy warmup and eligible quote.

Fake wallet baseline is an explicit independent fixture expectation, never inferred
from the recovered engine. No network. Does not measure a Testnet resume.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
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

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--journal',type=Path,required=True);p.add_argument('--binary',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--expected-position',type=int,required=True);p.add_argument('--reps',type=int,default=5);a=p.parse_args()
 identity=verified_identity(ROOT,a.binary,a.receipt)
 if identity['dirty'] or a.reps<5:p.error('clean source and five repetitions required')
 original=hashlib.sha256(a.journal.read_bytes()).hexdigest();rows=[]
 for rep in range(a.reps):
  copy=a.journal.with_name(f'fast-resume-{uuid.uuid4().hex}.jsonl');shutil.copyfile(a.journal,copy);start=time.monotonic_ns();ready=[];phases=[]
  with Engine(copy,recover=True,binary=a.binary,extra_args=['--sync','outbox','--cold-verify','pages']) as engine:
   engine_ready=time.monotonic_ns()
   if engine.state['position']!=a.expected_position or engine.state['orders']:raise ValueError('fixture baseline/hot state mismatch')
   adapter=FakeLiveAdapter(pace=.01,candles=10000,forget_retired=True);adapter.position=a.expected_position
   origin,initial=time.monotonic(),engine.state['now']
   def note(kind,**data):
    if kind=='risk_admission' and data['enabled'] and not ready:ready.append(data['monotonic_ns'])
    if kind=='reconciled':phases.append(time.monotonic_ns())
   session=ActorSession(engine,adapter,SmaStrategy(3,8,1,'1s',long_only=True),identity=('fake','dedicated','TEST'),now=lambda:initial+int((time.monotonic()-origin)*1000),note=note,lots=1,max_orders=10**9,poll_seconds=.01)
   session._reconcile();asyncio.run(session.run(.2));reconciled=session.close(reconcile=True)
   if not reconciled or engine.state['position']!=adapter.position:raise ValueError('full runner venue oracle mismatch')
   rows.append(dict(repetition=rep,launch_ns=start,engine_ready_ns=engine_ready,reconciled_ns=phases[0] if phases else None,eligible_quote_ns=ready[0] if ready else None,engine_startup_ms=(engine_ready-start)/1e6,full_resume_ms=(ready[0]-start)/1e6 if ready else None,position=engine.state['position'],venue_position=adapter.position,copy=copy.name))
 if hashlib.sha256(a.journal.read_bytes()).hexdigest()!=original:raise ValueError('original fixture mutated')
 result=dict(schema='lc_evidence_v2_fast',scope='fresh fake runner full resume',input_sha256=original,provenance=identity,rows=rows)
 with a.output.open('x') as f:json.dump(result,f,indent=2,allow_nan=False)
 print(json.dumps(rows,allow_nan=False))
if __name__=='__main__':main()
