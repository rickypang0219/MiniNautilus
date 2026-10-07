"""Bounded reconnect policy. Stable periods forgive earlier transient failures."""
class ReconnectBudget:
    def __init__(self, maximum=3, stable_seconds=30):
        self.maximum, self.stable_seconds = maximum, stable_seconds
        self.consecutive = 0
        self.healthy_since = None

    def connected(self, now):
        self.healthy_since = now

    def progress(self, now):
        if self.healthy_since is not None and now - self.healthy_since >= self.stable_seconds:
            self.consecutive = 0

    def failed(self):
        self.healthy_since = None
        self.consecutive += 1
        if self.consecutive > self.maximum:
            raise RuntimeError('consecutive reconnect budget exhausted')
        return min(2 ** (self.consecutive - 1), 8)
