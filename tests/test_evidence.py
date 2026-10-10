import importlib.util
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("soak_compare", ROOT / "ops/compare.py")
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)

class EvidenceTests(unittest.TestCase):
    def test_five_repetitions_ranges_and_effect_are_all_required(self):
        self.assertEqual(compare.verdict([10]*4, [1]*5), "inconclusive")
        self.assertEqual(compare.verdict([10]*5, [9.5]*5), "inconclusive")
        self.assertEqual(compare.verdict([10,10,10,10,20], [9,9,9,9,11]), "inconclusive")
        self.assertEqual(compare.verdict([10]*5, [8]*5), "improved")
        self.assertEqual(compare.verdict([8]*5, [10]*5), "regressed")
        self.assertEqual(compare.verdict([10]*5, [float("nan")]*5), "inconclusive")
    def test_missing_or_mismatched_metadata_cannot_support_verdict(self):
        def row(value, machine="same"):
            return {"experiment":{"machine":machine}, "session_exit":0,
                    "rust":{"request":{"":{"p99_ms":value}}}}
        before, after = [row(10)]*5, [row(1)]*5
        self.assertEqual(compare.compare_runs(before, after)["rows"][0]["verdict"], "improved")
        after[0] = row(1, "different")
        self.assertEqual(compare.compare_runs(before, after)["rows"][0]["verdict"], "inconclusive")
        after[0] = row(1); after[0]["session_exit"] = 1
        self.assertFalse(compare.compare_runs(before, after)["compatible_experiments"])
