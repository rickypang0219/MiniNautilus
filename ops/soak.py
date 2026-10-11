#!/usr/bin/env python3
"""Run a session for N minutes with Prometheus, periodic CPU profiles and a report.

  python3 ops/soak.py --minutes 90 --run-dir runs/soak-live -- \\
      examples/sma_spot.py --mode paper --interval 1s --fast 3 --slow 8
  python3 ops/soak.py --minutes 5 --run-dir runs/soak-synthetic --synthetic

The session must accept `--run-dir DIR --seconds N --metrics-port P` (both
examples/sma_spot.py and ops/synthetic_session.py do). Python metrics are scraped
on P and the Rust engine's on P-1. Every `--profile-every` minutes, `perf`
samples the Rust engine and `py-spy` the Python process for `--profile-seconds`.
At the end `ops/report.py` writes report.md/json; Prometheus data stays in
RUN_DIR/prometheus for follow-up queries (see docs/observability.md).
"""
import argparse
import json
import platform
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from system_sample import sample

ROOT = Path(__file__).resolve().parents[1]
PERF = os.environ.get("PERF", shutil.which("perf") or "/usr/lib/linux-tools-6.8.0-146/perf")


def wait_ready(url, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def children(pid):
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    return [int(x) for x in out.split()]


def find_engine(pid):
    """The mininautilus child of the session process (the Rust engine)."""
    for child in children(pid):
        try:
            if Path(f"/proc/{child}/exe").resolve().name == "mininautilus":
                return child
        except OSError:
            continue
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--metrics-port", type=int, default=9465)
    p.add_argument("--prometheus-port", type=int, default=9090)
    p.add_argument("--profile-every", type=float, default=15, help="minutes between profiles (0: none)")
    p.add_argument("--profile-seconds", type=int, default=30)
    p.add_argument("--synthetic", action="store_true", help="run ops/synthetic_session.py (offline)")
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    args, extra = p.parse_known_args(argv[:split])
    # `-- SCRIPT ARGS` names the session; other unknown options go to the session.
    session = argv[split + 1:] + extra
    if args.synthetic:
        session = ["ops/synthetic_session.py"] + session
    if not session:
        p.error("give --synthetic [SESSION OPTIONS] or -- SCRIPT [ARGS]")
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=False)
    seconds = int(args.minutes * 60)

    config = run / "prometheus.yml"
    config.write_text(
        "global:\n  scrape_interval: 1s\n  evaluation_interval: 1s\n"
        "scrape_configs:\n"
        f"  - job_name: rust\n    static_configs: [{{targets: ['127.0.0.1:{args.metrics_port - 1}']}}]\n"
        f"  - job_name: python\n    static_configs: [{{targets: ['127.0.0.1:{args.metrics_port}']}}]\n")
    prom_url = f"http://127.0.0.1:{args.prometheus_port}"
    prometheus = subprocess.Popen(
        ["prometheus", f"--config.file={config}", f"--storage.tsdb.path={run / 'prometheus'}",
         f"--web.listen-address=127.0.0.1:{args.prometheus_port}", "--storage.tsdb.retention.time=30d"],
        stdout=open(run / "prometheus.log", "w"), stderr=subprocess.STDOUT)
    if not wait_ready(f"{prom_url}/-/ready"):
        prometheus.kill()
        raise SystemExit("prometheus did not start; see prometheus.log")

    command = [sys.executable, *session, "--run-dir", str(run / "session"),
               "--seconds", str(seconds), "--metrics-port", str(args.metrics_port)]
    (run / "command.json").write_text(json.dumps(command))
    provenance = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())}
    started = time.time()
    log = open(run / "session.log", "w")
    proc = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=dict(os.environ, MINI_ORDER_TRACE=str(run / "orders.jsonl")))
    perf_files, pyspy_files = [], []
    next_profile = time.time() + 60  # first profile after warm-up
    system_log = open(run / "system.jsonl", "x")
    try:
        while proc.poll() is None:
            time.sleep(1)
            system_log.write(json.dumps(sample(proc.pid, find_engine(proc.pid))) + "\n")
            system_log.flush()
            if args.profile_every and time.time() >= next_profile and proc.poll() is None:
                n = len(perf_files) + 1
                engine = find_engine(proc.pid)
                jobs = []
                if engine and Path(PERF).exists():
                    out = run / f"perf-{n}.data"
                    jobs.append(subprocess.Popen(
                        [PERF, "record", "-F", "499", "-g", "-p", str(engine), "-o", str(out),
                         "--", "sleep", str(args.profile_seconds)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    perf_files.append(out)
                if shutil.which("py-spy"):
                    out = run / f"pyspy-{n}.txt"
                    jobs.append(subprocess.Popen(
                        ["py-spy", "record", "--pid", str(proc.pid), "--duration", str(args.profile_seconds),
                         "--rate", "200", "--format", "raw", "--output", str(out), "--nonblocking"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    pyspy_files.append(out)
                for job in jobs:
                    job.wait()
                next_profile = time.time() + args.profile_every * 60
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        proc.wait()
    finally:
        system_log.close()
    ended = time.time()
    time.sleep(3)  # last scrape
    subprocess.run([sys.executable, str(ROOT / "ops/report.py"), "--prometheus", prom_url,
                    "--start", str(started + 5), "--end", str(ended),
                    "--perf", *map(str, perf_files), "--pyspy", *map(str, pyspy_files),
                    "--output", str(run), "--title", f"Soak report: {' '.join(session)}"],
                   cwd=ROOT, stdout=subprocess.DEVNULL, check=False)
    report_path = run / "report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        # Remove only the candidate sync policy from the workload fingerprint.
        workload = list(session)
        candidates = {}
        for option in ("--sync", "--rotate-orders"):
            if option in workload:
                i = workload.index(option)
                candidates[option] = workload[i+1]
                del workload[i:i+2]
        candidates["unbatched"] = "--unbatched" in workload
        if "--unbatched" in workload:
            workload.remove("--unbatched")
        report["candidate"] = candidates
        report["provenance"] = provenance
        report["experiment"] = {"machine": hashlib.sha256(platform.node().encode()).hexdigest(),
                                "platform": platform.platform(), "minutes": args.minutes,
                                "session": workload, "metrics_port": args.metrics_port,
                                "profile_every": args.profile_every, "profile_seconds": args.profile_seconds, "order_trace": True}
        report["session_exit"] = proc.returncode
        report_path.write_text(json.dumps(report, indent=2))
    if os.environ.get("SOAK_KEEP_PROMETHEUS") != "1":
        prometheus.terminate()
        prometheus.wait()
    print(f"session exit {proc.returncode}; report: {run / 'report.md'}")
    sys.exit(proc.returncode or 0)


if __name__ == "__main__":
    main()
