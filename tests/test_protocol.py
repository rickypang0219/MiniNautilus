"""Protocol 2 parity: compact deltas, full-state responses and sim mode agree."""
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mininautilus import Engine
from mininautilus.bridge import next_order_id, open_orders
from mininautilus.targets import target_event, target_order

BINARY = ROOT / "target/debug/mininautilus"


def workload(seed, steps=150):
    """Market steps plus strategy decisions that depend on the observed state."""
    rng = random.Random(seed)
    for step in range(steps):
        price = 100 + rng.randint(-2, 2)
        yield step * 1000, price, rng.choice(["Buy", "Sell"]), rng.randint(1, 3), rng.randint(-3, 3), rng.random()


def drive(engine, seed, batch=False):
    states, effects = [], []
    for at, price, taker, volume, target, roll in workload(seed):
        market = [{"Quote": {"bid": price, "ask": price}}, {"Trade": {"taker": taker, "price": price, "qty": volume}}]
        if batch:
            effects.append(engine.send_batch(at, market))
        else:
            effects.append([e for event in market for e in engine.send(at, event)])
        state = engine.state
        if roll < 0.2:
            for order in list(open_orders(state)):
                effects.append(engine.send(at, {"Cancel": {"id": order["intent"]["id"]}}))
        elif roll < 0.6:
            effects.append(engine.send(at, target_event(engine.state, target)))
            command = target_order(engine.state)
            if command:
                effects.append(engine.send(at, command))
        states.append(json.loads(json.dumps(engine.state)))
    return states, effects


class ProtocolTests(unittest.TestCase):
    def test_compact_full_and_sim_modes_are_identical(self):
        for seed in range(4):
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                with Engine(d / "compact.jsonl", paper=True, time_mode="historical") as engine:
                    compact = drive(engine, seed)
                with Engine(d / "full.jsonl", paper=True, time_mode="historical", full_state=True) as engine:
                    full = drive(engine, seed)
                with Engine(sim=True, time_mode="historical") as engine:
                    sim = drive(engine, seed, batch=True)
                self.assertEqual(compact, full)
                # Batched market requests yield identical states (effects are grouped
                # per request, so only the states are compared).
                self.assertEqual(compact[0], sim[0])
                self.assertTrue(any(s["fills"] for s in compact[0]), "workload must fill")
                journal = json.loads(subprocess.check_output([str(BINARY), "inspect", str(d / "compact.jsonl")]))
                self.assertEqual(journal, compact[0][-1])

    def test_open_order_index_and_next_id_track_history(self):
        with Engine(sim=True, time_mode="historical") as engine:
            states, _ = drive(engine, 7)
            state = engine.state
            self.assertEqual(sorted(o["intent"]["id"] for o in open_orders(state)),
                             sorted(o["intent"]["id"] for o in open_orders(dict(state))))
            self.assertEqual(next_order_id(state), next_order_id(dict(state)))
            self.assertGreater(next_order_id(state), 1)


if __name__ == "__main__":
    unittest.main()
