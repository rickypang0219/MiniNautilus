#!/usr/bin/env python3
"""Frozen full-scan reference vs incremental audit; batch-average timing, offline."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'python'),str(ROOT/'scripts')]
from mininautilus.audit import IncrementalAudit,full_audit
from mininautilus.evidence import percentile,verified_identity
from evidence import verdict


def fixture(size):
    orders={};fills={}
    for n in range(1,size+1):
        side='Buy' if n%2 else 'Sell'
        orders[str(n)]=dict(intent=dict(id=n,side=side,qty=1,limit=100),filled=1)
        fills[str(n)]=dict(execution_id=n,order_id=n,qty=1,price=100)
    return dict(orders=orders,fills=fills,position=size%2,cash=-100*(size%2),seq=0)


def old_audit(state):
    position=0
    for oid,order in state['orders'].items():
        filled=sum(fill['qty'] for fill in state['fills'].values() if fill['order_id']==int(oid))
        assert filled==order['filled']
        position+=filled*(1 if order['intent']['side']=='Buy' else -1)
    assert position==state['position']


def batch(function,n):
    before=time.perf_counter_ns()
    for _ in range(n): function()
    return (time.perf_counter_ns()-before)/n


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--reps',type=int,default=5)
    args=p.parse_args()
    identity=verified_identity(ROOT,ROOT/'target/release/mininautilus',ROOT/'target/runtime-v2-build.json')
    if identity['dirty']:raise ValueError('clean tree required')
    args.run_dir.mkdir(parents=True,exist_ok=False)
    empty=dict(header={},orders={},fills={});rows=[]
    for size in (0,1000,100000):
        state=fixture(size);full_audit(state,10)
        audit=IncrementalAudit(10);audit.check(state,None)
        audit.entries_checked=0
        for rep in range(args.reps):
            calls=[batch(lambda:audit.check(state,empty),1000) for _ in range(30)]
            row=dict(history=size,rep=rep,incremental_batch_mean_p99_ns=percentile(calls,.99),entries_checked=audit.entries_checked)
            if size==1000: row['old_full_scan_ns']=batch(lambda:old_audit(state),3)
            rows.append(row)
    old=[r['old_full_scan_ns'] for r in rows if r['history']==1000]
    incremental=[r['incremental_batch_mean_p99_ns'] for r in rows if r['history']==1000]
    zero=[r['incremental_batch_mean_p99_ns'] for r in rows if r['history']==0]
    large=[r['incremental_batch_mean_p99_ns'] for r in rows if r['history']==100000]
    report=dict(provenance=identity,scope='Empty-delta audit only; incremental p99 is p99 of 1000-call batch means, not individual events. Full reference is original O(orders*fills) audit.',
        rows=rows,full_vs_incremental=verdict(old,incremental),history_ratio=statistics.median(large)/statistics.median(zero))
    (args.run_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))

if __name__=='__main__':main()
