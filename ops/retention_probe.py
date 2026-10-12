#!/usr/bin/env python3
"""G3 offline retention probe: long actor session, with and without live rotation.

  python3 ops/retention_probe.py --run-dir runs/retention-<date> --minutes 10 --rotate-orders 200

Real Rust engine (release receipt required), FakeLiveAdapter venue, no network.
Samples once per second: Python/Rust VmRSS and VmHWM, Core/mirror/audit hot
counts and rotations. Reports per-segment RSS (minute slices) and the RSS slope
after warm-up, hot-count maxima, and journal recovery time of the final journal.
HWM is reported, never used as evidence of release (docs/adr-live-rotation.md §4).
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))
from mininautilus.audit import IncrementalAudit
from mininautilus.bridge import Engine
from mininautilus.evidence import verified_identity
from mininautilus.live_actor import ActorSession
from mininautilus.offline import PRICES, FakeLiveAdapter
from mininautilus.sma import SmaStrategy
from system_sample import process


def kib(status, key):
    value = (status or {}).get(key)
    return int(value.split()[0]) if value else None


def slope_mib_per_hour(points):
    if len(points) < 2:
        return None
    n = len(points)
    mx = sum(t for t, _ in points)/n
    my = sum(v for _, v in points)/n
    var = sum((t-mx)**2 for t, _ in points)
    return (sum((t-mx)*(v-my) for t, v in points)/var)*3600/1024 if var else None


def run(case_dir, minutes, rotate_orders, binary, pace):
    case_dir.mkdir(parents=True)
    config = case_dir/'config.json'
    config.write_text(json.dumps(dict(max_abs_position=1, max_order_qty=1, max_order_notional=10**9,
                                      request_timeout_ms=30000, market_stale_ms=3000,
                                      private_stale_ms=30000, max_signal_lag=10)))
    adapter = FakeLiveAdapter(pace=pace, prices=PRICES, candles=10**9, forget_retired=True, record_submits=False)
    samples = []
    with (case_dir/'notes.jsonl').open('x') as log, \
            Engine(case_dir/'events.jsonl', config=config, binary=binary) as engine:
        origin, initial = time.monotonic(), engine.state['now']
        def now():
            return initial + int((time.monotonic()-origin)*1000)
        def note(kind, **data):
            if kind in ('rotated', 'coordinator_gated', 'rotation_pending', 'reconciled', 'summary'):
                log.write(json.dumps(dict(kind=kind, **data))+'\n')
        audit = IncrementalAudit(1, long_only=True)
        audit.check(engine.state, None)
        session = ActorSession(engine, adapter, SmaStrategy(2, 3, 1, '1s', long_only=True),
                               identity=('fake', 'dedicated', 'TEST'), now=now, note=note, audit=audit,
                               max_orders=10**9, poll_seconds=0.02, lots=1, rotate_orders=rotate_orders,
                               reconcile_retry=lambda attempt: None)
        stop = threading.Event()
        def sampler():
            started = time.monotonic()
            while not stop.wait(1):
                python, rust = process(__import__('os').getpid()), process(engine.process.pid)
                state = engine.state
                samples.append(dict(t=round(time.monotonic()-started, 1),
                    python_rss_kib=kib(python['status'], 'VmRSS'), python_hwm_kib=kib(python['status'], 'VmHWM'),
                    rust_rss_kib=kib(rust['status'], 'VmRSS'), rust_hwm_kib=kib(rust['status'], 'VmHWM'),
                    core_orders=len(state['orders']), core_fills=len(state['fills']),
                    audit_orders=len(audit.orders), submissions=session.counters['submissions'],
                    rotations=session.counters['rotations']))
        thread = threading.Thread(target=sampler, daemon=True)
        thread.start()
        error = None
        try:
            asyncio.run(session.run(minutes*60))
        except Exception as failure:
            error = type(failure).__name__
        stop.set(); thread.join()
        session.close(reconcile=True)
        journal = Path(engine.journal)
        report = session.report()
    started = time.perf_counter()
    with Engine(journal, recover=True, binary=binary):
        recovery_ms = (time.perf_counter()-started)*1000
    warm = [s for s in samples if s['t'] >= 60]
    slices = {}
    for s in samples:
        slices.setdefault(int(s['t']//60), []).append(s)
    result = dict(rotate_orders=rotate_orders, error=error, submissions=report['submissions'],
                  rotations=report['rotations'],
                  hot_max=dict(core_orders=max((s['core_orders'] for s in samples), default=None),
                               core_fills=max((s['core_fills'] for s in samples), default=None),
                               audit_orders=max((s['audit_orders'] for s in samples), default=None)),
                  journal=journal.name, journal_bytes=journal.stat().st_size, recovery_ms=round(recovery_ms, 1),
                  slope_mib_per_hour=dict(python=slope_mib_per_hour([(s['t'], s['python_rss_kib']) for s in warm]),
                                          rust=slope_mib_per_hour([(s['t'], s['rust_rss_kib']) for s in warm])),
                  minute_slices=[dict(minute=m, python_rss_max_kib=max(x['python_rss_kib'] for x in v),
                                      rust_rss_max_kib=max(x['rust_rss_kib'] for x in v),
                                      core_orders_max=max(x['core_orders'] for x in v),
                                      submissions=v[-1]['submissions']) for m, v in sorted(slices.items())],
                  lifetime_hwm_kib=dict(python=max((s['python_hwm_kib'] or 0) for s in samples) if samples else None,
                                        rust=max((s['rust_hwm_kib'] or 0) for s in samples) if samples else None))
    with (case_dir/'samples.jsonl').open('x') as handle:
        handle.writelines(json.dumps(s)+'\n' for s in samples)
    with (case_dir/'report.json').open('x') as handle:
        json.dump(result, handle, indent=2)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--minutes', type=float, default=10)
    p.add_argument('--baseline-minutes', type=float, help='default: same as --minutes; 0 skips the baseline')
    p.add_argument('--rotate-orders', type=int, default=200)
    p.add_argument('--pace', type=float, default=0.005, help='seconds between fake candles')
    p.add_argument('--binary', type=Path, default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt', type=Path, default=ROOT/'target/runtime-v2-build.json')
    p.add_argument('--allow-dirty', action='store_true')
    args = p.parse_args()
    identity = verified_identity(ROOT, args.binary, args.receipt)
    if identity['dirty'] and not args.allow_dirty:
        p.error('clean tree required for evidence')
    args.run_dir.mkdir(parents=True, exist_ok=False)
    (args.run_dir/'provenance.json').write_text(json.dumps(identity, indent=2))
    baseline = args.minutes if args.baseline_minutes is None else args.baseline_minutes
    cases = [('rotating', args.rotate_orders, args.minutes)] + ([('baseline', None, baseline)] if baseline else [])
    results = {case: run(args.run_dir/case, minutes, rotate, args.binary, args.pace)
               for case, rotate, minutes in cases}
    (args.run_dir/'summary.json').write_text(json.dumps(results, indent=2))
    print(json.dumps({k: {x: v[x] for x in ('submissions', 'rotations', 'hot_max', 'recovery_ms', 'slope_mib_per_hour', 'error')}
                      for k, v in results.items()}, indent=1))


if __name__ == '__main__':
    main()
