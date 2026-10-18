import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from fractions import Fraction as F
ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('closed_loop',ROOT/'ops/closed_loop_correspondence.py')
audit=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(audit)


class ClosedLoopComparisonTest(unittest.TestCase):
    def test_missing_zero_opening_cash_is_the_documented_serde_default(self):
        state=dict(position=0,cash=12)
        audit.verify_core_cash(state,dict(position=F(0),realized=F('0.000012')),F('0.000001'),F('0.000001'))
        state['opening_cash']=10
        audit.verify_core_cash(state,dict(position=F(0),realized=F('0.000002')),F('0.000001'),F('0.000001'))
        with self.assertRaises(ValueError):
            audit.verify_core_cash(state,dict(position=F(0),realized=F('0.000005')),F('0.000001'),F('0.000001'))

    def test_unmatched_actual_and_simulated_groups_remain_in_denominator(self):
        rows=[dict(kind='strategy',request=dict(revision=1,bar=dict(open_ms=60000)))]
        state=dict(orders={'1':dict(intent=dict(id=1,side='Buy'))},fills={'1':dict(order_id=1,qty=20,price=100)})
        simulated=dict(orders=[dict(intent=dict(id=7,side='Sell'),operational=False,signal_open_ms=120000)],
                       fills=[dict(order_id=7,side='Sell',qty=10,price=100)])
        with tempfile.TemporaryDirectory() as temporary:
            journal=Path(temporary)/'journal'
            event=dict(Input=dict(event=dict(SubmitTargeted=dict(intent=dict(id=1,side='Buy'),revision=1))))
            journal.write_text(json.dumps(dict(payload=json.dumps(event)))+'\n')
            result=audit.compare_groups(simulated,state,rows,journal)
        self.assertEqual(result['absolute_lot_error'],30)
        self.assertEqual(result['actual_lots'],20)
        self.assertEqual(len(result['groups']),2)
        self.assertIsNone(result['max_vwap_error_bps'])
