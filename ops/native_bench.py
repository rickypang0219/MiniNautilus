#!/usr/bin/env python3
"""Frozen raw-book replay: existing Python Coordinator+IPC vs native production writer.

One deterministic target per second; immediate simulated transport and fills.
Includes raw JSON decoding after receive timestamp. No socket, TLS, real signing,
account RPC, strategy alpha or exchange latency is represented in this workload.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import platform
from pathlib import Path
import queue
import resource
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'python'), str(ROOT / 'ops')]
from mininautilus.bridge import Engine, next_order_id, open_orders
from mininautilus.hyperliquid import Instrument
from mininautilus.offline import Input
from mininautilus.runtime import Coordinator
from mininautilus.evidence import percentile
from runtime_bench import process_cpu_ns


def emit(q, dropped, row):
    try:
        q.put_nowait(row)
    except queue.Full:
        dropped[0] += 1


def write_trace(q, path):
    with path.open('x') as stream:
        while (row := q.get()) is not None:
            stream.write(json.dumps(row, separators=(',', ':')) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


async def python_run(run_dir, manifest, binary):
    run_dir.mkdir()
    (run_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    config = dict(max_abs_position=20, max_order_qty=40, max_order_notional=40000000,
                  request_timeout_ms=20000, market_stale_ms=5000,
                  private_stale_ms=30000, max_signal_lag=10000)
    config_path = run_dir / 'config.json'
    config_path.write_text(json.dumps(config))
    q, dropped = queue.Queue(1024), [0]
    logger = threading.Thread(target=write_trace, args=(q, run_dir / 'trace.jsonl'))
    logger.start()
    pool = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    state = dict(target=None, target_received=None, market_completed=0, private_seq=0)
    instrument = Instrument('BTC', 3, 5)
    runtime = None
    digest = hashlib.sha256()
    market_offered = market_admitted = 0
    failure = None
    with Engine(run_dir / 'journal.jsonl', binary=binary, config=config_path,
                time_mode='historical', extra_args=['--sync', 'outbox']) as engine:
        def request(event, exchange_ms=None):
            before = time.perf_counter_ns()
            metadata = dict(event_time_ms=exchange_ms, received_time_ms=event['QuoteObserved']['observed_at'],time_source='hyperliquid-testnet:BTC:l2Book') if exchange_ms is not None else {}
            effects = engine.send(max(engine.state['now'], time.time_ns() // 1000000), event, **metadata)
            emit(q, dropped, dict(kind='writer', seq=engine.state['seq'], request_ns=time.perf_counter_ns()-before,
                                 ipc_ns=engine.last_roundtrip_ns, health=engine.state['health']))
            return effects

        request({'Heartbeat': {'epoch': 0}})

        def apply_sync(item):
            if item.lane == 'private':
                state['private_seq'] += 1
                return request({'Execution': dict(epoch=0, venue_seq=state['private_seq'], report=item.payload)})
            n = item.source_seq-1
            payload = item.payload
            effects = []
            if n % manifest['rate'] == 0:
                effects.extend(request({'Heartbeat': {'epoch': 0}}))
            if n % max(1, manifest['rate']//20) == 0:
                effects.extend(request('Tick'))
            if payload['target'] is not None:
                state['target'] = payload['target']
                state['target_received'] = item.received_ns
                effects.extend(request({'SetTarget': dict(revision=n+1, position=state['target'],
                                                          valid_until=time.time_ns()//1000000+65000)}))
            effects.extend(request({'QuoteObserved': dict(bid=payload['bid'], ask=payload['ask'],
                                                          observed_at=item.event_time_ms)}, exchange_ms=payload['exchange_ms']))
            current = engine.state
            if state['target'] is not None and current['health'] == 'Healthy' and not any(open_orders(current)):
                delta = state['target']-current['position']
                if delta:
                    intent = dict(id=next_order_id(current), side='Buy' if delta > 0 else 'Sell',
                                  qty=abs(delta), limit=payload['ask'] if delta > 0 else payload['bid'],
                                  based_on_seq=current['seq'], valid_until=time.time_ns()//1000000+3000)
                    effects.extend(request({'SubmitTargeted': dict(intent=intent,
                        revision=current['last_target_revision'], expected_position=current['position'])}))
            for effect in effects:
                if 'SendOrder' in effect:
                    effect['_received_ns'] = state['target_received']
            state['market_completed'] += 1
            emit(q, dropped, dict(kind='market_sample', index=n,
                included=n >= manifest['warmup_seconds']*manifest['rate'],
                receive_to_writer_complete_ns=time.perf_counter_ns()-item.received_ns,
                scheduled_to_complete_ns=time.perf_counter_ns()-item.scheduled_ns,
                producer_overrun_ns=item.received_ns-item.scheduled_ns))
            return effects

        async def apply(item):
            return await loop.run_in_executor(pool, apply_sync, item)

        async def execute(effect):
            if 'SendOrder' not in effect:
                return
            handoff = time.perf_counter_ns()
            intent = effect['SendOrder']
            emit(q, dropped, dict(kind='execution_complete', id=intent['id'],
                receive_to_transport_handoff_ns=handoff-effect['_received_ns'], signed=False,
                boundary='simulated executor handoff; excludes exchange/signing'))
            reports = [{'Accepted': {'id': intent['id']}}, {'Fill': dict(execution_id=intent['id'],
                order_id=intent['id'], qty=intent['qty'], price=intent['limit'])}]
            for report in reports:
                now = time.perf_counter_ns()
                if not runtime.offer(Input('private', 'private', report, received_ns=now, scheduled_ns=now)):
                    raise RuntimeError('private admission failed')

        def observe(kind, **record):
            if kind == 'input' and record['item'].lane == 'market':
                emit(q, dropped, dict(kind='queue_sample', index=record['item'].source_seq-1,
                    queue_ns=record['started_ns']-record['admitted_ns']))

        runtime = Coordinator(apply, execute, capacity=160, reserve=32, effect_capacity=32,
                              observer=observe, clock_ms=lambda: time.time_ns()//1000000).start()
        origin, wall = time.perf_counter_ns(), time.time_ns()//1000000
        cpu, child_cpu = time.process_time_ns(), process_cpu_ns(engine.process.pid)
        child_peak = 0
        try:
            for n in range(manifest['rate']*manifest['seconds']):
                due = origin+n*1000000000//manifest['rate']
                delay = (due-time.perf_counter_ns())/1e9
                if delay > 0:
                    await asyncio.sleep(delay)
                event_time = wall+n*1000//manifest['rate']
                raw = ('{"channel":"l2Book","data":{"coin":"BTC","time":'+str(event_time)+
                       ',"levels":[[{"px":"80000"}],[{"px":"80000"}]]}}')
                received = time.perf_counter_ns()
                market_offered += 1
                data = json.loads(raw)['data']
                if data['coin'] != 'BTC' or abs(time.time_ns()//1000000-data['time']) > 5000:
                    raise ValueError('controlled book freshness/identity')
                bid, ask = (instrument.price(data['levels'][i][0]['px']) for i in range(2))
                target = [20, 0, -20, 0][n//manifest['order_every'] % 4] if n % manifest['order_every'] == 0 else None
                digest.update(f'{n}:800000:{"none" if target is None else target}\n'.encode())
                if not runtime.offer(Input('market', 'market', dict(bid=bid, ask=ask, target=target, exchange_ms=data['time']),
                    source_seq=n+1, received_ns=received, scheduled_ns=due, event_time_ms=time.time_ns()//1000000)):
                    raise RuntimeError('market admission failed')
                market_admitted += 1
                if n % manifest['rate'] == 0:
                    for line in Path(f'/proc/{engine.process.pid}/status').read_text().splitlines():
                        if line.startswith('VmHWM:'):
                            child_peak = max(child_peak, int(line.split()[1]))
            await asyncio.wait_for(runtime.finish(), 30)
        except Exception as error:
            failure = type(error).__name__ + ': ' + str(error)
            await runtime.abort()
        elapsed = (time.perf_counter_ns()-origin)/1e9
        cpu = time.process_time_ns()-cpu+process_cpu_ns(engine.process.pid)-child_cpu
        final = {key: engine.state[key] for key in ('position', 'cash', 'health')}
        final.update(orders=len(engine.state['orders']), fills=len(engine.state['fills']))
        actions = [dict(id=o['intent']['id'], side=o['intent']['side'], qty=o['intent']['qty'],
                        limit=o['intent']['limit'], filled=o['filled'], lifecycle=o['lifecycle'])
                   for o in engine.state['orders'].values()]
        report = dict(schema=1, runtime='python-actor-controlled', failure=failure,
            counts=dict(offered=market_offered, admitted=market_admitted, completed=state['market_completed'],
                        rejected=market_offered-market_admitted, inflight=market_admitted-state['market_completed']),
            input_sha256=digest.hexdigest(), queue_high_water=runtime.high_water,
            cpu_ns=cpu, elapsed_seconds=elapsed,
            rss_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss+child_peak,
            telemetry_dropped=dropped[0], final=final, actions=actions,
            gate_reason=runtime.gate_reason, all_lane_counts=runtime.counts)
    pool.shutdown(wait=True)
    q.put(None)
    logger.join()
    (run_dir / 'raw-report.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def summarize(run_dir):
    raw = json.loads((run_dir / 'raw-report.json').read_text())
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    metrics = {key: [] for key in ('full_action', 'market', 'queue', 'scheduled', 'overrun')}
    warm_orders = manifest['warmup_seconds']*manifest['rate']//manifest['order_every']
    with (run_dir / 'trace.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            if row['kind'] == 'execution_complete' and row['id'] > warm_orders:
                value = row.get('receive_to_transport_handoff_ns')
                if value is not None:
                    metrics['full_action'].append(value)
            if row['kind'] == 'market_sample' and row['included']:
                for metric, field in [('market','receive_to_writer_complete_ns'),('queue','queue_ns'),
                                      ('scheduled','scheduled_to_complete_ns'),('overrun','producer_overrun_ns')]:
                    if field in row:
                        metrics[metric].append(row[field])
            if row['kind'] == 'queue_sample' and row['index'] >= manifest['warmup_seconds']*manifest['rate']:
                metrics['queue'].append(row['queue_ns'])
    raw['metrics'] = {key: dict(count=len(values), p50_ms=percentile(values,.5)/1e6 if values else None,
        p99_ms=percentile(values,.99)/1e6 if values else None,
        p999_ms=percentile(values,.999)/1e6 if len(values)>=100000 else None,
        p999_status='MEASURED' if len(values)>=100000 else 'NOT_MEASURED') for key,values in metrics.items()}
    raw['cpu_ns_per_market'] = raw['cpu_ns']/max(1,raw['counts']['completed'])
    expected = manifest['rate']*manifest['seconds']
    raw['valid'] = (not raw['failure'] and raw['counts'] == dict(offered=expected, admitted=expected,
        completed=expected,rejected=0,inflight=0) and raw['telemetry_dropped']==0 and
        raw['final']['health']=='Healthy' and raw['final']['orders']==expected//manifest['order_every'] and
        raw['final']['fills']==raw['final']['orders'] and len(metrics['full_action'])==raw['final']['orders']-warm_orders)
    (run_dir/'report.json').write_text(json.dumps(raw,indent=2)+'\n')
    return raw



def provenance(binary, manifest_path, allow_dirty):
    binary = binary.resolve()
    packaged = ROOT / 'BUILD.json'
    if packaged.exists():
        identity = json.loads(packaged.read_text())
        for name, expected in identity['files'].items():
            path = ROOT / name
            if path.resolve().is_relative_to(ROOT) is False or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('packaged source/binary mismatch: ' + name)
        if str(binary.relative_to(ROOT)) not in identity['files']:
            raise ValueError('binary missing from build receipt')
    else:
        commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        dirty = bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())
        if dirty and not allow_dirty:
            raise ValueError('clean source tree required; --allow-dirty is smoke-only')
        identity = dict(commit=commit,dirty=dirty,build_receipt='local executable; packaged receipt required on EC2')
    return dict(identity=identity,binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),kernel=platform.release(),
        machine=platform.machine(),host=hashlib.sha256(platform.node().encode()).hexdigest(),
        cpu_affinity=sorted(os.sched_getaffinity(0)),loadavg=os.getloadavg(),python=sys.version,
        measurement='raw application frame -> simulated executor handoff; includes decode/routing/Core/journal, excludes sockets/TLS/signing/exchange',
        started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['python','native','summarize'], required=True)
    parser.add_argument('--manifest', type=Path, default=ROOT/'comparisons/native-runtime/manifest-v1.json')
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--binary', type=Path)
    parser.add_argument("--allow-dirty", action="store_true", help="smoke only; excluded from performance acceptance")
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text())
    binary=args.binary or ROOT / ("target/release/mininautilus" if args.mode=="python" else "target/release/native-live")
    identity=provenance(binary,args.manifest,args.allow_dirty) if args.mode!="summarize" else None
    if args.mode=='python':
        asyncio.run(python_run(args.run_dir,manifest,args.binary or ROOT/'target/release/mininautilus'))
    elif args.mode=='native':
        subprocess.run([str(args.binary or ROOT/'target/release/native-live'),'bench','--run-dir',str(args.run_dir),
                        '--manifest',str(args.manifest)],check=True,stdout=subprocess.DEVNULL)
    if identity is not None:
        (args.run_dir/"provenance.json").write_text(json.dumps(identity,indent=2)+"\n")
    report=summarize(args.run_dir)
    print(json.dumps(dict(run=str(args.run_dir),valid=report['valid'],metrics=report['metrics'],cpu_ns_per_market=report['cpu_ns_per_market'])))
    return 0 if report['valid'] else 1


if __name__=='__main__':
    raise SystemExit(main())
