#!/usr/bin/env python3
"""Measure retained allocations for the same streaming SMA workload, 5 reps.
Uses an immutable Git source object as baseline, with no journal/network component.
"""
import ast
from collections import namedtuple
import gc
import hashlib
import json
from pathlib import Path
import subprocess
import tracemalloc

Bar = namedtuple('Bar', 'at price volume taker')


def source(directory):
    if directory.startswith('git:'):
        revision = directory[4:]
        path = revision + ':python/mininautilus/backtest.py'
        text = subprocess.check_output(['git','show',path],text=True)
    else:
        path = Path(directory)/'python/mininautilus/backtest.py'
        text = path.read_text()
    node = next(n for n in ast.parse(text).body if isinstance(n,ast.ClassDef) and n.name=='SmaTarget')
    namespace = {}
    code = ast.get_source_segment(text,node)
    exec(compile(code,str(path),'exec'),namespace)
    return namespace['SmaTarget'],hashlib.sha256(code.encode()).hexdigest()


def run(cls):
    gc.collect()
    digest = hashlib.sha256()
    tracemalloc.start()
    strategy = cls(3,8,lots=2)
    for i in range(100000):
        price = 10000 + (i*37)%1000
        target = strategy.on_bar(Bar(i,price,1,'Buy'))
        digest.update(str(target).encode())
    current,peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'retained_bytes':current,'peak_bytes':peak,'retained_prices':len(strategy.prices),
            'signal_sha256':digest.hexdigest()}


def main(before,after,output):
    classes = [source(before),source(after)]
    reports = []
    for directory,(_,sha) in zip([before,after],classes):
        immutable = directory.startswith('git:')
        commit = subprocess.check_output(['git','rev-parse',directory[4:] if immutable else 'HEAD'],cwd=None if immutable else directory,text=True).strip()
        dirty = False if immutable else bool(subprocess.check_output(['git','status','--porcelain'],cwd=directory,text=True).strip())
        reports.append({'commit':commit, 'source_kind':'immutable Git object' if immutable else 'working tree', 'dirty':dirty,
            'class_sha256':sha,'observations':100000,'fast':3,'slow':8,'runs':[]})
    assert not reports[0]['dirty'], 'baseline must be clean'
    for _ in range(5):
        for (cls,_),report in zip(classes,reports):
            report['runs'].append(run(cls))
    hashes={r['signal_sha256'] for report in reports for r in report['runs']}
    assert len(hashes)==1, 'signals changed'
    with Path(output).open('x') as f:json.dump({'before':reports[0],'after':reports[1]},f,indent=2)
    print(json.dumps({'before_retained':[r['retained_bytes'] for r in reports[0]['runs']],
                     'after_retained':[r['retained_bytes'] for r in reports[1]['runs']],
                     'signals_identical':True}))


if __name__=='__main__':
    import sys
    main(*sys.argv[1:])
