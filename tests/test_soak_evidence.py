import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))
from soak_evidence import enrich
spec = importlib.util.spec_from_file_location("soak_report", ROOT / "ops/report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class SoakEvidenceTests(unittest.TestCase):
    def test_short_window_is_no_data_without_invalid_prometheus_query(self):
        class NoQuery:
            def series(self, *args):
                raise AssertionError("end would precede query start")
        result = report.drift(NoQuery(), "mini_request_seconds", 100, 125)
        self.assertIsNone(result["last_over_first"])
        self.assertEqual(result["p99_ms_per_slice"], [])

    def test_exact_driver_counts_and_final_position_survive_stale_scrape(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "session.log").write_text('metrics log\n' + json.dumps({
                "updates": 100, "market_ipcs": 500, "overruns": 2, "position": 3}) + '\n')
            result = enrich({"rust_state":{"final_position":None}}, d)
            self.assertEqual(result["driver"]["market_ipcs_per_update"], 5)
            self.assertEqual(result["driver"]["overruns_per_1000_updates"], 20)
            self.assertEqual(result["rust_state"]["final_position"], 3)
