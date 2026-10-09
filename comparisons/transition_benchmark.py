#!/usr/bin/env python3
"""Compare two already-built checkouts with identical workload/timer boundaries.

Build both with the same compiler first. Baseline can be a git archive extraction;
neither checkout is modified. Results require a fresh output directory.
"""
import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path
from types import SimpleNamespace

import adapters
import history_scaling
import run


def compare(baseline, output, durable=False):
    current = adapters.ROOT
    roots = {'before': Path(baseline).resolve(), 'after': current}
    output = Path(output).resolve()
    for root in roots.values():
        assert (root/'target/release/examples/compare_engine').is_file(), root
        if durable:
            assert (root/'target/release/mininautilus').is_file(), root
            # Both binaries use one common Python driver in this process. Reject
            # bridge changes rather than accidentally benchmarking a cached import.
            assert (root/'python/mininautilus/bridge.py').read_bytes() == (current/'python/mininautilus/bridge.py').read_bytes(), 'Compare Rust changes with an unchanged Python bridge'
    output.mkdir(parents=True, exist_ok=False)
    metadata = dict(platform=platform.platform(),
                    compiler_on_path=subprocess.check_output(['rustc', '--version'], text=True).strip(),
                    note='Caller must build both binaries with the same compiler and flags.',
                    source_sha256={}, binary_sha256={})
    try:
        for label, root in roots.items():
            sources = sorted((root/'src').rglob('*.rs')) + [root/'Cargo.lock', root/'examples/compare_engine.rs']
            metadata['source_sha256'][label] = {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
            binaries = [root/'target/release/examples/compare_engine']
            if durable:
                binaries.append(root/'target/release/mininautilus')
            metadata['binary_sha256'][label] = {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in binaries}
            adapters.ROOT = root
            run.performance(SimpleNamespace(platforms=['mini'], sizes=[1000, 5000, 10000], repeats=3,
                                            output=str(output/f'{label}-performance.json')))
            history_scaling.run(str(output/f'{label}-history.json'))
            if durable:
                run.performance(SimpleNamespace(platforms=['mini_durable'], sizes=[1000], repeats=3,
                                                output=str(output/f'{label}-durable.json')))
    finally:
        adapters.ROOT = current
    (output/'metadata.json').write_text(json.dumps(metadata, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--durable', action='store_true')
    args = parser.parse_args()
    compare(args.baseline_root, args.output, args.durable)
