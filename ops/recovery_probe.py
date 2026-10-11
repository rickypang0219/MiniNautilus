#!/usr/bin/env python3
"""Offline recovery attribution; preserves full-prefix validation, no optimization."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.bridge import Engine
from mininautilus.evidence import verified_identity


def frame_profile(path):
    began=time.perf_counter_ns();data=path.read_bytes();read_ns=time.perf_counter_ns()-began
    frame_ns=checksum_ns=payload_ns=0;previous=0;records=0
    if not data.endswith(b'\n'): raise ValueError('probe requires a complete journal')
    for line in data.splitlines():
        began=time.perf_counter_ns();frame=json.loads(line);frame_ns+=time.perf_counter_ns()-began
        began=time.perf_counter_ns();h=0xcbf29ce484222325
        for byte in previous.to_bytes(8,'little')+frame['payload'].encode(): h=((h^byte)*0x100000001b3)&((1<<64)-1)
        if frame['previous']!=previous or frame['checksum']!=h: raise ValueError('checksum corruption')
        previous=h;checksum_ns+=time.perf_counter_ns()-began
        began=time.perf_counter_ns();json.loads(frame['payload']);payload_ns+=time.perf_counter_ns()-began
        records+=1
    return dict(bytes_read=len(data),records=records,read_ns=read_ns,
                python_frame_decode_ns=frame_ns,python_checksum_ns=checksum_ns,python_payload_decode_ns=payload_ns)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt',type=Path,default=ROOT/'target/runtime-v2-build.json')
    p.add_argument('--sizes',type=int,nargs='+',default=[1000,100000])
    p.add_argument('--reps',type=int,default=5)
    args=p.parse_args();identity=verified_identity(ROOT,args.binary,args.receipt)
    if identity['dirty']: raise ValueError('clean tree required')
    args.run_dir.mkdir(parents=True,exist_ok=False)
    rows=[]
    for size in args.sizes:
        directory=args.run_dir/str(size);directory.mkdir()
        journal=directory/'source.jsonl';snapshot=directory/'snapshot.json'
        with Engine(journal,binary=args.binary,time_mode='historical',extra_args=['--sync','outbox']) as engine:
            remaining=size
            while remaining:
                n=min(1000,remaining);engine.send_batch(0,[{'Quote':{'bid':100,'ask':100}}]*n);remaining-=n
        started=time.perf_counter_ns()
        subprocess.run([str(args.binary),'snapshot',str(journal),str(snapshot)],check=True,stdout=subprocess.DEVNULL)
        snapshot_create_ns=time.perf_counter_ns()-started
        # Fixed tail after the checkpoint. recover will still replay the entire prefix.
        with Engine(journal,binary=args.binary,recover=True,time_mode='historical',extra_args=['--sync','outbox']) as engine:
            engine.send_batch(engine.state['now'],['Tick']*20)
        attribution=frame_profile(journal)
        for rep in range(args.reps):
            began=time.perf_counter_ns();saved=json.loads(snapshot.read_text());load=time.perf_counter_ns()-began
            began=time.perf_counter_ns()
            state=json.loads(subprocess.check_output([str(args.binary),'inspect',str(journal)]))
            replay=time.perf_counter_ns()-began
            copy=directory/f'recover-{rep}.jsonl';shutil.copyfile(journal,copy)
            began=time.perf_counter_ns()
            with Engine(copy,binary=args.binary,recover=True,time_mode='historical',extra_args=[str(snapshot)]) as engine:
                recover=time.perf_counter_ns()-began
                assert engine.state['health']=='Disconnected'
                assert engine.state['seq']==state['seq']+1
                for key in ('orders','fills','position','cash','killed'): assert engine.state[key]==state[key]
            rows.append(dict(history=size,rep=rep,**attribution,snapshot_bytes=snapshot.stat().st_size,
                snapshot_seq=saved['core']['seq'],tail_records=state['seq']-saved['core']['seq'],
                snapshot_create_ns=snapshot_create_ns,snapshot_load_ns=load,
                authoritative_replay_process_ns=replay,local_recover_startup_ns=recover,
                orders=len(state['orders']),fills=len(state['fills'])))
    report=dict(provenance=identity,scope='Quote-only fixed active state; Python stage probe repeats work and is not authoritative recovery. Full Rust replay/recover include process launch. No venue reconciliation.',rows=rows)
    (args.run_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(rows,indent=2))

if __name__=='__main__':main()
