# Runtime v2 offline operation

The implemented entrypoints exercise the Rust OMS through the bounded coordinator and an independent fake venue. They do not connect to Binance, Hyperliquid or Kraken. The original live SMA example still uses its serial venue loop; its audit now uses deltas. A live actor runner needs adapter-specific stream buffering and reconciliation integration before execution is supported.

Read [design decisions](runtime-v2-design.md) and [scope/criteria](runtime-v2-proposal.md). No AWS resources or API keys are needed. Future cloud use keeps the existing Tokyo and under-USD-20 constraints; these commands do not deploy or resize anything.

## Build and correctness

```sh
python3 ops/runtime_bench.py --build
scripts/verify.sh
```

The build command uses locked dependencies and release profile, and writes `target/runtime-v2-build.json`. A benchmark verifies that its source inputs and binary SHA-256 match this receipt. Tests retain their debug binary; verify/CI also build the release binary and run a short version of the fault-soak driver.

Benchmark source must be committed and clean. `--allow-dirty` is for functional smoke only and records dirty provenance; comparisons reject it. Do not reuse run directories. Receipts are provenance, not cryptographic authorization: they identify the build the operator actually performed.

## Performance evidence

```sh
python3 ops/runtime_bench.py --runtime serial --run-dir runs/rv2-before-1
python3 ops/runtime_bench.py --runtime actor --run-dir runs/rv2-after-1
python3 ops/runtime_compare.py runs/rv2-before runs/rv2-after --output runs/rv2-comparison.json
```

Put at least five uniquely named per-run directories under each comparison directory; interleave serial/actor and run a same-version A/A first. Use identical manifests and seeds. To isolate adapter blocking, both sides can use `--fake-delay-seconds 0.02`. Record immediate-I/O guardrails too. A run's identity includes the actual manifest, payload hash, source/binary hashes, instrumentation and machine; changes to the benchmark harness invalidate a comparison.

`internal_tick_to_effect` includes queueing and ends before fake venue I/O. `ipc_roundtrip` includes Rust service and sync. `rust_before_response` excludes response serialization; `rust_full_request_histogram_p99_ms` is a labelled Prometheus bucket estimate including response work. Python encode/decode/mirror stages are durations from the same request; never subtract unrelated p99s. Histograms and exact rolling samples are different measurements. Low-overhead aggregate runs can set `trace: false` in a separate manifest; they lack per-action stage samples and are not directly interchangeable with trace runs.

A performance verdict requires five repetitions, nonoverlapping ranges and at least 10% median effect. A/A instability or guardrail uncertainty remains INCONCLUSIVE. The frozen 5 ms internal target and 0.5 ms Rust target can still fail on the current filesystem; passing correctness does not override them. CI has no absolute timing assertions.

## Long reliability coverage

```sh
python3 ops/runtime_soak.py --minutes 90 --seed 7 --run-dir runs/rv7-seed7
python3 ops/runtime_soak.py --minutes 90 --seed 17 --run-dir runs/rv7-seed17
python3 ops/runtime_soak.py --minutes 90 --seed 29 --run-dir runs/rv7-seed29
```

These are actual wall-clock durations. Running them concurrently is valid reliability coverage but introduces host contention and cannot supply controlled A/B timings. Default rate is ten quote observations per second; fault interval is ten minutes. The script exercises lost ACK, source gap, input overflow, Rust process kill, executor cancellation and late source epoch, then reconciles using the fake venue's independent history. An admitted but uncommitted input at a controlled crash stays explicitly counted as pending in that stopped generation; accepted venue fills are recovered via snapshot. The producer records missed deadlines rather than generating an unbounded catch-up burst.

Shutdown explicitly flattens this offline fixture, persists Kill, restarts and reconciles to prove the latch remains. This is not an automatic live liquidation policy. Raw journals, per-generation order traces, input/phase logs, OS samples, fake venue facts and report JSON remain under the run directory. Reports distinguish active/recovery/shutdown and expected versus unexpected faults.

Only queues, tasks and the strategy window are bounded. Core/mirror terminal orders, audit dedup evidence and the fake venue's history are retained; report their growth and restart peaks. There is no automatic live compaction or checkpoint shortcut. A finite 90-minute run is not a proof of indefinitely bounded RSS.

## Recovery and history-cost investigation

```sh
python3 ops/recovery_probe.py --run-dir runs/rv4-recovery
python3 ops/audit_bench.py --run-dir runs/rv3-audit
```

Recovery probe uses fixed active state and a fixed tail, quote-only history of 1,000 and 100,000, and fresh copies for recover. It records full bytes read, Python frame/checksum/payload attribution, authoritative Rust replay/startup and snapshot size/load. The Python inspection repeats work and is not an alternative recovery implementation; process-launch cost is included in Rust startup timings. Archive-independent startup cannot be claimed while full-prefix verification is required.

Audit benchmark separately tests terminal history 0/1,000/100,000 with zero delta work, and the frozen old quadratic audit at 1,000. Its p99 is the p99 of 1,000-call batch averages, not individual-event tail latency. Differential request fixtures and operation-count tests establish correctness and absence of hot-path scans.

## Using the coordinator

`Coordinator(apply, execute, clock_ms=logical_clock, dispatch_guard=guard)` expects an async `apply(Input)` callback returning effects only after Engine's durable response. One bounded worker owns all Engine calls; one executor serializes external account actions. `offer` is synchronous admission on the owning event loop; check its boolean result. Never create one task per incoming event, never ignore critical overflow, and never call Engine from an executor or producer. Source heads are FIFO; control/private/timer share fair service across sources ahead of market work.

A guard reads the mirror through the same writer worker, skips only confirmed terminal/non-uncertain cancels, and never mutates risk. The account logical clock prevents queued orders dispatching after expiry; a missing clock fails closed for actual Rust intents.

On `gated`, stop generating intents. The coordinator retains intent in the Rust journal and records unsent effects as abandoned; reservations stay. Resolve or cancel the single in-flight operation, stop the coordinator, reconcile through the sole Engine owner, and create a new coordinator. Do not reset a latch with a boolean or resubmit old IDs. A `BlockingExecutor` timeout leaves its worker busy; a replacement writer/executor cannot safely start while that worker could still send. Native async cancellation likewise requires a venue-specific outcome contract.

If the writer fails, abort and recover the journal. `Engine.close()` reaps killed children even after a buffered BrokenPipe; cleanup never retries submission. Rollback stops writers/executors first and preserves journal schema 1 / protocol 2. The new optional trace fields do not alter persisted frames.
