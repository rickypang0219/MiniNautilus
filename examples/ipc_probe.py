#!/usr/bin/env python3
"""H3 probe: Python<->Rust round trip and response size versus retained history.

Uses `mininautilus sim` (no journal, no fsync) to isolate serialization and IPC.
History is built through real submit/trade requests; only Quote round trips are
timed. Prints JSON; see docs/acceptance.md (H3).
"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from mininautilus import Engine
from mininautilus.bridge import ROOT, next_order_id

CONFIG = {"max_abs_position": 1_000_000, "max_order_qty": 1_000_000, "max_order_notional": 2**62,
          "request_timeout_ms": 100, "market_stale_ms": 1_000, "private_stale_ms": 2**62,
          "max_signal_lag": 100}


def probe(binary, config, history, repeats, full_state, mode="sim", sync="every", directory=None):
    if mode == "sim":
        engine = Engine(sim=True, config=config, binary=binary, time_mode="historical", full_state=full_state)
    else:
        journal = Path(directory) / f"probe-{mode}-{sync}-{history}-{full_state}.jsonl"
        journal.unlink(missing_ok=True)
        engine = Engine(journal, paper=mode == "paper", config=config, binary=binary,
                        time_mode="historical", full_state=full_state, extra_args=["--sync", sync])
    with engine:
        engine.send_batch(0, [{"Quote": {"bid": 100, "ask": 100}}])
        for i in range(history):
            side = "Buy" if i % 2 == 0 else "Sell"
            intent = {"id": next_order_id(engine.state), "side": side, "qty": 1, "limit": 100,
                      "based_on_seq": engine.state["seq"], "valid_until": 2**62}
            engine.send_batch(0, [{"Submit": intent},
                                  {"Trade": {"taker": "Sell" if side == "Buy" else "Buy", "price": 100, "qty": 1}}])
        assert len(engine.state["orders"]) == history and engine.state["health"] == "Healthy"
        samples = []
        for _ in range(repeats + 20):
            start = time.perf_counter_ns()
            engine.send_batch(0, [{"Quote": {"bid": 100, "ask": 100}}])
            samples.append((time.perf_counter_ns() - start) / 1000)
        samples = sorted(samples[20:])
        return {"mode": mode, "sync": sync, "history_orders": history, "full_state": full_state, "repeats": repeats,
                "response_bytes": engine.last_response_bytes,
                "round_trip_us_p50": samples[len(samples) // 2],
                "round_trip_us_p99": samples[min(len(samples) - 1, round(len(samples) * 0.99))]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/release/mininautilus")
    parser.add_argument("--history", type=int, nargs="+", default=[0, 10_000, 100_000])
    parser.add_argument("--full-history", type=int, nargs="+", default=[0, 1_000, 3_000])
    parser.add_argument("--repeats", type=int, default=2000)
    parser.add_argument("--durable-dir", type=Path, help="also time paper mode (journal) per sync policy here")
    args = parser.parse_args()
    config = Path(__file__).resolve().parents[1] / "runs" / "ipc-probe-config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps(CONFIG))
    rows = [probe(args.binary, config, h, args.repeats, False) for h in args.history]
    rows += [probe(args.binary, config, h, max(20, args.repeats // 40), True) for h in args.full_history]
    if args.durable_dir:
        for sync in ("every", "outbox"):
            rows.append(probe(args.binary, config, 0, max(50, args.repeats // 10), False, "paper", sync, args.durable_dir))
    compact = [r for r in rows if not r["full_state"] and r["mode"] == "sim"]
    ratio = compact[-1]["round_trip_us_p99"] / compact[0]["round_trip_us_p99"]
    size_ratio = compact[-1]["response_bytes"] / compact[0]["response_bytes"]
    print(json.dumps({"rows": rows, "p99_ratio": ratio, "size_ratio": size_ratio}, indent=2))
    print(f"H3 compact response: size ratio {size_ratio:.2f}, p99 ratio {ratio:.2f} (targets < 2) -> "
          f"{'PASS' if ratio < 2 and size_ratio < 2 else 'FAIL'}", file=sys.stderr)


if __name__ == "__main__":
    main()
