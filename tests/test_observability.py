"""Metrics endpoints: Rust /metrics counts what the engine did; Python metrics are optional."""
from pathlib import Path
import socket
import sys
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mininautilus import Engine, metrics

BINARY = ROOT / "target/debug/mininautilus"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scrape(port):
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=1) as r:
                return r.read().decode()
        except OSError:
            time.sleep(0.05)
    raise AssertionError("metrics endpoint did not answer")


def value(text, line_start):
    for line in text.splitlines():
        if line.startswith(line_start + " "):
            return float(line.split()[-1])
    raise AssertionError(f"missing {line_start}")


class ObservabilityTests(unittest.TestCase):
    def test_rust_metrics_count_inputs_effects_and_state(self):
        port = free_port()
        with Engine(sim=True, binary=BINARY, time_mode="historical",
                    env={"MINI_METRICS_ADDR": f"127.0.0.1:{port}"}) as engine:
            engine.send_batch(0, [{"Quote": {"bid": 99, "ask": 101}}, {"Heartbeat": {"epoch": 0}}])
            seq = engine.state["seq"]
            engine.send(1, {"Submit": {"id": 1, "side": "Buy", "qty": 2, "limit": 100,
                                       "based_on_seq": seq, "valid_until": 10_000}})
            engine.send(2, {"Trade": {"taker": "Sell", "price": 100, "qty": 1}})
            text = scrape(port)
        self.assertEqual(value(text, 'mini_events_total{kind="Quote"}'), 1)
        self.assertEqual(value(text, 'mini_events_total{kind="Submit"}'), 1)
        # The paper venue's ack and partial fill are inputs too.
        self.assertEqual(value(text, 'mini_events_total{kind="Execution"}'), 2)
        self.assertEqual(value(text, 'mini_effects_total{kind="SendOrder"}'), 1)
        self.assertEqual(value(text, "mini_requests_total"), 3)
        self.assertEqual(value(text, "mini_position_lots"), 1)
        self.assertEqual(value(text, "mini_open_orders"), 1)
        self.assertEqual(value(text, "mini_health"), 0)
        self.assertEqual(value(text, "mini_request_seconds_count"), 3)
        self.assertEqual(value(text, 'mini_request_seconds_bucket{le="+Inf"}'), 3)

    def test_python_metrics_are_noop_until_started_then_record_ipc(self):
        metrics.observe("ipc", 0.001)  # before start: silently ignored
        if metrics._prom is None:
            self.skipTest("prometheus_client not installed")
        port = free_port()
        self.assertTrue(metrics.start(port))
        with Engine(sim=True, binary=BINARY, time_mode="historical") as engine:
            for at in range(5):
                engine.send_batch(at, [{"Quote": {"bid": 99, "ask": 101}}])
        metrics.count("events", "unit_test")
        text = scrape(port)
        self.assertGreaterEqual(value(text, "mini_py_ipc_seconds_count"), 5)
        self.assertEqual(value(text, 'mini_py_events_total{kind="unit_test"}'), 1)


if __name__ == "__main__":
    unittest.main()
