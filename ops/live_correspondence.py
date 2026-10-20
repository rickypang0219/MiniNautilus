#!/usr/bin/env python3
"""Audit recorded native sessions; predict marketable fills independently of live fills.

Cold path only. The displayed-depth model has no passive queue/impact model.
Actual execution records NEVER supply simulated prices, quantities or fees.
"""
import argparse
from bisect import bisect_right
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def fraction(value):
    if not isinstance(value, str):
        raise ValueError("decimal strings required")
    return F(value)


def decimal(value):
    # Rational values remain lossless in machine-readable evidence.
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def unique(rows, key):
    result = {}
    for row in rows:
        identity = key(row)
        if identity in result and result[identity] != row:
            raise ValueError("changed duplicate fact")
        result[identity] = row
    return list(result.values())


def ledger(fills, funding):
    """Independent rational average-cost accounting, including cross-zero fills."""
    position = entry = realized = reported_realized = fees = F(0)
    for row in chronological_fills(fills):
        if row["coin"] != "BTC" or row["feeToken"] != "USDC" or row["side"] not in ("B", "A"):
            raise ValueError("unsupported instrument, fee or side")
        if fraction(row["startPosition"]) != position:
            raise ValueError("position gap")
        qty, price = fraction(row["sz"]), fraction(row["px"])
        if qty <= 0 or price <= 0:
            raise ValueError("nonpositive fill")
        delta = qty if row["side"] == "B" else -qty
        closing = position * delta < 0
        pnl = min(abs(position), qty) * (price-entry) * (1 if position > 0 else -1) if closing else F(0)
        # This BTC API field projects entry to0.1; exact cash comes from fills.
        displayed_entry=F(int(entry*10),10)
        displayed_pnl=min(abs(position),qty)*(price-displayed_entry)*(1 if position>0 else -1) if closing else F(0)
        if abs(displayed_pnl-fraction(row["closedPnl"])) > F(1, 10**6):
            raise ValueError("venue realized PnL disagreement")
        after = position + delta
        if position * delta >= 0:
            entry = (abs(position)*entry+qty*price)/abs(after)
        elif after == 0:
            entry = F(0)
        elif after * position < 0:
            entry = price
        position = after
        realized += pnl
        reported_realized += fraction(row["closedPnl"])
        fees += fraction(row["fee"])
    cash_funding = F(0)
    for row in unique(funding, lambda f: (f["time"], f["hash"], f["delta"]["coin"])):
        if row["delta"]["coin"] != "BTC" or row["delta"]["type"] != "funding":
            raise ValueError("foreign funding")
        cash_funding += fraction(row["delta"]["usdc"])
    return dict(position=position, entry=entry, realized=realized, reported_realized=reported_realized,
                display_pnl_difference=reported_realized-realized, fees=fees,
                funding=cash_funding, cash_change=realized-fees+cash_funding)


def chronological_fills(fills):
    """Independent list traversal of unique same-ms startPosition chains."""
    from itertools import groupby
    position=F(0)
    for _, group in groupby(sorted(unique(fills,lambda f:f["tid"]),key=lambda f:f["time"]),key=lambda f:f["time"]):
        pending=list(group)
        while pending:
            candidates=[f for f in pending if fraction(f["startPosition"])==position]
            if len(candidates)!=1:
                raise ValueError("position gap or ambiguous same-time fills")
            row=candidates[0];pending.remove(row)
            qty=fraction(row["sz"])
            if qty<=0 or row["side"] not in ("B","A"):
                raise ValueError("invalid chronological fill")
            position+=qty if row["side"]=="B" else -qty
            yield row


def strategy_parity(rows):
    """Recompute bounded 3/8 SMA independently, including warm-up and held ties."""
    from collections import deque
    window = deque(maxlen=8)
    target, last, mismatches, count = 0, None, 0, 0
    for row in rows:
        if row["kind"] != "strategy":
            continue
        bar = row["request"]["bar"]
        if last is not None and bar["open_ms"] != last+60000:
            raise ValueError("strategy recording gap or duplicate")
        if bar["close_ms"] != bar["open_ms"]+59999 or bar["close_ms"] >= row["received_ms"]:
            raise ValueError("unclosed strategy input")
        last = bar["open_ms"]
        window.append(bar["price"])
        expected = None
        if len(window) == 8:
            difference = sum(list(window)[-3:])*8-sum(window)*3
            if difference:
                target = 20 if difference > 0 else -20
            expected = target
        count += 1
        mismatches += expected != row["answer"]["target"]
    return dict(status="NOT_MEASURED" if not count else "PASS" if not mismatches else "FAIL",
                decisions=count, mismatches=mismatches, model="native SMA 3/8; not PCA")


def predict_orders(orders, books, manifest):
    """Available-at replay. A snapshot is consumed at most once per displayed lot."""
    times = [b["received_ms"] for b in books]
    if times != sorted(times):
        raise ValueError("nonmonotonic observation clock")
    depth = {}
    result = []
    for order in sorted(orders, key=lambda o: (o["at_ms"], o["id"])):
        arrival = order["at_ms"] + manifest["arrival_delay_ms"]
        index = bisect_right(times, arrival)-1
        base = dict(id=order["id"], requested_lots=order["qty"], filled_lots=0, vwap=None)
        if index < 0 or arrival-times[index] > manifest["max_book_age_ms"]:
            result.append(dict(base, status="NOT_MEASURED", reason="no fresh available book"))
            continue
        if index not in depth:
            message = books[index]["message"]["data"]
            if message["coin"] != "BTC" or message["time"] > times[index]+1000:
                raise ValueError("book identity/time")
            depth[index] = [[[fraction(level["px"]), fraction(level["sz"])] for level in side]
                            for side in message["levels"]]
            if len(depth[index]) != 2:
                raise ValueError("book sides")
            for side, reverse in zip(depth[index], (True, False)):
                prices = [level[0] for level in side]
                if any(p <= 0 or q < 0 for p, q in side) or prices != sorted(set(prices), reverse=reverse):
                    raise ValueError("invalid book levels")
            if depth[index][0] and depth[index][1] and depth[index][0][0][0] >= depth[index][1][0][0]:
                raise ValueError("crossed book")
        buy = order["side"] == "Buy"
        limit, lot = F(order["limit"])*F(manifest["tick"]), F(manifest["lot"])
        remaining, value = F(order["qty"])*lot, F(0)
        for level in depth[index][1 if buy else 0]:
            price, available = level
            if (buy and price > limit) or (not buy and price < limit):
                break
            take = min(remaining, (available//lot)*lot)
            remaining -= take
            level[1] -= take
            value += take*price
            if not remaining:
                break
        filled = F(order["qty"])*lot-remaining
        result.append(dict(base, status="MODELED", filled_lots=int(filled/lot),
                           vwap=decimal(value/filled) if filled else None,
                           reason="displayed-depth immediate liquidity only; resting remainder unmodeled"))
    return result


def recorded_books(rows):
    """Normalize all independently recorded L2 sources, including cleanup REST."""
    books=[]
    for row in rows:
        if row["kind"]=="ws" and row["message"]["channel"]=="l2Book":
            books.append(dict(row,source="ws"))
        elif row["kind"]=="http" and row["path"]=="/info" and row["request"].get("type")=="l2Book":
            books.append(dict(received_ms=row["received_ms"],message=dict(data=row["response"]),source="rest"))
    return sorted(books,key=lambda b:b["received_ms"])


def scenario_pnl(predictions, orders, mark, fee_rate, lot):
    """Linear-perp marked PnL for the predicted fill scenario, before funding."""
    if any(p["status"]!="MODELED" for p in predictions):
        return dict(status="NOT_MEASURED",reason="missing modeled order")
    position=cash=fees=F(0)
    for prediction in predictions:
        if not prediction["filled_lots"]:
            continue
        qty=prediction["filled_lots"]*F(lot)
        value=qty*F(prediction["vwap"])
        sign=1 if orders[prediction["id"]]["side"]=="Buy" else -1
        position+=sign*qty;cash-=sign*value;fees+=value*F(fee_rate)
    if position and mark is None:
        return dict(status="NOT_MEASURED",reason="open simulated position has no mark")
    gross=cash+position*(mark or F(0))
    return dict(status="SCENARIO_ONLY",position=decimal(position),gross_marked=decimal(gross),fees=decimal(fees),net_ex_funding=decimal(gross-fees))


def analyze(directory, manifest, binary=None):
    if manifest["schema"] != 1 or manifest["model"] != "displayed-depth-immediate-v1":
        raise ValueError("unsupported frozen model")
    if manifest["arrival_delay_ms"] < 0 or manifest["max_book_age_ms"] <= 0:
        raise ValueError("invalid time budget")
    binary = binary or ROOT/"target/release/mininautilus"
    journal = directory/"journal.jsonl"
    state = json.loads(subprocess.check_output([str(binary), "inspect", str(journal)], text=True))
    rows = [json.loads(line) for line in (directory/"observations.jsonl").read_text().splitlines()]
    session = json.loads((directory/"session.json").read_text())
    if session["instrument"]["symbol"] != "BTC" or session["instrument"]["sz_decimals"] != 5 or F(manifest["lot"]) != F(1, 100000) or F(manifest["tick"]) != F(1, 10):
        raise ValueError("instrument quantum mismatch")
    fills, funding = [], []
    books=recorded_books(rows)
    for row in rows:
        if row["kind"] == "http" and row["path"] == "/info":
            kind = row["request"]["type"]
            if kind == "userFillsByTime":
                fills.extend(row["response"])
            elif kind == "userFunding":
                funding.extend(row["response"])
    fills = unique(fills, lambda f: f["tid"])
    accounting = ledger(fills, funding)
    quantum = F(manifest["lot"])
    if accounting["position"] != state["position"]*quantum:
        raise ValueError("Core/venue position mismatch")
    by_order = {}
    for fill in fills:
        by_order.setdefault(fill["oid"], []).append(fill)
    local_fills = state["fills"]
    for fill in fills:
        local = local_fills.get(str(fill["tid"]+1))  # native execution ID reserves zero
        if local is None or local["qty"]*quantum != fraction(fill["sz"]) or local["price"]*F(manifest["tick"]) != fraction(fill["px"]):
            raise ValueError("Core/venue fill mismatch")
        if fill.get("cloid")!="0x"+session["session"]+f"{local['order_id']:016x}" or fill["side"]!=("B" if state["orders"][str(local["order_id"])]["intent"]["side"]=="Buy" else "A"):
            raise ValueError("Core/venue fill order identity mismatch")
    if len(local_fills) != len(fills):
        raise ValueError("missing venue fills")
    report = json.loads((directory/"run-report.json").read_text())
    for name, value in accounting.items():
        if name in report["ledger"] and abs(value-F(report["ledger"][name])) > F(1, 10**6):
            raise ValueError("native/independent ledger mismatch: "+name)
    # Order IDs are recovered from the validated journal; not all submit inputs
    # necessarily passed risk, so only committed Core orders enter the replay.
    orders = {}
    for line in journal.read_text().splitlines():
        payload = json.loads(json.loads(line)["payload"])
        event = payload.get("Input")
        if not event:
            continue
        body = event["event"]
        intent = body.get("Submit") or body.get("SubmitTargeted", {}).get("intent") if isinstance(body, dict) else None
        if intent and str(intent["id"]) in state["orders"]:
            orders[intent["id"]] = dict(intent, at_ms=event["at"])
    predictions = predict_orders(list(orders.values()), books, manifest)
    attempted={json.loads(line)["payload"]["key"] for line in (directory/"actions.jsonl").read_text().splitlines()}
    # cloid -> exchange oid comes from orderStatus identity, never simulated fills.
    oids = {}
    for row in rows:
        if row["kind"] == "http" and row["request"].get("type") == "orderStatus" and row["response"].get("status") == "order":
            remote = row["response"]["order"]["order"]
            cloid = remote["cloid"]
            if not cloid.startswith("0x"+session["session"]):
                raise ValueError("foreign order identity")
            identity = int(cloid[-16:], 16)
            if identity in oids and oids[identity] != remote["oid"]:
                raise ValueError("changed exchange order identity")
            oids[identity] = remote["oid"]
    for prediction in predictions:
        prediction["durable_adapter_attempt"]=f"order-{prediction['id']}" in attempted
        actual = by_order.get(oids.get(prediction["id"]), [])
        quantity = sum((fraction(f["sz"]) for f in actual), F(0))
        value = sum((fraction(f["sz"])*fraction(f["px"]) for f in actual), F(0))
        prediction["actual_filled_lots"] = int(quantity/quantum)
        prediction["actual_vwap"] = decimal(value/quantity) if quantity else None
        prediction["fill_difference_lots"] = prediction["filled_lots"]-prediction["actual_filled_lots"]
        prediction["vwap_error_bps"] = float(abs(F(prediction["vwap"])-value/quantity)/(value/quantity)*10000) if prediction["vwap"] and quantity else None
    measured = [p for p in predictions if p["status"] == "MODELED"]
    difference = sum(abs(p["fill_difference_lots"]) for p in measured)
    actual_lots = sum(p["actual_filled_lots"] for p in measured)
    error = float(F(difference, max(actual_lots, 1)))
    price_errors=[p["vwap_error_bps"] for p in measured if p["vwap_error_bps"] is not None]
    max_price_error=max(price_errors) if price_errors else None
    failed=error>manifest["max_filled_lot_error_fraction"] or (max_price_error is not None and max_price_error>manifest["max_vwap_error_bps"])
    contexts=[r for r in rows if r["kind"]=="asset_context" and r["available_ms"]<=state["now"]]
    mark=fraction(contexts[-1]["context"]["markPx"]) if contexts else None
    simulated=scenario_pnl(predictions,orders,mark,manifest["simulated_taker_fee_rate"],manifest["lot"])
    actual_gross=accounting["realized"] if not accounting["position"] else accounting["realized"]+(mark-accounting["entry"])*accounting["position"] if mark is not None else None
    pnl=dict(simulated=simulated,actual_gross_marked=decimal(actual_gross) if actual_gross is not None else None,
             actual_net_ex_funding=decimal(actual_gross-accounting["fees"]) if actual_gross is not None else None,
             actual_net_including_funding=decimal(actual_gross-accounting["fees"]+accounting["funding"]) if actual_gross is not None else None,
             actual_funding=decimal(accounting["funding"]),full_net_comparison="NOT_MEASURED: simulated funding and passive remainder unknown")
    scope = "fixed live order schedule conditional replay; not closed-loop strategy PnL validation"
    return dict(schema=1, scope=scope, accounting=dict(status="PASS", unique_fills=len(fills),
                unique_funding=len(unique(funding, lambda f: (f["time"], f["hash"], f["delta"]["coin"]))),
                values={k:decimal(v) for k,v in accounting.items()}), strategy=strategy_parity(rows),
                execution=dict(status="NOT_MEASURED" if not measured else "FAIL" if failed else "INCONCLUSIVE",
                modeled_orders=len(measured), total_orders=len(predictions), filled_lot_error_fraction=error,
                max_vwap_error_bps=max_price_error, enough_orders=len(measured)>=manifest["minimum_orders"],
                minimum_orders=manifest["minimum_orders"], reason="passive queue/latency/impact not validated; no fidelity PASS from this model"),
                simulated_funding="NOT_MEASURED: private actual cashflows cannot be applied to a different simulated position",
                pnl=pnl,orders=predictions, validator_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                analyzer_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                provenance={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [journal, directory/"session.json", directory/"actions.jsonl", directory/"observations.jsonl", directory/"run-report.json"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.directory, json.loads(args.manifest.read_text()))
    result["manifest_sha256"] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({k:v for k,v in result.items() if k not in ("orders", "provenance")}))
    if result["strategy"]["status"]=="FAIL":
        raise SystemExit(1)
