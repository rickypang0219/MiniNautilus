"""Incremental ledger checks must agree with a full oracle after each delta."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.bridge import Engine
from mininautilus.audit import IncrementalAudit, full_audit

class IncrementalAuditTests(unittest.TestCase):
    def test_delta_fill_cancel_duplicate_and_full_replacement(self):
        with Engine(sim=True,time_mode='historical') as engine:
            audit=IncrementalAudit(10,long_only=True)
            def send(at,event):
                effects=engine.send(at,event)
                audit.check(engine.state,engine.last_change)
                full_audit(engine.state,10,long_only=True)
                return effects
            audit.check(engine.state,None)
            send(0,{'Quote':{'bid':99,'ask':101}})
            intent=dict(id=1,side='Buy',qty=2,limit=100,based_on_seq=engine.state['seq'],valid_until=1000)
            send(1,{'Submit':intent})
            send(2,{'Trade':{'taker':'Sell','price':100,'qty':1}})
            send(3,{'Cancel':{'id':1}})
            self.assertEqual(audit.position,1)
            # A duplicate delta must not double-count fill totals.
            fill=next(iter(engine.state['fills'].values()))
            delta=dict(header={},orders={},fills={str(fill['execution_id']):fill})
            audit.check(engine.state,delta)
            self.assertEqual(audit.position,1)
            before=audit.full_scans
            audit.check(copy.deepcopy(dict(engine.state)),None)
            self.assertEqual(audit.full_scans,before+1)

    def test_corruption_is_not_hidden_by_incremental_accounting(self):
        with Engine(sim=True,time_mode='historical') as engine:
            audit=IncrementalAudit(10)
            audit.check(engine.state,None)
            engine.state['position']=1
            with self.assertRaises(RuntimeError): audit.check(engine.state,dict(header={},orders={},fills={}))

    def test_no_history_scan_on_empty_delta(self):
        class NoScan(dict):
            def items(self): raise AssertionError('hot path scanned history')
            def values(self): raise AssertionError('hot path scanned history')
        with Engine(sim=True,time_mode='historical') as engine:
            audit=IncrementalAudit(10)
            audit.check(engine.state,None)
            engine.state['orders']=NoScan(engine.state['orders'])
            engine.state['fills']=NoScan(engine.state['fills'])
            audit.check(engine.state,dict(header={},orders={},fills={}))
            self.assertEqual(audit.entries_checked,0)

    def test_seeded_protocol_workload_matches_full_oracle_after_every_request(self):
        from test_protocol import drive
        for seed in range(4):
            with Engine(sim=True,time_mode='historical') as engine:
                audit=IncrementalAudit(10)
                audit.check(engine.state,None)
                original=engine._receive
                def receive(response):
                    effects=original(response)
                    audit.check(engine.state,engine.last_change)
                    full_audit(engine.state,10)
                    return effects
                engine._receive=receive
                drive(engine,seed,batch=True)
                self.assertEqual(audit.full_scans,1)

    def test_conflicting_duplicate_delta_fails_closed(self):
        with Engine(sim=True,time_mode='historical') as engine:
            audit=IncrementalAudit(10)
            engine.send(0,{'Quote':{'bid':100,'ask':100}})
            engine.send(1,{'Submit':dict(id=1,side='Buy',qty=1,limit=100,based_on_seq=engine.state['seq'],valid_until=1000)})
            engine.send(2,{'Trade':dict(taker='Sell',price=100,qty=1)})
            audit.check(engine.state,None)
            fill=dict(next(iter(engine.state['fills'].values())));fill['price']+=1
            with self.assertRaises(RuntimeError):
                audit.check(engine.state,dict(header={},orders={},fills={str(fill['execution_id']):fill}))
