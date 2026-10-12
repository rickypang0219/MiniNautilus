"""Opt-in actor runner for a live (Testnet) or offline closed-candle SMA session.

The serial runner in examples/sma_spot.py stays the default. This runner puts
the existing bounded Coordinator between adapter producers and the Rust writer:

- market: one producer thread per coordinator owns the stream, warm-up,
  backfill and the per-candle quote; it offers bounded inputs and never blocks
  the writer on network I/O;
- private: one poll task (one worker thread, at most one poll in flight) offers
  venue reports; one BlockingExecutor sends orders/cancels and offers Accepted;
- timer: Tick once per second (TTL cancels scan open orders only).

A latch (overflow, uncertain delivery, gap, poll failure, Core QueryState)
stops the producers, waits until the blocking venue call has returned (a late
SDK result is discarded, never retried), reconciles through the writer and
starts a new coordinator. If the venue call never returns within
`quiescence_seconds` the session stops fail-closed without reconciling.

Adapter contract (all methods blocking, called from worker threads):
  stream() -> context manager yielding (Candle, received_ns), or None at least
    once per second while idle; raises on loss
  warmup(count) -> closed candles; backfill(last, candle) -> exact bridge
  quote() -> {'bid','ask'}; exchange_now_ms(); free_base_lots() -> int | None
  validate(intent); submit(intent); cancel(intent)
  poll(snapshot) -> reports; heartbeat(); reconcile(state) -> Reconcile payload
  timing(event) -> engine time metadata
"""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import threading
import time

from . import metrics
from .offline import Input
from .retry import ReconnectBudget
from .runtime import BlockingExecutor, Coordinator
from .sma import Candle, CandleGap
from .targets import target_event, target_order

TERMINAL = ('Filled', 'Canceled', 'Rejected')
MARKET_ERRORS = (OSError, TimeoutError, CandleGap, RuntimeError, ValueError)


class FailClosed(RuntimeError):
    """The session cannot prove quiescence; stop without reconciling or resubmitting."""


class ActorSession:
    def __init__(self, engine, adapter, strategy, *, identity, now, note, audit=None,
                 order_ttl_ms=10_000, max_orders=6, paper=False, lots=1,
                 execution_timeout=10.0, quiescence_seconds=60.0, poll_seconds=1.0,
                 market_errors=MARKET_ERRORS, reconcile_attempts=3, reconcile_retry=None,
                 coordinator_options=None, drills=(), rotate_orders=None, max_rotation_delay=60.0,
                 heartbeat_seconds=5.0):
        self.engine, self.adapter, self.strategy = engine, adapter, strategy
        self.identity, self.now, self.note, self.audit = tuple(identity), now, note, audit
        self.order_ttl_ms, self.max_orders, self.paper, self.lots = order_ttl_ms, max_orders, paper, lots
        self.execution_timeout, self.quiescence_seconds = execution_timeout, quiescence_seconds
        self.poll_seconds, self.market_errors = poll_seconds, market_errors
        self.heartbeat_seconds = heartbeat_seconds
        self.reconcile_attempts = reconcile_attempts
        self.coordinator_options = dict(coordinator_options or {})
        # Operator recovery drills (seconds after start): latch, reconcile, restart.
        self.drills = sorted(drills)
        # Live retention (docs/adr-live-rotation.md): soft trigger N, hard limit 2N.
        if rotate_orders is not None and (rotate_orders < 1 or paper):
            raise ValueError('live rotation needs a positive order count and a live venue')
        self.rotate_orders, self.max_rotation_delay = rotate_orders, max_rotation_delay
        self.rotation_pending_since = None
        self.rotation_requested = False
        self.hot_max = dict(core_orders=0, core_fills=0, audit_orders=0, sent_at=0)
        self.reconcile_retry = reconcile_retry or (lambda attempt: time.sleep(2 ** attempt))
        self.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='core-writer')
        self.poller = ThreadPoolExecutor(max_workers=1, thread_name_prefix='private-poll')
        self.blocking = BlockingExecutor(self._venue_call)
        self.coordinator = None
        self.sent_at = {}
        self.fills_by_order = {}
        self.resync = threading.Event()
        self.fatal = None
        self.fail_closed = False
        self.executing_since = None
        self.counters = dict(closed_bars=0, signals=0, submissions=0, cancels=0, reconnects=0,
                             reconciliations=0, coordinators=0, backfilled_bars=0, duplicates=0,
                             market_rejected=0, market_inputs=0, market_during_execution=0,
                             executions=0, late_results=0, invariant_checks=0, gates={},
                             rotations=0, rotation_blocked_signals=0, fills_applied=0)
        self.high_water = dict(input=0, effects=0)
        self.tick_to_submit_ms = deque(maxlen=10000)  # bounded: last 10k orders

    # ---- writer thread (owns Engine, strategy and audit) -------------------------------
    def _send(self, event, **timing):
        effects = self.engine.send(self.now(), event, **(self.adapter.timing(event) | timing))
        if self.audit is not None:
            self.audit.check(self.engine.state, self.engine.last_change)
            self.counters['invariant_checks'] += 1
        change = self.engine.last_change or {}
        for key, fill in change.get('fills', {}).items():
            self.fills_by_order.setdefault(str(fill['order_id']), set()).add(key)
            self.counters['fills_applied'] += 1
        return effects

    def _add(self, payload):
        candle = Candle(**payload)
        changed = self.strategy.add(candle)
        if changed:
            self.note('candle', **payload, signal=self.strategy.signal())
        return changed

    def _apply(self, item):
        state = self.engine.state
        lane, payload = item.lane, item.payload
        if lane == 'private':
            return self._send({'Execution': dict(epoch=state['epoch'], venue_seq=state['venue_seq'] + 1,
                                                 report=payload)})
        if payload == 'Tick':
            effects = self._send('Tick')
            for oid, order in list(self.engine.state.open_orders.items()):
                if (order['lifecycle'] not in TERMINAL and order['pending'] is None
                        and self.now() - self.sent_at.get(int(oid), 0) >= self.order_ttl_ms):
                    effects += self._send({'Cancel': {'id': int(oid)}})
            return effects
        if payload in ('Heartbeat',):
            return self._send({'Heartbeat': {'epoch': state['epoch']}})
        if payload in ('MarketUnavailable', 'Disconnect'):
            return self._send(payload)
        if 'Warmup' in payload:
            self.strategy.reset()
            for bar in payload['Warmup']:
                self._add(bar)
            self.note('warmup', ready=self.strategy.ready)
            return []
        return self._market(item, payload['Market'])

    def _market(self, item, market):
        effects = []
        try:
            changed = False
            for bar in market['backfill']:
                changed |= self._add(bar)
            if market['backfill']:
                self.counters['backfilled_bars'] += len(market['backfill'])
            changed |= self._add(market['candle'])
        except CandleGap:
            self.resync.set()
            return self._send('MarketUnavailable')
        if not changed:
            self.counters['duplicates'] += 1
            return effects
        self.counters['closed_bars'] += 1
        if self.coordinator is not None and self.coordinator.gated:
            return effects  # never create new intent behind a latch
        if self.rotation_pending_since is not None:
            self.counters['rotation_blocked_signals'] += 1
            return effects  # retention trigger reached: no new risk until rotated
        signal = self.strategy.signal()
        if signal:
            self.counters['signals'] += 1
            self.note('signal', **signal)
            effects += self._send(target_event(self.engine.state, signal['target_lots'],
                                               self.strategy.interval_ms + 5000),
                                  event_time_ms=signal['bar_close_ms'], time_source='candle close')
        quote = market['quote']
        effects += self._send({'QuoteObserved': dict(**quote, observed_at=self.now())})
        if self.paper:
            for taker, price in (('Sell', quote['ask']), ('Buy', quote['bid'])):
                effects += self._send({'Trade': dict(taker=taker, price=price, qty=self.lots)})
        state = self.engine.state
        intent = self.strategy.intent(state, market['exchange_now_ms'])
        if intent and len(state['orders']) < self.max_orders:
            self.adapter.validate(intent)
            if intent['side'] == 'Sell' and intent['qty'] > state['position']:
                raise RuntimeError('Spot cannot short')
            free = market.get('free_base_lots')
            if not self.paper and intent['side'] == 'Sell' and free is not None and free < intent['qty']:
                self.note('signal_blocked', reason='insufficient_free_base_including_fees')
                return effects
            command = target_order(state, price=intent['limit'])
            if command:
                new = self._send(command)
                for effect in new:
                    if 'SendOrder' in effect:
                        effect['_received_ns'] = item.received_ns
                effects += new
        return effects

    def _snapshot(self):
        """Acknowledged open orders and their known fills, for the poll thread."""
        orders = {oid: copy.deepcopy(order) for oid, order in self.engine.state.open_orders.items()
                  if order['lifecycle'] != 'Pending' or order['uncertain']}
        for oid in [oid for oid in self.fills_by_order if oid not in self.engine.state.open_orders]:
            del self.fills_by_order[oid]
        fills = {key: True for oid in orders for key in self.fills_by_order.get(oid, ())}
        return dict(orders=orders, fills=fills)

    def _guard(self, effect):
        if 'SendCancel' in effect:
            order = self.engine.state['orders'][str(effect['SendCancel']['id'])]
            if order['lifecycle'] in TERMINAL and not order['uncertain']:
                return 'terminal_cancel', None
            return 'send', copy.deepcopy(order['intent'])
        return 'send', None

    def _reconcile(self):
        """Writer thread, only while no producer runs and no venue call is outstanding."""
        for attempt in range(self.reconcile_attempts):
            self._send('Disconnect')
            self._send('Reconnect')
            try:
                snapshot = self.adapter.reconcile(self.engine.state)
                self._send({'Reconcile': snapshot})
                if self.engine.state['health'] != 'Healthy':
                    raise RuntimeError('Rust rejected reconciliation')
                if self.audit is not None:
                    self.audit.reset(self.engine.state)
                self.counters['reconciliations'] += 1
                self.note('reconciled', position_lots=self.engine.state['position'])
                return
            except Exception as error:  # adapter errors are typed; never print transport text
                self.note('reconciliation_failed', attempt=attempt + 1, error_type=type(error).__name__)
                if attempt == self.reconcile_attempts - 1:
                    raise
                self.reconcile_retry(attempt)

    # ---- venue worker thread -----------------------------------------------------------
    def _venue_call(self, request):
        kind, intent = request
        return self.adapter.submit(intent) if kind == 'submit' else self.adapter.cancel(intent)

    # ---- event loop --------------------------------------------------------------------
    async def _on_writer(self, function, *args):
        return await self.loop.run_in_executor(self.writer, function, *args)

    async def _apply_async(self, item):
        return await self._on_writer(self._apply, item)

    async def _guard_async(self, effect):
        decision, intent = await self._on_writer(self._guard, effect)
        if intent is not None:
            effect['_intent'] = intent
        return decision

    async def _execute(self, effect):
        received = effect.pop('_received_ns', None)
        cancel_intent = effect.pop('_intent', None)
        self.executing_since = time.perf_counter_ns()
        self.counters['executions'] += 1
        try:
            if 'SendOrder' in effect:
                intent = effect['SendOrder']
                self.counters['submissions'] += 1
                self.sent_at[intent['id']] = self.now()
                self.note('order', intent=intent)
                if received is not None:
                    elapsed = (time.perf_counter_ns() - received) / 1e9
                    self.tick_to_submit_ms.append(elapsed * 1000)
                    metrics.observe('tick_to_trade', elapsed)
                if not self.paper:
                    await self.blocking(('submit', intent))
                    self.coordinator.offer(Input('private-exec', 'private', {'Accepted': {'id': intent['id']}},
                                                 source_seq=self._next_seq('private-exec'),
                                                 **self._identity()))
            elif 'SendCancel' in effect and not self.paper:
                self.counters['cancels'] += 1
                await self.blocking(('cancel', cancel_intent))
        finally:
            self.executing_since = None

    def _observe(self, kind, **record):
        if kind == 'input' and record['item'].lane == 'market':
            self.counters['market_inputs'] += 1
            if self.executing_since is not None and record['started_ns'] >= self.executing_since:
                self.counters['market_during_execution'] += 1
        elif kind == 'gate':
            gates = self.counters['gates']
            gates[record['reason']] = gates.get(record['reason'], 0) + 1

    def _identity(self):
        venue, account, instrument = self.identity
        return dict(venue=venue, account=account, instrument=instrument)

    def _next_seq(self, source):
        self.sequences[source] = self.sequences.get(source, 0) + 1
        return self.sequences[source]

    def _offer_threadsafe(self, item, stop):
        async def offer():
            return self.coordinator.offer(item)
        future = asyncio.run_coroutine_threadsafe(offer(), self.loop)
        while not stop.is_set():
            try:
                return future.result(timeout=0.5)
            except TimeoutError:
                continue
        future.cancel()
        return False

    def _market_worker(self, stop):
        budget = ReconnectBudget()
        sequence = 0

        def offer(payload, lane='market', received_ns=0):
            nonlocal sequence
            item = Input('market', lane, payload, source_seq=sequence + 1, received_ns=received_ns,
                         scheduled_ns=received_ns, **self._identity())
            accepted = self._offer_threadsafe(item, stop)
            if accepted:
                sequence += 1
            return accepted

        while not stop.is_set():
            try:
                with self.adapter.stream() as stream:
                    self.resync.clear()
                    bars = self.adapter.warmup(self.strategy.slow + 1)
                    if not offer({'Warmup': [asdict(b) for b in bars]}, received_ns=time.perf_counter_ns()):
                        raise CandleGap('warm-up refused by a full queue')
                    last = bars[-1] if bars else None
                    budget.connected(time.monotonic())
                    for event in stream:
                        if stop.is_set():
                            return
                        if event is None:  # idle tick: lets the producer observe stop
                            continue
                        candle, received_ns = event
                        if self.resync.is_set():
                            raise CandleGap('writer requested resynchronisation')
                        budget.progress(time.monotonic())
                        if last is not None and candle.open_ms <= last.open_ms:
                            self.counters['duplicates'] += 1
                            continue
                        backfill = []
                        if last is not None and candle.open_ms > last.open_ms + self.strategy.interval_ms:
                            backfill = self.adapter.backfill(last, candle)
                        market = dict(candle=asdict(candle), backfill=[asdict(b) for b in backfill],
                                      quote=self.adapter.quote(), exchange_now_ms=self.adapter.exchange_now_ms(),
                                      free_base_lots=None if self.paper else self.adapter.free_base_lots())
                        if offer({'Market': market}, received_ns=received_ns):
                            last = candle
                        elif stop.is_set():
                            return  # the coordinator is closing; not a dropped candle
                        else:
                            # A dropped candle is a gap: resynchronise from REST, never guess.
                            self.counters['market_rejected'] += 1
                            raise CandleGap('market input refused by a full queue')
            except self.market_errors as error:
                if stop.is_set():
                    return
                self.counters['reconnects'] += 1
                self.note('market_failure', error_type=type(error).__name__)
                self._offer_threadsafe(Input('market-control', 'control', 'MarketUnavailable',
                                             source_seq=None, **self._identity()), stop)
                try:
                    delay = budget.failed()
                except RuntimeError:
                    self.fatal = 'market_reconnect_budget_exhausted'
                    return
                stop.wait(delay)

    async def _timer(self):
        while True:
            await asyncio.sleep(1)
            self.coordinator.offer(Input('clock', 'timer', 'Tick', **self._identity()))

    async def _poll(self):
        while True:
            await asyncio.sleep(self.poll_seconds)
            if self.paper:
                self.coordinator.offer(Input('heartbeat', 'timer', 'Heartbeat', **self._identity()))
                continue
            snapshot = await self._on_writer(self._snapshot)
            # A Heartbeat claims private connectivity only after a private request succeeded.
            proven = bool(snapshot['orders'])
            try:
                reports = await self.loop.run_in_executor(self.poller, self.adapter.poll, snapshot)
                if time.monotonic() - self.last_heartbeat >= self.heartbeat_seconds:
                    await self.loop.run_in_executor(self.poller, self.adapter.heartbeat)
                    self.last_heartbeat, proven = time.monotonic(), True
            except Exception:
                self.coordinator.gate('private_poll_failed')
                return
            for report in reports:
                self.coordinator.offer(Input('private-poll', 'private', report,
                                             source_seq=self._next_seq('private-poll'), **self._identity()))
            if proven:
                self.coordinator.offer(Input('heartbeat', 'timer', 'Heartbeat', **self._identity()))

    async def _quiesce(self):
        """Wait for an outstanding blocking venue call; its late result is discarded."""
        if not self.blocking.busy:
            return
        self.counters['late_results'] += 1
        deadline = time.monotonic() + self.quiescence_seconds
        while self.blocking.busy:
            if time.monotonic() >= deadline:
                raise FailClosed('venue call unresolved; reconcile manually before restarting')
            await asyncio.sleep(0.05)

    async def _segment(self, deadline):
        self.sequences = {}
        self.last_heartbeat = float('-inf')
        self.coordinator = coordinator = Coordinator(
            self._apply_async, self._execute, observer=self._observe, identity=self.identity,
            execution_timeout=self.execution_timeout, clock_ms=self.now,
            dispatch_guard=self._guard_async, **self.coordinator_options).start()
        self.counters['coordinators'] += 1
        stop = threading.Event()
        market = threading.Thread(target=self._market_worker, args=(stop,), name='market-producer', daemon=True)
        market.start()
        tasks = [asyncio.create_task(self._timer()), asyncio.create_task(self._poll())]
        try:
            while not coordinator.gated and not coordinator.closed and self.fatal is None:
                if time.monotonic() >= deadline:
                    break
                if self._retention_due(coordinator):
                    break
                if self.drills and time.monotonic() - self.started >= self.drills[0]:
                    self.drills.pop(0)
                    self.counters['drills'] = self.counters.get('drills', 0) + 1
                    coordinator.gate('recovery_drill')
                    break
                await asyncio.sleep(0.05)
        finally:
            writer_failure = coordinator.failure  # before abort() cancels the writer task
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(market.join)
            self.high_water['input'] = max(self.high_water['input'], coordinator.high_water)
            self.high_water['effects'] = max(self.high_water['effects'], coordinator.effect_high_water)
            if coordinator.gated or self.fatal is not None:
                await coordinator.abort()
            else:
                try:
                    await asyncio.wait_for(coordinator.finish(), timeout=self.execution_timeout + 5)
                except Exception:
                    await coordinator.abort()
            await self._quiesce()
        if self.fatal is not None:
            raise FailClosed(self.fatal)
        if writer_failure is not None:
            # A writer exception (invariant, validation, Core) is not a venue fault: stop.
            raise RuntimeError(f'writer failed: {writer_failure}')
        if coordinator.gated:
            self.note('coordinator_gated', reason=coordinator.gate_reason)
            return True
        return False

    def _hot(self):
        state = self.engine.state
        sizes = dict(core_orders=len(state['orders']), core_fills=len(state['fills']),
                     audit_orders=len(self.audit.orders) if self.audit is not None else 0,
                     sent_at=len(self.sent_at))
        for key, value in sizes.items():
            self.hot_max[key] = max(self.hot_max[key], value)
        return sizes

    def _retention_due(self, coordinator):
        """Soft trigger blocks new risk; rotate once nothing is open or after the delay."""
        if self.rotate_orders is None:
            return False
        orders = self._hot()['core_orders']
        if self.rotation_pending_since is None:
            if orders < self.rotate_orders:
                return False
            self.rotation_pending_since = time.monotonic()
            self.note('rotation_pending', hot_orders=orders)
        quiet = not self.engine.state.open_orders
        overdue = time.monotonic() - self.rotation_pending_since >= self.max_rotation_delay
        if quiet or overdue or orders >= 2 * self.rotate_orders:
            self.rotation_requested = True
            coordinator.gate('rotation')
            return True
        return False

    def _successor(self):
        current = Path(self.engine.journal)
        match = re.fullmatch(r'(.*?)(?:-(\d+))?', current.stem)
        number = int(match.group(2) or 0) + 1
        return current.with_name(f'{match.group(1)}-{number}{current.suffix}')

    def _rotate(self):
        """Writer thread, after the rotation reconciliation (ADR §1-§2)."""
        successor = self._successor()
        venue_state = self.adapter.rotation_state() if hasattr(self.adapter, 'rotation_state') else {}
        sidecar = successor.with_name(f'rotation-{successor.name}.json')
        with sidecar.open('x') as handle:
            json.dump(dict(successor=successor.name, venue=venue_state,
                           position=self.engine.state['position']), handle)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(sidecar.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        retired = self.engine.state.get('id_floor', 0), len(self.engine.state['orders'])
        self.engine.rotate(successor)
        if self.audit is not None:
            self.audit.reset(self.engine.state)
        self.sent_at.clear()
        self.fills_by_order.clear()
        if hasattr(self.adapter, 'rotated'):
            self.adapter.rotated(venue_state)
        self.counters['rotations'] += 1
        self.rotation_pending_since, self.rotation_requested = None, False
        self.note('rotated', journal=successor.name, id_floor=self.engine.state.get('id_floor', 0),
                  previous_floor=retired[0], retired_orders=retired[1], hot=self._hot())
        self._reconcile()  # the successor starts gated

    async def run(self, seconds):
        self.loop = asyncio.get_running_loop()
        self.started = time.monotonic()
        deadline = self.started + seconds
        try:
            while time.monotonic() < deadline:
                if await self._segment(deadline):
                    await self._on_writer(self._reconcile)
                    if self.rotation_requested:
                        try:
                            await self._on_writer(self._rotate)
                        except Exception as error:
                            # Never drop evidence or rotate without the boundary: stop (ADR §4.2).
                            self.note('rotation_failed', error_type=type(error).__name__)
                            raise RuntimeError('retention_rotation_failed') from None
        except FailClosed:
            self.fail_closed = True
            raise

    def close(self, *, reconcile):
        """After run(): no producer or venue call remains (unless fail-closed).

        Mirrors the serial runner's shutdown: MarketUnavailable, then reconcile
        (live) or cancel open orders (paper), then Disconnect. A fail-closed
        session skips reconciliation: an unresolved call could still mutate."""
        self.writer.shutdown(wait=True)
        self.poller.shutdown(wait=True)
        reconciled = False
        try:
            self._send('MarketUnavailable')
            if self.fail_closed:
                return False
            if reconcile:
                self._reconcile()
            else:
                for oid in list(self.engine.state.open_orders):
                    self._send({'Cancel': {'id': int(oid)}})
            reconciled = True
        finally:
            self._send('Disconnect')
        return reconciled

    def report(self):
        ordered = sorted(self.tick_to_submit_ms)
        def pct(q):
            return round(ordered[int((len(ordered) - 1) * q)], 3) if ordered else None
        hot_now = self._hot()
        return dict(self.counters, queue_high_water=self.high_water, hot_max=self.hot_max,
                    hot_now=hot_now,
                    tick_to_submit_ms=dict(samples=len(ordered), p50=pct(.5), p99=pct(.99),
                                           retention='last 10000 orders'))

