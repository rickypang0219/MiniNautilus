"""Validated, bounded candle backfill shared by replayable market adapters."""
import time
from .binance import units
from .sma import Candle, CandleGap


def missing_candles(venue, previous, current, interval, interval_ms, *, sleep=time.sleep):
    """Return an exact contiguous bridge; never mutate strategy on a partial result."""
    first = previous.open_ms + interval_ms
    span = current.open_ms - first
    if span <= 0:
        return []
    if span % interval_ms or span // interval_ms > 1000:
        raise CandleGap('invalid or excessive backfill range')
    count = span // interval_ms
    expected = list(range(first, current.open_ms, interval_ms))
    for attempt in range(4):
        rows = venue.request('GET', '/api/v3/klines', dict(symbol=venue.symbol, interval=interval,
                            startTime=first, endTime=current.open_ms - 1, limit=count))
        bars = [Candle(int(b[0]), int(b[6]), units(b[4], venue.tick)) for b in rows]
        if ([b.open_ms for b in bars] == expected and
                all(b.close_ms == b.open_ms + interval_ms - 1 for b in bars)):
            return bars
        if attempt < 3:
            sleep(1)
    raise CandleGap('REST history incomplete or inconsistent; keep market gate closed')
