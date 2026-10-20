---
name: soak
description: Run a timed MiniNautilus soak with Prometheus metrics and CPU profiles, read the report, find the bottleneck or failure, change code, and verify the change with a before/after comparison. Use when asked to measure live or paper performance, collect metrics, profile, or improve latency/reliability from runtime data.
---

# Soak → report → fix → verify

Follow docs/observability.md. Every step leaves files; never reason from memory
of an earlier run.

## 1. Prepare (once per container)

```sh
pip install prometheus_client py-spy numpy
apt-get install -y prometheus linux-tools-generic
cargo build --release
```

Check network before a live run: `curl -sS -m 5 https://testnet.binance.vision/api/v3/time`.
If the proxy answers 403, the environment's network policy blocks Binance: tell
the user which hosts to allow (testnet.binance.vision, stream.testnet.binance.vision)
and do not present synthetic numbers as live ones.

## 2. Run

- Live: `python3 ops/soak.py --minutes N --run-dir runs/soak-<label> -- examples/sma_spot.py --mode paper --interval 1s --fast 3 --slow 8 [--sync outbox]`
- Offline pipeline check: `python3 ops/soak.py --minutes 5 --run-dir runs/soak-<label> --synthetic --rate 50`
- Long runs: launch in the background (`run_in_background`, timeout up to 2 h) and
  wait for the completion notification instead of polling. Prometheus answers at
  http://127.0.0.1:9090 during the run for interim queries.

## 3. Read

Open `runs/soak-<label>/report.md`:

1. Checks table: list every FAIL / NO DATA.
2. Stage table: which stage owns the time (e.g. `journal_sync` vs `prepare`).
3. Counts: any Alert, QueryState, Refused, reconnect, connection_failure,
   rest_errors, overrun? Each non-zero count needs an explanation.
4. Drift: p99 growth across slices means cost depends on history.
5. Profiles: top self-time symbols (perf) and frames (py-spy).

For a gate or alert, find the cause in the journal: decode
`session/events.jsonl` frames (`payload` is JSON) and look at the inputs just
before the `Reconcile`/alert. The engine is deterministic, so
`target/release/mininautilus inspect session/events.jsonl` reproduces final state.

## 4. Change

Write the hypothesis and expected effect first. Keep one change per iteration.
Run `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`,
`cargo test`, and `python3 -m unittest discover -s tests -p 'test_*.py'`.

## 5. Verify

Rerun with identical parameters, then `python3 ops/compare.py runs/soak-<before> runs/soak-<after>`.
Keep the change only if the targeted metric improved and no check regressed.
Record the numbers in docs/acceptance.md (commit report.md excerpts; `runs/` is
not in Git).
