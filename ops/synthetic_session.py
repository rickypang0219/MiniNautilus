#!/usr/bin/env python3
"""Offline, real-time paced paper session: validates the observability pipeline.

No network: prices are a deterministic random walk, so there are NO network
latency numbers here. It exercises the same internal path as the live runner
(Python SMA -> JSON IPC -> Rust OMS/risk -> durable journal -> paper venue) at a
chosen rate, with both metrics endpoints enabled. Use examples/sma_spot.py for
live market data once the Binance hosts are reachable.
"""
import argparse
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from mininautilus import metrics
from mininautilus.backtest import SmaTarget, Bar
from mininautilus.bridge import ROOT, Engine, open_orders
from mininautilus.targets import target_event, target_order


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--seconds", type=int, default=300)
    p.add_argument("--rate", type=float, default=50.0, help="market updates per second")
    p.add_argument("--bar-every", type=int, default=10, help="updates per synthetic closed candle")
    p.add_argument("--metrics-port", type=int, default=9465)
    p.add_argument("--sync", choices=["every", "outbox"], default="every")
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/mininautilus")
    args = p.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    if not metrics.start(args.metrics_port):
        raise SystemExit("pip install prometheus_client")
    config = args.run_dir / "config.json"
    config.write_text(json.dumps(dict(max_abs_position=10, max_order_qty=10, max_order_notional=10**12,
                                      request_timeout_ms=30000, market_stale_ms=3000,
                                      private_stale_ms=30000, max_signal_lag=10)))
    rng = random.Random(7)
    strategy = SmaTarget(5, 20, lots=2)
    price, ttl_ms, period = 100_000, 3_000, 1.0 / args.rate
    started = time.monotonic()
    with Engine(args.run_dir / "events.jsonl", paper=True, config=config, binary=args.binary,
                time_mode="historical", extra_args=["--sync", args.sync],
                env={"MINI_METRICS_ADDR": f"127.0.0.1:{args.metrics_port - 1}"}) as engine:
        origin, initial = time.monotonic(), engine.state["now"]
        now = lambda: initial + int((time.monotonic() - origin) * 1000)
        sent_at, updates = {}, 0
        next_due = time.monotonic()
        while time.monotonic() - started < args.seconds:
            loop_started = time.perf_counter()
            engine.send(now(), "Tick")
            engine.send(now(), {"Heartbeat": {"epoch": engine.state["epoch"]}})
            price = max(1, price + rng.randint(-20, 20))
            observed = now()  # one clock read: observed_at must not exceed the input time
            engine.send(observed, {"QuoteObserved": {"bid": price - 1, "ask": price + 1, "observed_at": observed}})
            for taker, px in (("Sell", price + 1), ("Buy", price - 1)):
                engine.send(now(), {"Trade": {"taker": taker, "price": px, "qty": rng.randint(1, 3)}})
            updates += 1
            for order in list(open_orders(engine.state)):
                oid = order["intent"]["id"]
                if order["pending"] is None and now() - sent_at.get(oid, 0) >= ttl_ms:
                    engine.send(now(), {"Cancel": {"id": oid}})
                    metrics.count("events", "cancel_ttl")
            if updates % args.bar_every == 0:
                candle_received = time.perf_counter()
                with metrics.timer("strategy"):
                    target = strategy.on_bar(Bar(now(), price, 1, "Buy"))
                if target is not None:
                    if engine.state.get("target") is None or engine.state["target"]["position"] != target:
                        engine.send(now(), target_event(engine.state, target))
                        metrics.count("events", "signal")
                    command = target_order(engine.state)
                    if command:
                        effects = engine.send(now(), command)
                        for effect in effects:
                            if "SendOrder" in effect:
                                sent_at[effect["SendOrder"]["id"]] = now()
                                metrics.observe("tick_to_trade", time.perf_counter() - candle_received)
                                metrics.count("events", "order")
                            elif "Refused" in effect:
                                metrics.count("events", "refused")
            if engine.state["health"] != "Healthy":
                metrics.count("events", "gate_closed")
            metrics.observe("loop", time.perf_counter() - loop_started)
            next_due += period
            delay = next_due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                metrics.count("events", "overrun")
                next_due = time.monotonic()
        print(json.dumps(dict(updates=updates, orders=len(engine.state["orders"]),
                              fills=len(engine.state["fills"]), position=engine.state["position"],
                              health=engine.state["health"])), flush=True)


if __name__ == "__main__":
    main()
