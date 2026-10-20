"""Read-only Linux scheduler/I/O evidence, sampled once per second by soak.

Counters are cumulative; compare deltas. Missing files remain null, never zero.
No environment variables, command lines or credentials are collected.
"""
from pathlib import Path
import time


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def process(pid):
    status = read(f"/proc/{pid}/status") or ""
    keys = {"VmRSS", "VmHWM", "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"}
    return {"pid": pid, "status": {k: v.strip() for line in status.splitlines()
            if ":" in line for k, v in [line.split(":", 1)] if k in keys},
            "schedstat": read(f"/proc/{pid}/schedstat"),
            "io": read(f"/proc/{pid}/io")}


def sample(python_pid, engine_pid):
    return {"wall_time": time.time(), "monotonic_ns": time.monotonic_ns(),
            "python": process(python_pid), "rust": process(engine_pid) if engine_pid else None,
            "loadavg": read("/proc/loadavg"),
            "pressure": {k: read(f"/proc/pressure/{k}") for k in ("cpu", "io", "memory")},
            "cgroup_cpu": read("/sys/fs/cgroup/cpu.stat"),
            "cgroup_io": read("/sys/fs/cgroup/io.stat")}
