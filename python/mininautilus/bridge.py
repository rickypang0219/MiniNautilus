"""JSON-lines IPC keeps Python outside Rust's state ownership.

This reference bridge serializes requests, and is not an HFT transport.
Only the main runtime thread may call Engine; strategies receive snapshots.

Protocol 2: Rust sends the complete state once, then per request only the
fixed-size header and the orders/fills that request wrote. The bridge applies
each delta to a mirror whose contents equal Rust's full state JSON, and keeps
open-order and max-ID indexes so strategies need not scan history.
"""
import json
import os
import selectors
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = ("Filled", "Canceled", "Rejected")


def is_open(order):
    """Orders that can still fill or reserve risk (Rust `Order::remaining` > 0)."""
    return order["lifecycle"] not in TERMINAL or order["uncertain"]


class State(dict):
    """Rust Core JSON mirror. Compares equal to the plain dict Rust would send.

    `open_orders` (id string -> order) and `max_order_id` are derived indexes kept
    in step with `orders`, so per-event strategy code is O(open), not O(history).
    """

    def __init__(self, data):
        super().__init__(data)
        self.open_orders = {k: o for k, o in self["orders"].items() if is_open(o)}
        # IDs at or below `id_floor` belong to rotated-out journals (never reuse).
        self.max_order_id = max(max(map(int, self["orders"]), default=0), self.get("id_floor", 0))

    def apply(self, delta):
        self.update(delta["header"])
        orders = self["orders"]
        for key, order in delta["orders"].items():
            orders[key] = order
            if is_open(order):
                self.open_orders[key] = order
            else:
                self.open_orders.pop(key, None)
            self.max_order_id = max(self.max_order_id, int(key))
        self["fills"].update(delta["fills"])


def open_orders(state):
    """Open orders of a bridge State, or of a plain state dict (full scan)."""
    cached = getattr(state, "open_orders", None)
    if cached is not None:
        return cached.values()
    return [o for o in state["orders"].values() if is_open(o)]


def next_order_id(state):
    cached = getattr(state, "max_order_id", None)
    if cached is None:
        cached = max(max(map(int, state["orders"]), default=0), state.get("id_floor", 0))
    return cached + 1


class Engine:
    def __init__(self, journal=None, *, paper=False, sim=False, recover=False, config=None,
                 binary=None, time_mode="live", full_state=False, extra_args=()):
        """`sim=True` runs an in-memory paper venue without a journal (backtests).

        `full_state=True` asks Rust for the complete state after every request
        (protocol-1 behaviour, for debugging and parity checks)."""
        if time_mode not in ("live", "historical"):
            raise ValueError("time_mode must be live or historical")
        self.time_mode = time_mode
        binary = Path(binary) if binary else ROOT / "target/debug/mininautilus"
        if sim:
            if journal is not None or recover:
                raise ValueError("sim mode has no journal to write or recover")
            command = [str(binary), "sim"]
        else:
            command = [str(binary), "paper" if paper else "serve", str(journal)]
        if recover:
            command.append("--recover")
        elif config:
            command.append(str(config))
        command += list(extra_args)
        env = dict(os.environ, MINI_RESPONSE="full") if full_state else None
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1, env=env)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self._receive(self._read())
        except BaseException:
            self.close()
            raise

    def _read(self):
        if not self.selector.select(timeout=15):
            raise TimeoutError("Rust engine did not respond; do not dispatch or retry orders")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Rust engine stopped; recover its journal before continuing")
        self.last_response_bytes = len(line)
        return json.loads(line)

    def _receive(self, response):
        if "state" in response:
            self.state = State(response["state"])
        else:
            self.state.apply(response["delta"])
        return response["effects"]

    def send_batch(self, at, events):
        """Process events in order in one round trip; returns all their effects.

        For historical/backtest input only: no live receive-time metadata."""
        request = json.dumps({"at": at, "events": events}, separators=(",", ":"))
        self.process.stdin.write(request + "\n")
        self.process.stdin.flush()
        return self._receive(self._read())

    def send_pairs(self, pairs):
        """Process `(at, event)` pairs in order, each at its own engine time."""
        if not pairs:
            raise ValueError("empty batch")
        request = json.dumps({"at": pairs[0][0], "batch": pairs}, separators=(",", ":"))
        self.process.stdin.write(request + "\n")
        self.process.stdin.flush()
        return self._receive(self._read())

    def send(self, at, event, *, event_time_ms=None, received_time_ms=None,
             time_source=None, fill_event_times=None):
        metadata = {}
        if self.time_mode == "live":
            metadata["received_time_ms"] = received_time_ms if received_time_ms is not None else time.time_ns() // 1_000_000
        if event_time_ms is not None:
            metadata["event_time_ms"] = event_time_ms
            metadata["source"] = time_source or ("historical" if self.time_mode == "historical" else "exchange")
        if fill_event_times:
            metadata["fill_event_times"] = fill_event_times
        request = {"at": at, "event": event}
        if metadata:
            request["time"] = metadata
        request = json.dumps(request, separators=(",", ":"))
        self.process.stdin.write(request + "\n")
        self.process.stdin.flush()
        return self._receive(self._read())

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

