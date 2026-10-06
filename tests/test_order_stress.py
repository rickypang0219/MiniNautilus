import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'python'),str(Path(__file__).resolve().parents[1]/'examples')]
from order_stress import check_cross_ledger
from mininautilus.targets import target_order

class CrossLedgerTests(unittest.TestCase):
    def state(self):
        intent=dict(id=1,side='Buy',qty=5,limit=100)
        order=dict(intent=intent,filled=0,lifecycle='Accepted',uncertain=False)
        return dict(position=0,orders={'1':order},fills={})
    def test_latency_gap_must_be_explained_by_unseen_fills(self):
        state=self.state()
        fill=dict(execution_id=1,order_id=1,qty=2,price=100)
        venue=SimpleNamespace(position=2,orders={1:state['orders']['1']},fills={1:fill})
        self.assertEqual(check_cross_ledger(state,venue,5)['unseen_fill_delta'],2)
        venue.position=3
        with self.assertRaises(RuntimeError):check_cross_ledger(state,venue,5)
    def test_pending_exposure_cannot_escape_spot_long_only_bounds(self):
        state=self.state();state['orders']['1']['intent']['side']='Sell'
        venue=SimpleNamespace(position=0,orders={1:state['orders']['1']},fills={})
        with self.assertRaises(RuntimeError):check_cross_ledger(state,venue,5)
    def test_phantom_fill_is_detected_even_if_positions_happen_to_match(self):
        state=self.state();state['orders']['1']['filled']=2;state['position']=2
        state['fills']['1']=dict(execution_id=1,order_id=1,qty=2,price=100)
        venue=SimpleNamespace(position=2,orders={1:state['orders']['1']},fills={})
        with self.assertRaises(RuntimeError):check_cross_ledger(state,venue,5)
    def test_target_order_uses_current_delta_and_snapshot_identity(self):
        state=dict(target=dict(revision=7,position=5,valid_until=1000),position=2,now=10,
                   health='Healthy',killed=False,quote=[99,101,10],orders={},seq=42)
        command=target_order(state)['SubmitTargeted']
        self.assertEqual(command['revision'],7)
        self.assertEqual(command['expected_position'],2)
        self.assertEqual(command['intent']['qty'],3)
        state['target']['position']=0
        command=target_order(state)['SubmitTargeted']
        self.assertEqual((command['intent']['side'],command['intent']['qty']),('Sell',2))

if __name__=='__main__':unittest.main()
