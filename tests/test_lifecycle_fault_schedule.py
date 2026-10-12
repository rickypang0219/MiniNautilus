import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ops'))
spec=importlib.util.spec_from_file_location('lc_faults',ROOT/'ops/lifecycle_fast_soak.py')
harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)


class FaultRecoveryTests(unittest.TestCase):
    def test_eligible_quote_before_actual_gate_cannot_release_next_injection(self):
        pending=dict(kind='late_result',generation=1,injected_ns=100)
        self.assertFalse(harness.fault_recovered(pending,'risk_admission',dict(enabled=True,monotonic_ns=110)))
        self.assertFalse(harness.fault_recovered(pending,'coordinator_accounting',
                         dict(gate_reason='delivery_uncertain',runner_generation=1,monotonic_ns=120)))
        self.assertFalse(harness.fault_recovered(pending,'risk_admission',dict(enabled=False,monotonic_ns=130)))
        self.assertTrue(harness.fault_recovered(pending,'risk_admission',dict(enabled=True,monotonic_ns=140)))

    def test_other_fault_or_writer_generation_does_not_prove_recovery(self):
        pending=dict(kind='sdk_held',generation=3,injected_ns=100)
        for reason,generation in [('old_source_epoch',3),('delivery_uncertain',2)]:
            self.assertFalse(harness.fault_recovered(pending,'coordinator_accounting',
                             dict(gate_reason=reason,runner_generation=generation,monotonic_ns=120)))
        self.assertNotIn('gate_observed_ns',pending)
        self.assertFalse(harness.fault_recovered(pending,'risk_admission',dict(enabled=True,monotonic_ns=140)))
