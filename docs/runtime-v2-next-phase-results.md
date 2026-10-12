# Runtime v2 next phase: results

- Created at: 2026-10-12T13:00:00+08:00 (Asia/Hong_Kong)
- Proposal: [runtime-v2-next-phase-proposal.md](runtime-v2-next-phase-proposal.md) v1.2 (§6 decides which appendix ACs apply)
- Thresholds: [perf-thresholds.md](perf-thresholds.md) version 1, frozen at `b986486` before any comparison
- Branch: `codex/cross-platform-backtest-audit`

Status words: PASS, FAIL, INCONCLUSIVE, DEFERRED (conditional item with a reason), BLOCKED (an
external precondition is missing), TODO (not done). One section per outcome goal, then the ledger.

## Summary

| Goal | Result |
|---|---|
| G1 trustworthy performance conclusions | **Measurement complete** on the deployment host: 4 cases × 5+5 interleaved A/A, steal ≤ 0.6%, CPU credits never fell. 46 of 48 case×metric rows were A/A-stable. Serial tick-to-effect p99 was unstable. **No optimisation was adopted.** The one profiled candidate was bounded below 1% by a microbenchmark and was not run. The v0 absolute targets still FAIL on this host (Rust request p99 4.6 ms against 0.5 ms). The actor CPU guardrail still FAILs in the slow-I/O case. |
| G2 actor in the live runner | Offline fault tests PASS (12 cases). Release provenance is enforced. The Testnet soak result is below (§3). |
| G3 bounded long-run resources | ADR answers §4.1 and §4.2. Live rotation with a cold dedup index is implemented and tested (Rust 7, Python 4). It was exercised on Testnet (3 rotations in the smoke). Probe results are in §4. |

## 1. G1: measurement

**Cohort.** `runs/aa-v1-20261012` on EC2 t4g.micro `i-015087878f29f14cd`, ap-northeast-1.

| Item | Value |
|---|---|
| Source | `e788145`, clean |
| Binary | release receipt sha256 `1e6bf6ed…8a6d` |
| Platform | aarch64, 2 vCPU |
| Manifest | `comparisons/runtime-v2/manifest-v1.json`: 70 s runs, 10 s warm-up, 100 quotes/s, `--sync outbox` |
| Run order | Round-robin over repetition × case × side |
| Runs | 41 in total: 40 exited 0; the overload run exited 1, as designed (see below) |
| Steal | ≤ 0.59% per run |
| CPU credit balance | 201.7 → 204.6 during the cohort (no exhaustion) |
| Archive | `runs/aa-v1-20261012.tgz` in the stack bucket, sha256 `078122e4…f195` |
| In git | Summaries in `comparisons/results/aa-v1-20261012/` |

**A/A stability.** Medians of 5 runs per side.

| Case | Unstable metrics | Total CPU/event a1 / a2 | Internal tick-to-effect p99 a1 / a2 |
|---|---|---|---|
| serial-immediate | internal tick-to-effect p99 (+15.2%) | 747 / 763 µs | 3.72 / 4.29 ms |
| actor-immediate | none | 821 / 819 µs | 4.27 / 4.30 ms |
| serial-slow (20 ms I/O) | internal tick-to-effect p99 (ranges disjoint) | 732 / 740 µs | 28.3 / 27.4 ms |
| actor-slow (20 ms I/O) | none | 926 / 918 µs | 4.21 / 4.31 ms |

The serial tick-to-effect p99 cannot support any adoption claim on this host. Every CPU,
throughput, RSS and Rust/IPC metric was A/A-stable in all four cases.

**Absolute targets.** v0 is the original target; v1 is the version-1 threshold.

| Metric | v0 target | v1 rule | Observed (median of 10) | v0 | v1 |
|---|---|---|---|---|---|
| Internal tick-to-effect p99, immediate | ≤ 5 ms | ≤ 5 ms | serial 3.77 ms; actor 4.28 ms | PASS | PASS |
| Internal tick-to-effect p99, 20 ms I/O | ≤ 5 ms | ≤ 5 ms | serial 28.1 ms; actor 4.24 ms | serial FAIL; actor PASS | same |
| Full Rust request p99 (incl. fsync) | ≤ 0.5 ms | reported, v0 value kept | 4.58–4.70 ms | **FAIL** | **FAIL** |

Rust before-response p99 is 2.12 ms. The full request on gp3 adds the response path and the fsync.
This is the storage floor on this host, so 0.5 ms cannot be reached here without changing
durability. Nothing was renamed or excluded to reach it.

**Actor vs serial on the same host.** Interleaved; 10 runs per side
(`ab-serial-vs-actor-*.json`).

| Metric | Immediate | 20 ms I/O |
|---|---|---|
| Internal tick-to-effect p99 | +13.5%, INCONCLUSIVE (serial A/A unstable) | **−84.9%, improved** |
| Input queue age p99 | −10.7%, guardrail PASS | **−97.6%, improved** |
| Executor queue age p99 | +175% (0.27 → 0.74 ms), guardrail FAIL | +175%, FAIL |
| Total CPU / completed event | +8.6%, INCONCLUSIVE | **+24.9%, FAIL** |
| Total CPU / market input (7,000 per run) | +8.6% | +14.7% |

The slow-I/O event mix differs between runtimes: serial offered 9,933 inputs and the actor 9,122,
with different order counts. Per-completed-event CPU in that case is therefore partly a workload
difference. The per-market-input figure is shown alongside it; neither is presented alone. The
RV-era actor CPU guardrail FAIL is **not repaired**.

**Overload (functional only).** At 2,000/s: 20,033 offered, 187 admitted, 19,846 rejected; queue
high-water 95 ≤ 128. The run ended fail-closed on `source_sequence_gap`. The bench keeps
numbering refused market inputs, so the first admitted input after an overflow is a gap. Queues
stayed bounded and nothing was dispatched behind the latch. That matches ADR2, but the overload
case measures no latency.

**NP1 investigation (actor CPU).** py-spy on 20 s of steady-state actor-slow vs serial-slow.

| Area | Actor | Serial |
|---|---|---|
| `Coordinator.offer` | ≈ 28% of busy samples (JSON encode 16%, snapshot decode 12%) | — |
| `SerialRuntime.offer` | — | ≈ 25% of busy samples; it encodes `Input.size` up to three times |
| Executor/loop handoffs | Present in both | Present in both |

- Candidate C1 replaces the admission snapshot's `json.loads` with a scalar-tree copy. A
  microbenchmark puts that at about 4.7 → 2 µs per input. Against 820–920 µs/event this is under
  1%. It cannot reach the 10% adoption bar, so it was **not run**. Running it would only add noise
  to the evidence.
- The remaining cost is per-event thread and event-loop handoffs, which both runtimes pay. Reducing
  them means batching writer calls, and ADR2 forbids coalescing. That needs a design decision, not
  a single-factor candidate.
- Result: investigation complete, nothing adopted, actor stays opt-in.

## 2. G2: offline

The actor runner is `python/mininautilus/live_actor.py`, with the Binance adapter in
`binance_live.py`; it runs as `sma_spot.py --runtime actor`. `tests/test_live_actor.py` uses the
real Rust engine and a blocking fake venue.

| Fault | Test | Result |
|---|---|---|
| Full market buffer | capacity 40 / reserve 32, flood | PASS: drops cause a REST resync, high-water ≤ 40, no gate, position matches the venue |
| Slow execution | 200 ms submit | PASS: market inputs keep being applied while the call is outstanding |
| Late SDK result | submit returns after the 0.3 s timeout | PASS: `delivery_uncertain` latch, waits for quiescence, reconciles, no resubmission |
| Unresolved call | submit never returns | PASS: fails closed after 0.4 s, no reconciliation attempted |
| Market disconnect | stream raises | PASS: reconnects, warms up again, MarketUnavailable sent |
| Private poll failure | poll raises once | PASS: latch, reconcile, new coordinator |
| Lost before venue | submit raises before sending | PASS: proven absent → Rejected, never resubmitted |
| Recovery drill | operator latch at 1 s | PASS |

Release provenance:

- `--runtime actor` requires `--receipt`, and the binary hash must match a release receipt for the
  current Rust inputs.
- Testnet runs also require a clean tree.
- `ops/soak.py` records the binary path, hash and profile, or marks `provenance_complete: false`.

## 3. G2: Testnet

**Smoke.** `runs/smoke-actor-20261012` on EC2: 300 s, `--rotate-orders 10`, one drill.

| Item | Value |
|---|---|
| Closed candles | 261 |
| Orders | 32 |
| Coordinators | 5 |
| Reconciliations | 8 |
| Rotations | 3 (`events-1..3.jsonl` + `.cold`) |
| Drills | 1 |
| Hot orders | ≤ 10 |
| Queue high-water | input 2, effects 1 |
| Shutdown | reconciled, position 0 |
| Binary | release, sha256 `e8ab3699…1a11` |

The smoke found one issue, now fixed in `2ffc97e`: an account request per candle on top of the
quote and polling, against a 4 req/s budget.

**90-minute soak.** `runs/live-actor-20261012`. TODO: filled in after the run.

## 4. G3: retention

ADR: [adr-live-rotation.md](adr-live-rotation.md). Rust coverage is in `tests/live_rotation.rs`.

| Test | What it shows |
|---|---|
| Carry and cold reference | History is freed; position, floor and cold reference are carried |
| Exact dedup | A late duplicate fill, cancel or ack for a retired order consumes its sequence and changes nothing; replay works without the cold file |
| Contradictions | Each one gates |
| Corrupt index | Verdict is `Unavailable` and the session gates |
| Forged verdicts | A caller-supplied `Retired` is refused |
| Second rotation | The index is extended; rerunning after a crash between Closed and the successor is idempotent |
| Stray cold file | Ignored |

Python coverage (`LiveRetentionTests`):

- Hot orders stay at or below N + 1, and late retired duplicates never gate.
- An open order past the delay is reconciled and the journal rotated.
- No barrier means the session stops without rotating.
- Resume follows the rotation chain.

Probe (`ops/retention_probe.py`): TODO, results to follow.

## 5. Ledger (proposal §6)

| Item | Status | Evidence / reason |
|---|---|---|
| §2.1 decision table frozen | PASS | `docs/perf-thresholds.md` v1 at `b986486`, before the cohort |
| NP0-AC1 clean source/receipt | PASS | `provenance.json` per run; dirty or receipt mismatch refused (`runtime_bench`, `sma_spot --receipt`) |
| NP0-AC2 frozen manifest, 10 s warm-up / 60 s measure | PASS | `manifest-v1.json`; immediate, 20 ms and overload cases (1 s fault case: functional tests only) |
| NP0-AC3 A/A ≥5, interleaved | PASS (measurement) | 46/48 stable; serial tick-to-effect p99 unstable, not usable for adoption |
| NP0-AC4 correlated stages, no p99 subtraction | PASS | `orders.jsonl` per-request traces (`MINI_ORDER_TRACE`), unchanged harness |
| NP0-AC5 keep failed runs, report all guardrails | PASS | overload run (exit 1) kept; all metrics in `compare-*.json` |
| Common gate v0 absolute targets | **FAIL** | Rust request p99 4.6 ms vs 0.5 ms; serial-slow tick-to-effect 28 ms |
| NP1-AC1 profile | PASS | py-spy steady-state, §1 |
| NP1-AC2 ≤3 single-factor candidates | PASS (none adopted) | C1 bounded <1% by microbenchmark, not run (§1) |
| NP1-AC3 new vs serial reference | PASS (reported) | `ab-serial-vs-actor-*.json`: guardrail FAIL persists in slow case |
| NP1-AC4 runtime fault tests still pass | PASS | `tests/test_runtime.py` in every `VERIFY OK` |
| NP1-AC5 actor stays opt-in | PASS | serial remains default; `--runtime actor` explicit |
| NP2-AC1 ADR with trust contract | PASS | `docs/adr-live-rotation.md` §2, §5 |
| NP2-AC2 terminal IDs not TTL-dropped; evidence missing fails closed | PASS | cold index; `Unavailable` gates (`corrupt_cold_index_fails_closed`) |
| NP2-AC3 (replaced by §4.1 item 4) | see §4 | hot counts bounded N + open; RSS per segment reported, HWM not used |
| NP2-AC4 replay oracle equality and crash points | PASS (scoped) | journaled verdicts replay without the cold file; crash after Closed/before successor; stray cold before Closed. Not covered: power-loss hardware, ENOSPC during cold write |
| NP2-AC5 cold miss bounded | PASS | O(log n) positioned reads only for retired reports; no queue of lookups |
| NP2-AC6 fast startup | PASS (scoped) | recovery replays the current segment only (§4 probe); this is the ADR trust contract, not a checkpoint of one long journal |
| NP2b bounded hot state (now mandatory) | PASS (opt-in flag) | `--rotate-orders N`; without the flag history still grows |
| NP3 durability candidates | DEFERRED | storage floor measured (gp3 fsync ≈ 2.5 ms of 4.6 ms p99); no candidate run: changing durability needs a design choice and NP0 shows the absolute target unreachable on gp3 without it |
| NP4-AC1 bounds | PASS | coordinator bounds; one market thread, one poll worker, one venue worker; WS `max_queue=8` |
| NP4-AC2 gap/duplicate/reconnect/late epoch/lost ACK fixtures | PASS | `tests/test_live_actor.py`, `tests/test_runtime.py` |
| NP4-AC3 cancel ≠ external cancel; late result reconciled; no replacement while busy | PASS | `test_late_sdk_result_*`, `test_unresolved_venue_call_fails_closed_*` |
| NP4-AC4 actor/serial state parity | PASS (existing) | `tests/test_runtime_parity.py` (offline); live parity is not claimed |
| NP4-AC5 capability gaps listed | PASS | Binance adapter: REST polling for private state, local delivery sequence only, 4 req/s budget |
| NP5-AC1 verify | PASS | `scripts/verify.sh` → `VERIFY OK` on each commit (one intermittent pre-existing timing test failure observed once, then 33/33 passes; not loosened) |
| NP5-AC2 three 90-min offline fault soaks | DEFERRED | RV7 cohort (`c71ec6a`) is the latest; the coordinator is unchanged in this phase, the new runner is covered by fault tests and a Testnet soak |
| NP5-AC4 ≥3 real rotation cycles | PASS | smoke 3, container probe 65 rotations |
| G2 Testnet soak | TODO | §3 |
