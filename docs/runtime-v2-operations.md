# Runtime v2 offline operation

The implemented entrypoints exercise the Rust OMS through the bounded coordinator and an independent fake venue. They do not connect to Binance, Hyperliquid or Kraken. The live SMA example keeps its serial venue loop by default.

**Next phase (G2/G3, opt-in):** `examples/sma_spot.py --runtime actor --receipt target/runtime-v2-build.json --binary target/release/mininautilus` runs the same strategy through `mininautilus.live_actor.ActorSession` with the Binance Testnet adapter (`binance_live.py`). It refuses a binary without a matching release receipt and, for Testnet, a dirty tree. `--recovery-drill SECONDS` latches, reconciles and restarts a coordinator on purpose. `--rotate-orders N` (Testnet only) enables live rotation per [the ADR](adr-live-rotation.md): new risk stops at N hot orders, the session reconciles and rotates the journal, retired evidence moves to a cold index, and `--resume` follows the rotation chain using the `rotation-<journal>.json` baseline. Offline fault coverage: `tests/test_live_actor.py`, `tests/live_rotation.rs`.

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

The admission timestamp is captured only after validation/snapshot and queue
insertion. `admission` now measures scheduled time to that timestamp, while
`input_queue_age` measures it to writer start. Producer overrun is separate.
Source/event-kind queue samples retain at most 10,000 observations in each of
32 regular groups plus one overflow group. Bad clocks remain explicit unknown
samples. Reports produced before the admission-timestamp change measured
receive-to-writer under the old queue-age name; do not relabel their raw values
as the corrected endpoint or compare across harness versions.

A performance verdict requires five repetitions, nonoverlapping ranges and at least 10% median effect. A/A instability or guardrail uncertainty remains INCONCLUSIVE. The frozen 5 ms internal target and 0.5 ms Rust target can still fail on the current filesystem; passing correctness does not override them. CI has no absolute timing assertions.

## Long reliability coverage

```sh
python3 ops/runtime_soak.py --minutes 90 --seed 7 --run-dir runs/rv7-seed7
python3 ops/runtime_soak.py --minutes 90 --seed 17 --run-dir runs/rv7-seed17
python3 ops/runtime_soak.py --minutes 90 --seed 29 --run-dir runs/rv7-seed29
```

These are actual wall-clock durations. Running them concurrently is valid reliability coverage but introduces host contention and cannot supply controlled A/B timings. Default rate is ten quote observations per second; fault interval is ten minutes. The script exercises lost ACK, source gap, input overflow, Rust process kill, executor cancellation and late source epoch, then reconciles using the fake venue's independent history. An admitted but uncommitted input at a controlled crash stays explicitly counted as pending in that stopped generation; accepted venue fills are recovered via snapshot. The producer records missed deadlines rather than generating an unbounded catch-up burst.

Shutdown explicitly flattens this offline fixture, persists Kill, restarts and reconciles to prove the latch remains. This is not an automatic live liquidation policy. Raw journals, per-generation order traces, input/phase logs, OS samples, fake venue facts and report JSON remain under the run directory. Reports distinguish active/recovery/shutdown and expected versus unexpected faults.

Only queues, tasks and the strategy window are bounded by default. Core/mirror terminal orders, audit dedup evidence and the fake venue's history are retained; report their growth and restart peaks. The opt-in actor `--rotate-orders N` bounds Core/mirror/audit/runner hot orders to N plus open orders by rotating (see the ADR); without it there is no automatic live compaction or checkpoint shortcut. A finite 90-minute run is not a proof of indefinitely bounded RSS.

The active deadline is checked again after producer sleeps and after recovery;
no new scripted fault or quote is admitted after that deadline. Shutdown drains
the actor before inspecting its gate and writer process, so a failure discovered
by `finish()` is recovered before further IPC. Scripted fault order has its own
counter; unexpected recoveries never advance it and remain reported errors.
Fixture-only flattening and Kill/restart checks follow reconciliation.

System samples include bootstrap, each completed recovery, periodic active
work, and the final shutdown restart. VmHWM is the process lifetime maximum;
Rust starts a new lifetime at every restart. Recovery samples are taken after
reconciliation with the previous actor quiescent, before opening new admission.
They include retained indexes and the stopped actor's queue counts. Keep failed
cohorts and retries in separate directories; completing 90 minutes alone is
not a clean reliability pass.

## Recovery and history-cost investigation

```sh
python3 ops/recovery_probe.py --run-dir runs/rv4-recovery
python3 ops/audit_bench.py --run-dir runs/rv3-audit
python3 ops/history_runtime_probe.py --run-dir runs/rv7-history
```

Recovery probe varies quote-only archive 1,000/100,000 with fixed state/tail,
then holds archive 1,000 while varying active orders 100/1,000 and tail
20/2,000. It uses fresh copies for recover and records full bytes read,
Python frame/checksum/payload attribution, authoritative Rust replay/startup,
snapshot size/load, child VmHWM and Python lifetime peak RSS. The Python
inspection repeats work and is not an alternative recovery implementation;
process-launch cost is included in Rust startup timings. Archive-independent
startup cannot be claimed while full-prefix verification is required.

The history runtime probe builds real terminal order/fill journals at
0/1,000/100,000 with the existing paper matcher, then connects a bounded actor
and independent fake venue. It verifies late duplicate fills, successful new
execution and accepted-but-ACK-lost reconciliation without resubmission.
Its deliberately full-scan oracle makes it functional coverage, not a timing
benchmark. The final fixture position is one confirmed fake fill; it is not
a live trade or an unresolved order. Memory peaks include full state and
reconciliation; no compaction is enabled.

Audit benchmark separately tests terminal history 0/1,000/100,000 with zero delta work, and the frozen old quadratic audit at 1,000. Its p99 is the p99 of 1,000-call batch averages, not individual-event tail latency. Differential request fixtures and operation-count tests establish correctness and absence of hot-path scans.

## Using the coordinator

`Coordinator(apply, execute, clock_ms=logical_clock, dispatch_guard=guard)` expects an async `apply(Input)` callback returning effects only after Engine's durable response. One bounded worker owns all Engine calls; one executor serializes external account actions. `offer` is synchronous admission on the owning event loop; check its boolean result. Never create one task per incoming event, never ignore critical overflow, and never call Engine from an executor or producer. Source heads are FIFO; control/private/timer share fair service across sources ahead of market work.

A guard reads the mirror through the same writer worker, skips only confirmed terminal/non-uncertain cancels, and never mutates risk. The account logical clock prevents queued orders dispatching after expiry; a missing clock fails closed for actual Rust intents.

On `gated`, stop generating intents. The coordinator retains intent in the Rust journal and records unsent effects as abandoned; reservations stay. Resolve or cancel the single in-flight operation, stop the coordinator, reconcile through the sole Engine owner, and create a new coordinator. Do not reset a latch with a boolean or resubmit old IDs. A `BlockingExecutor` timeout leaves its worker busy; a replacement writer/executor cannot safely start while that worker could still send. Native async cancellation likewise requires a venue-specific outcome contract.

If the writer fails, abort and recover the journal. `Engine.close()` reaps killed children even after a buffered BrokenPipe; cleanup never retries submission. Rollback stops writers/executors first and preserves journal schema 1 / protocol 2. The new optional trace fields do not alter persisted frames.

Historical `send_batch` and `send_pairs` retain their existing protocol:
inputs are parsed before processing, then committed in order. A processing or
persistence failure can leave a committed prefix and terminate the response.
Treat a failed batch as uncertain, recover/query its sequence, and never retry
the whole batch blindly. The actor uses individual receive-time-aware calls;
no new live batching codec or accumulation delay is introduced. A future batch
runner must explicitly cap events/bytes/wait and carry each input's live time.

## LC lifecycle experiments and recovery

Read `docs/runtime-v2-lifecycle-results.md` before adopting the LC changes. Overall
LC acceptance is still FAIL. No W2/W3 long soak has passed in this phase.

Live rotation additionally requires an approved trust decision and the explicit
operator flag `--rotation-trust-contract current-segment-v1`. The actor stays
opt-in. `--batch-reconcile` uses bounded bulk discovery without changing the
absence fence or wallet/quantity checks. `--reclaim-cold` skips pinned readers;
without a bounded retry catalogue it does not prove a global disk bound.

With `--rotate-orders N`, `--rotate-bytes` defaults to 8 MiB. Quote-only history
therefore has a rotation trigger too. Disk preflight reserves the next cold file
plus max(2 GiB, 20% filesystem capacity). A low-headroom stop file is evidence of
a fail-closed stop, not proof of the final durable sequence; recover/reconcile
before trading. Do not delete stop evidence or uncertain orders to reopen a gate.

If a predecessor is already Closed and its named successor was not published,
`mininautilus repair-retaining CLOSED_PREDECESSOR EXACT_NAMED_SUCCESSOR` retries
publication. It refuses an unclosed predecessor and refuses to overwrite an
existing successor. A partial/empty successor or conflicting sidecar requires
manual inspection; preserve the files before any cleanup. Full journal corruption
is never silently repaired. A successor starts disconnected and needs venue
reconciliation; repair never resends an effect.

LC collection examples (fresh directories only):

```sh
cargo build --locked --release --example lifecycle_probe
python3 ops/lifecycle_evidence.py run --run-dir runs/lc-v1/new-case -- \
  target/release/examples/lifecycle_probe runs/lc-v1/new-fixture 1000000 20 gc
python3 ops/lifecycle_evidence.py analyse runs/lc-v1/new-case
python3 ops/lifecycle_startup_probe.py --journal CURRENT_JOURNAL \
  --binary target/release/mininautilus --output runs/lc-v1/new-startup.json --reps 5
```

`lifecycle_probe ... 20 gc pin` is an isolated reader-pin stress case, not a live
command. Verify capacity on the actual output/spool mounts first; `/tmp` may be a
small tmpfs. Stream compressed archives to S3, verify SHA on download, then remove
only task-owned local derived copies. No source/evidence may be overwritten.

The raw recorder is a prototype: complete LC0 receipt/schema validation and W0
host-gate qualification are still required. Short native probe results do not
certify full actor CPU, memory or trade-enabled availability.
