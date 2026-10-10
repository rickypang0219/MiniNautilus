#!/usr/bin/env python3
"""Closed-candle Python SMA -> Rust OMS -> Binance Spot TESTNET, long/flat only.

Market candles use WebSocket; quotes and private order reports use REST. This is
an observable correctness experiment, not a low-latency execution architecture.
"""
import argparse
from decimal import Decimal
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time
import uuid
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed
from mininautilus import metrics
from mininautilus.bridge import Engine
from mininautilus.binance import BinanceSpot, VenueError, units
from mininautilus.market import missing_candles
from mininautilus.retry import ReconnectBudget
from mininautilus.targets import target_event, target_order
from mininautilus.sma import Candle, CandleGap, SmaStrategy, INTERVALS

TERMINAL = ('Filled', 'Canceled', 'Rejected')


def load_env(path):
    allowed = {'BINANCE_TESTNET_API_KEY', 'BINANCE_TESTNET_API_SECRET'}
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key.strip() not in allowed:
            raise ValueError('unsupported env assignment; use Spot Testnet variable names')
        if value.strip():
            os.environ[key.strip()] = value.strip().strip('\"\'')


def closed_candle(message, tick, symbol, interval):
    if message.get('e') != 'kline' or message.get('s') != symbol:
        raise ValueError('unexpected market stream')
    bar = message['k']
    if bar['i'] != interval:
        raise ValueError('unexpected candle interval')
    if not bar['x']:
        return None
    return Candle(int(bar['t']), int(bar['T']), units(bar['c'], tick))


def audit(state, limit):
    position = 0
    for oid, order in state['orders'].items():
        filled = sum(f['qty'] for f in state['fills'].values() if f['order_id'] == int(oid))
        if filled != order['filled'] or not 0 <= filled <= order['intent']['qty']:
            raise RuntimeError('fill accounting invariant failed')
        position += filled * (1 if order['intent']['side'] == 'Buy' else -1)
    if position != state['position'] or not 0 <= position <= limit:
        raise RuntimeError('Spot position invariant failed')


def validate(venue, intent, cap):
    qty, price = venue.lot * intent['qty'], venue.tick * intent['limit']
    f = venue.filters['LOT_SIZE']
    if not Decimal(f['minQty']) <= qty <= Decimal(f['maxQty']):
        raise ValueError('quantity outside LOT_SIZE')
    minimum = max(Decimal(venue.filters.get(name, {}).get('minNotional', '0'))
                  for name in ('MIN_NOTIONAL', 'NOTIONAL'))
    if not minimum <= qty * price <= cap:
        raise ValueError('order notional outside venue minimum / experiment cap')


def latency_summary(samples):
    ordered = sorted(samples)
    if not ordered: return {'samples': 0}
    return dict(samples=len(ordered), p50=round(ordered[(len(ordered)-1)//2], 3),
                p99=round(ordered[int((len(ordered)-1)*.99)], 3), maximum=round(ordered[-1], 3))


def write_summary(path, invocation, summary):
    """Keep every invocation; atomically replace the latest convenience pointer."""
    payload = json.dumps(summary, indent=2)
    with (path / f'summary-{invocation}.json').open('x') as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    temporary = path / f'.summary-{invocation}.tmp'
    with temporary.open('x') as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path / 'summary.json')
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def run(args):
    load_env(args.env_file)
    engine_env = {}
    if args.metrics_port:
        # Python metrics on PORT, the Rust engine's on PORT-1 (docs/observability.md).
        if not metrics.start(args.metrics_port):
            raise RuntimeError('--metrics-port needs prometheus_client (pip install prometheus_client)')
        engine_env['MINI_METRICS_ADDR'] = f'127.0.0.1:{args.metrics_port - 1}'

    venue = BinanceSpot(args.symbol, execute=args.mode == 'testnet')
    venue.initialize()
    lots = units(str(args.quantity), venue.lot)
    strategy = SmaStrategy(args.fast, args.slow, lots, args.interval, long_only=True)
    path = args.run_dir
    if args.resume:
        if args.mode != 'testnet':
            raise ValueError('only Testnet sessions support resume')
        metadata = json.loads((path / 'session.json').read_text())
        expected = [args.symbol, args.mode, str(venue.tick), str(venue.lot), args.fast, args.slow, args.interval, lots, str(args.max_notional)]
        if metadata['parameters'] != expected:
            raise ValueError('resume parameters differ from original session')
    else:
        path.mkdir(parents=True, exist_ok=False)
        if args.mode == 'testnet' and venue.open_orders():
            raise VenueError('fresh run requires no open orders on symbol')
        metadata = {'session': uuid.uuid4().hex[:8], 'baseline_base': str(venue.base_balance()) if args.mode == 'testnet' else '0',
                    'parameters': [args.symbol, args.mode, str(venue.tick), str(venue.lot), args.fast, args.slow, args.interval, lots, str(args.max_notional)]}
        with (path / 'session.json').open('x') as f:
            json.dump(metadata, f); f.flush(); os.fsync(f.fileno())
        config = dict(max_abs_position=lots, max_order_qty=lots,
                      max_order_notional=int(args.max_notional / (venue.tick * venue.lot)),
                      request_timeout_ms=30000, market_stale_ms=3000, private_stale_ms=30000, max_signal_lag=10)
        with (path / 'config.json').open('x') as f:
            json.dump(config, f); f.flush(); os.fsync(f.fileno())
        fd = os.open(path, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    venue.session = metadata['session']
    counters = dict(closed_bars=0, signals=0, submissions=0, reconnects=0, invariant_checks=0, backfilled_bars=0, duplicates=0)
    invocation = uuid.uuid4().hex
    latencies = deque(maxlen=10000)
    fired_faults = set()
    retry_budget = ReconnectBudget()
    started = time.monotonic()
    with (path / 'observations.jsonl').open('a') as log, Engine(path / 'events.jsonl', paper=args.mode == 'paper',
            recover=args.resume, config=path / 'config.json', env=engine_env,
            extra_args=['--sync', args.sync]) as engine:
        origin, initial = time.monotonic(), engine.state['now']
        sent_at = {}
        def now(): return initial + int((time.monotonic() - origin) * 1000)
        def note(kind, **data):
            metrics.count('events', kind)
            record = dict(kind=kind, elapsed_ms=now(), **data)
            log.write(json.dumps(record) + '\n'); log.flush()
            print(json.dumps(record), flush=True)
        def send(event, **timing):
            before = time.monotonic()
            effects = engine.send(now(), event, **(venue.timing(event) | timing))
            latencies.append((time.monotonic() - before) * 1000)
            audit(engine.state, lots); counters['invariant_checks'] += 1
            return effects
        def deliver(report):
            effects = send({'Execution': dict(epoch=engine.state['epoch'], venue_seq=engine.state['venue_seq'] + 1, report=report)})
            if any('QueryState' in e for e in effects):
                raise VenueError('private report requires reconciliation')
        def reconcile():
            # Rediscover state on every attempt; never retry Submit itself.
            for attempt in range(3):
                send('Disconnect'); send('Reconnect')
                try:
                    snapshot = venue.reconcile(engine.state, metadata['baseline_base'])
                    send({'Reconcile': snapshot})
                    if engine.state['health'] != 'Healthy':
                        raise VenueError('Rust rejected reconciliation')
                    note('reconciled', position_lots=engine.state['position'], accounting=venue.last_reconciliation)
                    return
                except VenueError as error:
                    # VenueError messages are adapter-owned and contain no signed URLs.
                    note('reconciliation_failed', attempt=attempt+1, reason=str(error), venue_code=error.code)
                    if attempt == 2 or error.code in (-2014, -2015, -1022): raise
                    time.sleep(2 ** attempt)
        def dispatch(effects):
            for e in effects:
                if 'SendOrder' in e:
                    counters['submissions'] += 1
                    intent = e['SendOrder']; sent_at[intent['id']] = now()
                    note('order', intent=intent)
                    if args.mode == 'testnet':
                        venue.submit(intent)  # Never retry an ambiguous mutation.
                    if candle_received[0] is not None:
                        metrics.observe('tick_to_trade', time.perf_counter() - candle_received[0])
                    if args.mode == 'testnet':
                        deliver({'Accepted': {'id': intent['id']}})
                elif 'SendCancel' in e and args.mode == 'testnet':
                    venue.cancel(engine.state['orders'][str(e['SendCancel']['id'])]['intent'])
                elif 'QueryState' in e:
                    raise VenueError('engine requested recovery')
                elif 'Refused' in e or 'Alert' in e:
                    note('engine_effect', effect=e)
        candle_received = [None]
        def add_bar(candle):
            with metrics.timer('strategy'):
                changed = strategy.add(candle)
            if changed:
                note('candle', **asdict(candle), signal=strategy.signal())
            return changed
        def warmup():
            strategy.reset()
            note('strategy_reset')
            rows = venue.request('GET', '/api/v3/klines', {'symbol': args.symbol, 'interval': args.interval, 'limit': args.slow + 1})
            exchange_now = int(time.time()*1000) + venue.offset_ms
            for b in rows:
                if int(b[6]) < exchange_now:
                    add_bar(Candle(int(b[0]), int(b[6]), units(b[4], venue.tick)))
            note('warmup', ready=strategy.ready)
        completed = False
        shutdown_reconciled = False
        try:
            if args.resume: reconcile()
            while not args.reconcile_only and time.monotonic() - started < args.seconds:
                try:
                    # Connect before backfill. Queued duplicates are discarded by SMA.
                    url = f'wss://stream.testnet.binance.vision/ws/{args.symbol.lower()}@kline_{args.interval}'
                    with connect(url, open_timeout=10, close_timeout=2, max_queue=8) as ws:
                        warmup()
                        retry_budget.connected(time.monotonic())
                        last_poll = 0.0
                        last_message = time.monotonic()
                        while time.monotonic() - started < args.seconds:
                            if args.faults and time.monotonic() - started >= 60 and 'disconnect' not in fired_faults:
                                fired_faults.add('disconnect'); note('injected_fault', fault='disconnect')
                                ws.close()
                                raise OSError('injected paper disconnect')
                            dispatch(send('Tick'))
                            if time.monotonic() - last_poll >= 1:
                                if args.mode == 'testnet':
                                    for report in venue.poll(engine.state): deliver(report)
                                    # Empty order polling performs no request; verify private connectivity explicitly.
                                    venue.base_balance()
                                send({'Heartbeat': {'epoch': engine.state['epoch']}})
                                last_poll = time.monotonic()
                                for oid, order in list(engine.state['orders'].items()):
                                    if order['lifecycle'] not in TERMINAL and order['pending'] is None and now() - sent_at.get(int(oid), 0) >= args.order_ttl * 1000:
                                        dispatch(send({'Cancel': {'id': int(oid)}}))
                            loop_started = time.perf_counter()
                            try: message = json.loads(ws.recv(timeout=1))
                            except TimeoutError:
                                if time.monotonic() - last_message > 15:
                                    raise VenueError('market stream silent for 15 seconds')
                                continue
                            last_message = time.monotonic()
                            if args.faults and time.monotonic() - started >= 45 and 'stale' not in fired_faults:
                                fired_faults.add('stale'); note('injected_fault', fault='stale')
                                message['E'] -= 60000
                            exchange_now = int(time.time()*1000) + venue.offset_ms
                            metrics.observe('ws_lag', max(exchange_now - int(message['E']), 0) / 1000)
                            if not 0 <= exchange_now - int(message['E']) <= 5000:
                                raise VenueError('stale market stream; reconnect and backfill')
                            retry_budget.progress(time.monotonic())
                            candle = closed_candle(message, venue.tick, args.symbol, args.interval)
                            if candle is None:
                                metrics.observe('loop', time.perf_counter() - loop_started)
                                continue
                            candle_received[0] = time.perf_counter()
                            metrics.observe('candle_lag', max(exchange_now - candle.close_ms, 0) / 1000)
                            if args.faults and time.monotonic() - started >= 15 and 'drop' not in fired_faults:
                                fired_faults.add('drop'); note('injected_fault', fault='drop')
                                continue
                            if strategy.last and candle.open_ms > strategy.last.open_ms + strategy.interval_ms:
                                send('MarketUnavailable')
                                bars = missing_candles(venue, strategy.last, candle, args.interval, strategy.interval_ms)
                                for bar in bars: add_bar(bar)
                                note('backfill', bars=len(bars))
                                counters['backfilled_bars'] += len(bars)
                            if not add_bar(candle):
                                counters['duplicates'] += 1
                                continue
                            if args.faults and time.monotonic() - started >= 30 and 'duplicate' not in fired_faults:
                                fired_faults.add('duplicate'); note('injected_fault', fault='duplicate')
                                if add_bar(candle): raise RuntimeError('duplicate changed strategy')
                                counters['duplicates'] += 1
                            counters['closed_bars'] += 1
                            signal = strategy.signal()
                            if signal:
                                counters['signals'] += 1; note('signal', **signal)
                                send(target_event(engine.state, signal['target_lots'], strategy.interval_ms + 5000),
                                     event_time_ms=signal['bar_close_ms'], time_source='candle close')
                            observed = now()
                            quote = venue.quote()
                            send({'QuoteObserved': dict(**quote, observed_at=observed)})
                            if args.mode == 'paper':
                                for taker, price in [('Sell', quote['ask']), ('Buy', quote['bid'])]:
                                    send({'Trade': dict(taker=taker, price=price, qty=lots)})
                            intent = strategy.intent(engine.state, int(time.time()*1000) + venue.offset_ms)
                            if intent and len(engine.state['orders']) < args.max_orders:
                                validate(venue, intent, args.max_notional)
                                # Explicit Spot long/flat boundary, independent of SMA implementation.
                                if intent['side'] == 'Sell' and intent['qty'] > engine.state['position']:
                                    raise RuntimeError('Spot cannot short')
                                if args.mode == 'testnet' and intent['side'] == 'Sell' and venue.available_base() < venue.lot * intent['qty']:
                                    note('signal_blocked', reason='insufficient_free_base_including_fees')
                                    continue
                                command = target_order(engine.state, price=intent['limit'])
                                if command: dispatch(send(command))
                            metrics.observe('loop', time.perf_counter() - loop_started)
                except (OSError, TimeoutError, VenueError, ConnectionClosed, CandleGap) as error:
                    send('MarketUnavailable')
                    note('connection_failure', error_type=type(error).__name__, reason=str(error) if isinstance(error, VenueError) else None, venue_code=getattr(error, 'code', None))
                    if args.mode == 'testnet': reconcile()
                    counters['reconnects'] += 1
                    time.sleep(retry_budget.failed())
            completed = True
        finally:
            send('MarketUnavailable')
            try:
                if args.mode == 'testnet': reconcile()
                else:
                    for oid, order in list(engine.state['orders'].items()):
                        if order['lifecycle'] not in TERMINAL: send({'Cancel': {'id': int(oid)}})
                shutdown_reconciled = True
            finally:
                send('Disconnect')
                summary = dict(counters, invocation=invocation, total_session_orders=len(engine.state['orders']), faults=sorted(fired_faults),
                               ipc_ms=latency_summary(latencies), completed=completed, shutdown_reconciled=shutdown_reconciled, position_lots=engine.state['position'], fills=len(engine.state['fills']),
                               elapsed_seconds=round(time.monotonic()-started, 2), mode=args.mode)
                write_summary(path, invocation, summary)
                note('summary', **summary)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', required=True, type=Path)
    p.add_argument('--env-file', type=Path, default=Path('.env.testnet'))
    p.add_argument('--mode', choices=['paper', 'testnet'], default='paper')
    p.add_argument('--symbol', default='BTCUSDT')
    p.add_argument('--quantity', type=Decimal, default=Decimal('0.001'))
    p.add_argument('--max-notional', type=Decimal, default=Decimal('200'))
    p.add_argument('--fast', type=int, default=5)
    p.add_argument('--slow', type=int, default=20)
    p.add_argument('--interval', choices=INTERVALS, default='1m')
    p.add_argument('--seconds', type=int, default=120)
    p.add_argument('--order-ttl', type=int, default=10)
    p.add_argument('--max-orders', type=int, default=6)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--faults', action='store_true', help='paper only: inject drop, duplicate, stale data and disconnect')
    p.add_argument('--reconcile-only', action='store_true', help='resume, reconcile, then exit without new signals')
    p.add_argument('--metrics-port', type=int, help='expose Python metrics here and Rust metrics on PORT-1')
    p.add_argument('--sync', choices=['every', 'outbox'], default='every', help='journal sync policy (acceptance L3)')
    a = p.parse_args()
    if a.faults and a.mode != 'paper':
        p.error('--faults is restricted to paper mode')
    if a.reconcile_only and (not a.resume or a.mode != 'testnet'):
        p.error('--reconcile-only requires --resume --mode testnet')
    if a.seconds <= 0 or a.order_ttl <= 0 or a.max_orders <= 0 or not a.quantity.is_finite() or a.quantity <= 0 or not a.max_notional.is_finite() or a.max_notional <= 0:
        p.error('positive finite limits required')
    try: run(a)
    except Exception as error:
        # Transport exceptions can contain URLs. Do not print arbitrary exceptions.
        print('Stopped safely: ' + type(error).__name__ + '; inspect journal and reconcile before restarting', file=sys.stderr)
        sys.exit(1)
