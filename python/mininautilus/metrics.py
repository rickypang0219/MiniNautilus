"""Optional Prometheus metrics for the Python side (bridge, REST, market stream).

`prometheus_client` is optional: without it, or before `start()`, every call is a
no-op, so instrumented code never depends on the observability stack. Rust-side
metrics are separate (`MINI_METRICS_ADDR`, src/metrics.rs). See
docs/observability.md for the metric list and how the soak runner uses them.
"""
import time

try:
    import prometheus_client as _prom
except ImportError:  # pragma: no cover - exercised when the package is absent
    _prom = None

# 10 µs .. 30 s: covers pipe IPC, journal fsync, REST round trips and stalls.
BUCKETS = (1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2,
           0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
_metrics = {}


def _define():
    h, c = _prom.Histogram, _prom.Counter
    return {
        "ipc": h("mini_py_ipc_seconds", "Bridge round trip per request (pipe, Rust, JSON).",
                 buckets=BUCKETS),
        "rest": h("mini_py_rest_seconds", "REST round trip, excluding client-side pacing.",
                  ["method", "path"], buckets=BUCKETS),
        "rest_pacing": h("mini_py_rest_pacing_seconds", "Client-side rate-limit wait before a REST call.",
                         buckets=BUCKETS),
        "rest_errors": c("mini_py_rest_errors_total", "REST failures by path and HTTP status/kind.",
                         ["path", "status"]),
        "ws_lag": h("mini_py_ws_event_lag_seconds",
                    "Receive time minus exchange event time (E), clock-offset corrected.", buckets=BUCKETS),
        "candle_lag": h("mini_py_candle_close_lag_seconds",
                        "Receive time minus candle close time for closed candles.", buckets=BUCKETS),
        "strategy": h("mini_py_strategy_seconds", "Strategy compute per closed candle.", buckets=BUCKETS),
        "tick_to_trade": h("mini_py_tick_to_trade_seconds",
                           "Closed-candle receipt to order handed to the venue (paper: to SendOrder).",
                           buckets=BUCKETS),
        "loop": h("mini_py_loop_seconds", "One coordinator loop iteration, including blocking waits.",
                  buckets=BUCKETS),
        "events": c("mini_py_events_total", "Session notes by kind (reconnects, gaps, faults, orders...).",
                    ["kind"]),
    }


def start(port, address="127.0.0.1"):
    """Expose /metrics on `port`. Returns False when prometheus_client is missing."""
    if _prom is None:
        return False
    if not _metrics:
        _metrics.update(_define())
    _prom.start_http_server(port, addr=address)
    return True


def observe(name, seconds, *labels):
    metric = _metrics.get(name)
    if metric is not None:
        (metric.labels(*labels) if labels else metric).observe(seconds)


def count(name, *labels):
    metric = _metrics.get(name)
    if metric is not None:
        (metric.labels(*labels) if labels else metric).inc()


class timer:
    """`with timer("ipc"):` observes the block's wall time."""

    def __init__(self, name, *labels):
        self.name, self.labels = name, labels

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *_):
        observe(self.name, time.perf_counter() - self.start, *self.labels)
        return False
