"""Closed-candle SMA targets. Integer sums keep crossing decisions exact."""
from collections import deque
from dataclasses import dataclass
from fractions import Fraction

INTERVALS = {"1s": 1000, "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
             "30m": 1_800_000, "1h": 3_600_000}


class CandleGap(ValueError):
    pass


@dataclass(frozen=True)
class Candle:
    open_ms: int
    close_ms: int
    price: int


class SmaStrategy:
    def __init__(self, fast=5, slow=20, lots=2, interval="1m", long_only=False):
        if not 1 <= fast < slow <= 500 or lots <= 0 or interval not in INTERVALS:
            raise ValueError("require 1 <= fast < slow <= 500, positive lots, supported interval")
        self.fast, self.slow, self.lots = fast, slow, lots
        self.long_only = long_only
        self.interval_ms = INTERVALS[interval]
        self.reset()

    def reset(self):
        self.values = deque(maxlen=self.slow)
        self.last = None
        self.target = 0
        self.fast_sum = self.slow_sum = 0

    def add(self, candle):
        if candle.price <= 0 or candle.open_ms < 0 or candle.close_ms != candle.open_ms + self.interval_ms - 1:
            raise ValueError("invalid closed candle")
        if self.last is not None:
            if candle.open_ms < self.last.open_ms:
                return False
            if candle.open_ms == self.last.open_ms:
                if candle != self.last:
                    raise ValueError("same closed candle changed")
                return False
            if candle.open_ms != self.last.open_ms + self.interval_ms:
                self.reset()
                raise CandleGap("missing closed candles; reset and REST backfill")
        if len(self.values) >= self.fast:
            self.fast_sum -= self.values[-self.fast]
        if len(self.values) == self.slow:
            self.slow_sum -= self.values[0]
        self.values.append(candle.price)
        self.fast_sum += candle.price
        self.slow_sum += candle.price
        self.last = candle
        if self.ready:
            difference = self.fast_sum * self.slow - self.slow_sum * self.fast
            if difference:
                self.target = self.lots if difference > 0 else (0 if self.long_only else -self.lots)
            # Equal averages hold the previous target, initially flat.
        return True

    @property
    def ready(self):
        return len(self.values) == self.slow

    def signal(self):
        if not self.ready:
            return None
        return {"target_lots": self.target, "bar_close_ms": self.last.close_ms,
                "fast_sma_ticks": str(Fraction(self.fast_sum, self.fast)),
                "slow_sma_ticks": str(Fraction(self.slow_sum, self.slow))}

    def intent(self, state, exchange_now_ms, ttl_ms=3000):
        if not self.ready or state["health"] != "Healthy" or state["killed"] or state["quote"] is None:
            return None
        # Last finalized bar is valid during the following interval plus 5 s grace.
        if not 0 <= exchange_now_ms - self.last.close_ms <= self.interval_ms + 5000:
            return None
        if any(not order["lifecycle"] in ("Filled", "Canceled", "Rejected") or order["uncertain"]
               for order in state["orders"].values()):
            return None
        delta = self.target - state["position"]
        if not delta:
            return None
        bid, ask, _ = state["quote"]
        return {"id": max(map(int, state["orders"]), default=0) + 1,
                "side": "Buy" if delta > 0 else "Sell", "qty": abs(delta),
                "limit": ask if delta > 0 else bid, "based_on_seq": state["seq"],
                "valid_until": state["now"] + ttl_ms}
