#!/usr/bin/env python3
"""Live Binance Spot Testnet quotes; paper execution by default.

--mode testnet enables real API mutations of TESTNET funds only. Production URLs
are not configurable. Credentials are read from environment and never persisted.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from mininautilus import Engine, TargetPosition
from mininautilus.binance import BinanceSpot, VenueError


def decide(state, target):
    intent = TargetPosition(target).on_quote(state)
    if intent:
        intent["valid_until"] = state["now"] + 5000
    return intent


def run(args):
    metadata_path = args.run_dir / "session.json"
    if args.resume:
        if args.mode != "testnet":
            raise ValueError("resume is supported for testnet; paper uses a fresh run directory")
        metadata = json.loads(metadata_path.read_text())
        if metadata["symbol"] != args.symbol or metadata["mode"] != args.mode:
            raise ValueError("session mode/symbol must match")
    else:
        args.run_dir.mkdir(parents=True, exist_ok=False)
        metadata = {"symbol": args.symbol, "mode": args.mode, "session": uuid.uuid4().hex[:8]}
    venue = BinanceSpot(args.symbol, metadata["session"], execute=args.mode == "testnet")
    venue.initialize()
    if args.resume:
        if (metadata["tick"], metadata["lot"]) != (str(venue.tick), str(venue.lot)):
            raise VenueError("tick/lot configuration changed; cannot replay with new units")
    else:
        if args.mode == "testnet":
            if venue.open_orders():
                raise VenueError("fresh session requires no open orders on this symbol")
            metadata["baseline_base"] = str(venue.base_balance())
        metadata.update(tick=str(venue.tick), lot=str(venue.lot), base_asset=venue.base_asset)
        # Session identity must reach disk before a journal can expose any order.
        with metadata_path.open("x") as handle:
            json.dump(metadata, handle, indent=2)
            handle.flush()
            import os
            os.fsync(handle.fileno())
        dir_fd = os.open(args.run_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        config = {"max_abs_position": args.lots, "max_order_qty": args.lots,
                  "max_order_notional": args.max_notional_units, "request_timeout_ms": 30000,
                  "market_stale_ms": 5000, "private_stale_ms": 30000, "max_signal_lag": 100}
        (args.run_dir / "config.json").write_text(json.dumps(config))

    with Engine(args.run_dir / "events.jsonl", paper=args.mode == "paper", recover=args.resume,
                config=args.run_dir / "config.json") as engine:
        clock_origin = time.monotonic()
        time_origin = engine.state["now"]

        def now():
            return time_origin + int((time.monotonic() - clock_origin) * 1000)

        def deliver(report):
            event = {"Execution": {"epoch": engine.state["epoch"],
                "venue_seq": engine.state["venue_seq"] + 1, "report": report}}
            return engine.send(now(), event, **venue.timing(event))

        def recover():
            engine.send(now(), "Disconnect")
            engine.send(now(), "Reconnect")
            snapshot = venue.reconcile(engine.state, metadata["baseline_base"])
            engine.send(now(), {"Reconcile": snapshot}, **venue.timing({"Reconcile": snapshot}))
            if engine.state["health"] != "Healthy":
                raise VenueError("Rust rejected reconciliation; journal retained")

        def dispatch(effects):
            if args.mode == "paper":
                return  # The Rust paper runtime already executed these effects.
            for effect in effects:
                if "SendOrder" in effect:
                    intent = effect["SendOrder"]
                    venue.submit(intent)
                    deliver({"Accepted": {"id": intent["id"]}})
                elif "SendCancel" in effect:
                    venue.cancel(engine.state["orders"][str(effect["SendCancel"]["id"])]["intent"])
                elif "QueryState" in effect:
                    recover()
                    break

        if args.resume:
            recover()
        try:
            # Dedicated process: a slow Python callback cannot own Rust account state.
            with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as worker:
                pending = None
                for _ in range(args.iterations):
                    dispatch(engine.send(now(), "Tick"))
                    if args.mode == "testnet":
                        for report in venue.poll(engine.state):
                            dispatch(deliver(report))
                    engine.send(now(), {"Heartbeat": {"epoch": engine.state["epoch"]}})
                    quote = venue.quote()
                    engine.send(now(), {"Quote": quote})
                    if args.mode == "paper":
                        # Optimistic touch fill, capped to one lot per poll; no queue model.
                        for taker, price in [("Sell", quote["ask"]), ("Buy", quote["bid"])]:
                            engine.send(now(), {"Trade": {"taker": taker, "price": price, "qty": 1}})
                    if pending is not None and pending.done():
                        intent = pending.result()
                        pending = None
                        if intent:
                            dispatch(engine.send(now(), {"Submit": intent}))
                    if pending is None:
                        pending = worker.submit(decide, engine.state, args.lots)
                    print(json.dumps({"seq": engine.state["seq"], "position_lots": engine.state["position"],
                        "health": engine.state["health"], "quote": quote}), flush=True)
                    time.sleep(args.interval)
        finally:
            # A normal stop also cancels our open testnet orders and verifies fills.
            # This does not flatten inventory. Any failure leaves the durable gate closed.
            if args.mode == "testnet":
                try:
                    recover()
                except Exception:
                    engine.send(now(), "Disconnect")
                    raise
            engine.send(now(), "Disconnect")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=["paper", "testnet"], default="paper")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--lots", type=int, default=3, help="integer LOT_SIZE increments, not whole coins")
    parser.add_argument("--max-notional-units", type=int, default=10**15,
                        help="integer tick*lot notional cap; inspect exchangeInfo before testnet trading")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.lots <= 0 or args.iterations <= 0 or args.interval < 0.25:
        parser.error("positive lots/iterations and interval >= 0.25 seconds required")
    try:
        run(args)
    except (VenueError, ValueError, OSError, RuntimeError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        sys.exit(1)
