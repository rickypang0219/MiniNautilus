"""Binance Spot TESTNET transport, stdlib only.

REST facts have a local delivery sequence, NOT an exchange stream sequence.
Recovery is deliberately conservative: cancel our open orders, wait for terminal
states, reconcile every known order's paginated fills and base-asset balance.
Use a dedicated testnet account and one instrument; no other writers/transfers.
"""
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

BASE = "https://testnet.binance.vision"
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


class VenueError(RuntimeError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class OrderAbsent(VenueError):
    """Neither client ID exists at the venue (yet)."""


# Binance rejects a signed request that arrives later than timestamp + recvWindow.
RECV_WINDOW_MS = 5000
# Allowance for local/server clock offset error and server processing.
ABSENCE_MARGIN_S = 2.0


def units(value, increment):
    result = Decimal(str(value)) / increment
    if not result.is_finite() or result != result.to_integral_value() or not 0 <= result <= 2**63 - 1:
        raise VenueError("quantity/price is not an exact supported integer unit")
    return int(result)


class BinanceSpot:
    base_url = BASE
    api_prefix = "/api/v3/"
    def __init__(self, symbol="BTCUSDT", session="lab", *, execute=False):
        if not re.fullmatch(r"[A-Z0-9]{3,20}", symbol) or not re.fullmatch(r"[a-z0-9]{1,10}", session):
            raise ValueError("invalid symbol/session")
        self.symbol, self.session, self.execute = symbol, session, execute
        self.key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
        self.secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
        self.offset_ms = 0
        self.next_request = 0.0
        self.tick = self.lot = None
        self.base_asset = None
        self.execution_times = {}
        # Every request this process signs is signed after `created`; a crashed
        # predecessor's requests were signed before it. See `reconcile`.
        self.created = time.monotonic()
        self.sent_at = {}

    def request(self, method, path, params=None, *, signed=False):
        if method != "GET" and not self.execute:
            raise VenueError("testnet execution is disabled")
        if signed and (not self.key or not self.secret):
            raise VenueError("set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET locally")
        if not path.startswith(self.api_prefix):
            raise ValueError("unsupported endpoint")
        # Conservative fixed pacing. HTTP 429/418 adds server-directed backoff.
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + 0.25
        params = dict(params or {})
        if signed:
            params.update(timestamp=int(time.time() * 1000) + self.offset_ms, recvWindow=RECV_WINDOW_MS)
        query = urllib.parse.urlencode(params)
        if signed:
            signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += "&signature=" + signature
        headers = {"X-MBX-APIKEY": self.key} if signed else {}
        request = urllib.request.Request(self.base_url + path + ("?" + query if query else ""),
                                         method=method, headers=headers)
        try:
            # No redirects: signed parameters must never be forwarded elsewhere.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs):
                    return None
            with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (418, 429):
                try:
                    retry = max(1, float(error.headers.get("Retry-After", "60")))
                except ValueError:
                    retry = 60
                self.next_request = time.monotonic() + retry
            try:
                code = json.loads(error.read()).get("code")
            except (ValueError, AttributeError):
                code = None
            # Never stringify HTTPError: its URL contains the signed query.
            raise VenueError(f"Binance HTTP {error.code}, code {code}; do not retry mutations", code=code) from None
        except (OSError, TimeoutError, ValueError):
            raise VenueError("transport/response failure; mutation outcome may be unknown") from None

    def initialize(self):
        before = int(time.time() * 1000)
        server = self.request("GET", "/api/v3/time")["serverTime"]
        self.offset_ms = server - (before + int(time.time() * 1000)) // 2
        info = self.request("GET", "/api/v3/exchangeInfo", {"symbol": self.symbol})["symbols"][0]
        if info["status"] != "TRADING":
            raise VenueError("symbol is not trading")
        filters = {f["filterType"]: f for f in info["filters"]}
        self.tick = Decimal(filters["PRICE_FILTER"]["tickSize"])
        self.lot = Decimal(filters["LOT_SIZE"]["stepSize"])
        if self.tick <= 0 or self.lot <= 0:
            raise VenueError("disabled tick/lot filters are unsupported")
        self.base_asset = info["baseAsset"]
        self.filters = filters
        return info

    def quote(self):
        quote = self.request("GET", "/api/v3/ticker/bookTicker", {"symbol": self.symbol})
        return {"bid": units(quote["bidPrice"], self.tick), "ask": units(quote["askPrice"], self.tick)}

    def client_id(self, order_id):
        if not 1 <= order_id <= 2**64 - 1:
            raise ValueError("invalid client ID")
        return f"mn{self.session}-{order_id}"

    def available_base(self):
        """Unreserved base inventory; gross strategy position can include base fees."""
        account = self.request("GET", "/api/v3/account", signed=True)
        return next((Decimal(b["free"]) for b in account["balances"] if b["asset"] == self.base_asset), Decimal(0))

    def base_balance(self):
        account = self.request("GET", "/api/v3/account", signed=True)
        for balance in account["balances"]:
            if balance["asset"] == self.base_asset:
                return Decimal(balance["free"]) + Decimal(balance["locked"])
        return Decimal(0)

    def open_orders(self):
        return self.request("GET", "/api/v3/openOrders", {"symbol": self.symbol}, signed=True)

    def submit(self, intent):
        # Exact formatting; the venue remains authoritative for changing filters,
        # available balances, percentage-price limits, and order-count constraints.
        self.sent_at[intent["id"]] = time.monotonic()
        return self.request("POST", "/api/v3/order", {
            "symbol": self.symbol, "side": intent["side"].upper(), "type": "LIMIT", "timeInForce": "GTC",
            "quantity": format(self.lot * intent["qty"], "f"),
            "price": format(self.tick * intent["limit"], "f"),
            "newClientOrderId": self.client_id(intent["id"]), "newOrderRespType": "ACK",
        }, signed=True)

    def query(self, intent):
        # Cancel replaces client ID with our deterministic cancellation ID. Both
        # names can therefore be rediscovered even if its HTTP response was lost.
        cid = self.client_id(intent["id"])
        for candidate in (cid, cid + "c"):
            try:
                order = self.request("GET", "/api/v3/order", {
                    "symbol": self.symbol, "origClientOrderId": candidate}, signed=True)
            except VenueError as error:
                if error.code == -2013:
                    continue
                raise
            if (order["clientOrderId"] not in (cid, cid + "c")
                    or order["side"] != intent["side"].upper()
                    or units(order["origQty"], self.lot) != intent["qty"]
                    or units(order["price"], self.tick) != intent["limit"]):
                raise VenueError("venue order identity/terms disagree with durable intent")
            return order
        raise OrderAbsent("order absent: unresolved, never automatically resubmit")

    def cancel(self, intent):
        order = self.query(intent)
        if order["status"] in TERMINAL:
            return order
        return self.request("DELETE", "/api/v3/order", {"symbol": self.symbol,
            "orderId": order["orderId"], "newClientOrderId": self.client_id(intent["id"]) + "c"}, signed=True)

    def trades(self, remote_order_id):
        trades, cursor = [], 0
        while True:
            page = self.request("GET", "/api/v3/myTrades", {"symbol": self.symbol,
                "orderId": remote_order_id, "fromId": cursor, "limit": 1000}, signed=True)
            if any(t["orderId"] != remote_order_id or t["id"] < cursor for t in page):
                raise VenueError("trade pagination/order identity mismatch")
            trades.extend(page)
            if len(page) < 1000:
                return trades
            cursor = max(t["id"] for t in page) + 1

    def collect(self, intent):
        remote = self.query(intent)
        raw_trades = self.trades(remote["orderId"])
        fills = [{"execution_id": trade["id"] + 1, "order_id": intent["id"],
                  "qty": units(trade["qty"], self.lot), "price": units(trade["price"], self.tick)}
                 for trade in raw_trades]
        filled = units(remote["executedQty"], self.lot)
        if len({f["execution_id"] for f in fills}) != len(fills) or sum(f["qty"] for f in fills) != filled:
            raise VenueError("order/trades not yet consistent; keep recovery gate closed")
        for trade in raw_trades:
            if isinstance(trade.get("time"), int) and trade["time"] >= 0:
                self.execution_times[trade["id"] + 1] = trade["time"]
        status = remote["status"]
        lifecycle = {"NEW": "Accepted", "PARTIALLY_FILLED": "Partial", "FILLED": "Filled",
                     "CANCELED": "Canceled", "EXPIRED": "Canceled", "EXPIRED_IN_MATCH": "Canceled",
                     "REJECTED": "Rejected"}.get(status)
        if lifecycle is None:
            raise VenueError("unsupported venue order status")
        return {"intent": intent, "filled": filled, "lifecycle": lifecycle}, fills, raw_trades

    def timing(self, event):
        """Preserve venue trade times separately from receipt and engine clocks."""
        if not isinstance(event, dict):
            return {}
        report = event.get("Execution", {}).get("report", {})
        if "Fill" in report:
            timestamp = self.execution_times.get(report["Fill"]["execution_id"])
            return dict(event_time_ms=timestamp, time_source="exchange trade") if timestamp is not None else {}
        if "Reconcile" in event:
            times = {f["execution_id"]: self.execution_times[f["execution_id"]]
                     for f in event["Reconcile"]["fills"] if f["execution_id"] in self.execution_times}
            return dict(fill_event_times=times)
        return {}

    def poll(self, state):
        reports = []
        for order in state["orders"].values():
            if order["lifecycle"] in ("Filled", "Canceled", "Rejected") and not order["uncertain"]:
                continue
            remote, fills, _ = self.collect(order["intent"])
            reports.extend({"Fill": fill} for fill in fills if str(fill["execution_id"]) not in state["fills"])
            oid, status = order["intent"]["id"], remote["lifecycle"]
            if status in ("Accepted", "Partial"):
                reports.append({"Accepted": {"id": oid}})
            elif status == "Canceled":
                reports.append({"Canceled": {"id": oid, "cumulative_filled": remote["filled"]}})
            elif status == "Rejected":
                reports.append({"Rejected": {"id": oid}})
        return reports

    def reconcile(self, state, baseline_base):
        # Quiescent barrier: after every owned order is terminal, its cumulative
        # fill count is stable. REST history may lag; mismatches remain blocked.
        known = {self.client_id(int(oid)) + suffix for oid in state["orders"] for suffix in ("", "c")}
        if any(order["clientOrderId"] not in known for order in self.open_orders()):
            raise VenueError("unknown open order; manual investigation required")
        # Persisted-but-unacknowledged orders may never have reached the venue.
        # Absence is proof only once every request that could create them expired:
        # wait past recvWindow after their last send (or this process's start).
        unacked = {oid for oid, o in state["orders"].items()
                   if o.get("lifecycle") == "Pending" and o.get("filled") == 0
                   and not any(f["order_id"] == int(oid) for f in state.get("fills", {}).values())}
        if unacked:
            deadline = max(max(self.sent_at.get(int(oid), self.created), self.created) for oid in unacked)
            delay = deadline + RECV_WINDOW_MS / 1000 + ABSENCE_MARGIN_S - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        absent = []

        def resolve(oid, error):
            if oid not in unacked:
                raise VenueError("acknowledged or filled order missing at venue; manual investigation") from error
            if int(oid) not in absent:
                absent.append(int(oid))

        for oid, order in state["orders"].items():
            try:
                self.cancel(order["intent"])
            except OrderAbsent as error:
                resolve(oid, error)
        orders, fills, raw = [], [], []
        for oid, order in state["orders"].items():
            if int(oid) in absent:
                continue
            remote, trades, raw_trades = self.collect(order["intent"])
            if remote["lifecycle"] not in ("Filled", "Canceled", "Rejected"):
                raise VenueError("recovery requires every owned order to be terminal")
            orders.append(remote); fills.extend(trades); raw.extend(raw_trades)
        # Independent cumulative-quantity position, checked by Rust against fills.
        position = sum(o["filled"] * (1 if o["intent"]["side"] == "Buy" else -1) for o in orders)
        base_fees = sum((Decimal(t["commission"]) for t in raw if t["commissionAsset"] == self.base_asset), Decimal(0))
        expected = Decimal(str(baseline_base)) + self.lot * position - base_fees
        actual = self.base_balance()
        if actual != expected:
            raise VenueError("base balance disagrees with session fills/fees; recovery blocked")
        if self.open_orders():
            raise VenueError("account not quiescent; another writer may be active")
        commissions = {}
        for trade in raw:
            asset = trade["commissionAsset"]
            commissions[asset] = commissions.get(asset, Decimal(0)) + Decimal(trade["commission"])
        self.last_reconciliation = {
            "gross_position_lots": position,
            "net_base_change": str(self.lot * position - base_fees),
            "base_commission": str(base_fees),
            "commissions_by_asset": {asset: str(amount) for asset, amount in commissions.items()},
            "balance_verified": True,
        }
        self.last_reconciliation["absent_orders"] = sorted(absent)
        return {"epoch": state["epoch"], "watermark": state["venue_seq"],
                "orders": orders, "fills": fills, "position": position, "absent": sorted(absent)}
