#!/usr/bin/env python3
"""Independent LC W1 report. Preserves short-run host-gate limitations.

Usage: lifecycle_report.py BEFORE_ROOT AB_ROOT OUTPUT
Reads immutable stdout and resource artifacts; never rewrites original runs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from lifecycle_evidence import percentile, sha


def read_run(directory):
    index=json.loads((directory/'artifact-index.json').read_text())
    for entry in index['files']:
        target=(directory/entry['path']).resolve()
        if not target.is_relative_to(directory.resolve()) or sha(target)!=entry['sha256']:
            raise ValueError('invalid artifact '+str(target))
    manifest=json.loads((directory/'manifest.json').read_text())
    probe=json.loads((directory/'stdout.log').read_text())
    if manifest['exit_code'] or not probe['correct']:
        raise ValueError('failed history probe '+str(directory))
    return dict(manifest=manifest,probe=probe,path=str(directory))


def compare(before,after,field,extract=None):
    getter=extract or (lambda r:r['probe'][field])
    a,b=[getter(r) for r in before],[getter(r) for r in after]
    ma,mb=statistics.median(a),statistics.median(b)
    return dict(metric=field,n_before=len(a),n_after=len(b),
                before=dict(values=a,median=ma,range=[min(a),max(a)]),
                after=dict(values=b,median=mb,range=[min(b),max(b)]),
                effect_pct=100*(mb/ma-1) if ma else None,
                disjoint=max(a)<min(b) or max(b)<min(a),
                verdict='OBSERVATION_ONLY',
                reason='W1 short probes do not satisfy full W0 host/CPU/throughput guardrails')


def report(before_root,ab_root):
    result=dict(schema='lc_w1_report_v1',threshold_version='LC-v1',histories={},checks=[],
                limitations=['Earlier resource sampler used 100ms steal instead of complete 1s intervals',
                             'Short-run resource coverage did not meet 99%; no full performance adoption claim',
                             'Rust HWM is one process, not the full actor process-group peak',
                             'Generated fixture has valid cold bytes; full historic journals are not synthesised',
                             'Cold first-read byte count is a lower bound, excluding binary-search reads',
                             'engine_replay_ms is in-process recovery, not fresh-process startup or full resume'])
    candidate={}
    for h in (1000,100000,1000000):
        before=[read_run(p) for p in sorted(Path(ab_root).glob(f'ab-baseline-{h}-*'))]
        after=[read_run(p) for p in sorted(Path(ab_root).glob(f'ab-candidate-{h}-*'))]
        if len(before)<5 or len(after)<5:raise ValueError('at least five per side required')
        candidate[h]=after
        result['histories'][str(h)]=dict(rows=[compare(before,after,f) for f in ('rotation_ms','first_lookup_ms','engine_replay_ms')]+
             [compare(before,after,'rust_lifetime_hwm_kib',lambda r:r['probe']['rotation_resources']['hwm_kib'])],
             before_paths=[r['path'] for r in before],after_paths=[r['path'] for r in after],
             binary_sha256={side:sorted({r['manifest']['command_binary_sha256'] for r in rows}) for side,rows in [('baseline',before),('candidate',after)]})
        for metric,limit,identifier in [('rotation_ms',250,'LC2-3'),('first_lookup_ms',100,'LC1-7'),('engine_replay_ms',100,'LC2-4')]:
            values=[r['probe'][metric] for r in after]
            value=max(values)
            result['checks'].append(dict(ac_id=identifier,history=h,metric=metric,statistic='max',
                 observed=value,limit=limit,unit='ms',n=len(values),status=('NOT_MEASURED' if identifier=='LC2-4' else ('PASS (scoped)' if value<=limit else 'FAIL')),
                 scope='in-process replay only' if identifier=='LC2-4' else 'isolated cold operation',
                 evidence_paths=[r['path']+'/stdout.log' for r in after]))
        warm=[percentile(r['probe']['warm_lookup_ms']) for r in after]
        result['checks'].append(dict(ac_id='LC1-7',history=h,metric='warm_lookup_p99_max',observed=max(warm),limit=5,unit='ms',n=1000,status='PASS (scoped)' if max(warm)<=5 else 'FAIL'))
    low,high=candidate[1000],candidate[1000000]
    peak=lambda rows:max(r['probe']['rotation_resources']['hwm_kib'] for r in rows)*1024
    result['checks'].append(dict(ac_id='LC1-3',metric='one_rust_process_hwm_growth',observed=peak(high)-peak(low),limit=32*2**20,unit='bytes',status='PASS (scoped)' if peak(high)-peak(low)<=32*2**20 else 'FAIL'))
    for metric,identifier in [('rotation_ms','LC2-3'),('engine_replay_ms','LC2-4')]:
        ratio=max(r['probe'][metric] for r in high)/max(r['probe'][metric] for r in low)
        result['checks'].append(dict(ac_id=identifier,metric=metric+'_history_ratio',observed=ratio,limit=2,unit='ratio',status='NOT_MEASURED' if identifier=='LC2-4' else ('PASS (scoped)' if ratio<=2 else 'FAIL')))
    # The low-history binary search reads at most bit_length(count)+1 records.
    # Subtract its upper bound, not its lower bound, to obtain a real growth bound.
    low_upper=max(r['probe']['first_lookup_logical_read_bytes']+32*((1000+200).bit_length()+1) for r in low)
    read_bytes=min(r['probe']['first_lookup_logical_read_bytes'] for r in high)-low_upper
    result['checks'].append(dict(ac_id='LC1-7',metric='first_lookup_read_growth_lower_bound',observed=read_bytes,limit=2**20,unit='bytes',status='FAIL' if read_bytes>2**20 else 'NOT_MEASURED'))
    result['raw_index']=[dict(path=str(p),sha256=sha(p),bytes=p.stat().st_size) for root in (before_root,ab_root) for p in sorted(Path(root).rglob('*')) if p.is_file()]
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('before',type=Path);p.add_argument('ab',type=Path);p.add_argument('output',type=Path)
    args=p.parse_args();value=report(args.before,args.ab)
    with args.output.open('x') as f:json.dump(value,f,indent=2,allow_nan=False)
    print(json.dumps(value['checks'],indent=2))


if __name__=='__main__':main()
