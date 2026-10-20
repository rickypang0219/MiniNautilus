#!/usr/bin/env python3
"""Python strategy + Rust OMS/risk/matching + durable journal, entirely offline."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from mininautilus import Engine, TargetPosition


def run(journal, events):
    strategy = TargetPosition(target=3, exit_bid=105)
    with Engine(journal, paper=True, time_mode="historical") as engine:
        for item in events:
            engine.send(item["at"], item["event"], event_time_ms=item.get("event_time_ms"))
            if isinstance(item["event"], dict) and "Quote" in item["event"]:
                intent = strategy.on_quote(engine.state)
                if intent:
                    engine.send(item["at"], {"Submit": intent}, event_time_ms=item.get("event_time_ms"))
        state = engine.state
        position = state["position"]
        mark = state["quote"][1 if position < 0 else 0] if state["quote"] else None
        gross = (state["cash"] + position * mark if mark is not None
                 else state["cash"] if position == 0 else None)
        result = {"position": state["position"], "cash_tick_lots": state["cash"],
                  "gross_marked_pnl_tick_lots": gross,
                  "orders": len(state["orders"]), "unique_fills": len(state["fills"])}
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--data", type=Path, default=Path(__file__).with_name("ticks.jsonl"))
    args = parser.parse_args()
    events = [json.loads(line) for line in args.data.read_text().splitlines() if line.strip()]
    print(json.dumps(run(args.journal, events), indent=2))
