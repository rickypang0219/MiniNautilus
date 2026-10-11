"""Offline serial reference and controllable venue; never opens a network socket."""
import asyncio
from collections import deque
from dataclasses import dataclass
import json
import time


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
