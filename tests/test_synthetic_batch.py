import importlib.util
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mininautilus.bridge import Engine
from mininautilus.backtest import Bar, SmaTarget, plan
spec = importlib.util.spec_from_file_location("synthetic_session", ROOT / "ops/synthetic_session.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class SyntheticBatchTests(unittest.TestCase):
    def test_scalar_and_batch_preserve_effects_state_and_fills(self):
        rng = random.Random(7)
        price = 100
        strategy = SmaTarget(3, 8, lots=2)
        with Engine(sim=True, time_mode="historical") as scalar, Engine(sim=True, time_mode="historical") as batch:
            for n in range(500):
                at = n*20
                price, events = driver.market_update(rng, price, at, scalar.state["epoch"])
                effects = []
                for event in events:
                    effects.extend(scalar.send(at, event))
                self.assertEqual(batch.send_batch(at, events), effects)
                self.assertEqual(scalar.state, batch.state)
                bar = Bar(at, price, 1, "Buy")
                commands = plan(scalar.state, bar, strategy.on_bar(bar), order_ttl_ms=20)
                if commands:
                    self.assertEqual(scalar.send_batch(at, commands), batch.send_batch(at, commands))
                self.assertEqual(scalar.state, batch.state)
            self.assertGreater(len(batch.state["fills"]), 50)
            self.assertEqual(batch.state["health"], "Healthy")
