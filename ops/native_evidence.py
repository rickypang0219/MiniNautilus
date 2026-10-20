#!/usr/bin/env python3
"""Strict run-level evidence for the frozen native-runtime experiment."""
import argparse
import json
import math
from pathlib import Path
import statistics


def load(root, group):
    paths=sorted(root.glob(group+'-*/report.json'))
    return [dict(report=json.loads(p.read_text()), provenance=json.loads((p.parent/'provenance.json').read_text()),path=str(p.parent)) for p in paths]


def values(runs, metric):
    return [r['report']['metrics']['full_action']['p99_ms'] if metric=='full_action_p99_ms' else r['report'][metric] for r in runs]


def limits(v):
    return dict(min=min(v),median=statistics.median(v),max=max(v))


def analyze(root):
    groups={g:load(root,g) for g in ('aa0','aa1','ab-a','ab-b')}
    all_runs=[r for runs in groups.values() for r in runs]
    compatibility={(r['provenance']['host'],r['provenance']['manifest_sha256'],tuple(r['provenance']['cpu_affinity'])) for r in all_runs}
    valid=bool(all_runs) and len(compatibility)==1 and all(r['report']['valid'] and not r['provenance']['identity']['dirty'] for r in all_runs)
    hashes={r['report']['input_sha256'] for r in all_runs}
    action_sets={json.dumps(sorted(r['report']['actions'],key=lambda a:a['id']),sort_keys=True) for r in all_runs}
    valid=valid and len(hashes)==1 and len(action_sets)==1
    # A/A and A/B baseline must use the same executable; do not pool candidate builds.
    baseline=groups['aa0']+groups['aa1']+groups['ab-a']
    valid=valid and len({r['provenance']['binary_sha256'] for r in baseline})==1
    valid=valid and len({r['provenance']['binary_sha256'] for r in groups['ab-b']})<=1
    for r in all_runs:
        metric=r['report']['metrics']['full_action']['p99_ms']
        if not isinstance(metric,(int,float)) or not math.isfinite(metric) or metric<=0:
            r['report']['valid']=False;valid=False
    aa=groups['aa0']+groups['aa1']
    stable=False;aa_result=dict(verdict='INSUFFICIENT',runs=len(aa))
    if all(len(groups[g])>=5 for g in ('aa0','aa1')) and all(r['report']['valid'] for r in aa):
        data=values(aa,'full_action_p99_ms');spread=(max(data)-min(data))/statistics.median(data)
        identical=len({r['provenance']['binary_sha256'] for r in aa})==1
        aa_compatible=len({(r['provenance']['host'],r['provenance']['manifest_sha256'],tuple(r['provenance']['cpu_affinity'])) for r in aa})==1
        stable=all(r['report']['valid'] and not r['provenance']['identity']['dirty'] for r in aa) and aa_compatible and identical and spread<=.20
        aa_result=dict(verdict='PASS' if stable else 'INCONCLUSIVE',runs=len(aa),spread_ratio=spread,**limits(data))
    rows={}
    for metric in ('full_action_p99_ms','cpu_ns_per_market','rss_peak_kib'):
        a,b=groups['ab-a'],groups['ab-b']
        if min(len(a),len(b))<5 or not all(r['report']['valid'] for r in a+b):
            rows[metric]=dict(verdict='INSUFFICIENT');continue
        av,bv=values(a,metric),values(b,metric)
        effect=1-statistics.median(bv)/statistics.median(av)
        verdict='INCONCLUSIVE'
        if stable and valid:
            if max(bv)<min(av) and effect>=.10:verdict='IMPROVED'
            elif min(bv)>max(av) and effect<=-.10:verdict='REGRESSED'
        rows[metric]=dict(verdict=verdict,baseline=limits(av),native=limits(bv),median_improvement_fraction=effect)
    return dict(schema=1,compatible_complete_runs=valid,counts={g:len(r) for g,r in groups.items()},
        aa=aa_result,comparison=rows,order_p999='NOT_MEASURED: fewer than 100000 order samples per run',
        scope='Controlled raw frame to simulated transport; not exchange, TLS, signing or alpha latency',
        inventory=[dict(path=r['path'],valid=r['report']['valid']) for r in all_runs])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);a=p.parse_args();report=analyze(a.root)
    (a.root/'comparison.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
