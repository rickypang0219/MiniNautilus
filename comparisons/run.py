#!/usr/bin/env python3
"""Reproducible differential audit; writes full inputs and native fill ledgers.

Optional platforms must be installed explicitly; a missing dependency is a failed
run, never a silently skipped validation. No network or exchange credentials used.
"""
import argparse
import gc
import hashlib
import importlib.metadata
import json
import platform
import statistics
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from adapters import mini, mini_durable, backtrader, vnpy, ledger
from cases import fixtures, random_passive, random_liquidity, benchmark
from oracle import reference

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = dict(mini=mini, mini_durable=mini_durable, backtrader=backtrader,
                backtrader_volume=lambda w:backtrader(w,volume_filler=True),vnpy=vnpy)


def verify_ledger(result, workload):
    expected=ledger(result['fills'],workload['steps'])
    for key in ('position','cash','checkpoints'):
        assert result[key] == expected[key], (key,result[key],expected[key])
    orders={a['id']:a for s in workload['steps'] for a in s['actions'] if a['kind']=='Submit'}
    totals={}
    for f in result['fills']:
        o=orders[f['id']]
        assert f['side']==o['side'] and f['qty']>0 and f['price']>0
        assert int(f['qty']) == f['qty'] and int(f['price']) == f['price'], ('off grid', f)
        assert f['price']<=o['limit'] if f['side']=='Buy' else f['price']>=o['limit']
        totals[f['id']]=totals.get(f['id'],0)+f['qty']
        assert totals[f['id']]<=o['qty'], ('overfill',f)


def compare(a,b):
    return {k:a[k]==b[k] for k in ('fills','checkpoints','position','cash')}


def audit(args):
    output=Path(args.output)
    output.mkdir(parents=True,exist_ok=False)
    versions={}
    for package in ('backtrader','nautilus_trader','vnpy','vnpy_ctastrategy'):
        try:
            versions[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    sources = list((ROOT/'src').rglob('*.rs')) + list((ROOT/'comparisons').glob('*.py')) + [ROOT/'examples/compare_engine.rs']
    metadata=dict(python=sys.version,platform=platform.platform(),machine=platform.machine(),
                  timestamp_utc=datetime.now(timezone.utc).isoformat(),
                  git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  versions=versions,
                  source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)})
    (output/'metadata.json').write_text(json.dumps(metadata,indent=2))
    workloads=fixtures()
    workloads.update({f'random_passive_{s}':random_passive(s,args.steps) for s in range(args.seeds)})
    rows=[]
    for name,w in workloads.items():
        expected=reference(w)
        results={}
        for label in args.platforms:
            r=ADAPTERS[label](w)
            verify_ledger(r,w)
            results[label]=r
        if 'mini' in results:
            assert all(compare(results['mini'],expected).values()), name
        if 'mini_durable' in results:
            assert all(compare(results['mini_durable'],expected).values()), name
        parity={label:compare(r,expected) for label,r in results.items()}
        # Mandatory common-denominator cases. Model-sensitive fixtures are retained
        # as observations and have explicit version-pinned expectations in tests.
        if name in ('long_round_trip','short_round_trip','reversal','scale_in_out','cancel_unfilled','unfilled_at_end'):
            assert all(all(p.values()) for p in parity.values()), (name,parity)
        record=dict(name=name,workload=w,oracle=expected,results=results,parity_with_mini_model=parity)
        (output/f'{name}.json').write_text(json.dumps(record,indent=2))
        rows.append(dict(name=name,parity=parity))
        print(name,{k:len(v['fills']) for k,v in results.items()},flush=True)
    for seed in range(args.seeds):
        w=random_liquidity(seed,args.steps)
        actual,expected=mini(w),reference(w)
        verify_ledger(actual,w)
        assert all(compare(actual,expected).values()),seed
        (output/f'liquidity_{seed}.json').write_text(json.dumps(dict(workload=w,result=actual,oracle=expected)))
    summary=dict(cases=rows,liquidity_oracle_passes=args.seeds,
                 input_digest=hashlib.sha256(json.dumps(workloads,sort_keys=True).encode()).hexdigest())
    (output/'summary.json').write_text(json.dumps(summary,indent=2))


def performance(args):
    rows=[]
    for trading in (False,True):
        for n in args.sizes:
            w=benchmark(n,trading)
            expected=reference(w)
            for label in args.platforms:
                times=[]
                for repeat in range(args.repeats+1):
                    gc.collect()
                    r=ADAPTERS[label](w)
                    verify_ledger(r,w)
                    assert all(compare(r,expected).values()),(label,n)
                    if repeat:times.append(r['elapsed_seconds'])
                row=dict(platform=label,steps=n,trading=trading,fills=len(expected['fills']),
                         seconds=times,median_seconds=statistics.median(times),
                         steps_per_second=n/statistics.median(times))
                rows.append(row)
                print(row,flush=True)
    Path(args.output).write_text(json.dumps(rows,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--platforms',nargs='+',default=['mini','backtrader','vnpy','nautilus'])
    parser.add_argument('--seeds',type=int,default=20)
    parser.add_argument('--steps',type=int,default=200)
    parser.add_argument('--benchmark',action='store_true')
    parser.add_argument('--sizes',nargs='+',type=int,default=[1000,5000,10000])
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    if any(p in args.platforms for p in ('nautilus','nautilus_tick')):
        from adapters import nautilus
        ADAPTERS['nautilus']=nautilus
        ADAPTERS['nautilus_tick']=lambda w:nautilus(w,data_kind='trade')
    (performance if args.benchmark else audit)(args)
