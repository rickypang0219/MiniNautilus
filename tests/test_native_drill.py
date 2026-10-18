import importlib.util
import copy
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
    def test_recovery_proof_accepts_split_fills_but_rejects_missing_changed_and_foreign_facts(self):
        session={"session":"0123456789abcdef","instrument":{"symbol":"BTC","sz_decimals":5}}
        orders={str(i):dict(intent=dict(id=i,side=side,qty=20),filled=20,lifecycle="Filled",pending=None,uncertain=False)
                for i,side in [(1,"Buy"),(2,"Sell")]}
        fills={str(tid+1):dict(execution_id=tid+1,order_id=oid,qty=qty,price=800000)
               for tid,oid,qty in [(11,1,20),(22,2,6),(33,2,14)]}
        state=dict(position=0,orders=orders,fills=fills)
        raw=[dict(tid=tid,oid=oid+100,cloid="0x"+session["session"]+f"{oid:016x}",coin="BTC",
                  side="B" if oid==1 else "A",sz=size,px="80000")
             for tid,oid,size in [(11,1,"0.0002"),(22,2,"0.00006"),(33,2,"0.00014")]]
        self.assertEqual(worker.recovered_fill_proof(state,session,raw+raw)["unique_fills"],3)
        # A first order can be partly filled then canceled before flattening.
        partial=copy.deepcopy(state);partial["orders"]["1"].update(filled=6,lifecycle="Canceled")
        partial["orders"]["2"]["intent"]["qty"]=6;partial["orders"]["2"]["filled"]=6
        partial["fills"].pop("34");partial["fills"]["12"]["qty"]=6
        partial_raw=copy.deepcopy(raw[:2]);partial_raw[0]["sz"]="0.00006"
        self.assertEqual(worker.recovered_fill_proof(partial,session,partial_raw)["unique_fills"],2)
        for field,value in [("sz","0.00007"),("px","80001"),("cloid","foreign"),("side","B"),("coin","ETH"),("oid",101)]:
            wrong=copy.deepcopy(raw);wrong[1][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):worker.recovered_fill_proof(state,session,wrong)
        for wrong in [raw[:2],raw+[dict(raw[0],sz="0.0001")],raw+[dict(raw[0],tid=44)]]:
            with self.assertRaises(ValueError):worker.recovered_fill_proof(state,session,wrong)
        for field,value in [("position",1),("fills",{}),("orders",dict(orders,extra=orders["1"]))]:
            wrong=copy.deepcopy(state);wrong[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):worker.recovered_fill_proof(wrong,session,raw)
        wrong=copy.deepcopy(state);wrong["orders"]["2"]["filled"]=19
        with self.assertRaises(ValueError):worker.recovered_fill_proof(wrong,session,raw)
        for field,value in [("uncertain",True),("pending","Cancel"),("lifecycle","Accepted")]:
            wrong=copy.deepcopy(state);wrong["orders"]["1"][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):worker.recovered_fill_proof(wrong,session,raw)

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

    def test_rotated_tree_proof_requires_every_original_file_and_append_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'segment-000001').mkdir()
            (root/'current.json').write_text('pointer')
            (root/'journal-000000.jsonl').write_text('closed\n')
            (root/'journal-000001.jsonl').write_text('genesis\n')
            (root/'segment-000001/actions.jsonl').write_text('intent\n')
            inventory=worker.durable_tree(root)
            worker.verify_tree(root,inventory,False)
            with (root/'journal-000001.jsonl').open('a') as f:f.write('recovered\n')
            with self.assertRaises(ValueError):worker.verify_tree(root,inventory,False)
            worker.verify_tree(root,inventory,True)
            (root/'journal-000000.jsonl').write_text('changed\n')
            with self.assertRaises(ValueError):worker.verify_tree(root,inventory,True)

    def test_generic_rotated_proof_accepts_nonzero_id_floor_and_terminal_unfilled_orders(self):
        session=dict(session='0123456789abcdef',instrument=dict(symbol='BTC',asset=0,sz_decimals=5))
        orders={str(i):dict(intent=dict(id=i,side=side,qty=20),filled=n,lifecycle=lc,pending=None,uncertain=False)
                for i,side,n,lc in [(5,'Buy',20,'Filled'),(6,'Sell',0,'Canceled'),(7,'Sell',20,'Filled')]}
        fills={str(t+1):dict(execution_id=t+1,order_id=i,qty=20,price=800000) for t,i in [(101,5),(102,7)]}
        state=dict(position=0,orders=orders,fills=fills)
        raw=[dict(tid=t,oid=i+100,cloid='0x'+session['session']+f'{i:016x}',coin='BTC',side=side,sz='0.0002',px='80000')
             for t,i,side in [(101,5,'B'),(102,7,'A')]]
        self.assertEqual(worker.session_fill_proof(state,session,raw)['unique_fills'],2)
        for key,value in [('cloid','foreign'),('px','80001'),('sz','0.0001')]:
            bad=copy.deepcopy(raw);bad[0][key]=value
            with self.assertRaises(ValueError):worker.session_fill_proof(state,session,bad)
        with self.assertRaises(ValueError):worker.session_fill_proof(state,session,raw[:1])
