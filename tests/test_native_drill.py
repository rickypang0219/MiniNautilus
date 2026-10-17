import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location("native_drill_worker",Path(__file__).resolve().parents[1]/"ops/aws/native_drill_worker.py")
worker=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class NativeDrillTest(unittest.TestCase):
    def test_only_completed_flat_sessions_are_excluded_from_later_session_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertFalse(worker.completed_root(root))
            report=dict(position=0,segments=[dict(cleanup_verified=True,failed=False)])
            pointer=dict(payload=dict(phase="stopped"))
            (root/"lifecycle-report.json").write_text(json.dumps(report))
            (root/"current.json").write_text(json.dumps(pointer))
            self.assertTrue(worker.completed_root(root))
            for bad in [dict(position=20,segments=report["segments"]),dict(position=0,segments=[]),
                        dict(position=0,segments=[dict(cleanup_verified=False,failed=True)])]:
                (root/"lifecycle-report.json").write_text(json.dumps(bad))
                self.assertFalse(worker.completed_root(root))
