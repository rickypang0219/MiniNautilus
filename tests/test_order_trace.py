"""Per-action timing must correlate to the actual request and journal sequence."""
import json
from pathlib import Path
import tempfile
import unittest
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mininautilus import Engine

class OrderTraceTests(unittest.TestCase):
    def test_order_trace_correlates_sequence_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            with Engine(d / "events.jsonl", time_mode="historical",
                        env={"MINI_ORDER_TRACE": str(d / "trace.jsonl")}) as engine:
                engine.send(0, {"Quote": {"bid": 99, "ask": 101}})
                engine.send(1, {"Submit": {"id": 1, "side": "Buy", "qty": 2,
                            "limit": 100, "based_on_seq": engine.state["seq"], "valid_until": 1000}})
            rows = [json.loads(x) for x in (d / "trace.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row["request_id"], row["seq"], row["order_id"]), (2, 2, 1))
            self.assertEqual(row["effect"], "SendOrder")
            self.assertGreater(row["python_roundtrip_ns"], 0)
            self.assertGreater(row["stages"]["written_bytes"], 0)
            self.assertGreaterEqual(row["stages"]["sync_pending_bytes"], row["stages"]["written_bytes"])
            self.assertTrue(row["stages"]["synced"])
            self.assertFalse(row["venue_delivery_measured"])
