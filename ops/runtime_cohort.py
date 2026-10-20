#!/usr/bin/env python3
"""Interleaved A/A cohort of ops/runtime_bench.py runs (docs/perf-thresholds.md v1).

  python3 ops/runtime_cohort.py --out runs/aa-v1-<date> [--reps 5]

Both sides run the same release binary and receipt. Order is round-robin over
(repetition, case, side) so slow drift spreads across sides. Every run is kept,
including failures; nothing is retried. Afterwards each case is compared with
`runtime_compare.py --aa` and an overload case is recorded as functional only.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT/'comparisons/runtime-v2/manifest-v1.json'
CASES = {
    'serial-immediate': ['--runtime', 'serial'],
    'actor-immediate': ['--runtime', 'actor'],
    'serial-slow': ['--runtime', 'serial', '--fake-delay-seconds', '0.02'],
    'actor-slow': ['--runtime', 'actor', '--fake-delay-seconds', '0.02'],
}
OVERLOAD = ['--runtime', 'actor', '--rate', '2000', '--seconds', '10']


def bench(run_dir, options, log):
    command = [sys.executable, str(ROOT/'ops/runtime_bench.py'), '--manifest', str(MANIFEST),
               '--run-dir', str(run_dir), *options]
    started = time.time()
    code = subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    log.write(json.dumps(dict(run=str(run_dir), command=command[1:], exit=code,
                              started=started, seconds=round(time.time()-started, 1)))+'\n')
    log.flush()
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--reps', type=int, default=5)
    p.add_argument('--cases', nargs='*', default=list(CASES))
    args = p.parse_args()
    if args.reps < 5:
        p.error('thresholds v1 require at least five runs per side')
    args.out.mkdir(parents=True, exist_ok=False)
    with (args.out/'runs.jsonl').open('x') as log:
        for rep in range(args.reps):
            for case in args.cases:
                for side in ('a1', 'a2'):
                    bench(args.out/case/side/f'rep-{rep+1}', CASES[case], log)
        bench(args.out/'overload'/'rep-1', OVERLOAD, log)
    for case in args.cases:
        with (args.out/f'compare-{case}.json').open('x') as stream:
            subprocess.run([sys.executable, str(ROOT/'ops/runtime_compare.py'), '--aa',
                            str(args.out/case/'a1'), str(args.out/case/'a2')],
                           cwd=ROOT, stdout=stream, check=False)
    print(f'cohort complete: {args.out}')


if __name__ == '__main__':
    main()
