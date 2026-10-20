#!/usr/bin/env python3
"""Public Testnet reachability probe. No credentials and no order submission.

Run this ON the candidate host; --location labels the result, it does not move
network egress. A 451 is a failed eligibility/reachability gate, not a retry hint.
"""
import argparse
import json
from pathlib import Path
import platform
import time
import urllib.error
import urllib.request


def rest(path):
    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen("https://testnet.binance.vision" + path, timeout=8) as response:
            response.read(65536)
            status = response.status
        error = None
    except urllib.error.HTTPError as exc:
        status, error = exc.code, "HTTPError"
    except (OSError, TimeoutError) as exc:
        status, error = None, type(exc).__name__
    return {"path": path, "status": status, "error": error,
            "roundtrip_ms": (time.perf_counter_ns()-started)/1e6}


def probe(location, samples):
    report = {"location_label": location, "egress_location_verified": False,
              "utc_epoch": time.time(), "platform": platform.platform(), "authenticated": False,
              "rest": [], "websocket": {}}
    for _ in range(samples):
        row = rest("/api/v3/ping")
        report["rest"].append(row)
        if row["status"] in (403, 451):
            break
    for path in ("/api/v3/time", "/api/v3/ticker/bookTicker?symbol=BTCUSDT"):
        report["rest"].append(rest(path))
    try:
        from websockets.sync.client import connect
        started = time.perf_counter_ns()
        with connect("wss://stream.testnet.binance.vision/ws/btcusdt@kline_1s",
                     open_timeout=8, close_timeout=2, max_queue=8) as ws:
            report["websocket"]["handshake_ms"] = (time.perf_counter_ns()-started)/1e6
            messages = []
            for _ in range(samples):
                message = json.loads(ws.recv(timeout=8))
                messages.append({"event": message.get("e"), "event_ms": message.get("E"),
                                 "received_wall_ms": time.time_ns()//1_000_000})
            report["websocket"].update(connected=True, messages=messages)
    except Exception as exc:
        report["websocket"].update(connected=False, error=type(exc).__name__)
    report["public_gate_pass"] = (all(r["status"] == 200 for r in report["rest"])
                                  and report["websocket"].get("connected", False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--location", default="current-cloud-container")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("samples must be 1..100")
    # Reserve the output before probing, never overwrite a prior result.
    with args.output.open("x") as out:
        report = probe(args.location, args.samples)
        json.dump(report, out, indent=2)
    print(json.dumps({"public_gate_pass": report["public_gate_pass"],
                      "rest_status": [r["status"] for r in report["rest"]],
                      "ws_connected": report["websocket"].get("connected")}))
    return 0 if report["public_gate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
