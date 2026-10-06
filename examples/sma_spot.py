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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed
from mininautilus.bridge import Engine
from mininautilus.binance import BinanceSpot, VenueError, units
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


def run(args):
    load_env(args.env_file)
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
    counters = dict(closed_bars=0, signals=0, submissions=0, reconnects=0, invariant_checks=0)
    started = time.monotonic()
    with (path / 'observations.jsonl').open('a') as log, Engine(path / 'events.jsonl', paper=args.mode == 'paper',
            recover=args.resume, config=path / 'config.json') as engine:
        origin, initial = time.monotonic(), engine.state['now']
        sent_at = {}
        def now(): return initial + int((time.monotonic() - origin) * 1000)
        def note(kind, **data):
            record = dict(kind=kind, elapsed_ms=now(), **data)
            log.write(json.dumps(record) + '\n'); log.flush()
            print(json.dumps(record), flush=True)
        def send(event):
            effects = engine.send(now(), event)
            audit(engine.state, lots); counters['invariant_checks'] += 1
            return effects
        def deliver(report):
            effects = send({'Execution': dict(epoch=engine.state['epoch'], venue_seq=engine.state['venue_seq'] + 1, report=report)})
            if any('QueryState' in e for e in effects):
                raise VenueError('private report requires reconciliation')
        def reconcile():
            send('Disconnect'); send('Reconnect')
            snapshot = venue.reconcile(engine.state, metadata['baseline_base'])
            send({'Reconcile': snapshot})
            if engine.state['health'] != 'Healthy':
                raise VenueError('Rust rejected reconciliation')
            note('reconciled', position_lots=engine.state['position'])
        def dispatch(effects):
            for e in effects:
                if 'SendOrder' in e:
                    counters['submissions'] += 1
                    intent = e['SendOrder']; sent_at[intent['id']] = now()
                    note('order', intent=intent)
                    if args.mode == 'testnet':
                        venue.submit(intent)  # Never retry an ambiguous mutation.
                        deliver({'Accepted': {'id': intent['id']}})
                elif 'SendCancel' in e and args.mode == 'testnet':
                    venue.cancel(engine.state['orders'][str(e['SendCancel']['id'])]['intent'])
                elif 'QueryState' in e:
                    raise VenueError('engine requested recovery')
                elif 'Refused' in e or 'Alert' in e:
                    note('engine_effect', effect=e)
        def add_bar(candle):
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
                        last_poll = 0.0
                        while time.monotonic() - started < args.seconds:
                            dispatch(send('Tick'))
                            if time.monotonic() - last_poll >= 1:
                                if args.mode == 'testnet':
                                    for report in venue.poll(engine.state): deliver(report)
                                send({'Heartbeat': {'epoch': engine.state['epoch']}})
                                last_poll = time.monotonic()
                                for oid, order in list(engine.state['orders'].items()):
                                    if order['lifecycle'] not in TERMINAL and order['pending'] is None and now() - sent_at.get(int(oid), 0) >= args.order_ttl * 1000:
                                        dispatch(send({'Cancel': {'id': int(oid)}}))
                            try: message = json.loads(ws.recv(timeout=1))
                            except TimeoutError: continue
                            exchange_now = int(time.time()*1000) + venue.offset_ms
                            if not 0 <= exchange_now - int(message['E']) <= 5000:
                                raise VenueError('stale market stream; reconnect and backfill')
                            candle = closed_candle(message, venue.tick, args.symbol, args.interval)
                            if candle is None: continue
                            if strategy.last and candle.open_ms > strategy.last.open_ms + strategy.interval_ms:
                                send('MarketUnavailable')
                                first = strategy.last.open_ms + strategy.interval_ms
                                count = (candle.open_ms - first) // strategy.interval_ms
                                if count > 1000:
                                    raise CandleGap('gap exceeds bounded backfill')
                                for attempt in range(4):
                                    rows = venue.request('GET', '/api/v3/klines', dict(symbol=args.symbol, interval=args.interval,
                                        startTime=first, endTime=candle.open_ms - 1, limit=count))
                                    if len(rows) == count: break
                                    time.sleep(1)
                                if len(rows) != count: raise CandleGap('REST history not caught up')
                                for b in rows:
                                    add_bar(Candle(int(b[0]), int(b[6]), units(b[4], venue.tick)))
                                note('backfill', bars=count)
                            if not add_bar(candle): continue
                            counters['closed_bars'] += 1
                            signal = strategy.signal()
                            if signal:
                                counters['signals'] += 1; note('signal', **signal)
                            observed = now()
                            quote = venue.quote()
                            send({'QuoteObserved': dict(**quote, observed_at=observed)})
                            if args.mode == 'paper':
                                for taker, price in [('Sell', quote['ask']), ('Buy', quote['bid'])]:
                                    send({'Trade': dict(taker=taker, price=price, qty=lots)})
                            intent = strategy.intent(engine.state, int(time.time()*1000) + venue.offset_ms)
                            if intent and counters['submissions'] < args.max_orders:
                                validate(venue, intent, args.max_notional)
                                # Explicit Spot long/flat boundary, independent of SMA implementation.
                                if intent['side'] == 'Sell' and intent['qty'] > engine.state['position']:
                                    raise RuntimeError('Spot cannot short')
                                dispatch(send({'Submit': intent}))
                except (OSError, TimeoutError, VenueError, ConnectionClosed, CandleGap) as error:
                    send('MarketUnavailable')
                    note('connection_failure', error_type=type(error).__name__, venue_code=getattr(error, 'code', None))
                    if args.mode == 'testnet': reconcile()
                    counters['reconnects'] += 1
                    if counters['reconnects'] > 3: raise
                    time.sleep(1)
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
                summary = dict(counters, completed=completed, shutdown_reconciled=shutdown_reconciled, position_lots=engine.state['position'], fills=len(engine.state['fills']),
                               elapsed_seconds=round(time.monotonic()-started, 2), mode=args.mode)
                (path / 'summary.json').write_text(json.dumps(summary, indent=2))
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
    p.add_argument('--reconcile-only', action='store_true', help='resume, reconcile, then exit without new signals')
    a = p.parse_args()
    if a.reconcile_only and (not a.resume or a.mode != 'testnet'):
        p.error('--reconcile-only requires --resume --mode testnet')
    if a.seconds <= 0 or a.order_ttl <= 0 or a.max_orders <= 0 or not a.quantity.is_finite() or a.quantity <= 0 or not a.max_notional.is_finite() or a.max_notional <= 0:
        p.error('positive finite limits required')
    try: run(a)
    except Exception as error:
        # Transport exceptions can contain URLs. Do not print arbitrary exceptions.
        print('Stopped safely: ' + type(error).__name__ + '; inspect journal and reconcile before restarting', file=sys.stderr)
        sys.exit(1)
