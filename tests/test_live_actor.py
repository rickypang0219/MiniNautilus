"""G2 offline fault tests: the opt-in actor runner against a blocking fake adapter.

Real Rust engine (serve mode, durable journal); no network. Covers bounded
buffering, slow and late venue results, disconnects, poll failure and
reconciliation with proof of absence.
"""
import asyncio
from contextlib import contextmanager
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
from mininautilus.sma import Candle, SmaStrategy

# Rising then falling closes: long, then flat again.
PRICES = [100, 100, 100, 101, 102, 103, 104, 105, 104, 103, 102, 101, 100, 99, 98, 97] * 3


class FakeAdapter:
    """Blocking adapter with a trivial venue: resting orders fill on the next poll."""

    def __init__(self, *, interval_ms=1000, pace=0.03, submit_delay=0.0, submit_block=None,
                 submit_lost_before_send=False, disconnect_after=None, poll_fail_once_after=None,
                 timing_delay=0.0, prices=PRICES):
        self.interval_ms, self.pace, self.prices = interval_ms, pace, prices
        self.submit_delay, self.submit_block = submit_delay, submit_block
        self.submit_lost_before_send = submit_lost_before_send
        self.disconnect_after, self.poll_fail_once_after = disconnect_after, poll_fail_once_after
        self.timing_delay = timing_delay
        self.lock = threading.Lock()
        self.orders, self.fills, self.submits, self.position = {}, {}, [], 0
        self.next_execution, self.cursor, self.streams, self.polls, self.reconciles = 0, 0, 0, 0, 0
        self.last_close = 0

    def candle(self, index):
        open_ms = 1_000_000 + index*self.interval_ms
        return Candle(open_ms, open_ms+self.interval_ms-1, self.prices[index % len(self.prices)])

    @contextmanager
    def stream(self):
        self.streams += 1
        disconnect_at = (self.cursor+self.disconnect_after
                         if self.disconnect_after and self.streams == 1 else None)

        def candles():
            while self.cursor < len(self.prices) - 1:
                if disconnect_at is not None and self.cursor >= disconnect_at:
                    raise OSError('fake stream closed')
                time.sleep(self.pace)
                self.cursor += 1
                candle = self.candle(self.cursor)
                self.last_close = candle.close_ms
                yield candle, time.perf_counter_ns()
            while True:  # idle stream until the runner stops
                time.sleep(self.pace)
                yield None
        yield candles()

    def warmup(self, count):
        bars = [self.candle(i) for i in range(max(0, self.cursor-count+1), self.cursor+1)]
        self.last_close = bars[-1].close_ms
        return bars

    def backfill(self, last, candle):
        return [self.candle(i) for i in range((last.open_ms-1_000_000)//self.interval_ms+1,
                                              (candle.open_ms-1_000_000)//self.interval_ms)]

    def quote(self):
        price = self.prices[self.cursor % len(self.prices)]
        return {'bid': price, 'ask': price}

    def exchange_now_ms(self):
        return self.last_close + 1

    def free_base_lots(self):
        return self.position

    def validate(self, intent):
        pass

    def timing(self, event):
        if self.timing_delay:
            time.sleep(self.timing_delay)
        return {}

    def submit(self, intent):
        self.submits.append(intent['id'])
        if self.submit_lost_before_send:
            self.submit_lost_before_send = False
            raise TimeoutError('request never left the client')
        if self.submit_block is not None:
            self.submit_block.wait()
        if self.submit_delay:
            time.sleep(self.submit_delay)
        with self.lock:
            if intent['id'] in self.orders:
                raise AssertionError('blind resubmission reached the fake venue')
            self.orders[intent['id']] = dict(intent=intent, filled=0, lifecycle='Accepted')

    def cancel(self, intent):
        with self.lock:
            order = self.orders.get(intent['id'])
            if order and order['lifecycle'] == 'Accepted':
                order['lifecycle'] = 'Canceled'

    def _fill(self, order):
        intent = order['intent']
        self.next_execution += 1
        fill = dict(execution_id=self.next_execution, order_id=intent['id'], qty=intent['qty'], price=intent['limit'])
        self.fills[self.next_execution] = fill
        order['filled'], order['lifecycle'] = intent['qty'], 'Filled'
        self.position += intent['qty'] * (1 if intent['side'] == 'Buy' else -1)

    def poll(self, snapshot):
        self.polls += 1
        if self.poll_fail_once_after is not None and self.polls == self.poll_fail_once_after:
            raise OSError('fake private endpoint failure')
        reports = []
        with self.lock:
            for oid, local in snapshot['orders'].items():
                order = self.orders.get(int(oid))
                if order is None:
                    continue
                if order['lifecycle'] == 'Accepted':
                    self._fill(order)
                for fill in self.fills.values():
                    if fill['order_id'] == int(oid) and str(fill['execution_id']) not in snapshot['fills']:
                        reports.append({'Fill': fill})
                if order['lifecycle'] == 'Canceled':
                    reports.append({'Canceled': {'id': int(oid), 'cumulative_filled': order['filled']}})
        return reports

    def heartbeat(self):
        pass

    def reconcile(self, state):
        self.reconciles += 1
        with self.lock:
            for order in self.orders.values():
                if order['lifecycle'] == 'Accepted':
                    order['lifecycle'] = 'Canceled'
            orders, absent = [], []
            for oid, local in state['orders'].items():
                order = self.orders.get(int(oid))
                if order is not None:
                    orders.append(dict(intent=order['intent'], filled=order['filled'], lifecycle=order['lifecycle']))
                elif local['lifecycle'] == 'Pending' and local['filled'] == 0:
                    absent.append(int(oid))
                elif local['lifecycle'] == 'Rejected':
                    orders.append(dict(intent=local['intent'], filled=0, lifecycle='Rejected'))
            return dict(epoch=state['epoch'], watermark=state['venue_seq'], orders=orders,
                        fills=list(self.fills.values()), position=self.position, absent=sorted(absent))


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
            reconciled = session.close(reconcile=True)
            final = dict(position=engine.state['position'], health=engine.state['health'],
                         orders=dict(engine.state['orders']), fills=len(engine.state['fills']))
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


if __name__ == '__main__':
    unittest.main()
