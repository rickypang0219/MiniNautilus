"""Hyperliquid actor boundary. WS candle liveness, REST-finalized closed bars.

Hyperliquid candles don't have Binance's x flag. Never infer finality from a
forming WS candle. At each time boundary fetch only the two recent closed bars;
the actor backfills a bounded missing window before allowing a fresh signal.
Quotes and private reports currently use bounded REST polling, not private WS.
"""
from collections import OrderedDict
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from .bridge import next_order_id
import json
import time

from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

from .hyperliquid import HyperliquidError, integer
from .sma import Candle, CandleGap, INTERVALS


class HyperliquidActorAdapter:
    supports_short = True

    def __init__(self, venue, interval='1m'):
        if interval not in INTERVALS or interval == '1s':
            raise ValueError('Hyperliquid native candles require supported minute/hour interval')
        self.venue, self.interval = venue, interval
        self.interval_ms = INTERVALS[interval]
        self.subscription_confirmed = False
        self.closed_bars = OrderedDict()

    def exchange_now_ms(self): return int(time.time()*1000)
    def free_base_lots(self): return None
    def validate(self, intent): self.venue.validate(intent)
    def submit(self, intent): return self.venue.submit(intent)
    def cancel(self, intent): return self.venue.cancel(intent)
    def quote(self): return self.venue.quote()
    def poll(self, state): return self.venue.poll(state)
    def reconcile(self, state): return self.venue.reconcile(state)
    def timing(self, event): return self.venue.timing(event)
    def heartbeat(self): self.venue.audit_account()

    def parse(self, row):
        if row['s'] != self.venue.symbol or row['i'] != self.interval:
            raise HyperliquidError('wrong candle subscription')
        start, end = integer(row['t']), integer(row['T'])
        if start % self.interval_ms or end != start+self.interval_ms-1:
            raise HyperliquidError('invalid candle boundary')
        return Candle(start, end, self.venue.instrument.price(row['c']))

    def candles(self, start, end):
        if end < start or end-start > self.interval_ms*502:
            raise CandleGap('bounded candle window exceeded')
        rows = self.venue.info('candleSnapshot', req=dict(coin=self.venue.symbol, interval=self.interval,
                                                        startTime=start, endTime=end))
        if not isinstance(rows, list) or len(rows) > 503:
            raise CandleGap('candle response exceeds bound')
        result = {}
        now = self.exchange_now_ms()
        for row in rows:
            bar = self.parse(row)
            if not start <= bar.open_ms <= end:
                raise CandleGap(f'candle outside window: requested={start}:{end}, actual={bar.open_ms}')
            if bar.close_ms >= now: continue
            if bar.open_ms in result and result[bar.open_ms] != bar:
                raise CandleGap('changed duplicate closed candle')
            result[bar.open_ms] = bar
        ordered = sorted(result.values(), key=lambda b: b.open_ms)
        if any(b.open_ms-a.open_ms != self.interval_ms for a,b in zip(ordered, ordered[1:])):
            raise CandleGap(f'missing candle in bounded response: requested={start}:{end}, starts={[b.open_ms for b in ordered]}')
        for bar in ordered:
            previous = self.closed_bars.get(bar.open_ms)
            if previous is not None and previous != bar:
                raise CandleGap(f'changed previously closed candle: open_ms={bar.open_ms}')
            self.closed_bars[bar.open_ms] = bar
            if len(self.closed_bars) > 503:
                self.closed_bars.popitem(last=False)
        return ordered

    def warmup(self, count):
        if not 1 <= count <= 501: raise ValueError('warmup count exceeds strategy bound')
        boundary = self.exchange_now_ms()//self.interval_ms*self.interval_ms
        # An empty latest minute may be published only with the next venue trade.
        # Read one extra closed bar; never invent the unpublished observation.
        rows = self.candles(boundary-(count+1)*self.interval_ms, boundary-1)
        if len(rows) < count or rows[-1].close_ms < boundary-self.interval_ms-1:
            raise CandleGap(f'incomplete warmup: count={count}, boundary={boundary}, starts={[b.open_ms for b in rows]}')
        return rows[-count:]

    def backfill(self, last, candle):
        start, end = last.open_ms+self.interval_ms, candle.open_ms-1
        rows = self.candles(start, end)
        if len(rows) != (candle.open_ms-last.open_ms)//self.interval_ms-1:
            raise CandleGap('incomplete backfill')
        return rows

    @contextmanager
    def stream(self):
        url = self.venue.base_url.replace('https://', 'wss://')+'/ws'
        with connect(url, open_timeout=8, close_timeout=2, max_size=65536, max_queue=8) as ws:
            self.subscription_confirmed = False
            ws.send(json.dumps(dict(method='subscribe', subscription=dict(type='candle', coin=self.venue.symbol,
                                                                          interval=self.interval))))
            def events():
                last_message = last_ping = time.monotonic()
                subscribed_at = time.monotonic()
                last_boundary = self.exchange_now_ms()//self.interval_ms-1
                while True:
                    message = None
                    try: message = json.loads(ws.recv(timeout=1))
                    except TimeoutError: pass
                    except ConnectionClosed: raise OSError('Hyperliquid market disconnected') from None
                    if message is not None:
                        channel = message.get('channel')
                        if channel == 'candle':
                            bar = self.parse(message['data'])
                            if bar.open_ms > self.exchange_now_ms()+2000:
                                raise HyperliquidError('future candle')
                        elif channel == 'subscriptionResponse':
                            data = message['data']
                            expected = dict(type='candle', coin=self.venue.symbol, interval=self.interval)
                            if data.get('method') != 'subscribe' or data.get('subscription') != expected:
                                raise HyperliquidError('wrong subscription acknowledgement')
                            self.subscription_confirmed = True
                        elif channel != 'pong':
                            raise HyperliquidError('unknown market channel')
                        last_message = time.monotonic()
                    if time.monotonic()-last_ping >= 20:
                        ws.send('{"method":"ping"}'); last_ping=time.monotonic()
                    if time.monotonic()-last_message > 45:
                        raise HyperliquidError('market stream silent')
                    if not self.subscription_confirmed and time.monotonic()-subscribed_at > 10:
                        raise HyperliquidError('subscription not acknowledged')
                    boundary = self.exchange_now_ms()//self.interval_ms
                    if self.subscription_confirmed and boundary > last_boundary and self.exchange_now_ms()%self.interval_ms >= 1000:
                        bar = self.warmup(2)[-1]
                        published_boundary = (bar.close_ms+1)//self.interval_ms
                        if published_boundary > last_boundary:
                            last_boundary = published_boundary
                            yield bar, time.perf_counter_ns()
                        else:
                            yield None
                    else: yield None
            yield events()


def reconcile_engine(engine, venue):
    at = engine.state['now']+1
    engine.send(at, 'Disconnect')
    engine.send(at, 'Reconnect')
    engine.send(at, {'Reconcile': venue.reconcile(engine.state)})
    if engine.state['health'] != 'Healthy':
        raise HyperliquidError('Rust rejected venue reconciliation')


def submit_target(engine, venue, target, *, timeout=20):
    """Submit exactly one durable, bounded, marketable GTC intent; never retry."""
    delta = target-engine.state['position']
    if not delta: return None
    quote = venue.quote()
    # Small limit collar, valid whole-dollar price for BTC; no unbounded market order.
    side = 'Buy' if delta > 0 else 'Sell'
    px = quote['ask' if delta > 0 else 'bid']*venue.tick
    px = (px*(Decimal('1.001') if delta > 0 else Decimal('0.999'))).to_integral_value(
        rounding=ROUND_CEILING if delta > 0 else ROUND_FLOOR)
    at = engine.state['now']+1
    engine.send(at, {'Heartbeat': {'epoch': engine.state['epoch']}})
    engine.send(at, {'Quote': quote})
    intent = dict(id=next_order_id(engine.state), side=side, qty=abs(delta),
                  limit=venue.instrument.price(px), based_on_seq=engine.state['seq'], valid_until=at+3000)
    effects = engine.send(at, {'Submit': intent})
    if not any(e.get('SendOrder') == intent for e in effects):
        raise HyperliquidError('Rust refused diagnostic intent')
    venue.submit(intent)
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        remote = venue.query(intent)
        if remote['status'] == 'filled': return intent
        if remote['status'] != 'open':
            raise HyperliquidError('diagnostic order did not fill')
        time.sleep(0.5)
    raise HyperliquidError('diagnostic fill timeout; reconcile saved session, never retry')



def flatten_known_position(engine, venue):
    """A stopped writer needs a fresh proven account snapshot before closeout."""
    reconcile_engine(engine, venue)
    submit_target(engine, venue, 0)
    reconcile_engine(engine, venue)
    if engine.state['position'] != 0:
        raise HyperliquidError('closeout incomplete')
