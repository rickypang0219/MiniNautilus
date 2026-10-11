#!/usr/bin/env python3
"""Run-level A/A and A/B evidence; explicit primary metrics and guardrails."""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'python'), str(ROOT/'scripts')]
from mininautilus.evidence import compatible_reports
from evidence import verdict

METRICS = {
    'internal_tick_to_effect_p99_ms': ('internal_tick_to_effect', 'p99_ms'),
    'input_queue_age_p99_ms': ('input_queue_age', 'p99_ms'),
    'rust_before_response_p99_ms': ('rust_before_response', 'p99_ms'),
    'ipc_roundtrip_p99_ms': ('ipc_roundtrip', 'p99_ms'),
    'executor_queue_age_p99_ms': ('executor_queue_age', 'p99_ms'),
    'completed_per_second': ('completed_per_second',),
    'python_cpu_ns_per_event': ('python_cpu_ns_per_event',),
}


def get(report, path):
    value = report.get('metrics', {})
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def compare(before, after, aa=False):
    compatible = compatible_reports(before+after)
    rows = []
    for name, path in METRICS.items():
        a, b = [[get(report, path) for report in group] for group in (before, after)]
        complete = a and b and all(isinstance(x, (int, float)) and math.isfinite(x) for x in a+b)
        row = dict(metric=name, verdict='inconclusive', guardrail='INCONCLUSIVE')
        if complete:
            x, y = statistics.median(a), statistics.median(b)
            change = (y-x)/x*100 if x else None
            row.update(before_median=x, after_median=y, before_range=[min(a),max(a)], after_range=[min(b),max(b)], change_pct=change)
            higher = name == 'completed_per_second'
            if compatible:
                row['verdict'] = verdict(a,b,higher_is_better=higher)
                if min(len(a),len(b)) >= 5 and change is not None:
                    bad = -change if higher else change
                    worst = ((max(a)-min(b))/max(a)*100 if higher
                             else (max(b)-min(a))/min(a)*100 if min(a) else math.inf)
                    row['guardrail'] = 'PASS' if worst <= 10 else ('FAIL' if bad > 10 and row['verdict']=='regressed' else 'INCONCLUSIVE')
                    if aa:
                        row['aa_stability'] = 'PASS' if abs(change)<=10 and max(min(a),min(b))<=min(max(a),max(b)) else 'INCONCLUSIVE'
        rows.append(row)
    return dict(compatible=compatible, before_reps=len(before), after_reps=len(after), aa=aa, rows=rows)


def load(path):
    path=Path(path)
    files=[path/'report.json'] if (path/'report.json').exists() else sorted(path.glob('*/report.json'))
    if not files:
        raise ValueError('no report.json found')
    return [json.loads(file.read_text()) for file in files]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('before'); p.add_argument('after'); p.add_argument('--aa', action='store_true')
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    result=compare(load(args.before),load(args.after),args.aa)
    payload=json.dumps(result,indent=2)
    if args.output:
        with args.output.open('x') as stream: stream.write(payload+'\n')
    print(payload)

if __name__=='__main__':main()
