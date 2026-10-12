"""Constant-space, monotonic lifecycle timing; event logs remain external.

The denominator starts when new risk is first allowed and ends when shutdown is
requested. Rotation pending, drills and recovery are included as downtime.
"""
import threading
import time


class AdmissionClock:
    def __init__(self, emit, clock=time.perf_counter_ns):
        self.emit, self.clock = emit, clock
        self.lock = threading.Lock()
        self.start = self.last = self.end = None
        self.enabled = False
        self.enabled_ns = 0

    def update(self, enabled, reason):
        with self.lock:
            if self.end is not None:
                return False
            now = self.clock()
            if self.start is None:
                if not enabled:
                    return False
                self.start = self.last = now
            if enabled == self.enabled:
                return False
            if self.enabled:
                self.enabled_ns += now-self.last
            self.enabled, self.last = enabled, now
            self.emit('risk_admission', monotonic_ns=now, enabled=enabled, reason=reason)
            return True

    def finish(self):
        with self.lock:
            if self.end is None:
                self.end = self.clock()
                if self.enabled and self.last is not None:
                    self.enabled_ns += self.end-self.last
                self.enabled = False

    def report(self):
        with self.lock:
            now = self.end if self.end is not None else self.clock()
            total = now-self.start if self.start is not None else None
            enabled = self.enabled_ns + (now-self.last if self.enabled and self.last is not None else 0)
            return dict(enabled_ns=enabled, denominator_ns=total,
                        trade_enabled_ratio=enabled/total if total else None,
                        start_monotonic_ns=self.start, end_monotonic_ns=self.end)
