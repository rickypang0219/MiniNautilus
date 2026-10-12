#!/usr/bin/env python3
"""Build and bind a lifecycle example binary to exact Rust/example inputs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.evidence import command,digest,source_hashes,rust_inputs

def build(example,destination):
    if command(ROOT,'git','status','--porcelain'):raise ValueError('clean source required')
    cmd=['cargo','build','--locked','--release','--example',example]
    subprocess.run(cmd,cwd=ROOT,check=True)
    inputs=rust_inputs(source_hashes(ROOT));inputs['examples/'+example+'.rs']=digest(ROOT/'examples'/f'{example}.rs')
    binary=ROOT/'target/release/examples'/example
    receipt=dict(schema='lc_example_receipt_v1',source_commit=command(ROOT,'git','rev-parse','HEAD'),profile='release',build_command=cmd,inputs=inputs,binary=str(binary),binary_sha256=digest(binary),rustc=command(ROOT,'rustc','-Vv'),rustflags=os.environ.get('RUSTFLAGS',''),encoded_rustflags=os.environ.get('CARGO_ENCODED_RUSTFLAGS',''))
    with destination.open('x') as f:json.dump(receipt,f,indent=2)
    return receipt

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--example',choices=['lifecycle_probe','cold_pages_probe'],required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();build(a.example,a.output)
if __name__=='__main__':main()
