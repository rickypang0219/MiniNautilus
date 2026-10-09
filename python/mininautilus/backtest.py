"""Backtests with Python strategies over the Rust Core and paper venue.

Two equivalent paths (tests/test_backtest.py proves they agree fill for fill):

* `run_interactive`: the strategy sees Rust state after every bar through
  `mininautilus sim` (no journal). One IPC round trip per bar: the previous bar's
  decisions travel in the same request as the next bar's market events. Use it
  when decisions depend on fills, orders or position.
* `run_targets`: the strategy's target series is computed first (it must depend
  only on market data), then `mininautilus backtest` runs every bar in-process.
  This is the fast path for parameter sweeps.

Both use the same execution policy (`plan`, mirrored in src/backtest.rs): set a
changed target, cancel every open order (one-bar time in force), submit the
remaining delta at `price +/- limit_offset`. Prices are integer ticks, sizes lots.
"""
from collections import namedtuple
import json
from pathlib import Path
import random
import subprocess
import tempfile

from .bridge import ROOT, next_order_id, open_orders

Bar = namedtuple("Bar", "at price volume taker")
U64_MAX = 2**64 - 1
HEADER = "at,price,volume,taker"


def read_bars(path):
    with open(path) as f:
        if f.readline().strip() != HEADER:
            raise ValueError(f"bars header must be {HEADER}")
        return [Bar(int(a), int(p), int(v), t.strip()) for a, p, v, t in
                (line.split(",") for line in f if line.strip())]


def write_bars(path, bars):
    with open(path, "w") as f:
        f.write(HEADER + "\n")
        f.writelines(f"{b.at},{b.price},{b.volume},{b.taker}\n" for b in bars)


def synthetic_bars(count, seed=1, start_price=100_000, interval_ms=60_000):
    """Deterministic random walk; aggressor side alternates at random."""
    rng = random.Random(seed)
    price, bars = start_price, []
    for i in range(count):
        price = max(1, price + rng.randint(-20, 20))
        bars.append(Bar(i * interval_ms, price, rng.randint(1, 50), rng.choice(("Buy", "Sell"))))
    return bars


def market_events(bar, epoch):
    return [{"Quote": {"bid": bar.price, "ask": bar.price}},
            {"Trade": {"taker": bar.taker, "price": bar.price, "qty": bar.volume}},
            {"Heartbeat": {"epoch": epoch}},
            "Tick"]


def plan(state, bar, target, *, limit_offset=0, order_ttl_ms=60_000):
    """Mirror of `backtest::plan`; computed from the state before any decision."""
    if target is None:
        return []
    events = []
    current = state.get("target")
    if current is not None and current["position"] == target:
        revision = current["revision"]
    else:
        revision = state["last_target_revision"] + 1
        events.append({"SetTarget": {"revision": revision, "position": target, "valid_until": U64_MAX}})
    for order in sorted(open_orders(state), key=lambda o: o["intent"]["id"]):
        if order["pending"] != "Cancel":
            events.append({"Cancel": {"id": order["intent"]["id"]}})
    delta = target - state["position"]
    if delta:
        side = "Buy" if delta > 0 else "Sell"
        sign = 1 if delta > 0 else -1
        events.append({"SubmitTargeted": {
            "intent": {"id": next_order_id(state), "side": side, "qty": abs(delta),
                       "limit": max(1, bar.price + sign * limit_offset),
                       "based_on_seq": state["seq"], "valid_until": bar.at + order_ttl_ms},
            "revision": revision, "expected_position": state["position"]}})
    return events


def run_interactive(engine, bars, strategy, **policy):
    """`strategy.on_bar(bar, state)` returns a target position or None."""
    pending = []
    for bar in bars:
        market = [(bar.at, e) for e in market_events(bar, engine.state["epoch"])]
        engine.send_pairs(pending + market)
        pending = [(bar.at, e) for e in plan(engine.state, bar, strategy.on_bar(bar, engine.state), **policy)]
    if pending:
        engine.send_pairs(pending)
    return engine.state


class SmaTarget:
    """Bar-close SMA crossover (same rule as `sma.SmaStrategy`, no candle checks)."""

    def __init__(self, fast, slow, lots=1, long_only=False):
        if not 1 <= fast < slow:
            raise ValueError("require 1 <= fast < slow")
        self.fast, self.slow, self.lots, self.long_only = fast, slow, lots, long_only
        self.prices, self.target = [], 0
        self.fast_sum = self.slow_sum = 0

    def on_bar(self, bar, _state=None):
        prices = self.prices
        prices.append(bar.price)
        n = len(prices)
        self.fast_sum += bar.price - (prices[n - 1 - self.fast] if n > self.fast else 0)
        self.slow_sum += bar.price - (prices[n - 1 - self.slow] if n > self.slow else 0)
        if n < self.slow:
            return None
        difference = self.fast_sum * self.slow - self.slow_sum * self.fast
        if difference:
            self.target = self.lots if difference > 0 else (0 if self.long_only else -self.lots)
        return self.target


def sma_targets(prices, fast, slow, lots=1, long_only=False, prefix=None):
    """Sparse `(bar, position)` changes of `SmaTarget` without per-bar objects.

    `prefix` (cumulative sums with a leading 0) can be shared across a sweep."""
    if prefix is None:
        prefix = [0]
        for p in prices:
            prefix.append(prefix[-1] + p)
    changes, target, last = [], 0, None
    low = 0 if long_only else -lots
    for i in range(slow - 1, len(prices)):
        difference = (prefix[i + 1] - prefix[i + 1 - fast]) * slow - (prefix[i + 1] - prefix[i + 1 - slow]) * fast
        if difference:
            target = lots if difference > 0 else low
        if target != last:
            changes.append((i, target))
            last = target
    return changes


def write_targets(path, changes):
    with open(path, "w") as f:
        f.write("bar,position\n")
        f.writelines(f"{bar},{position}\n" for bar, position in changes)


def run_targets(bars_path, changes, *, config=None, ledger=None, binary=None,
                limit_offset=0, order_ttl_ms=60_000):
    """Run `mininautilus backtest`; returns its JSON summary."""
    binary = Path(binary) if binary else ROOT / "target/release/mininautilus"
    with tempfile.TemporaryDirectory() as directory:
        targets = Path(directory) / "targets.csv"
        write_targets(targets, changes)
        command = [str(binary), "backtest", str(bars_path), str(targets)]
        if config:
            command.append(str(config))
        if ledger:
            command += ["--ledger", str(ledger)]
        command += ["--limit-offset", str(limit_offset), "--order-ttl-ms", str(order_ttl_ms)]
        return json.loads(subprocess.check_output(command))
