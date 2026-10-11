"""Source/build receipts and exact, correlated offline runtime evidence.

No environment dump or credential files are read. A release receipt binds the
executable to its Rust build inputs; Git cleanliness covers the entire checkout.
"""
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess


def command(root, *args):
    return subprocess.check_output(args, cwd=root, text=True).strip()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def source_hashes(root):
    root = Path(root)
    names = command(root, 'git', 'ls-files', '-z').split('\0')
    return {name: digest(root / name) for name in sorted(names)
            if name and not Path(name).name.startswith('.env')
            and (root / name).is_file()
            and (name.startswith(('src/', 'python/', 'ops/', 'examples/', 'scripts/', 'tests/'))
                 or name in ('Cargo.toml', 'Cargo.lock', 'requirements.txt', 'rust-toolchain.toml'))}


def rust_inputs(hashes):
    return {name: value for name, value in hashes.items()
            if name.startswith('src/') or name in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml')}


def build_identity(root, binary):
    root, binary = Path(root), Path(binary).resolve()
    return dict(commit=command(root, 'git', 'rev-parse', 'HEAD'),
                dirty=bool(command(root, 'git', 'status', '--porcelain', '--untracked-files=all')),
                source_hashes=source_hashes(root), binary_path=str(binary), binary_sha256=digest(binary))


def write_receipt(root, binary, destination):
    identity = build_identity(root, binary)
    receipt = dict(profile='release', build_command=['cargo', 'build', '--locked', '--release', '--bin', 'mininautilus'],
                   features=[], rustc=command(root, 'rustc', '-Vv'),
                   rustflags=os.environ.get('RUSTFLAGS',''), encoded_rustflags=os.environ.get('CARGO_ENCODED_RUSTFLAGS',''),
                   profile_overrides={name:os.environ[name] for name in ('CARGO_PROFILE_RELEASE_OPT_LEVEL','CARGO_PROFILE_RELEASE_LTO','CARGO_PROFILE_RELEASE_CODEGEN_UNITS','CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS','CARGO_PROFILE_RELEASE_OVERFLOW_CHECKS','CARGO_PROFILE_RELEASE_PANIC') if name in os.environ},
                   rust_inputs=rust_inputs(identity['source_hashes']), binary_sha256=identity['binary_sha256'])
    Path(destination).write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def verified_identity(root, binary, receipt_path):
    identity = build_identity(root, binary)
    receipt = json.loads(Path(receipt_path).read_text())
    if receipt.get('profile') != 'release' or receipt.get('binary_sha256') != identity['binary_sha256']:
        raise ValueError('binary has no matching release build receipt')
    if receipt.get('rust_inputs') != rust_inputs(identity['source_hashes']):
        raise ValueError('release build inputs changed; rebuild and issue a new receipt')
    identity['build'] = receipt
    identity['platform'] = platform.platform()
    identity['python'] = platform.python_version()
    cpu = Path('/proc/cpuinfo').read_text() if Path('/proc/cpuinfo').exists() else ''
    identity['resources'] = dict(cpus=os.cpu_count(), affinity=sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
        cpu_model=next((line.split(':',1)[1].strip() for line in cpu.splitlines() if line.startswith('model name')), platform.processor()),
        filesystem_block_bytes=os.statvfs(root).f_bsize, loadavg=list(os.getloadavg()) if hasattr(os,'getloadavg') else None)
    return identity


def percentile(values, quantile):
    values = sorted(values)
    if not 0 <= quantile <= 1 or not values or not all(math.isfinite(x) for x in values):
        raise ValueError('finite nonempty samples and quantile in [0,1] required')
    index = (len(values) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def validate_actions(effects, rows, *, request_id):
    expected = Counter((name, effect[name]['id']) for effect in effects
                       for name in ('SendOrder', 'SendCancel') if name in effect)
    actual = Counter((row['effect'], row['order_id']) for row in rows)
    if expected != actual:
        raise ValueError('missing, duplicate or extra external action trace')
    for row in rows:
        if (row.get('request_id') != request_id or not row.get('stages', {}).get('synced')
                or row.get('durable_seq') != row.get('seq')):
            raise ValueError('external action lacks matching durable causal prefix')


def compatible_reports(reports):
    """Implementation hashes may differ; each executable must match its receipt."""
    if not reports:
        return False
    fingerprint = reports[0].get('experiment')
    for report in reports:
        provenance = report.get('provenance', {})
        build = provenance.get('build', {})
        if (report.get('session_exit') != 0 or provenance.get('dirty') is not False
                or not fingerprint or report.get('experiment') != fingerprint
                or build.get('profile') != 'release'
                or build.get('binary_sha256') != provenance.get('binary_sha256')
                or build.get('rust_inputs') != rust_inputs(provenance.get('source_hashes', {}))
                or provenance.get('source_hashes', {}).get('ops/runtime_bench.py') != reports[0].get('provenance', {}).get('source_hashes', {}).get('ops/runtime_bench.py')):
            return False
    return True


def histogram_estimate(text,name,quantile=.99):
    """Prometheus bucket interpolation, explicitly distinct from exact samples."""
    import re
    pattern=re.compile(re.escape(name)+r'_bucket\{le="([^"]+)"\}\s+(\d+)')
    buckets=sorted((float(bound),int(count)) for bound,count in pattern.findall(text))
    if not buckets or buckets[-1][1]==0: return None
    rank=quantile*buckets[-1][1];low=0.;previous=0
    for bound,count in buckets:
        if count>=rank:
            if not math.isfinite(bound): return low
            return low+(bound-low)*(rank-previous)/(count-previous) if count>previous else bound
        low,previous=bound,count
    return None
