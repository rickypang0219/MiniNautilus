"""B3: interactive Python backtests, the Rust target runner and paper agree."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mininautilus import Engine
from mininautilus.backtest import (SmaTarget, read_bars, run_interactive, run_targets,
                                   sma_targets, synthetic_bars, write_bars)

BINARY = ROOT / "target/debug/mininautilus"


def ledger_of(state):
    """Fills in execution order as (order_id, side, qty, price)."""
    fills = sorted(state["fills"].values(), key=lambda f: f["execution_id"])
    return [(f["order_id"], state["orders"][str(f["order_id"])]["intent"]["side"], f["qty"], f["price"])
            for f in fills]


def ledger_csv(path):
    rows = Path(path).read_text().splitlines()[1:]
    return [(int(r[2]), r[3], int(r[4]), int(r[5])) for r in (line.split(",") for line in rows)]


class BacktestTests(unittest.TestCase):
    def test_sparse_targets_equal_incremental_strategy(self):
        bars = synthetic_bars(2000, seed=3)
        for fast, slow, long_only in ((3, 10, False), (5, 20, True)):
            strategy = SmaTarget(fast, slow, long_only=long_only)
            dense, last = [], None
            for i, bar in enumerate(bars):
                target = strategy.on_bar(bar)
                if target is not None and target != last:
                    dense.append((i, target))
                    last = target
            self.assertEqual(dense, sma_targets([b.price for b in bars], fast, slow, long_only=long_only))

    def test_interactive_targets_and_paper_paths_agree(self):
        bars = synthetic_bars(1500, seed=5)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_bars(d / "bars.csv", bars)
            self.assertEqual(read_bars(d / "bars.csv"), bars)
            for fast, slow, offset in ((5, 20, 0), (3, 12, 15)):
                with Engine(sim=True, binary=BINARY, time_mode="historical") as engine:
                    interactive = run_interactive(engine, bars, SmaTarget(fast, slow), limit_offset=offset)
                summary = run_targets(d / "bars.csv", sma_targets([b.price for b in bars], fast, slow),
                                      ledger=d / "fills.csv", binary=BINARY, limit_offset=offset)
                self.assertGreater(summary["fills"], 10)
                self.assertEqual(summary["health"], "Healthy")
                self.assertEqual(summary["events"], interactive["seq"])
                self.assertEqual(summary["orders"], len(interactive["orders"]))
                self.assertEqual(summary["position"], interactive["position"])
                self.assertEqual(int(summary["cash_tick_lots"]), interactive["cash"])
                self.assertEqual(ledger_csv(d / "fills.csv"), ledger_of(interactive))
            # Durable paper path (journal + fsync) gives the same state on a prefix.
            prefix = bars[:300]
            with Engine(d / "paper.jsonl", paper=True, binary=BINARY, time_mode="historical") as engine:
                paper = run_interactive(engine, prefix, SmaTarget(5, 20))
            with Engine(sim=True, binary=BINARY, time_mode="historical") as engine:
                sim = run_interactive(engine, prefix, SmaTarget(5, 20))
            self.assertEqual(paper, sim)
            replayed = json.loads(subprocess.check_output([str(BINARY), "inspect", str(d / "paper.jsonl")]))
            self.assertEqual(replayed, paper)

    def test_bad_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "bars.csv").write_text("at,price,volume,taker\n0,100,1,Buy\n0,101,1,Sell\n")
            (d / "t.csv").write_text("bar,position\n")
            result = subprocess.run([str(BINARY), "backtest", str(d / "bars.csv"), str(d / "t.csv")],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("strictly increase", result.stderr)


if __name__ == "__main__":
    unittest.main()
