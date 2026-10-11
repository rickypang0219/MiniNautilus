#!/usr/bin/env python3
"""Summarize a soak run from Prometheus (+ perf / py-spy profiles) into report.md/json.

Everything an operator would read off Grafana is computed here as numbers with
explicit pass/fail checks, so an agent can read one file, decide what to change,
and rerun. Quantiles are Prometheus `histogram_quantile` estimates (bucket
interpolation), not exact order statistics.
"""
import argparse
from collections import Counter
import json
import math
import os
import shutil
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request
from soak_evidence import enrich, system_summary

PERF = os.environ.get("PERF", shutil.which("perf") or "/usr/lib/linux-tools-6.8.0-146/perf")


class Prometheus:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.errors = []

    def _get(self, path, **params):
        query = urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(f"{self.url}{path}?{query}", timeout=30) as response:
                body = json.load(response)
        except OSError as error:  # includes HTTPError: report "no data", not a crash
            self.errors.append(f"{params.get('query')}: {error}")
            return []
        return body["data"]["result"] if body.get("status") == "success" else []

    def vector(self, expr, at):
        """{label-tuple: value} for an instant query."""
        out = {}
        for row in self._get("/api/v1/query", query=expr, time=at):
            value = float(row["value"][1])
            out[tuple(sorted(row["metric"].items()))] = None if math.isnan(value) else value
        return out

    def scalar(self, expr, at):
        values = list(self.vector(expr, at).values())
        return values[0] if values else None

    def series(self, expr, start, end, step):
        rows = self._get("/api/v1/query_range", query=expr, start=start, end=end, step=step)
        if not rows:
            return []
        return [(float(t), None if v == "NaN" else float(v)) for t, v in rows[0]["values"]]


def quantiles(prom, name, window, at, by=""):
    """p50/p90/p99/max-bucket of a histogram over the window, optionally per label."""
    group = f"le{', ' + by if by else ''}"
    result = {}
    for q in (0.5, 0.9, 0.99):
        expr = f"histogram_quantile({q}, sum by ({group}) (increase({name}_bucket[{window}s])))"
        for labels, value in prom.vector(expr, at).items():
            key = dict(labels).get(by, "") if by else ""
            result.setdefault(key, {})[f"p{int(q * 100)}_ms"] = None if value is None else value * 1e3
    counts = prom.vector(f"sum by ({by or 'job'}) (increase({name}_count[{window}s]))", at)
    sums = prom.vector(f"sum by ({by or 'job'}) (increase({name}_sum[{window}s]))", at)
    for labels, count in counts.items():
        key = dict(labels).get(by, "") if by else ""
        total = sums.get(labels) or 0.0
        result.setdefault(key, {}).update(count=round(count or 0),
                                          mean_ms=(total / count * 1e3) if count else None)
    return result


def drift(prom, name, start, end, slices=6):
    """p99 per slice is a diagnostic signal, not proof of history-dependent cost."""
    step = max(60, int((end - start) / slices))
    if end - start < 2 * step:
        return {"p99_ms_per_slice": [], "slice_seconds": step, "last_over_first": None,
                "reason": "need at least two complete slices"}
    expr = f"histogram_quantile(0.99, sum by (le) (increase({name}_bucket[{step}s])))"
    points = [v * 1e3 for _, v in prom.series(expr, start + step, end, step) if v is not None]
    ratio = points[-1] / points[0] if len(points) >= 2 and points[0] else None
    return {"p99_ms_per_slice": points, "slice_seconds": step, "last_over_first": ratio}


def counters(prom, name, window, at, by):
    return {dict(k).get(by, ""): round(v or 0) for k, v in
            prom.vector(f"sum by ({by}) (increase({name}[{window}s]))", at).items() if v}


def perf_top(path, limit=15):
    """Top self-time symbols from a `perf record` file."""
    if not Path(PERF).exists() or not Path(path).exists():
        return []
    out = subprocess.run([PERF, "report", "-i", str(path), "--stdio", "--no-children",
                          "--sort", "symbol", "-g", "none", "--percent-limit", "0.5"],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "%" in line:
            percent, _, symbol = line.partition("%")
            rows.append({"percent": float(percent), "symbol": symbol.split("]", 1)[-1].strip()})
    return rows[:limit]


def pyspy_top(path, limit=15):
    """Self and inclusive sample shares from py-spy's collapsed (`--format raw`) output."""
    if not Path(path).exists():
        return {}
    own, inclusive, total = Counter(), Counter(), 0
    for line in Path(path).read_text().splitlines():
        stack, _, count = line.rpartition(" ")
        if not count.isdigit():
            continue
        frames, count = stack.split(";"), int(count)
        total += count
        own[frames[-1]] += count
        for frame in set(frames):
            inclusive[frame] += count
    share = lambda c: [{"percent": round(100 * n / total, 1), "frame": f} for f, n in c.most_common(limit)]
    return {"samples": total, "self": share(own), "inclusive": share(inclusive)} if total else {}


CHECKS = [
    # (id, description, metric path in report, comparison, threshold)
    ("L1-rust", "Rust per-request p99 <= 0.5 ms", ("rust", "request", "", "p99_ms"), "<=", 0.5),
    ("ipc", "Python-observed IPC p99 <= 2 ms", ("python", "ipc", "", "p99_ms"), "<=", 2.0),
    ("tick-to-trade", "Candle receipt -> order p99 <= 5 ms", ("python", "tick_to_trade", "", "p99_ms"), "<=", 5.0),
    ("drift", "Rust request p99 last/first slice < 2", ("drift", "rust_request", "last_over_first"), "<", 2.0),
    ("errors", "No REST errors", ("totals", "rest_errors"), "==", 0),
    ("gate", "Engine healthy >= 99% of the run", ("rust_state", "healthy_fraction"), ">=", 0.99),
]


def lookup(report, path):
    node = report
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def evaluate(report):
    results = []
    for check, text, path, op, limit in CHECKS:
        value = lookup(report, path)
        if value is None:
            results.append({"check": check, "description": text, "value": None, "status": "NO DATA"})
            continue
        ok = {"<=": value <= limit, "<": value < limit, "==": value == limit, ">=": value >= limit}[op]
        results.append({"check": check, "description": text, "value": value, "status": "PASS" if ok else "FAIL"})
    return results


def build(prom, start, end, perf_files, pyspy_files):
    window = max(int(end - start), 1)
    report = {"window": {"start": start, "end": end, "seconds": window}}
    report["rust"] = {
        "request": quantiles(prom, "mini_request_seconds", window, end),
        "response_encode": quantiles(prom, "mini_response_encode_seconds", window, end),
        "stages": quantiles(prom, "mini_input_stage_seconds", window, end, by="stage"),
    }
    report["python"] = {name: quantiles(prom, f"mini_py_{metric}", window, end) for name, metric in [
        ("ipc", "ipc_seconds"), ("strategy", "strategy_seconds"), ("tick_to_trade", "tick_to_trade_seconds"),
        ("loop", "loop_seconds"), ("ws_event_lag", "ws_event_lag_seconds"),
        ("candle_close_lag", "candle_close_lag_seconds"), ("rest_pacing", "rest_pacing_seconds")]}
    report["python"]["rest"] = quantiles(prom, "mini_py_rest_seconds", window, end, by="path")
    report["totals"] = {
        "rust_events": counters(prom, "mini_events_total", window, end, "kind"),
        "rust_effects": counters(prom, "mini_effects_total", window, end, "kind"),
        "python_events": counters(prom, "mini_py_events_total", window, end, "kind"),
        "rest_errors": sum(counters(prom, "mini_py_rest_errors_total", window, end, "status").values()),
        "rest_errors_by_status": counters(prom, "mini_py_rest_errors_total", window, end, "status"),
        "journal_syncs": prom.scalar(f"sum(increase(mini_journal_syncs_total[{window}s]))", end),
        "requests": prom.scalar(f"sum(increase(mini_requests_total[{window}s]))", end),
    }
    healthy = prom.scalar(f"avg_over_time((mini_health == bool 0)[{window}s:1s])", end)
    report["rust_state"] = {
        "healthy_fraction": healthy,
        "max_retained_orders": prom.scalar(f"max_over_time(mini_retained_orders[{window}s])", end),
        "max_open_orders": prom.scalar(f"max_over_time(mini_open_orders[{window}s])", end),
        "final_position": prom.scalar("mini_position_lots", end),
        "killed": prom.scalar("mini_killed", end),
    }
    report["drift"] = {"rust_request": drift(prom, "mini_request_seconds", start, end),
                       "python_ipc": drift(prom, "mini_py_ipc_seconds", start, end)}
    report["profiles"] = {"rust_perf": [{"file": str(f), "top": perf_top(f)} for f in perf_files],
                          "python_pyspy": [{"file": str(f), **pyspy_top(f)} for f in pyspy_files]}
    report["checks"] = evaluate(report)
    report["query_errors"] = prom.errors[:20]
    return report


def fmt(value, digits=3):
    if value is None:
        return "—"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def markdown(report, title):
    lines = [f"# {title}", "", f"Window: {report['window']['seconds']} s. Quantiles are Prometheus bucket estimates.", "",
             "## Checks", "", "| Check | Condition | Value | Status |", "|---|---|---:|---|"]
    for c in report["checks"]:
        lines.append(f"| {c['check']} | {c['description']} | {fmt(c['value'])} | {c['status']} |")
    def table(heading, rows):
        lines.extend(["", f"## {heading}", "", "| Metric | count | mean ms | p50 ms | p90 ms | p99 ms |",
                      "|---|---:|---:|---:|---:|---:|"])
        for name, q in rows:
            lines.append(f"| {name} | {q.get('count', '—')} | {fmt(q.get('mean_ms'))} | {fmt(q.get('p50_ms'))} | "
                         f"{fmt(q.get('p90_ms'))} | {fmt(q.get('p99_ms'))} |")
    rust = report["rust"]
    table("Rust (engine process)", [("request", rust["request"].get("", {})),
                                    ("response encode", rust["response_encode"].get("", {}))] +
          [(f"input stage: {k}", v) for k, v in sorted(rust["stages"].items())])
    py = report["python"]
    table("Python (strategy process, network)", [(k, v.get("", {})) for k, v in py.items() if k != "rest"] +
          [(f"REST {path}", v) for path, v in sorted(py["rest"].items())])
    lines.extend(["", "## Counts", "", "```json", json.dumps(report["totals"], indent=1), "```",
                  "", "## Engine state", "", "```json", json.dumps(report["rust_state"], indent=1), "```",
                  "", "## Drift (p99 per slice, ms)", "", "```json", json.dumps(report["drift"], indent=1), "```"])
    for profile in report["profiles"]["rust_perf"]:
        lines.extend(["", f"## Rust CPU profile `{Path(profile['file']).name}` (self time)", ""])
        lines.extend(f"- {r['percent']:.1f}% {r['symbol']}" for r in profile["top"])
    for profile in report["profiles"]["python_pyspy"]:
        lines.extend(["", f"## Python profile `{Path(profile['file']).name}` ({profile.get('samples', 0)} samples)",
                      "", "Self:"])
        lines.extend(f"- {r['percent']}% {r['frame']}" for r in profile.get("self", []))
        lines.extend(["", "Inclusive:"])
        lines.extend(f"- {r['percent']}% {r['frame']}" for r in profile.get("inclusive", []))
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prometheus", default="http://127.0.0.1:9090")
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True)
    p.add_argument("--perf", type=Path, nargs="*", default=[])
    p.add_argument("--pyspy", type=Path, nargs="*", default=[])
    p.add_argument("--output", type=Path, required=True, help="directory for report.md/json")
    p.add_argument("--title", default="Soak report")
    args = p.parse_args()
    report = build(Prometheus(args.prometheus), args.start, args.end, args.perf, args.pyspy)
    report = enrich(report, args.output)
    report["system"] = system_summary(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=1))
    (args.output / "report.md").write_text(markdown(report, args.title))
    print(markdown(report, args.title))


if __name__ == "__main__":
    main()
