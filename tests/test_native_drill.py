import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location("native_drill_worker",Path(__file__).resolve().parents[1]/"ops/aws/native_drill_worker.py")
worker=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
CONTROLLER_SPEC=importlib.util.spec_from_file_location("native_host_drill",Path(__file__).resolve().parents[1]/"ops/aws/native_host_drill.py")
controller=importlib.util.module_from_spec(CONTROLLER_SPEC)
CONTROLLER_SPEC.loader.exec_module(controller)


class NativeDrillTest(unittest.TestCase):
    def test_later_trading_invalidates_flat_proof_and_retains_state_on_failed_cleanup(self):
        resources=dict(trading_started=True,flat_verified=True,potential_exposure=False)
        self.assertTrue(controller.state_volume_safe(resources))
        controller.begin_signed_phase(resources)
        # A later lifecycle failed, and cleanup also failed. The earlier host
        # recovery's flat result cannot authorize deletion of its original EBS.
        resources["cleanup_failure"]="original session unresolved"
        self.assertFalse(controller.state_volume_safe(resources))
        resources["flat_verified"]=True
        self.assertFalse(controller.state_volume_safe(resources))
        resources["potential_exposure"]=False
        self.assertTrue(controller.state_volume_safe(resources))
        self.assertTrue(controller.state_volume_safe(dict(trading_started=False)))

    def test_verified_diagnostic_is_not_reopened_after_later_independent_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=worker.STATE;worker.STATE=root
            try:
                self.assertFalse(worker.completed_diagnostic())
                report=dict(status="PASS",exit=0,replayed=dict(position=0),after=dict(positions=[],open_orders=[]))
                (root/"recover-result.json").write_text(json.dumps(report));self.assertTrue(worker.completed_diagnostic())
                report["status"]="FAIL";(root/"recover-result.json").write_text(json.dumps(report));self.assertFalse(worker.completed_diagnostic())
            finally:worker.STATE=original
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
