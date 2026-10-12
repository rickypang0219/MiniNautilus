"""Offline serial reference and controllable venue; never opens a network socket."""
import asyncio
from collections import deque
from contextlib import contextmanager
import threading
from dataclasses import dataclass
import json
import time

from .sma import Candle


@dataclass(frozen=True)
class Input:
    source: str
    lane: str
    payload: object
    at: int = 0
    source_seq: int | None = None
    epoch: int = 0
    event_time_ms: int | None = None
    received_ns: int = 0
    scheduled_ns: int = 0
    venue: str = 'offline'
    account: str = 'dedicated'
    instrument: str = 'TEST'
    schema_version: int = 1
    correlation_id: str = ''

    @property
    def size(self):
        return len(json.dumps(self.__dict__, separators=(',', ':')).encode())


class FakeVenue:
    """Async execution with explicit barriers and durable-session reconciliation.

    Accepted+filled is an integration fixture, not a realistic matching model.
    Futures/events can hold execution indefinitely without creating extra tasks.
    """
    def __init__(self, *, delay=0, mode='immediate', barrier=None):
        self.delay, self.mode, self.barrier = delay, mode, barrier
        self.started = asyncio.Event()
        self.orders, self.fills, self.position = {}, {}, 0
        self.proven_absent = {}
        self.execution_id = 0
        self.calls = 0

    async def execute(self, effect):
        self.calls += 1
        self.started.set()
        if self.barrier is not None:
            await self.barrier.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == 'never':
            await asyncio.Future()
        if 'SendOrder' in effect:
            intent = effect['SendOrder']
            oid = intent['id']
            if oid in self.orders or oid in self.proven_absent:
                raise RuntimeError('fake venue detected a blind submission retry')
            if self.mode == 'reject':
                self.orders[oid] = dict(intent=intent, filled=0, lifecycle='Rejected')
                return [{'Rejected': {'id': oid}}]
            self.orders[oid] = dict(intent=intent, filled=intent['qty'], lifecycle='Filled')
            self.execution_id += 1
            fill = dict(execution_id=self.execution_id, order_id=oid, qty=intent['qty'], price=intent['limit'])
            self.fills[self.execution_id] = fill
            self.position += intent['qty'] * (1 if intent['side'] == 'Buy' else -1)
            reports = [{'Accepted': {'id': oid}}, {'Fill': fill}]
            if self.mode == 'lost_ack':
                raise TimeoutError('accepted by fake venue; delivery outcome is ambiguous')
            if self.mode == 'duplicate':
                reports.append({'Fill': fill})
            if self.mode == 'reordered':
                reports.reverse()
            return reports
        if 'SendCancel' in effect:
            oid = effect['SendCancel']['id']
            order = self.orders.get(oid)
            if order is None:
                raise TimeoutError('unknown fake order; cancel outcome is ambiguous')
            return [{'Canceled': dict(id=oid, cumulative_filled=order['filled'])}]
        return []

    def snapshot(self, epoch, watermark, local_orders):
        # This offline venue guarantees requests cannot arrive after cancellation
        # of execute(). It can therefore prove absence; real adapters must supply
        # their own expiry/watermark contract instead of reusing this rule.
        history=list(self.orders.values())
        absent=[]
        for order in local_orders.values():
            oid=order['intent']['id']
            if oid in self.orders: continue
            if order['lifecycle']=='Pending' and not order['filled']:
                absent.append(oid)
                self.proven_absent[oid]=dict(order['intent'])
            elif order['lifecycle']=='Rejected' and oid in self.proven_absent:
                history.append(dict(intent=self.proven_absent[oid],filled=0,lifecycle='Rejected'))
            # Missing acknowledged/filled history intentionally remains incomplete.
        return dict(epoch=epoch,watermark=watermark,position=self.position,
                    orders=history,fills=list(self.fills.values()),absent=absent)


class SerialRuntime:
    """Reference: I/O is awaited in the single coordinator, creating head-of-line delay."""
    def __init__(self, apply, execute, *, capacity=128, max_bytes=1 << 20, max_message=65536,
                 observer=None, **_unused):
        self.apply, self.execute, self.observer = apply, execute, observer
        self.capacity, self.max_bytes, self.max_message = capacity, max_bytes, max_message
        self.queue, self.bytes = deque(), 0
        self.wake = asyncio.Event()
        self.counts = dict(offered=0, admitted=0, completed=0, rejected=0, coalesced=0, pending=0)
        self.high_water = 0
        self.closed = False
        self.finishing = False
        self.failure = None
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self._run())
        return self

    def offer(self, item):
        self.counts['offered'] += 1
        if self.closed or item.size > self.max_message or len(self.queue) >= self.capacity or self.bytes + item.size > self.max_bytes:
            self.counts['rejected'] += 1
            return False
        admitted=time.perf_counter_ns()
        if not item.received_ns:
            from dataclasses import replace
            item=replace(item,received_ns=admitted,scheduled_ns=item.scheduled_ns or admitted)
        self.queue.append((item,admitted))
        self.bytes += item.size
        self.counts['admitted'] += 1
        self.counts['pending'] += 1
        self.high_water = max(self.high_water, len(self.queue))
        if self.observer:self.observer('admission',item=item,admitted_ns=admitted)
        self.wake.set()
        return True

    async def _run(self):
        try:
            while True:
                if not self.queue:
                    self.wake.clear()
                    if self.finishing:
                        self.closed = True
                        return
                    await self.wake.wait()
                    continue
                item,admitted = self.queue.popleft()
                self.bytes -= item.size
                began = time.perf_counter_ns()
                effects = await self.apply(item)
                if self.observer:
                    self.observer('input', item=item, admitted_ns=admitted, started_ns=began, finished_ns=time.perf_counter_ns())
                self.counts['pending'] -= 1
                self.counts['completed'] += 1
                for effect in effects:
                    await self.execute(effect)
        except Exception as error:
            self.failure = type(error).__name__
            self.closed = True
            raise

    async def finish(self):
        self.finishing = True
        self.wake.set()
        await self.task


# Rising then falling closes: long, then flat again.
PRICES = [100, 100, 100, 101, 102, 103, 104, 105, 104, 103, 102, 101, 100, 99, 98, 97] * 3


class FakeLiveAdapter:
    """Blocking live-actor adapter (live_actor.ActorSession contract) with a trivial
    venue: resting orders fill on the next poll. Offline only; never opens a socket."""

    def __init__(self, *, interval_ms=1000, pace=0.03, submit_delay=0.0, submit_block=None,
                 submit_lost_before_send=False, disconnect_after=None, poll_fail_once_after=None,
                 timing_delay=0.0, prices=PRICES, auto_fill=True, replay_retired=False,
                 reconcile_fail_from=None, forget_retired=False, candles=None, record_submits=True):
        self.interval_ms, self.pace, self.prices = interval_ms, pace, prices
        self.submit_delay, self.submit_block = submit_delay, submit_block
        self.submit_lost_before_send = submit_lost_before_send
        self.disconnect_after, self.poll_fail_once_after = disconnect_after, poll_fail_once_after
        self.timing_delay = timing_delay
        self.auto_fill, self.replay_retired = auto_fill, replay_retired
        self.reconcile_fail_from = reconcile_fail_from
        self.retired, self.rotations, self.replayed = set(), 0, 0
        self.forget_retired = forget_retired  # long probes: keep fake venue memory flat
        self.limit = len(prices) - 1 if candles is None else candles
        self.record_submits = record_submits  # tests assert on IDs; long probes stay flat
        # One-shot faults armed by soaks: next submit late, next stream drop, next poll error.
        self.delay_next_submit = 0.0
        self.disconnect_next = False
        self.fail_next_poll = False
        self.resubmissions = 0
        self.lock = threading.Lock()
        self.orders, self.fills, self.submits, self.position = {}, {}, [], 0
        self.fills_by_order = {}  # poll is O(open orders), not O(venue history)
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
            while self.cursor < self.limit:
                if disconnect_at is not None and self.cursor >= disconnect_at:
                    raise OSError('fake stream closed')
                if self.disconnect_next:
                    self.disconnect_next = False
                    raise OSError('fake stream dropped')
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
        if self.record_submits:
            self.submits.append(intent['id'])
        if self.submit_lost_before_send:
            self.submit_lost_before_send = False
            raise TimeoutError('request never left the client')
        if self.submit_block is not None:
            self.submit_block.wait()
        if self.submit_delay:
            time.sleep(self.submit_delay)
        if self.delay_next_submit:
            delay, self.delay_next_submit = self.delay_next_submit, 0.0
            time.sleep(delay)
        with self.lock:
            if intent['id'] in self.orders:
                self.resubmissions += 1
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
        self.fills_by_order.setdefault(intent['id'], []).append(fill)
        order['filled'], order['lifecycle'] = intent['qty'], 'Filled'
        self.position += intent['qty'] * (1 if intent['side'] == 'Buy' else -1)

    def poll(self, snapshot):
        self.polls += 1
        if self.poll_fail_once_after is not None and self.polls == self.poll_fail_once_after:
            raise OSError('fake private endpoint failure')
        if self.fail_next_poll:
            self.fail_next_poll = False
            raise OSError('fake private endpoint failure')
        reports = []
        with self.lock:
            for oid, local in snapshot['orders'].items():
                order = self.orders.get(int(oid))
                if order is None:
                    continue
                if order['lifecycle'] == 'Accepted' and self.auto_fill:
                    self._fill(order)
                for fill in self.fills_by_order.get(int(oid), ()):
                    if str(fill['execution_id']) not in snapshot['fills']:
                        reports.append({'Fill': fill})
                if order['lifecycle'] == 'Canceled':
                    reports.append({'Canceled': {'id': int(oid), 'cumulative_filled': order['filled']}})
            if self.replay_retired and self.retired:
                # A late duplicate of a retired fill, as a venue could resend it.
                fill = next(f for f in self.fills.values() if f['order_id'] in self.retired)
                reports.append({'Fill': dict(fill)})
                self.replayed += 1
        return reports

    def rotation_state(self):
        return {'segment': self.rotations + 1}

    def rotated(self, venue_state):
        self.rotations += 1
        if self.forget_retired:
            with self.lock:
                self.orders = {k: v for k, v in self.orders.items() if v['lifecycle'] == 'Accepted'}
                self.fills, self.fills_by_order = {}, {}
            return
        self.retired |= set(self.orders)

    def heartbeat(self):
        pass

    def reconcile(self, state):
        self.reconciles += 1
        if self.reconcile_fail_from is not None and self.reconciles >= self.reconcile_fail_from:
            raise OSError('fake venue cannot establish a barrier')
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
            local = {int(oid) for oid in state['orders']}
            fills = [f for oid in sorted(local) for f in self.fills_by_order.get(oid, ())]
            position = sum(f['qty'] * (1 if self.orders[f['order_id']]['intent']['side'] == 'Buy' else -1)
                           for f in fills)  # relative to the segment's opening position
            return dict(epoch=state['epoch'], watermark=state['venue_seq'], orders=orders,
                        fills=fills, position=position, absent=sorted(absent))
