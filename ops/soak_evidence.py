"""Read exact driver counters and OS samples alongside a Prometheus report."""
import json
from pathlib import Path


def enrich(report, directory):
    report = dict(report)
    log = Path(directory) / "session.log"
    if log.exists():
        # Only the synthetic driver's final summary has these fields.
        for line in reversed(log.read_text().splitlines()):
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict) and all(k in row for k in ("updates", "market_ipcs", "overruns")):
                report["driver"] = dict(row)
                if row["updates"] > 0:
                    report["driver"]["market_ipcs_per_update"] = row["market_ipcs"] / row["updates"]
                    report["driver"]["overruns_per_1000_updates"] = 1000 * row["overruns"] / row["updates"]
                if "position" in row:
                    report["rust_state"] = dict(report.get("rust_state", {}),
                        final_position=row["position"], final_position_source="session_summary")
                break
    return report


def system_summary(directory):
    path = Path(directory) / "system.jsonl"
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    result = {"samples": len(rows), "processes": {}}
    for who in ("rust", "python"):
        samples = [r[who] for r in rows if r.get(who) and r[who]["status"].get("VmRSS")]
        if not samples:
            continue
        rss = [int(r["status"]["VmRSS"].split()[0]) for r in samples]
        n = len(rss)
        slices = [rss[i*n//6:(i+1)*n//6] for i in range(6)]
        entry = {"rss_kib": {"first": rss[0], "last": rss[-1], "min": min(rss), "max": max(rss)},
                 "rss_slice_ranges_kib": [[min(s), max(s)] if s else None for s in slices]}
        if samples[0].get("schedstat") and samples[-1].get("schedstat"):
            a, b = [list(map(int, s["schedstat"].split())) for s in (samples[0], samples[-1])]
            entry["scheduler_delta"] = {"cpu_ms": (b[0]-a[0])/1e6,
                                         "runqueue_ms": (b[1]-a[1])/1e6, "timeslices": b[2]-a[2]}
        if samples[0].get("io") and samples[-1].get("io"):
            a, b = [dict((k.rstrip(":"), int(v)) for k, v in
                         (line.split() for line in s["io"].splitlines())) for s in (samples[0], samples[-1])]
            entry["io_delta"] = {k: b[k]-a[k] for k in a.keys() & b.keys()}
        result["processes"][who] = entry
    return result
