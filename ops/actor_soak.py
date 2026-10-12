#!/usr/bin/env python3
"""Offline wall-clock fault soak of the live actor runner (G2, proposal §3).

  python3 ops/actor_soak.py --run-dir runs/actor-soak-s7 --minutes 90 --seed 7

Real Rust engine (release receipt required) and mininautilus.live_actor with
FakeLiveAdapter; no network. Every `--fault-interval` seconds (jittered by the
seed) one fault is armed: late SDK result, send lost before the venue, private
poll failure, market stream drop, or an operator recovery drill. Live rotation
runs throughout. Seeds may run concurrently: reliability coverage only, never
performance evidence.

Pass criteria (report.json `checks`): session exits without error, shutdown
reconciliation succeeds, local position equals the venue's, no order ID ever
reaches the venue twice, every latch reason is an injected fault or rotation.
"""
import argparse
import asyncio
import json
from pathlib import Path
import random
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

FAULTS = ('late_result', 'lost_before_send', 'poll_failure', 'stream_drop', 'drill')
EXPECTED_GATES = {'rotation', 'delivery_uncertain', 'private_poll_failed', 'recovery_drill'}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--minutes', type=float, default=90)
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--fault-interval', type=float, default=90)
    p.add_argument('--rotate-orders', type=int, default=200)
    p.add_argument('--binary', type=Path, default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt', type=Path, default=ROOT/'target/runtime-v2-build.json')
    p.add_argument('--allow-dirty', action='store_true')
    args = p.parse_args()
    identity = verified_identity(ROOT, args.binary, args.receipt)
    if identity['dirty'] and not args.allow_dirty:
        p.error('clean tree required')
    args.run_dir.mkdir(parents=True, exist_ok=False)
    (args.run_dir/'provenance.json').write_text(json.dumps(identity, indent=2))
    config = args.run_dir/'config.json'
    config.write_text(json.dumps(dict(max_abs_position=1, max_order_qty=1, max_order_notional=10**9,
                                      request_timeout_ms=30000, market_stale_ms=3000,
                                      private_stale_ms=30000, max_signal_lag=10)))
    rng = random.Random(args.seed)
    adapter = FakeLiveAdapter(pace=0.02, prices=PRICES, candles=10**9, forget_retired=True)
    injected = []
    log = (args.run_dir/'notes.jsonl').open('x')
    started = time.monotonic()

    def note(kind, **data):
        if kind not in ('candle', 'signal', 'order'):
            log.write(json.dumps(dict(kind=kind, t=round(time.monotonic()-started, 2), **data))+'\n')
            log.flush()

    with Engine(args.run_dir/'events.jsonl', config=config, binary=args.binary) as engine:
        origin, initial = time.monotonic(), engine.state['now']
        def now():
            return initial + int((time.monotonic()-origin)*1000)
        audit = IncrementalAudit(1, long_only=True)
        audit.check(engine.state, None)
        session = ActorSession(engine, adapter, SmaStrategy(2, 3, 1, '1s', long_only=True),
                               identity=('fake', 'dedicated', 'TEST'), now=now, note=note, audit=audit,
                               max_orders=10**9, poll_seconds=0.1, lots=1, execution_timeout=0.5,
                               rotate_orders=args.rotate_orders, reconcile_retry=lambda attempt: time.sleep(0.2))
        stop = threading.Event()

        def faults():
            while not stop.wait(args.fault_interval * rng.uniform(0.5, 1.5)):
                fault = FAULTS[len(injected) % len(FAULTS)] if len(injected) < len(FAULTS) else rng.choice(FAULTS)
                if fault == 'late_result':
                    adapter.delay_next_submit = 1.5  # beyond the 0.5 s execution timeout
                elif fault == 'lost_before_send':
                    adapter.submit_lost_before_send = True
                elif fault == 'poll_failure':
                    adapter.fail_next_poll = True
                elif fault == 'stream_drop':
                    adapter.disconnect_next = True
                else:
                    session.drills.append(time.monotonic() - session.started)
                injected.append(dict(fault=fault, t=round(time.monotonic()-started, 2)))
                note('fault_armed', fault=fault)

        thread = threading.Thread(target=faults, daemon=True)
        error = None
        try:
            thread.start()
            asyncio.run(session.run(args.minutes*60))
        except Exception as failure:
            error = type(failure).__name__
        stop.set()
        try:
            reconciled = session.close(reconcile=True)
        except Exception as failure:
            reconciled, error = False, error or type(failure).__name__
        report = session.report()
        final = dict(position=engine.state['position'], venue_position=adapter.position,
                     journal=Path(engine.journal).name, health=engine.state['health'])
    gates = set(report['gates'])
    checks = dict(no_error=error is None, shutdown_reconciled=reconciled,
                  position_matches_venue=final['position'] == final['venue_position'],
                  no_resubmission=adapter.resubmissions == 0 and len(adapter.submits) == len(set(adapter.submits)),
                  only_expected_gates=gates <= EXPECTED_GATES,
                  every_fault_type_injected=set(FAULTS) <= {f['fault'] for f in injected},
                  rotated_at_least_three_times=report['rotations'] >= 3)
    result = dict(seed=args.seed, minutes=args.minutes, error=error, checks=checks,
                  passed=all(checks.values()), final=final, injected=injected, session=report,
                  provenance=dict(commit=identity['commit'], dirty=identity['dirty'],
                                  binary_sha256=identity['binary_sha256']))
    (args.run_dir/'report.json').write_text(json.dumps(result, indent=2, default=list))
    print(json.dumps(dict(seed=args.seed, passed=result['passed'], checks=checks, rotations=report['rotations'],
                          submissions=report['submissions'], gates=report['gates'])))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
