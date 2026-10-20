#!/usr/bin/env python3
"""Seeded differential stress test of the production Mini matching/accounting core."""
import argparse
import hashlib
import json
from pathlib import Path
from adapters import mini
from cases import random_liquidity
from oracle import reference
from run import compare, verify_ledger


def run(seeds,steps,output):
    digest=hashlib.sha256()
    for seed in range(seeds):
        workload=random_liquidity(seed,steps)
        digest.update(json.dumps(workload,sort_keys=True).encode())
        actual,expected=mini(workload),reference(workload)
        try:
            verify_ledger(actual,workload)
            assert all(compare(actual,expected).values()),seed
        except AssertionError:
            Path(output).with_suffix('.failure.json').write_text(json.dumps(dict(seed=seed,workload=workload,actual=actual,expected=expected),indent=2))
            raise
    summary=dict(seeds=seeds,seed_start=0,seed_end=seeds-1,observations_per_seed=steps,
                 observations=seeds*steps,input_digest=digest.hexdigest(),
                 checks=['every fill','every cash balance','every position','every marked equity'])
    Path(output).write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--seeds',type=int,default=1000)
    parser.add_argument('--steps',type=int,default=200)
    args=parser.parse_args()
    if args.seeds<1 or args.steps<1:parser.error('seeds and steps must be positive')
    run(args.seeds,args.steps,args.output)
