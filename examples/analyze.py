#!/usr/bin/env python3
"""Cold-path SQLite projection of Rust-validated journal state.

Gross tick-lot PnL only: no fees, financing, or reporting-currency conversion.
Realized PnL uses average cost; the fill ID order is the supported venue chronology.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
from contextlib import closing
from decimal import Decimal, localcontext
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[1]


def analyze(journal, database):
    state = json.loads(subprocess.check_output(
        [str(ROOT / "target/debug/mininautilus"), "inspect", str(journal)], text=True))
    database = Path(database)
    # Never overwrite an existing analysis by accident.
    with database.open("xb"):
        pass
    position, average, realized = 0, Fraction(0), Fraction(0)
    rows = []
    for fill in sorted(state["fills"].values(), key=lambda f: f["execution_id"]):
        side = state["orders"][str(fill["order_id"])]["intent"]["side"]
        delta = fill["qty"] * (1 if side == "Buy" else -1)
        price = Fraction(fill["price"])
        if position == 0 or position * delta > 0:
            average = (average * abs(position) + price * abs(delta)) / (abs(position) + abs(delta))
        else:
            closed = min(abs(position), abs(delta))
            realized += closed * (price - average) * (1 if position > 0 else -1)
            if abs(delta) > abs(position):
                average = price
            elif abs(delta) == abs(position):
                average = Fraction(0)
        position += delta
        rows.append((str(fill["execution_id"]), str(fill["order_id"]), side, fill["qty"], fill["price"]))
    if position != state["position"]:
        raise ValueError("analysis position does not match Rust")
    mark_index = 1 if position < 0 else 0
    mark = Fraction(state["quote"][mark_index]) if state["quote"] else None
    if mark is not None:
        unrealized = (mark - average) * position
        gross = Fraction(state["cash"]) + position * mark
        mark_basis = "last_ask" if position < 0 else "last_bid"
    elif position == 0:
        unrealized, gross = Fraction(0), Fraction(state["cash"])
        mark_basis = "flat_position"
    else:
        unrealized = gross = None
        mark_basis = "unavailable"
    if gross is not None and realized + unrealized != gross:
        raise ValueError("PnL components do not reconcile")
    def decimal_string(value):
        if value is None:
            return None
        with localcontext() as context:
            context.prec = 80
            return str(Decimal(value.numerator) / Decimal(value.denominator))
    summary = {"seq": state["seq"], "position_lots": position,
               "realized_gross_tick_lots": decimal_string(realized), "unrealized_gross_tick_lots": decimal_string(unrealized),
               "total_gross_tick_lots": decimal_string(gross), "fees_included": False,
               "mark_basis": mark_basis}
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE fills (execution_id TEXT PRIMARY KEY, order_id TEXT, side TEXT, qty INTEGER, price INTEGER)")
        db.executemany("INSERT INTO fills VALUES (?,?,?,?,?)", rows)
        db.execute("CREATE TABLE summary (json TEXT NOT NULL)")
        db.execute("INSERT INTO summary VALUES (?)", (json.dumps(summary),))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.journal, args.db), indent=2))
