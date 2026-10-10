#!/usr/bin/env python3
"""Compare two soak reports (before/after a change): checks and key latencies."""
import json
import math
import statistics
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evidence import verdict

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


def load_runs(directory):
    root = Path(directory)
    paths = [root / "report.json"] if (root / "report.json").exists() else sorted(root.glob("*/report.json"))
    if not paths:
        raise ValueError(f"no report.json files in {root}")
    return [json.loads(p.read_text()) for p in paths]


def compare_runs(before, after):
    # Only the candidate implementation/configuration may differ. No conclusion
    # from old reports without workload/machine metadata, or failed sessions.
    all_runs = before + after
    fingerprints = [r.get("experiment") for r in all_runs]
    valid = (all(f is not None for f in fingerprints)
             and all(f == fingerprints[0] for f in fingerprints)
             and all(r.get("session_exit") == 0 for r in all_runs))
    rows = []
    for name, path in KEYS:
        a, b = ([get(r, path) for r in group] for group in (before, after))
        complete = all(isinstance(x, (int, float)) and math.isfinite(x) for x in a+b)
        rows.append(dict(metric=name, before=statistics.median(a) if complete else None,
                         after=statistics.median(b) if complete else None,
                         before_range=[min(a), max(a)] if complete else None,
                         after_range=[min(b), max(b)] if complete else None,
                         verdict=(verdict(a, b, higher_is_better=name == "healthy fraction")
                                  if valid and complete and name not in ("requests", "journal syncs")
                                  else "inconclusive")))
    return {"before_reps": len(before), "after_reps": len(after),
            "compatible_experiments": valid, "rows": rows}


def main(before, after):
    result = compare_runs(load_runs(before), load_runs(after))
    print(f"Repetitions: {result['before_reps']} / {result['after_reps']}; "
          f"compatible successful experiments: {result['compatible_experiments']}.")
    print("Verdicts require >=5 runs per side, non-overlapping ranges and >=10% median effect.")
    print("| Metric | Before median [range] | After median [range] | Verdict |\n|---|---:|---:|---|")
    for r in result["rows"]:
        print(f"| {r['metric']} | {r['before']} {r['before_range']} | {r['after']} {r['after_range']} | {r['verdict']} |")
    return result


if __name__ == "__main__":
    main(*sys.argv[1:3])
