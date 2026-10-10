#!/usr/bin/env python3
"""Collect and compare repeated performance evidence for agent-driven changes.

  scripts/evidence.py collect --label before --reps 5
  scripts/evidence.py compare before after

Each collect writes runs/evidence/<label>/{env.json,runs.jsonl,summary.json}.
compare only calls a change "improved"/"regressed" when both sides have at
least MIN_REPS reps, the per-rep ranges do not overlap, and the medians differ
by at least MIN_EFFECT_PCT; otherwise it is "inconclusive". Run an A/A check
(two collects of the same commit) to see this machine's noise floor. Agents
must quote this verdict rather than a single run.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "runs" / "evidence"
METRICS = ("p50_ns", "p99_ns", "p999_ns", "messages_per_sec")
HIGHER_IS_BETTER = {"messages_per_sec"}
MIN_REPS = 5
MIN_EFFECT_PCT = 10.0


def sh(*cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout.strip()


def environment():
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        cpu = platform.processor()
    return {
        "commit": sh("git", "rev-parse", "HEAD"),
        "dirty": bool(sh("git", "status", "--porcelain", "--", "src", "benches")),
        "os": platform.platform(),
        "cpu": cpu,
        "cpus": os.cpu_count(),
        "loadavg": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "samples": os.environ.get("MINI_SAMPLES", "50000"),
        "pin": os.environ.get("MINI_PIN"),
    }


def bench_once():
    proc = subprocess.run(
        ["cargo", "bench", "--locked", "--bench", "latency"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    rows, header = [], None
    for line in proc.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split(",")
        if header is None:
            header = fields
            continue
        row = dict(zip(header, fields))
        rows.append({
            "case": f"padded={row['padded']},wait={row['wait']}",
            **{m: float(row[m]) for m in METRICS},
        })
    if not rows:
        sys.exit("no benchmark rows parsed")
    return rows


def collect(args):
    target = OUT / args.label
    if target.exists():
        sys.exit(f"{target} exists; evidence is never overwritten")
    target.mkdir(parents=True)
    env = environment()
    (target / "env.json").write_text(json.dumps(env, indent=2))
    per_case = {}
    with open(target / "runs.jsonl", "w") as log:
        for rep in range(args.reps):
            for row in bench_once():
                log.write(json.dumps({"rep": rep, **row}) + "\n")
                per_case.setdefault(row["case"], []).append(row)
            print(f"rep {rep + 1}/{args.reps} done", file=sys.stderr)
    summary = {
        case: {
            m: {
                "median": statistics.median(r[m] for r in rows),
                "min": min(r[m] for r in rows),
                "max": max(r[m] for r in rows),
            }
            for m in METRICS
        }
        for case, rows in per_case.items()
    }
    summary = {"reps": args.reps, "cases": summary}
    (target / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"label": args.label, "env": env, "summary": summary}, indent=2))


def compare(args):
    sa = json.loads((OUT / args.before / "summary.json").read_text())
    sb = json.loads((OUT / args.after / "summary.json").read_text())
    enough = min(sa["reps"], sb["reps"]) >= MIN_REPS
    if not enough:
        print(f"# fewer than {MIN_REPS} reps on a side: every verdict is inconclusive", file=sys.stderr)
    a, b = sa["cases"], sb["cases"]
    print("case,metric,before_median,after_median,change_pct,verdict")
    for case in sorted(set(a) & set(b)):
        for m in METRICS:
            x, y = a[case][m], b[case][m]
            change = (y["median"] - x["median"]) / x["median"] * 100 if x["median"] else 0.0
            if not enough or abs(change) < MIN_EFFECT_PCT:
                better = None
            elif y["max"] < x["min"]:
                better = m not in HIGHER_IS_BETTER
            elif y["min"] > x["max"]:
                better = m in HIGHER_IS_BETTER
            else:
                better = None
            verdict = "inconclusive" if better is None else ("improved" if better else "regressed")
            print(f"{case},{m},{x['median']:.0f},{y['median']:.0f},{change:+.1f},{verdict}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    c = sub.add_parser("collect")
    c.add_argument("--label", required=True)
    c.add_argument("--reps", type=int, default=5)
    c.set_defaults(func=collect)
    d = sub.add_parser("compare")
    d.add_argument("before")
    d.add_argument("after")
    d.set_defaults(func=compare)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
