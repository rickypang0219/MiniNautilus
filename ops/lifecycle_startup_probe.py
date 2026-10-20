#!/usr/bin/env python3
"""Fresh-process IPC startup on copies; does not connect to a venue or enable trade."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import uuid
ROOT=next(p for p in Path(__file__).resolve().parents if (p/'Cargo.toml').is_file() and (p/'python/mininautilus').is_dir())
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.bridge import Engine


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal',type=Path,required=True)
    p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--reps',type=int,default=5)
    args=p.parse_args()
    if args.reps<5:p.error('at least five fresh-process repetitions')
    original_hash=hashlib.sha256(args.journal.read_bytes()).hexdigest()
    rows=[]
    for rep in range(args.reps):
        copy=args.journal.with_name(f'lc-startup-{uuid.uuid4().hex}.jsonl')
        shutil.copyfile(args.journal,copy)
        start=time.perf_counter_ns()
        with Engine(copy,recover=True,binary=args.binary) as engine:
            elapsed=(time.perf_counter_ns()-start)/1e6
            rows.append(dict(repetition=rep,startup_ms=elapsed,position=engine.state['position'],
                             id_floor=engine.state['id_floor'],health=engine.state['health'],
                             seq=engine.state['seq'],journal_bytes=copy.stat().st_size,
                             journal_copy=copy.name,copy_input_sha256=original_hash))
    assert hashlib.sha256(args.journal.read_bytes()).hexdigest()==original_hash
    result=dict(schema='lc_startup_probe_v1',binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
                input_sha256=original_hash,rows=rows,scope='engine IPC startup only; no venue reconciliation or trade-ready claim')
    with args.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))


if __name__=='__main__':main()
