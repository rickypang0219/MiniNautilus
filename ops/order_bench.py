#!/usr/bin/env python3
"""Fixed-seed, fixed-event paced order-path evidence; no exchange access.

Run each candidate >=5 times, interleaving variants. Journals/traces are never
reused. Rust timing here is explicitly before response serialization; use the
normal Prometheus soak to validate full request latency after a candidate wins.
"""
import argparse
import hashlib
import json
import math
import platform
from pathlib import Path
import random
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mininautilus.bridge import Engine
from mininautilus.backtest import SmaTarget, Bar, market_events, plan


def stats(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "p99_ms": None}
    return {"count": len(values), "p50_ms": values[len(values)//2]/1e6,
            "p99_ms": values[math.ceil(.99*len(values))-1]/1e6,
            "max_ms": values[-1]/1e6}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--binary', type=Path, default=ROOT/'target/release/mininautilus')
    p.add_argument('--updates', type=int, default=1000)
    p.add_argument('--rate', type=float, default=100)
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--sync', choices=['every','outbox'], default='outbox')
    p.add_argument('--flush-ms', type=int, default=0)
    p.add_argument('--flush-bytes', type=int, default=32768)
    a=p.parse_args()
    if a.updates<30 or a.rate<=0:p.error('updates >=30 and rate >0 required')
    a.run_dir.mkdir(parents=True,exist_ok=False)
    config=a.run_dir/'config.json'
    config.write_text(json.dumps(dict(max_abs_position=10,max_order_qty=10,max_order_notional=10**12,
        request_timeout_ms=30000,market_stale_ms=3000,private_stale_ms=30000,max_signal_lag=100)))
    extra=['--sync',a.sync]
    if a.flush_ms:extra+=['--flush-ms',str(a.flush_ms),'--flush-bytes',str(a.flush_bytes)]
    rng=random.Random(a.seed);strategy=SmaTarget(3,8,lots=2);price=100000
    rust=[];ipc=[];ticks=[];loops=[];overruns=0;inputs=hashlib.sha256()
    started=time.perf_counter();next_due=started
    with Engine(a.run_dir/'events.jsonl',paper=True,binary=a.binary,config=config,
                time_mode='historical',extra_args=extra,
                env={'MINI_ORDER_TRACE':str(a.run_dir/'orders.jsonl')}) as e:
        for n in range(a.updates):
            begin=time.perf_counter_ns();price=max(1,price+rng.randint(-20,20))
            bar=Bar(n*20,price,rng.randint(1,5),rng.choice(['Buy','Sell']))
            events=market_events(bar,e.state['epoch'])
            inputs.update(json.dumps((bar,events)).encode())
            e.decision_started_ns=None
            e.send_batch(bar.at,events)
            rust.append(e.last_trace['rust_before_response_ns']);ipc.append(e.last_roundtrip_ns)
            e.decision_started_ns=time.perf_counter_ns()
            target=strategy.on_bar(bar,e.state)
            commands=plan(e.state,bar,target,order_ttl_ms=20)
            if commands:
                e.send_batch(bar.at,commands)
                rust.append(e.last_trace['rust_before_response_ns']);ipc.append(e.last_roundtrip_ns)
                if any(x['effect']=='SendOrder' for x in e.last_trace['actions']):
                    ticks.append(e.last_returned_ns-e.decision_started_ns)
            assert e.state['health']=='Healthy'
            loops.append(time.perf_counter_ns()-begin)
            next_due+=1/a.rate;delay=next_due-time.perf_counter()
            if delay>0:time.sleep(delay)
            else:overruns+=1;next_due=time.perf_counter()
        final=dict(position=e.state['position'],cash=e.state['cash'],orders=len(e.state['orders']),fills=len(e.state['fills']))
    rows=[json.loads(x) for x in (a.run_dir/'orders.jsonl').read_text().splitlines()]
    assert len(ticks)>0
    report={'session_exit':0,'experiment':{'machine':hashlib.sha256(platform.node().encode()).hexdigest(),
        'platform':platform.platform(),'workload':'fixed-order-bench-v1','updates':a.updates,'rate':a.rate,
        'seed':a.seed,'input_sha256':inputs.hexdigest(),'trace':True},
        'provenance':{'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
          'dirty':bool(subprocess.check_output(['git','status','--porcelain','--','src','python','ops'],cwd=ROOT,text=True).strip())},
        'candidate':{'sync':a.sync,'flush_ms':a.flush_ms,'flush_bytes':a.flush_bytes,
          'binary_sha256':hashlib.sha256(a.binary.read_bytes()).hexdigest()},
        'scope':'Rust timing stops before response encoding; no exchange or network latency.',
        'elapsed_seconds':time.perf_counter()-started,'overruns':overruns,'final':final,
        'rust':{'request':{'':stats(rust)},'stages':{'journal_sync':stats([r['stages']['sync_ns'] for r in rows if r['stages']['synced']])}},
        'python':{'ipc':{'':stats(ipc)},'tick_to_trade':{'':stats(ticks)},'loop':{'':stats(loops)}},
        'rust_state':{'healthy_fraction':1.0}}
    (a.run_dir/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'elapsed':report['elapsed_seconds'],'actions':len(rows),'tick_to_trade':stats(ticks),'final':final}))

if __name__=='__main__':main()
