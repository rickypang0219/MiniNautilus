#!/usr/bin/env python3
"""Recompute LC-v2 budgets from indexed resources and raw latency/fault traces.

Does not read a runner's `passed` field. Component observations are explicitly
scoped; this report alone cannot certify all workloads or performance adoption.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from statistics import median

from lifecycle_evidence import analyse, percentile, sha


def read(path):
    def invalid(value):
        raise ValueError('nonfinite JSON: ' + value)
    return json.loads(Path(path).read_text(), parse_constant=invalid)


def lines(path):
    def invalid(value):
        raise ValueError('nonfinite JSON: ' + value)
    return [json.loads(line, parse_constant=invalid)
            for line in Path(path).read_text().splitlines() if line.strip()]


def check(run, metric, observed, limit, unit, paths, ac_id='AC-R',
          n=1, comparator='<=', scope='EC2 single run', statistic='max'):
    good = observed is not None and (observed <= limit if comparator == '<='
                                     else observed >= limit)
    return dict(ac_id=ac_id, threshold_version='LC-v2-fast', scope=scope,
                metric=metric, statistic=statistic, observed=observed, limit=limit,
                comparator=comparator, unit=unit, n=n, run_ids=[run],
                status='NOT_MEASURED' if observed is None else 'PASS' if good else 'FAIL',
                reason='raw-derived observation; required cohorts remain separate',
                evidence_paths=[str(p) for p in paths])


def credits_check(directory, path):
    m = read(directory/'manifest.json')
    start, end = (datetime.fromisoformat(m[k]).timestamp() for k in ('start', 'end'))
    data = read(path)
    points = sorted((datetime.fromisoformat(p['timestamp']).timestamp(), p['minimum'])
                    for p in data['datapoints'])
    before = [p for p in points if p[0] <= start]
    after = [p for p in points if p[0] >= end]
    selected = [p for p in points if start <= p[0] <= end]
    if before: selected.insert(0, before[-1])
    if after: selected.append(after[0])
    covered = (bool(before and after) and start-before[-1][0] <= 300
               and after[0][0]-end <= 300
               and all(b[0]-a[0] <= 600 for a, b in zip(selected, selected[1:])))
    return check(directory.name, 'cpu_credit_min', min(p[1] for p in selected)
                 if covered else None, 20, 'credits', [path], ac_id='HOST',
                 comparator='>=', n=len(selected))


def report(directory, session=None, credits=None):
    directory = Path(directory)
    base = analyse(directory)  # verifies hashes, ordering and process generations
    m = read(directory/'manifest.json')
    rows = lines(directory/'resources.jsonl')
    out = []
    for c in base['checks']:
        c.update(threshold_version='LC-v2-fast', run_ids=[directory.name], scope='EC2 single run')
        c['ac_id'] = 'HOST' if c['metric'] in ('steal_p99', 'sample_coverage', 'max_sample_gap') else 'AC-R'
        out.append(c)
    for field in ('pswpin', 'pswpout', 'oom_kill'):
        values = [r['host'][field] for r in rows if field in r['host']]
        out.append(check(directory.name, field+'_delta', values[-1]-values[0]
                         if len(values)>1 else None, 0, 'count',
                         [directory/'resources.jsonl'], n=len(values)))
    if credits: out.append(credits_check(directory, Path(credits)))
    if session:
        session = Path(session)
        if (session/'durations.jsonl').exists():
            # Raw same-clock endpoints, not subtraction of two percentile values.
            ds = lines(session/'durations.jsonl')
            for kind in ('internal', 'executor_queue'):
                selected = [r for r in ds if r['kind']==kind]
                seen = set()
                for r in selected:
                    if r['duration_ns'] < 0: raise ValueError('negative duration')
                    if kind == 'internal':
                        if r['order_id'] in seen: raise ValueError('duplicate latency event')
                        seen.add(r['order_id'])
                        if r['ready_ns']-r['received_ns'] != r['duration_ns']:
                            raise ValueError('duration endpoint mismatch')
                values = [r['duration_ns']/1e6 for r in selected]
                out.append(check(directory.name, kind+'_p99', percentile(values)
                                 if len(values)>=1000 else None, 5 if kind=='internal' else None,
                                 'ms', [session/'durations.jsonl'], ac_id='W0', n=len(values),
                                 statistic='p99') if kind=='internal' else
                           dict(metric='executor_queue_p99_ms', observed=percentile(values),
                                n=len(values), scope='observation; no absolute queue budget'))
            s = read(session/'summary.json')
            start = s['start_perf_counter_ns']+60*10**9
            end = start+300*10**9
            measured = [r for r in rows if start<=r['monotonic_ns']<=end]
            # Cumulative per-PID/starttime maxima retain CPU from exited writers.
            totals, samples = {}, []
            for r in rows:
                for p in r['processes']:
                    key=(p['pid'],p['start_ticks'])
                    totals[key]=max(totals.get(key,0),p['cpu_seconds'])
                if start<=r['monotonic_ns']<=end:
                    samples.append((r['monotonic_ns'],sum(totals.values())))
            mean = ((samples[-1][1]-samples[0][1])*100 /
                    ((samples[-1][0]-samples[0][0])/1e9)) if len(samples)>1 else None
            cpus=[]
            if samples:
                anchor=samples[0]
                for current in samples[1:]:
                    dt=(current[0]-anchor[0])/1e9
                    if dt>=1:
                        cpus.append(100*(current[1]-anchor[1])/dt);anchor=current
            out.extend([check(directory.name,'cpu_mean_one_core',mean,30,'%',
                              [directory/'resources.jsonl'],ac_id='W0',n=len(measured),statistic='mean'),
                        check(directory.name,'cpu_p95_one_core',percentile(cpus,.95),80,'%',
                              [directory/'resources.jsonl'],ac_id='W0',n=len(cpus),statistic='p95')])
        if (session/'faults.jsonl').exists():
            faults=lines(session/'faults.jsonl')
            injected=[f for f in faults if f.get('stage')=='injected']
            recovered=[f for f in faults if 'recovered_ns' in f]
            ids=[f['fault_id'] for f in injected]
            if len(ids)!=len(set(ids)):raise ValueError('duplicate fault ID')
            counts=Counter(f['kind'] for f in injected)
            for kind in ('late_result','sdk_held','gap','overflow','writer_kill','executor_cancel','old_epoch'):
                out.append(check(directory.name,kind+'_injections',counts[kind],3,'count',
                                 [session/'faults.jsonl'],ac_id='AC-F',comparator='>='))
            unmatched=set(ids)-{f['fault_id'] for f in recovered}
            out.append(check(directory.name,'unrecovered_faults',len(unmatched),0,'count',
                             [session/'faults.jsonl'],ac_id='AC-F'))
            elapsed=[f['injected_ns'] for f in injected]
            spacing=min((b-a)/1e9 for a,b in zip(elapsed,elapsed[1:])) if len(elapsed)>1 else None
            out.append(check(directory.name,'fault_spacing',spacing,30,'seconds',
                             [session/'faults.jsonl'],ac_id='AC-F',comparator='>=',statistic='min'))
            venue=lines(session/'venue.jsonl')
            submits=[r for r in venue if r['kind']=='external_submit']
            ids=[r['intent']['id'] for r in submits]
            durable={}
            for trace in session.glob('requests-*.jsonl'):
                for r in lines(trace):
                    if r['effect']=='SendOrder':durable[r['order_id']]=r
            bad=sum(oid not in durable or not durable[oid]['stages']['synced'] or
                    durable[oid]['durable_seq']!=durable[oid]['seq'] for oid in ids)
            out.extend([check(directory.name,'external_submits',len(ids),5000,'count',
                              [session/'venue.jsonl'],ac_id='AC-F',comparator='>='),
                        check(directory.name,'duplicate_external_ids',len(ids)-len(set(ids)),0,'count',
                              [session/'venue.jsonl'],ac_id='AC-C'),
                        check(directory.name,'unproved_durable_external_submits',bad,0,'count',
                              [session/'venue.jsonl'],ac_id='AC-C')])
            notes=lines(session/'lifecycle.jsonl')
            rotations=sum(r['kind']=='rotated' for r in notes)
            mismatches=0
            for row in notes:
                if row['kind']=='coordinator_accounting':
                    for c in row['source_counts']:
                        mismatches += (c['offered']!=c['admitted']+c['rejected'] or
                                       c['admitted']!=c['completed']+c['pending'])
                    mismatches += bool(row['accounting_overflow'])
            out.extend([check(directory.name,'successful_rotations',rotations,20,'count',
                              [session/'lifecycle.jsonl'],ac_id='AC-F',comparator='>='),
                        check(directory.name,'source_accounting_mismatches',mismatches,0,'count',
                              [session/'lifecycle.jsonl'],ac_id='AC-C')])
    # Every supplemental input is hashed; the collector index pre-dates these files.
    inputs=[directory/'manifest.json',directory/'resources.jsonl',directory/'artifact-index.json']
    if session:inputs += sorted(p for p in session.glob('*.json*') if p.is_file())
    if credits:inputs.append(Path(credits))
    return dict(schema='lc_evidence_v2_fast_analysis',run_id=directory.name,checks=out,
                input_hashes={str(p):sha(p) for p in inputs},
                limitations=['No A/A or A/B adoption verdict from one run',
                             'Sampled process CPU may miss short-lived unwaited children',
                             'Cash/position final replay and aggregate buffer bounds need separate evidence',
                             'Diagnostic runs cannot certify formal integration gates'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory',type=Path);p.add_argument('--session',type=Path)
    p.add_argument('--credits',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();result=report(a.directory,a.session,a.credits)
    with a.output.open('x') as f:json.dump(result,f,indent=2,allow_nan=False)


if __name__=='__main__':main()
