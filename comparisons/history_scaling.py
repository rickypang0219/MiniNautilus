#!/usr/bin/env python3
"""Isolate Core::apply quote cost at a fixed retained order/fill history size."""
import argparse
import json
import statistics
from pathlib import Path
from adapters import mini
from cases import benchmark


def run(output, probes=5000, repeats=3):
    rows=[]
    for n in (0,200,2000,10000):
        workload=benchmark(n)
        workload['probe_quotes']=probes
        samples=[]
        for repeat in range(repeats+1):
            result=mini(workload)
            assert result['retained_orders']==result['retained_fills']==n//2
            assert result['position']==0 and result['cash']==n//2
            if repeat:samples.append(result['probe_elapsed_seconds'])
        row=dict(history_observations=n,retained_orders=result['retained_orders'],
                 retained_fills=result['retained_fills'],probe_quotes=probes,seconds=samples,
                 median_ns_per_quote=statistics.median(samples)*1e9/probes)
        rows.append(row)
        print(row,flush=True)
    Path(output).write_text(json.dumps(rows,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--probes',type=int,default=5000)
    args=parser.parse_args()
    run(args.output,args.probes)
