#!/usr/bin/env python3
"""LC-v1 evidence recorder and independent raw-data analyser.

No AWS resources or trading sessions are created. Runs a supplied command;
records the whole descendant process group, host resources and immutable hashes.
Unknown data stays null. CPU seconds use procfs utime+stime, not wall-time.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'lc_evidence_v1'


def dump(path, obj):
    with Path(path).open('x') as handle:
        json.dump(obj, handle, indent=2, allow_nan=False)
        handle.write('\n')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def percentile(values, fraction=.99):
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else None


def proc(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        status = {}
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('VmRSS', 'VmHWM'):
                status[key] = int(value.split()[0])*1024
        io = {}
        try:
            for line in Path(f'/proc/{pid}/io').read_text().splitlines():
                key, value = line.split(':', 1)
                io[key] = int(value)
        except OSError:
            pass
        return dict(pid=pid, ppid=int(fields[1]), start_ticks=int(fields[19]),
                    cpu_seconds=(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK'),
                    rss_bytes=status.get('VmRSS'), hwm_bytes=status.get('VmHWM'), io=io)
    except (OSError, ValueError, IndexError):
        return None


def descendants(root):
    rows = {int(p.name): r for p in Path('/proc').iterdir() if p.name.isdigit()
            if (r := proc(int(p.name))) is not None}
    selected = {root} if root in rows else set()
    while True:
        more = {pid for pid, r in rows.items() if r['ppid'] in selected}
        if more <= selected:
            break
        selected |= more
    return [rows[p] for p in sorted(selected)]


def host(previous=None):
    text = Path('/proc/stat').read_text().splitlines()[0].split()[1:]
    ticks = [int(x) for x in text[:8]]  # guest already included in user/nice
    current = (sum(ticks), ticks[7])
    delta = current[0] - previous[0] if previous else 0
    steal = 100*(current[1]-previous[1])/delta if delta > 0 else None
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemAvailable', 'SwapTotal', 'SwapFree'):
            mem[key] = int(value.split()[0])*1024
    vm = {}
    for line in Path('/proc/vmstat').read_text().splitlines():
        key, value = line.split()
        if key in ('pswpin', 'pswpout', 'oom_kill'):
            vm[key] = int(value)
    return dict(steal_pct=steal, mem_available_bytes=mem.get('MemAvailable'),
                loadavg=list(os.getloadavg()), **vm), current


def cgroup(pid, expected=None):
    try:
        relative = Path(f'/proc/{pid}/cgroup').read_text().strip().split('0::', 1)[1]
        path = Path('/sys/fs/cgroup')/relative.lstrip('/')
        if expected is None or path.resolve() != Path(expected).resolve():
            return dict(current_bytes=None, peak_bytes=None,
                        missing_reason='dedicated cgroup not declared or path mismatch')
        return dict(current_bytes=int((path/'memory.current').read_text()),
                    peak_bytes=int((path/'memory.peak').read_text()), path=str(path))
    except (OSError, ValueError, IndexError):
        return dict(current_bytes=None, peak_bytes=None, missing_reason='cgroup unavailable')


def ac(identifier, metric, observed, limit, unit, paths, scope='local', comparator='<=', n=1):
    status = 'NOT_MEASURED' if observed is None else ('PASS' if
             (observed <= limit if comparator == '<=' else observed >= limit) else 'FAIL')
    return dict(ac_id=identifier, threshold_version='LC-v1', scope=scope, metric=metric,
                statistic='max' if comparator == '<=' else 'min', observed=observed,
                limit=limit, comparator=comparator, unit=unit, n=n, status=status,
                reason='missing required data' if observed is None else 'raw-derived budget check',
                evidence_paths=paths)


def analyse(directory):
    d = Path(directory)
    manifest = json.loads((d/'manifest.json').read_text())
    index = json.loads((d/'artifact-index.json').read_text())
    for row in index['files']:
        if sha(d/row['path']) != row['sha256']:
            raise ValueError('artifact hash mismatch: '+row['path'])
    rows = [json.loads(line) for line in (d/'resources.jsonl').read_text().splitlines()]
    if not rows:
        raise ValueError('zero resource samples')
    times = [r['monotonic_ns'] for r in rows]
    if any(a >= b for a, b in zip(times, times[1:])):
        raise ValueError('duplicate or unordered sample timestamps')
    duration = manifest['duration_seconds']
    interval = manifest['sample_interval_seconds']
    measured = sum(min(interval, (b-a)/1e9) for a, b in zip(times, times[1:]))
    coverage = min(1., measured/duration) if duration > 0 else None
    gaps = [(b-a)/1e9 for a, b in zip(times, times[1:])]
    gaps += [max(0, (times[0]-manifest['start_monotonic_ns'])/1e9),
             max(0, (manifest['end_monotonic_ns']-times[-1])/1e9)]
    rss = [r['rss_bytes'] for r in rows if r['rss_bytes'] is not None]
    peaks = [r['cgroup']['peak_bytes'] for r in rows if r['cgroup']['peak_bytes'] is not None]
    avail = [r['host']['mem_available_bytes'] for r in rows if r['host']['mem_available_bytes'] is not None]
    steals = [r['host']['steal_pct'] for r in rows if r['host']['steal_pct'] is not None]
    checks = [ac('LC1-2', 'process_group_rss_peak', max(rss) if rss else None, 384*2**20, 'bytes', ['resources.jsonl']),
              ac('LC1-2', 'cgroup_memory_peak', max(peaks) if peaks else None, 640*2**20, 'bytes', ['resources.jsonl']),
              ac('LC1-2', 'host_mem_available_min', min(avail) if avail else None, 192*2**20, 'bytes', ['resources.jsonl'], comparator='>='),
              ac('LC0-2', 'steal_p99', percentile(steals), 5., '%', ['resources.jsonl'], n=len(steals)),
              ac('LC0-2', 'sample_coverage', coverage, .99, 'ratio', ['resources.jsonl'], comparator='>='),
              ac('LC0-2', 'max_sample_gap', max(gaps), 5., 'seconds', ['resources.jsonl'])]
    # Process HWM does not replace the process-group sampled maximum, but is
    # retained to disclose short-lived per-process peaks and process generations.
    generations = {}
    for row in rows:
        for p in row['processes']:
            key = f"{p['pid']}:{p['start_ticks']}"
            old = generations.setdefault(key, dict(cpu_seconds=0, hwm_bytes=0))
            old['cpu_seconds'] = max(old['cpu_seconds'], p['cpu_seconds'])
            old['hwm_bytes'] = max(old['hwm_bytes'], p['hwm_bytes'] or 0)
    return dict(schema=SCHEMA, run_id=manifest['run_id'], exit_code=manifest['exit_code'],
                counts=dict(samples=len(rows), generations=len(generations)),
                process_generations=generations, checks=checks,
                unexpected=[] if manifest['exit_code'] == 0 else ['command_failed'],
                limits=['CPU/RSS samples can miss very short-lived children; dedicated cgroup peak required',
                        'CloudWatch credit and lifecycle metrics require their dedicated collectors'])


def run(args):
    d = args.run_dir
    d.mkdir(parents=True, exist_ok=False)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise ValueError('command required')
    git = lambda *a: subprocess.check_output(['git', *a], cwd=ROOT, text=True).strip()
    threshold = ROOT/'docs/runtime-v2-lifecycle-acceptance-proposal.md'
    start = time.monotonic_ns()
    manifest = dict(schema=SCHEMA, run_id=d.name, source_commit=git('rev-parse', 'HEAD'),
                    harness_commit=git('rev-parse', 'HEAD'), dirty=bool(git('status', '--porcelain')),
                    threshold_sha256=sha(threshold), profile=args.profile, seed=args.seed,
                    command=command, scope=args.scope, start=utc(), start_monotonic_ns=start,
                    sample_interval_seconds=args.interval, dedicated_cgroup=args.cgroup,
                    trust_contract_id='legacy-full-cold-checksum', instance_id=args.instance_id)
    if manifest['dirty'] and not args.allow_dirty:
        raise ValueError('clean tree required for evidence')
    binary = Path(command[0])
    manifest['command_binary_sha256'] = sha(binary) if binary.is_file() else None
    with (d/'stdout.log').open('x') as out, (d/'stderr.log').open('x') as err, (d/'resources.jsonl').open('x') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err, start_new_session=True)
        stop = threading.Event()
        def sample():
            previous = None
            while True:
                ps = descendants(process.pid)
                h, previous = host(previous)
                vfs = os.statvfs(d)
                record = dict(timestamp=utc(), monotonic_ns=time.monotonic_ns(), phase=args.profile,
                              processes=ps, rss_bytes=sum(p['rss_bytes'] or 0 for p in ps) if ps else None,
                              cgroup=cgroup(process.pid, args.cgroup), host=h,
                              disk_free_bytes=vfs.f_bavail*vfs.f_frsize)
                log.write(json.dumps(record, allow_nan=False)+'\n'); log.flush()
                if stop.wait(args.interval):
                    break
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        code = process.wait()
        stop.set(); thread.join()
    end = time.monotonic_ns()
    manifest.update(end=utc(), end_monotonic_ns=end, duration_seconds=(end-start)/1e9, exit_code=code)
    dump(d/'manifest.json', manifest)
    files = [dict(path=p.name, bytes=p.stat().st_size, sha256=sha(p)) for p in sorted(d.iterdir()) if p.is_file()]
    dump(d/'artifact-index.json', dict(schema=SCHEMA, files=files, availability='local', retention_days=None))
    summary = analyse(d)
    dump(d/'summary.json', summary)
    print(json.dumps(summary, allow_nan=False))
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_subparsers(dest='mode', required=True)
    r = modes.add_parser('run')
    r.add_argument('--run-dir', type=Path, required=True)
    r.add_argument('--profile', default='W1')
    r.add_argument('--seed', type=int, default=7)
    r.add_argument('--scope', default='local')
    r.add_argument('--instance-id')
    r.add_argument('--interval', type=float, default=.1)
    r.add_argument('--cgroup')
    r.add_argument('--allow-dirty', action='store_true')
    r.add_argument('command', nargs=argparse.REMAINDER)
    a = modes.add_parser('analyse')
    a.add_argument('directory', type=Path)
    args = p.parse_args()
    if args.mode == 'run':
        if args.interval <= 0:
            p.error('interval must be positive')
        return run(args)
    recomputed = analyse(args.directory)
    recorded = json.loads((args.directory/'summary.json').read_text())
    if recorded != recomputed:
        raise ValueError('summary disagrees with raw evidence')
    print(json.dumps(recomputed, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
