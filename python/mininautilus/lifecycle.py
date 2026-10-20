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


def publish_sidecar(path, record, on_boundary=lambda stage: None):
    """Create a durable bounded sidecar, or re-sync an identical crash survivor.

    Partial/conflicting survivors fail closed; they are never overwritten.
    """
    import json
    import os
    from pathlib import Path
    path = Path(path)
    encoded = json.dumps(record, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 65536:
        raise ValueError('rotation sidecar exceeds 64 KiB')
    on_boundary('sidecar_before')
    try:
        handle = path.open('xb')
    except FileExistsError:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise RuntimeError('unsafe rotation sidecar survivor') from None
        with path.open('rb') as existing:
            try:
                saved = json.load(existing)
            except (ValueError, UnicodeError):
                raise RuntimeError('partial rotation sidecar; manual recovery required') from None
            if saved != record:
                raise RuntimeError('conflicting rotation sidecar; manual recovery required')
            os.fsync(existing.fileno())
    else:
        with handle:
            handle.write(encoded)
            handle.flush()
            on_boundary('sidecar_written')
            os.fsync(handle.fileno())
    on_boundary('sidecar_fsync_after')
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    on_boundary('sidecar_dirsync_after')


def history_counts(state):
    """O(1) counts with explicit scope; never infer history from the ID floor."""
    orders, fills = len(state['orders']), len(state['fills'])
    cold = state.get('cold')
    complete = cold is not None or state.get('id_floor', 0) == 0
    return dict(summary_schema=2, current_segment_orders=orders, current_segment_fills=fills,
                total_session_orders=(cold.get('orders', 0) if cold else 0)+orders if complete else None,
                total_session_unique_fills=(cold.get('fills', 0) if cold else 0)+fills if complete else None,
                history_counts_complete=complete, fills_scope='current_segment')
