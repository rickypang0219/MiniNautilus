"""Binance Spot TESTNET adapter for the opt-in actor runner (live_actor.ActorSession).

Market candles use the kline WebSocket; quotes, private reports and recovery use
the REST transport in binance.py, whose requests are serialised by its lock.
"""
from contextlib import contextmanager
from decimal import Decimal, ROUND_FLOOR
import json
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from . import metrics
from .binance import VenueError, units
from .market import missing_candles
from .sma import INTERVALS, Candle

STREAM = 'wss://stream.testnet.binance.vision/ws/{symbol}@kline_{interval}'


def closed_candle(message, tick, symbol, interval):
    if message.get('e') != 'kline' or message.get('s') != symbol:
        raise ValueError('unexpected market stream')
    bar = message['k']
    if bar['i'] != interval:
        raise ValueError('unexpected candle interval')
    if not bar['x']:
        return None
    return Candle(int(bar['t']), int(bar['T']), units(bar['c'], tick))


class BinanceActorAdapter:
    def __init__(self, venue, interval, baseline_base, *, validate, silent_seconds=15, max_lag_ms=5000):
        self.venue, self.interval, self.baseline_base = venue, interval, baseline_base
        self.interval_ms = INTERVALS[interval]
        self.validate = validate
        self.silent_seconds, self.max_lag_ms = silent_seconds, max_lag_ms
        self.free_lots = None

    def exchange_now_ms(self):
        return int(time.time() * 1000) + self.venue.offset_ms

    @contextmanager
    def stream(self):
        url = STREAM.format(symbol=self.venue.symbol.lower(), interval=self.interval)
        with connect(url, open_timeout=10, close_timeout=2, max_queue=8) as ws:
            def events():
                last_message = time.monotonic()
                while True:
                    try:
                        raw = ws.recv(timeout=1)
                    except TimeoutError:
                        if time.monotonic() - last_message > self.silent_seconds:
                            raise VenueError('market stream silent') from None
                        yield None
                        continue
                    except ConnectionClosed:
                        raise OSError('market stream closed') from None
                    received_ns = time.perf_counter_ns()
                    last_message = time.monotonic()
                    message = json.loads(raw)
                    lag = self.exchange_now_ms() - int(message['E'])
                    metrics.observe('ws_lag', max(lag, 0) / 1000)
                    if not 0 <= lag <= self.max_lag_ms:
                        raise VenueError('stale market stream; reconnect and backfill')
                    candle = closed_candle(message, self.venue.tick, self.venue.symbol, self.interval)
                    yield None if candle is None else (candle, received_ns)
            yield events()

    def warmup(self, count):
        rows = self.venue.request('GET', '/api/v3/klines', {'symbol': self.venue.symbol,
                                                           'interval': self.interval, 'limit': count})
        now = self.exchange_now_ms()
        return [Candle(int(b[0]), int(b[6]), units(b[4], self.venue.tick)) for b in rows if int(b[6]) < now]

    def backfill(self, last, candle):
        return missing_candles(self.venue, last, candle, self.interval, self.interval_ms)

    def quote(self):
        return self.venue.quote()

    def free_base_lots(self):
        """Free base from the last private heartbeat (no request per candle; the
        REST budget is 4 requests/s). The venue still rejects an unaffordable sell."""
        return self.free_lots

    def submit(self, intent):
        return self.venue.submit(intent)

    def cancel(self, intent):
        return self.venue.cancel(intent)

    def poll(self, snapshot):
        return self.venue.poll(snapshot)

    def heartbeat(self):
        # Empty order polling performs no request; verify private connectivity explicitly.
        account = self.venue.request('GET', '/api/v3/account', signed=True)
        free = next((Decimal(b['free']) for b in account['balances'] if b['asset'] == self.venue.base_asset),
                    Decimal(0)) / self.venue.lot
        self.free_lots = int(free.to_integral_value(rounding=ROUND_FLOOR)) if free >= 0 else 0

    def reconcile(self, state):
        return self.venue.reconcile(state, Decimal(str(self.baseline_base)))

    def timing(self, event):
        return self.venue.timing(event)

    def rotation_state(self):
        """Balance identity baseline for the successor segment (ADR §2 sidecar).

        Called right after a successful reconciliation, which verified
        balance == baseline + net change; that balance is the new baseline."""
        change = Decimal(self.venue.last_reconciliation['net_base_change'])
        return {'baseline_base': str(Decimal(str(self.baseline_base)) + change)}

    def rotated(self, venue_state):
        self.baseline_base = venue_state['baseline_base']
        # Retired orders are never polled again; drop their per-order bookkeeping.
        self.venue.sent_at.clear()
        self.venue.execution_times.clear()
