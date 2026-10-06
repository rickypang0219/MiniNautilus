"""Offline transport contract and real Python-to-Rust integration tests."""
import hashlib
import hmac
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "examples")]
from mininautilus import Engine, TargetPosition
from mininautilus.binance import BinanceSpot, VenueError, units
from replay import run
from analyze import analyze


def venue():
    gateway = BinanceSpot(session="test", execute=True)
    gateway.tick, gateway.lot, gateway.base_asset = Decimal("0.01"), Decimal("0.001"), "BTC"
    return gateway


def intent():
    return {"id": 1, "side": "Buy", "qty": 2, "limit": 10000, "based_on_seq": 1, "valid_until": 1000}


class AdapterTests(unittest.TestCase):
    def test_decimal_grid_is_exact(self):
        self.assertEqual(units("0.003", Decimal("0.001")), 3)
        for value in ("0.0031", "-1", "Infinity", "NaN"):
            with self.assertRaises(VenueError):
                units(value, Decimal("0.001"))

    def test_default_transport_cannot_mutate(self):
        with self.assertRaises(VenueError):
            BinanceSpot().request("POST", "/api/v3/order")

    def test_order_formatting_and_stable_client_id(self):
        gateway = venue()
        gateway.request = Mock(return_value={"orderId": 88})
        gateway.submit(intent())
        args, kwargs = gateway.request.call_args
        self.assertEqual(args[:2], ("POST", "/api/v3/order"))
        self.assertEqual(args[2]["quantity"], "0.002")
        self.assertEqual(args[2]["price"], "100.00")
        self.assertEqual(args[2]["newClientOrderId"], "mntest-1")
        self.assertTrue(kwargs["signed"])

    def test_signing_and_errors_do_not_expose_secret_or_signed_url(self):
        gateway = venue()
        gateway.key, gateway.secret = "fake-key", "fake-secret"
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"ok":true}')
        with patch("urllib.request.build_opener", return_value=opener), patch("time.time", return_value=1):
            gateway.request("GET", "/api/v3/account", signed=True)
        request = opener.open.call_args.args[0]
        query = request.full_url.split("?", 1)[1]
        unsigned, signature = query.rsplit("&signature=", 1)
        self.assertEqual(signature, hmac.new(b"fake-secret", unsigned.encode(), hashlib.sha256).hexdigest())
        error = urllib.error.HTTPError(request.full_url, 500, "error", {}, io.BytesIO(b'{"code":-1007}'))
        opener.open.side_effect = error
        gateway.next_request = 0
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(VenueError) as raised:
                gateway.request("POST", "/api/v3/order", signed=True)
        self.assertNotIn("signature", str(raised.exception))
        self.assertNotIn("fake-secret", str(raised.exception))
        self.assertEqual(opener.open.call_count, 2)  # No automatic mutation retry.

    def test_cancel_id_can_be_rediscovered_after_lost_cancel_response(self):
        gateway = venue()
        gateway.request = Mock(side_effect=[VenueError("absent", code=-2013), {
            "clientOrderId": "mntest-1c", "side": "BUY", "origQty": "0.002", "price": "100.00"}])
        self.assertEqual(gateway.query(intent())["clientOrderId"], "mntest-1c")

    def test_absent_order_is_unresolved_not_rejected_or_resent(self):
        gateway = venue()
        gateway.request = Mock(side_effect=VenueError("absent", code=-2013))
        with self.assertRaisesRegex(VenueError, "unresolved"):
            gateway.query(intent())
        self.assertTrue(all(call.args[0] == "GET" for call in gateway.request.call_args_list))

    def test_paginated_trades_start_from_beginning(self):
        gateway = venue()
        gateway.request = Mock(side_effect=[
            [{"id": i, "orderId": 7} for i in range(1000)], [{"id": 1000, "orderId": 7}]])
        self.assertEqual(len(gateway.trades(7)), 1001)
        self.assertEqual(gateway.request.call_args_list[0].args[2]["fromId"], 0)
        self.assertEqual(gateway.request.call_args_list[1].args[2]["fromId"], 1000)

    def test_incomplete_fill_history_is_not_accepted(self):
        gateway = venue()
        gateway.query = Mock(return_value={"orderId": 7, "executedQty": "0.002", "status": "FILLED"})
        gateway.trades = Mock(return_value=[{"id": 0, "qty": "0.001", "price": "100.00"}])
        with self.assertRaisesRegex(VenueError, "not yet consistent"):
            gateway.collect(intent())

    def test_quiescent_recovery_checks_fees_and_independent_balance(self):
        gateway = venue()
        state = {"epoch": 1, "venue_seq": 5, "orders": {"1": {"intent": intent()}}}
        gateway.open_orders = Mock(return_value=[])
        gateway.cancel = Mock()
        gateway.collect = Mock(return_value=({"intent": intent(), "filled": 2, "lifecycle": "Filled"},
            [{"execution_id": 1, "order_id": 1, "qty": 2, "price": 10000}],
            [{"commission": "0.000002", "commissionAsset": "BTC"}]))
        gateway.base_balance = Mock(return_value=Decimal("1.001998"))
        self.assertEqual(gateway.reconcile(state, "1")["position"], 2)
        self.assertEqual(gateway.last_reconciliation['net_base_change'], '0.001998')
        self.assertEqual(gateway.last_reconciliation['commissions_by_asset'], {'BTC':'0.000002'})
        gateway.base_balance.return_value = Decimal("2")
        with self.assertRaisesRegex(VenueError, "balance disagrees"):
            gateway.reconcile(state, "1")

    def test_available_base_excludes_reserved_inventory(self):
        gateway = venue()
        gateway.request = Mock(return_value={'balances':[{'asset':'BTC','free':'0.001','locked':'0.002'}]})
        self.assertEqual(gateway.available_base(), Decimal('0.001'))
        self.assertEqual(gateway.base_balance(), Decimal('0.003'))

    def test_unknown_order_prevents_recovery_without_canceling_it(self):
        gateway = venue()
        gateway.open_orders = Mock(return_value=[{"clientOrderId": "someone-else"}])
        gateway.cancel = Mock()
        with self.assertRaisesRegex(VenueError, "unknown open order"):
            gateway.reconcile({"orders": {}}, "1")
        gateway.cancel.assert_not_called()


class IntegrationTests(unittest.TestCase):
    def test_python_strategy_rust_matching_and_journal_are_repeatable(self):
        events = [json.loads(line) for line in (ROOT / "examples/ticks.jsonl").read_text().splitlines()]
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "a.jsonl", Path(directory) / "b.jsonl"
            self.assertEqual(run(first, events), {"position": 0, "cash_tick_lots": 12,
                "gross_marked_pnl_tick_lots": 12, "orders": 2, "unique_fills": 3})
            self.assertEqual(run(first.with_name("c.jsonl"), events), run(second, events))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            before = first.read_bytes()
            summary = analyze(first, Path(directory) / "analysis.sqlite")
            self.assertEqual(summary["realized_gross_tick_lots"], "12")
            self.assertEqual(summary["unrealized_gross_tick_lots"], "0")
            self.assertEqual(first.read_bytes(), before)  # Analysis is read-only.

    def test_python_restart_starts_gated_and_cannot_resubmit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with Engine(path) as engine:
                engine.send(0, {"Quote": {"bid": 99, "ask": 101}})
                order = TargetPosition(3).on_quote(engine.state)
                self.assertIn("SendOrder", engine.send(0, {"Submit": order})[0])
            with Engine(path, recover=True) as recovered:
                self.assertEqual(recovered.state["health"], "Disconnected")
                self.assertEqual(len(recovered.state["orders"]), 1)
                self.assertIn("Refused", recovered.send(0, {"Submit": order})[0])


if __name__ == "__main__":
    unittest.main()
