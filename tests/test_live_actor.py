"""G2 offline fault tests: the opt-in actor runner against a blocking fake adapter.

Real Rust engine (serve mode, durable journal); no network. Covers bounded
buffering, slow and late venue results, disconnects, poll failure and
reconciliation with proof of absence.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))
from mininautilus.audit import IncrementalAudit
from mininautilus.bridge import Engine
from mininautilus.live_actor import ActorSession, FailClosed
from mininautilus.offline import PRICES, FakeLiveAdapter as FakeAdapter
from mininautilus.sma import SmaStrategy

def run_session(adapter, seconds=3.0, **options):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        config = path/'config.json'
        config.write_text(json.dumps(dict(max_abs_position=1, max_order_qty=1, max_order_notional=10**9,
                                          request_timeout_ms=30000, market_stale_ms=3000,
                                          private_stale_ms=30000, max_signal_lag=10)))
        notes = []
        with Engine(path/'events.jsonl', config=config) as engine:
            origin, initial = time.monotonic(), engine.state['now']
            def now():
                return initial + int((time.monotonic()-origin)*1000)
            strategy = SmaStrategy(2, 3, 1, '1s', long_only=True)
            audit = IncrementalAudit(1, long_only=True)
            audit.check(engine.state, None)
            session = ActorSession(engine, adapter, strategy, identity=('fake', 'dedicated', 'TEST'),
                                   now=now, note=lambda kind, **data: notes.append(dict(kind=kind, **data)),
                                   audit=audit, max_orders=50, poll_seconds=0.1, lots=1,
                                   reconcile_retry=lambda attempt: None, **options)
            error = None
            try:
                asyncio.run(session.run(seconds))
            except Exception as failure:
                error = failure
            try:
                reconciled = session.close(reconcile=True)
            except Exception:
                reconciled = False
            final = dict(position=engine.state['position'], health=engine.state['health'],
                         orders=dict(engine.state['orders']), fills=len(engine.state['fills']),
                         journal=Path(engine.journal).name, id_floor=engine.state.get('id_floor', 0),
                         files=sorted(p.name for p in path.iterdir()))
        return session, final, notes, error, reconciled


class LiveActorFaultTests(unittest.TestCase):
    def test_trades_end_to_end_and_reconciles_with_venue(self):
        adapter = FakeAdapter(submit_delay=0.2)
        session, final, notes, error, reconciled = run_session(adapter)
        self.assertIsNone(error)
        self.assertTrue(reconciled)
        report = session.report()
        self.assertGreater(report['submissions'], 0)
        self.assertGreater(final['fills'], 0, 'zero fills cannot pass the fill path')
        self.assertEqual(final['position'], adapter.position)
        self.assertEqual(len(adapter.submits), len(set(adapter.submits)))
        # Market inputs keep being applied while a 200 ms venue call is outstanding.
        self.assertGreater(report['market_during_execution'], 0)
        self.assertLessEqual(report['queue_high_water']['input'], 128)

    def test_full_market_buffer_drops_into_resync_not_unbounded_growth(self):
        adapter = FakeAdapter(pace=0.001, timing_delay=0.004, prices=PRICES*4)
        session, final, notes, error, reconciled = run_session(
            adapter, seconds=2.5, coordinator_options=dict(capacity=40, reserve=32))
        self.assertIsNone(error)
        report = session.report()
        self.assertGreater(report['market_rejected'], 0)
        self.assertLessEqual(report['queue_high_water']['input'], 40)
        self.assertGreater(adapter.streams, 1, 'a dropped candle must resynchronise from REST')
        self.assertEqual(final['health'], 'Disconnected')  # close() always ends with Disconnect
        self.assertEqual(final['position'], adapter.position)

    def test_late_sdk_result_is_never_retried_and_is_reconciled(self):
        release = threading.Event()
        adapter = FakeAdapter(submit_block=release)
        threading.Timer(1.2, release.set).start()
        session, final, notes, error, reconciled = run_session(adapter, seconds=3.0, execution_timeout=0.3)
        release.set()
        self.assertIsNone(error)
        report = session.report()
        self.assertGreaterEqual(report['gates'].get('delivery_uncertain', 0), 1)
        self.assertGreaterEqual(report['late_results'], 1)
        self.assertGreaterEqual(report['reconciliations'], 1)
        self.assertEqual(len(adapter.submits), len(set(adapter.submits)))
        self.assertEqual(final['position'], adapter.position)
        self.assertTrue(reconciled)

    def test_unresolved_venue_call_fails_closed_without_reconciling(self):
        never = threading.Event()
        adapter = FakeAdapter(submit_block=never)
        session, final, notes, error, reconciled = run_session(
            adapter, seconds=3.0, execution_timeout=0.2, quiescence_seconds=0.4)
        never.set()
        self.assertIsInstance(error, FailClosed)
        self.assertFalse(reconciled)
        self.assertEqual(adapter.reconciles, 0)

    def test_market_disconnect_reconnects_and_warms_up_again(self):
        adapter = FakeAdapter(disconnect_after=5)
        session, final, notes, error, reconciled = run_session(adapter)
        self.assertIsNone(error)
        self.assertGreaterEqual(session.report()['reconnects'], 1)
        self.assertGreaterEqual(adapter.streams, 2)
        self.assertGreaterEqual(sum(n['kind'] == 'warmup' for n in notes), 2)
        self.assertEqual(final['position'], adapter.position)

    def test_private_poll_failure_latches_reconciles_and_restarts(self):
        adapter = FakeAdapter(poll_fail_once_after=5)
        session, final, notes, error, reconciled = run_session(adapter)
        self.assertIsNone(error)
        report = session.report()
        self.assertEqual(report['gates'].get('private_poll_failed'), 1)
        self.assertGreaterEqual(report['coordinators'], 2)
        self.assertEqual(final['position'], adapter.position)

    def test_send_lost_before_venue_is_proven_absent_never_resubmitted(self):
        adapter = FakeAdapter(submit_lost_before_send=True)
        session, final, notes, error, reconciled = run_session(adapter)
        self.assertIsNone(error)
        first = adapter.submits[0]
        self.assertEqual(adapter.submits.count(first), 1)
        self.assertEqual(final['orders'][str(first)]['lifecycle'], 'Rejected')
        self.assertGreaterEqual(session.report()['gates'].get('delivery_uncertain', 0), 1)
        self.assertEqual(final['position'], adapter.position)

    def test_recovery_drill_reconciles_and_trading_continues(self):
        adapter = FakeAdapter(prices=PRICES*2)
        session, final, notes, error, reconciled = run_session(adapter, seconds=4.0, drills=[1.0])
        self.assertIsNone(error)
        report = session.report()
        self.assertEqual(report['drills'], 1)
        self.assertEqual(report['gates'].get('recovery_drill'), 1)
        self.assertGreaterEqual(report['reconciliations'], 2)  # drill + shutdown
        self.assertGreaterEqual(report['coordinators'], 2)
        self.assertEqual(len(adapter.submits), len(set(adapter.submits)))
        self.assertEqual(final['position'], adapter.position)


class LiveRetentionTests(unittest.TestCase):
    def test_rotation_bounds_hot_state_and_dedups_late_retired_fills(self):
        adapter = FakeAdapter(prices=PRICES*3, replay_retired=True)
        session, final, notes, error, reconciled = run_session(adapter, seconds=6.0, rotate_orders=3)
        self.assertIsNone(error)
        report = session.report()
        self.assertGreaterEqual(report['rotations'], 2)
        # Soft trigger N blocks new risk: hot orders never exceed N + one open order.
        self.assertLessEqual(report['hot_max']['core_orders'], 4)
        self.assertLessEqual(report['hot_max']['audit_orders'], 4)
        self.assertGreater(adapter.replayed, 0)
        self.assertEqual(set(report['gates']), {'rotation'}, 'late duplicates must not gate')
        self.assertEqual(final['position'], adapter.position)
        self.assertGreaterEqual(final['id_floor'], 3)
        self.assertTrue(final['journal'].startswith('events-'))
        self.assertTrue(any(name.endswith('.cold') for name in final['files']))
        self.assertTrue(any(name.startswith('rotation-events-') for name in final['files']))
        self.assertTrue(reconciled)

    def test_resume_locates_the_newest_segment(self):
        from mininautilus.bridge import current_journal
        adapter = FakeAdapter(prices=PRICES*2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with Engine(path/'events.jsonl') as engine:
                engine.send(engine.state['now'], 'Disconnect')
                engine.send(engine.state['now'], 'Reconnect')
                engine.send(engine.state['now'], {'Reconcile': adapter.reconcile(engine.state)})
                engine.rotate(path/'events-1.jsonl')
            self.assertEqual(current_journal(path/'events.jsonl'), (path/'events-1.jsonl').resolve())
            with Engine(current_journal(path/'events.jsonl'), recover=True) as engine:
                self.assertEqual(engine.state['health'], 'Disconnected')

    def test_open_order_past_the_delay_is_reconciled_then_rotated(self):
        adapter = FakeAdapter(auto_fill=False)
        session, final, notes, error, reconciled = run_session(
            adapter, seconds=3.0, rotate_orders=1, max_rotation_delay=0.4, order_ttl_ms=10**7)
        self.assertIsNone(error)
        self.assertGreaterEqual(session.report()['rotations'], 1)
        self.assertTrue(any(n['kind'] == 'rotation_pending' for n in notes))
        self.assertEqual(final['position'], adapter.position)

    def test_rotation_that_cannot_reach_a_barrier_stops_without_rotating(self):
        adapter = FakeAdapter(reconcile_fail_from=1)
        session, final, notes, error, reconciled = run_session(adapter, seconds=3.0, rotate_orders=2)
        self.assertIsNotNone(error)
        self.assertEqual(session.report()['rotations'], 0)
        self.assertEqual(final['journal'], 'events.jsonl')
        self.assertFalse(any(name.endswith('.cold') for name in final['files']))
        self.assertFalse(reconciled)


if __name__ == '__main__':
    unittest.main()
