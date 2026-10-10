#!/usr/bin/env python3
"""Compare two soak reports (before/after a change): checks and key latencies."""
import json
import sys
from pathlib import Path

KEYS = [("rust request p99 ms", ("rust", "request", "", "p99_ms")),
        ("rust journal_sync p99 ms", ("rust", "stages", "journal_sync", "p99_ms")),
        ("python ipc p50 ms", ("python", "ipc", "", "p50_ms")),
        ("python ipc p99 ms", ("python", "ipc", "", "p99_ms")),
        ("tick_to_trade p99 ms", ("python", "tick_to_trade", "", "p99_ms")),
        ("loop p99 ms", ("python", "loop", "", "p99_ms")),
        ("journal syncs", ("totals", "journal_syncs")),
        ("requests", ("totals", "requests")),
        ("healthy fraction", ("rust_state", "healthy_fraction"))]


def get(report, path):
    for key in path:
        report = report.get(key) if isinstance(report, dict) else None
    return report


def main(before, after):
    a, b = (json.loads(Path(p, "report.json").read_text()) for p in (before, after))
    print(f"| Metric | {Path(before).name} | {Path(after).name} | after/before |\n|---|---:|---:|---:|")
    for name, path in KEYS:
        x, y = get(a, path), get(b, path)
        ratio = f"{y / x:.2f}" if isinstance(x, (int, float)) and isinstance(y, (int, float)) and x else "—"
        show = lambda v: f"{v:.3f}" if isinstance(v, float) else str(v)
        print(f"| {name} | {show(x)} | {show(y)} | {ratio} |")
    print("\n| Check | before | after |\n|---|---|---|")
    for c, d in zip(a["checks"], b["checks"]):
        print(f"| {c['check']} | {c['status']} | {d['status']} |")


if __name__ == "__main__":
    main(*sys.argv[1:3])
