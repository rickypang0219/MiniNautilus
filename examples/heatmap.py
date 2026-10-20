#!/usr/bin/env python3
"""B1/B2: Python SMA targets over the Rust backtest runner, single run and sweep.

Generates (or reads) a bar CSV once, then for each (fast, slow) pair computes the
sparse SMA target series in Python and runs `mininautilus backtest` on it. Pairs
run in parallel worker processes. Results are deterministic per pair.

  python3 examples/heatmap.py --bars 2629440 --fast 5 50 --slow 60 250 --grid 20
  python3 examples/heatmap.py --single 20 60            # B1 single run timing

See docs/acceptance.md (B1, B2). Synthetic data only unless --input is given.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import subprocess
from mininautilus.backtest import (cumulative, read_prices, run_targets, sma_targets,
                                   synthetic_bars, write_bars_binary)
from mininautilus.bridge import ROOT

MINUTES_5Y = 5 * 365 * 24 * 60 + 24 * 60
CONFIG = {"max_abs_position": 1_000, "max_order_qty": 1_000, "max_order_notional": 2**62,
          "request_timeout_ms": 100, "market_stale_ms": 1_000, "private_stale_ms": 5_000,
          "max_signal_lag": 100}
_shared = {}


def _init(bars_path, config_path):
    # Each worker reads the binary bar file itself (milliseconds) and shares
    # one prefix-sum array across all of its parameter pairs.
    prices = read_prices(bars_path)
    _shared.update(bars=bars_path, config=config_path, prices=prices, prefix=cumulative(prices))


def _run(pair):
    fast, slow = pair
    start = time.perf_counter()
    changes = sma_targets(_shared["prices"], fast, slow, prefix=_shared["prefix"])
    targets_seconds = time.perf_counter() - start
    summary = run_targets(_shared["bars"], changes, config=_shared["config"])
    summary.update(fast=fast, slow=slow, python_targets_seconds=targets_seconds,
                   wall_seconds=time.perf_counter() - start)
    return summary


def grid(low, high, count):
    if count == 1:
        return [low]
    return sorted({round(low + (high - low) * i / (count - 1)) for i in range(count)})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="bar CSV at,price,volume,taker (default: synthetic)")
    parser.add_argument("--bars", type=int, default=MINUTES_5Y)
    parser.add_argument("--fast", type=int, nargs=2, default=[5, 50])
    parser.add_argument("--slow", type=int, nargs=2, default=[60, 250])
    parser.add_argument("--grid", type=int, default=20)
    parser.add_argument("--single", type=int, nargs=2, metavar=("FAST", "SLOW"))
    parser.add_argument("--workers", type=int, default=os.cpu_count())
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / "heatmap.json")
    args = parser.parse_args()
    args.output.parent.mkdir(exist_ok=True)
    config = args.output.with_suffix(".config.json")
    config.write_text(json.dumps(CONFIG))
    start = time.perf_counter()
    bars_path = args.output.with_suffix(".bars.bin")
    if args.input:
        # CSV (or binary) input is converted once to the binary format.
        subprocess.check_call([str(ROOT / "target/release/mininautilus"), "convert-bars",
                               str(args.input), str(bars_path)], stdout=subprocess.DEVNULL)
    else:
        write_bars_binary(bars_path, synthetic_bars(args.bars))
    bar_count = (bars_path.stat().st_size - 8) // 32
    prepare_seconds = time.perf_counter() - start

    if args.single:
        _init(bars_path, config)
        result = _run(tuple(args.single))
        result["prepare_seconds"] = prepare_seconds
        print(json.dumps(result, indent=2))
        print(f"B1 single run (Python targets + Rust load + simulate): {result['wall_seconds']:.2f}s; "
              f"Rust simulate {result['simulate_seconds']:.2f}s", file=sys.stderr)
        return

    pairs = [(f, s) for f in grid(*args.fast, args.grid) for s in grid(*args.slow, args.grid) if f < s]
    start = time.perf_counter()
    with ProcessPoolExecutor(args.workers, initializer=_init, initargs=(bars_path, config)) as pool:
        results = list(pool.map(_run, pairs))
    elapsed = time.perf_counter() - start
    report = {"bars": bar_count, "pairs": len(pairs), "workers": args.workers,
              "prepare_seconds": prepare_seconds, "sweep_seconds": elapsed, "results": results}
    args.output.write_text(json.dumps(report, indent=1))
    best = max(results, key=lambda r: int(r["gross_equity_tick_lots"]))
    print(json.dumps({k: v for k, v in report.items() if k != "results"}
                     | {"best": {k: best[k] for k in ("fast", "slow", "gross_equity_tick_lots", "fills")}}, indent=2))
    print(f"B2 sweep: {len(pairs)} pairs x {bar_count} bars in {elapsed:.1f}s with {args.workers} workers "
          f"(target <= 600s) -> {'PASS' if elapsed <= 600 else 'FAIL'}", file=sys.stderr)


if __name__ == "__main__":
    main()
