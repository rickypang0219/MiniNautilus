#!/usr/bin/env python3
"""Open-loop offline runtime evidence; release receipts required, no exchange access."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import socket
import urllib.request
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'python'), str(ROOT / 'ops')]
from mininautilus.bridge import Engine
from mininautilus.backtest import Bar, SmaTarget, plan
from mininautilus.evidence import percentile, validate_actions, verified_identity, write_receipt, histogram_estimate, QueueMeasurements
from mininautilus.offline import FakeVenue, Input, SerialRuntime
from system_sample import sample


def process_cpu_ns(pid):
    """utime+stime of a child process from /proc; None when unavailable (never zero)."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return (int(fields[11])+int(fields[12]))*1_000_000_000//os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None


def host_cpu():
    """Aggregate /proc/stat jiffies (for steal share) and the 1-minute load."""
    try:
        values = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
        return dict(jiffies=values, loadavg=os.getloadavg()[0])
    except (OSError, ValueError, IndexError):
        return None


def host_report(before, after):
    if not before or not after:
        return dict(steal_pct=None, loadavg_start=None, loadavg_end=None)
    delta = [b-a for a, b in zip(before['jiffies'], after['jiffies'])]
    total = sum(delta[:8])
    return dict(steal_pct=100*delta[7]/total if total and len(delta) > 7 else None,
                loadavg_start=before['loadavg'], loadavg_end=after['loadavg'])


def summary(values):
    if not values:
        return dict(count=0, p50_ms=None, p99_ms=None, max_ms=None)
    return dict(count=len(values), p50_ms=percentile(values, .5)/1e6,
                p99_ms=percentile(values, .99)/1e6, max_ms=max(values)/1e6)


async def run(args):
    config = json.loads(args.manifest.read_text())
    identity = verified_identity(ROOT, args.binary, args.receipt)
    if identity['dirty'] and not args.allow_dirty:
        raise ValueError('clean tree required for runtime evidence')
    # Command-line workload overrides are part of the comparable experiment.
    for name in ('rate', 'seconds', 'fake_delay_seconds', 'fake_mode'):
        value = getattr(args, name, None)
        if value is not None:
            config[name] = value
    if config['rate'] <= 0 or config['seconds'] <= 0:
        raise ValueError('positive rate and seconds required')
    args.run_dir.mkdir(parents=True, exist_ok=False)
    config_path = args.run_dir / 'core-config.json'
    config_path.write_text(json.dumps(dict(max_abs_position=2, max_order_qty=2, max_order_notional=10**12,
        request_timeout_ms=30000, market_stale_ms=3000, private_stale_ms=30000, max_signal_lag=10000)))
    (args.run_dir / 'manifest.json').write_text(json.dumps(config, indent=2))
    (args.run_dir / 'provenance.json').write_text(json.dumps(identity, indent=2))
    origin = time.perf_counter_ns()
    strategy = SmaTarget(config['fast'], config['slow'], lots=config['lots'], long_only=True)
    venue = FakeVenue(delay=config['fake_delay_seconds'], mode=config['fake_mode'])
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='core-writer')
    loop = asyncio.get_running_loop()
    input_hash = hashlib.sha256()
    admission, ages, internal, rust, ipc, sync, executor_ages = (deque(maxlen=10000) for _ in range(7))
    missed = 0
    private_seq = 0
    runtime = None
    last_at = 0
    warm_ns = int(config['warmup_seconds'] * 1e9)
    trace_path = args.run_dir / 'orders.jsonl'
    encode,decode,mirror=(deque(maxlen=10000) for _ in range(3))
    queue_groups=QueueMeasurements(retention=config.get('latency_retention_samples',10000),
                                   max_groups=config.get('queue_metric_group_limit',32))
    producer_overrun=deque(maxlen=10000)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));metrics_port=listener.getsockname()[1]
    rust_env={'MINI_METRICS_ADDR':f'127.0.0.1:{metrics_port}'}
    if config['trace']:rust_env['MINI_ORDER_TRACE']=str(trace_path)
    with Engine(args.run_dir / 'events.jsonl', binary=args.binary, config=config_path,
                time_mode='historical', extra_args=['--sync', config['sync']],
                env=rust_env) as engine, \
            (args.run_dir / 'inputs.jsonl').open('x') as input_log, \
            (args.run_dir / 'system.jsonl').open('x') as system_log:
        from mininautilus.evidence import validate_actions
        def request(at, event):
            effects = engine.send(at, event)
            if config['trace']:
                if engine.last_trace['request_id'] != engine.request_id:
                    raise ValueError('Rust request trace ID mismatch')
                rows = [dict(r, request_id=engine.request_id) for r in engine.last_trace['actions']]
                validate_actions(effects, rows, request_id=engine.request_id)
                rust.append(engine.last_trace['rust_before_response_ns'])
                sync.extend(r['stages']['sync_ns'] for r in rows if r['stages']['synced'])
                encode.append(engine.last_encode_ns);decode.append(engine.last_decode_ns);mirror.append(engine.last_mirror_apply_ns)
            ipc.append(engine.last_roundtrip_ns)
            return effects

        def apply_sync(item):
            nonlocal last_at
            last_at = max(last_at, (time.perf_counter_ns() - origin)//1_000_000)
            payload = item.payload
            if item.lane == 'private':
                payload = {'Execution': dict(epoch=engine.state['epoch'], venue_seq=engine.state['venue_seq']+1, report=payload)}
            elif payload == 'Heartbeat':
                payload = {'Heartbeat': {'epoch': engine.state['epoch']}}
            effects = request(last_at, payload)
            if item.lane == 'market' and engine.state['health'] == 'Healthy':
                bar = Bar(last_at, payload['Quote']['bid'], 1, 'Sell')
                target = strategy.on_bar(bar, engine.state)
                for command in plan(engine.state, bar, target, order_ttl_ms=30000):
                    effects.extend(request(last_at, command))
            ready = time.perf_counter_ns()
            if any('SendOrder' in effect for effect in effects) and item.received_ns-origin >= warm_ns:
                internal.append(ready-item.received_ns)
            for effect in effects:
                # The wrapper is private to this harness; identity travels with the effect.
                effect['_ready_ns'] = ready
            input_log.write(json.dumps(dict(source=item.source, source_seq=item.source_seq,
                source_epoch=item.epoch, receive_ns=item.received_ns, engine_seq=engine.state['seq'],
                at=last_at, payload=payload))+'\n')
            return effects

        async def apply(item):
            return await loop.run_in_executor(executor, apply_sync, item)

        async def execute(effect):
            nonlocal private_seq
            ready = effect.pop('_ready_ns', time.perf_counter_ns())
            if not any(name in effect for name in ('SendOrder', 'SendCancel')):
                return
            executor_ages.append(time.perf_counter_ns()-ready)
            reports = await venue.execute(effect)
            for report in reports:
                private_seq += 1
                now = time.perf_counter_ns()
                if not runtime.offer(Input('private', 'private', report, source_seq=private_seq,
                                           received_ns=now, scheduled_ns=now)):
                    raise RuntimeError('private report overflow; reconciliation required')

        def observe(kind, **record):
            if kind=='admission':
                admission.append(record['admitted_ns']-record['item'].scheduled_ns)
            elif kind == 'input':
                item = record['item']
                if item.received_ns-origin >= warm_ns:
                    ages.append(record['started_ns']-record['admitted_ns'])
                    queue_groups.record(item,record['admitted_ns'],record['started_ns'])

        runtime_type = SerialRuntime
        if args.runtime == 'actor':
            from mininautilus.runtime import Coordinator
            runtime_type = Coordinator
        runtime = runtime_type(apply, execute, observer=observe,
            capacity=config['input_capacity'], max_bytes=config['input_bytes'],
            max_message=config['message_bytes'], reserve=config['critical_reserve'],
            effect_capacity=config['effect_capacity'], effect_bytes=config['effect_bytes'],
            source_limit=config['source_limit'], max_market_burst=config['max_market_burst']).start()
        async def dispatch_guard(effect):
            def inspect():
                if 'SendCancel' in effect:
                    order=engine.state['orders'][str(effect['SendCancel']['id'])]
                    if order['lifecycle'] in ('Filled','Canceled','Rejected') and not order['uncertain']:
                        return 'terminal_cancel'
                return 'send'
            return await loop.run_in_executor(executor,inspect)
        if hasattr(runtime,'dispatch_guard'):
            runtime.dispatch_guard=dispatch_guard
            runtime.clock_ms=lambda:(time.perf_counter_ns()-origin)//1_000_000
        cpu_started = time.process_time_ns()
        rust_cpu_started, host_started = process_cpu_ns(engine.process.pid), host_cpu()
        count = int(config['seconds']*config['rate'])
        rng = random.Random(args.seed)
        price = 100000
        try:
            for n in range(count):
                due = origin + int(n/config['rate']*1e9)
                delay = (due-time.perf_counter_ns())/1e9
                if delay > 0:
                    await asyncio.sleep(delay)
                now = time.perf_counter_ns()
                if now-due > 1e9/config['rate']:
                    missed += 1
                price = max(1, price+rng.randint(-20, 20))
                payload = {'Quote': {'bid': price, 'ask': price}}
                input_hash.update(json.dumps(payload).encode())
                producer_overrun.append(now-due)
                runtime.offer(Input('market', 'market', payload, source_seq=n+1, received_ns=now, scheduled_ns=due))
                if n % max(1, int(config['rate'])) == 0:
                    runtime.offer(Input('timer', 'timer', 'Heartbeat', received_ns=now, scheduled_ns=due))
                    runtime.offer(Input('clock', 'timer', 'Tick', received_ns=now, scheduled_ns=due))
                    system_log.write(json.dumps(sample(os.getpid(), engine.process.pid))+'\n')
                    system_log.flush()
            await asyncio.wait_for(runtime.finish(), timeout=35)
            exit_code = 0
        except Exception as error:
            exit_code = 1
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
                await asyncio.gather(runtime.task, return_exceptions=True)
            failure = type(error).__name__
        else:
            failure = None
        cpu_ns = time.process_time_ns()-cpu_started
        rust_cpu_finished, host_finished = process_cpu_ns(engine.process.pid), host_cpu()
        rust_cpu_ns = (rust_cpu_finished-rust_cpu_started
                       if None not in (rust_cpu_started, rust_cpu_finished) else None)
        with urllib.request.urlopen(f'http://127.0.0.1:{metrics_port}/metrics',timeout=3) as response:
            metrics_text=response.read().decode()
        (args.run_dir/'rust.prom').write_text(metrics_text)
        rust_full_p99=histogram_estimate(metrics_text,'mini_request_seconds')
        final = dict(position=engine.state['position'], cash=engine.state['cash'], seq=engine.state['seq'],
                     orders=len(engine.state['orders']), fills=len(engine.state['fills']), health=engine.state['health'])
    executor.shutdown(wait=True, cancel_futures=True)
    if final['health']!='Healthy' or runtime.counts['rejected'] or runtime.counts['pending'] or getattr(runtime,'gated',False):
        exit_code=1;failure=failure or 'unexpected_gate_or_incomplete_work'
    elapsed = (time.perf_counter_ns()-origin)/1e9
    report = dict(session_exit=exit_code, failure=failure, provenance=identity,
        experiment=dict(machine=hashlib.sha256(platform.node().encode()).hexdigest(),
                        platform=platform.platform(), workload=config,
                        seed=args.seed, latency_retention='last 10000 observations per metric', input_sha256=input_hash.hexdigest()),
        candidate=dict(runtime=args.runtime, binary_sha256=identity['binary_sha256']),
        counts=runtime.counts, queue_high_water=runtime.high_water, missed_deadlines=missed,
        gate_reason=getattr(runtime,'gate_reason',None),effects_abandoned=getattr(runtime,'effects_abandoned',0),
        elapsed_seconds=elapsed, final=final,
        metrics=dict(admission=summary(admission), input_queue_age=summary(ages),
                     producer_overrun=summary(producer_overrun),input_queue_age_by_source_and_kind=queue_groups.report(),
                     internal_tick_to_effect=summary(internal), rust_before_response=summary(rust),
                     rust_full_request_histogram_p99_ms=rust_full_p99*1000 if rust_full_p99 is not None else None,
                     python_encode=summary(encode),python_decode=summary(decode),python_mirror_apply=summary(mirror),
                     ipc_roundtrip=summary(ipc), order_sync=summary(sync), executor_queue_age=summary(executor_ages),
                     completed_per_second=runtime.counts['completed']/elapsed,
                     python_cpu_ns_per_event=cpu_ns/max(1, runtime.counts['completed']),
                     rust_cpu_ns_per_event=(rust_cpu_ns/max(1, runtime.counts['completed'])
                                            if rust_cpu_ns is not None else None),
                     total_cpu_ns_per_event=((cpu_ns+rust_cpu_ns)/max(1, runtime.counts['completed'])
                                             if rust_cpu_ns is not None else None)),
        host=host_report(host_started, host_finished),
        metric_scope=dict(total_cpu_ns_per_event='Python process CPU plus Rust child utime+stime over the run, per completed input (thresholds v1)',
                          rust_before_response='Excludes response serialization; not full Rust request',
                          ipc_roundtrip='Includes Rust, fsync, pipe, decode and mirror apply',
                          internal_tick_to_effect='receive to durable effect; excludes fake venue I/O'))
    (args.run_dir / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(dict(run=str(args.run_dir), exit=exit_code, counts=runtime.counts,
                         internal=report['metrics']['internal_tick_to_effect'])))
    return exit_code


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build', action='store_true')
    p.add_argument('--binary', type=Path, default=ROOT/'target/release/mininautilus')
    p.add_argument('--receipt', type=Path, default=ROOT/'target/runtime-v2-build.json')
    p.add_argument('--manifest', type=Path, default=ROOT/'comparisons/runtime-v2/manifest.json')
    p.add_argument('--run-dir', type=Path)
    p.add_argument('--runtime', choices=['serial', 'actor'], default='serial')
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--seconds', type=float)
    p.add_argument('--rate', type=float)
    p.add_argument('--fake-delay-seconds', type=float)
    p.add_argument('--fake-mode', choices=['immediate', 'never', 'reject', 'lost_ack', 'duplicate', 'reordered'])
    p.add_argument('--allow-dirty', action='store_true', help='smoke tests only; never a clean baseline')
    args = p.parse_args()
    if args.build:
        if args.binary.resolve()!=(ROOT/'target/release/mininautilus').resolve():
            p.error('--build only issues a receipt for target/release/mininautilus')
        subprocess.run(['cargo', 'build', '--locked', '--release', '--bin', 'mininautilus'], cwd=ROOT, check=True)
        write_receipt(ROOT, args.binary, args.receipt)
        return 0
    if args.run_dir is None:
        p.error('--run-dir required unless --build')
    try:
        return asyncio.run(run(args))
    except Exception as error:
        # Bootstrap/read/scrape failures must not disappear from a cohort. An
        # unknown count is unknown, never a fabricated zero or success sample.
        if args.run_dir.exists() and not (args.run_dir/'report.json').exists():
            provenance=args.run_dir/'provenance.json'
            (args.run_dir/'report.json').write_text(json.dumps(dict(session_exit=1,
                failure=type(error).__name__,counts=None,metrics=None,
                provenance=json.loads(provenance.read_text()) if provenance.exists() else None))+'\n')
        raise

if __name__ == '__main__':
    sys.exit(main())
