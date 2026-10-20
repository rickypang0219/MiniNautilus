# Runtime v2 next phase: results

- Created at: 2026-10-12T13:00:00+08:00 (Asia/Hong_Kong)
- Proposal: [runtime-v2-next-phase-proposal.md](runtime-v2-next-phase-proposal.md) v1.2 (§6 decides which appendix ACs apply)
- Thresholds: [perf-thresholds.md](perf-thresholds.md) version 1, frozen at `b986486` before any comparison
- Branch: `codex/cross-platform-backtest-audit`

Status words: PASS, FAIL, INCONCLUSIVE, DEFERRED (conditional item with a reason), BLOCKED (an
external precondition is missing), TODO (not done). One section per outcome goal, then the ledger.

## LC review correction (2026-10-12)

The summaries below record the prior phase at `b0223f3`; their numbers are retained.
The lifecycle review supersedes several interpretations:

- G3 is **partial**, not full long-run-resource acceptance. The old cold merge
  materialised all retired records and a full output buffer, and each rotation
  rewrote complete history. New LC measurements include rotation peaks and disk.
- The serial-slow tick-to-effect A/A was unstable. Its −84.9% is an observation,
  not adoption evidence, even though the original table said improved.
- gp3 sync measurements show this configuration missed 0.5ms; they do not prove
  every durability-preserving implementation is incapable of reaching it.
- Batching and coalescing are different. Ordered batching need not discard inputs
  or weaken the durable barrier; no candidate has yet established its benefit.
- Warm cold lookup is O(log n), but its first open performs an O(history) checksum.
  The original NP2-AC5 PASS did not cover that first-use cost.
- 8ms measured current-segment engine replay, not full runner/venue resume.
  Archive-independent adoption is **BLOCKED** pending an explicit trust decision.
- Tests used process crashes and a limited cold-write fault matrix; this does not
  establish hardware power-loss safety or complete new-runner soak coverage.

See [LC results](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/runtime-v2-lifecycle-results.md) for the current itemised verdicts.
No old run, raw data, numerical result or original commit is deleted.

## Historical next-phase summary (interpretations superseded above)

| Goal | Result |
|---|---|
| G1 trustworthy performance conclusions | **Measurement complete** on the deployment host: 4 cases × 5+5 interleaved A/A, steal ≤ 0.6%, CPU credits never fell. 46 of 48 case×metric rows were A/A-stable. Serial tick-to-effect p99 was unstable. **No optimisation was adopted.** The one profiled candidate was bounded below 1% by a microbenchmark and was not run. The v0 absolute targets still FAIL on this host (Rust request p99 4.6 ms against 0.5 ms). The actor CPU guardrail still FAILs in the slow-I/O case. |
| G2 actor in the live runner | **PASS on the §3 acceptance items.** Offline fault tests pass (12 cases), and release provenance is enforced. The 95-minute Testnet soak placed 506 orders with 496 fills, ran 2 drills and 5 rotations with 0 REST errors, and its final reconciliation agreed with the venue. The absolute latency checks still FAIL (single run, limited by REST pacing). |
| G3 bounded long-run resources | **PASS (opt-in `--rotate-orders`).** The ADR answers §4.1 and §4.2, and live rotation keeps exact dedup in a cold index. Tests: Rust 7, Python 4 plus 3 offline 90-min soaks (≈ 160 rotations each). On Testnet: 5 rotations in 95 min and resume across the chain. On t4g.micro, hot orders stayed ≤ N + open, the Python RSS slope was 0.34 MiB/h over 30 min, and recovery took 8 ms vs 684 ms. The trust contract changed and needs the owner's review (§6). |

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

**90-minute soak.** `runs/live-actor-20261012`, single run.

```
ops/soak.py --minutes 95 -- examples/sma_spot.py --mode testnet --runtime actor \
  --receipt target/runtime-v2-build.json --binary target/release/mininautilus \
  --interval 1s --fast 3 --slow 8 --max-orders 100000 --order-ttl 10 --sync outbox \
  --rotate-orders 100 --recovery-drill 1800 --recovery-drill 3600
```

| Item | Value |
|---|---|
| Source | `2ffc97e`, clean |
| Binary | release receipt, sha256 `e8ab3699…1a11`; `provenance_complete: true` |
| Archive | `runs/live-actor-20261012.tgz` in the stack bucket, sha256 `eabfbf0a…ef9f` |
| In git | Summaries in `comparisons/results/live-actor-20261012/` |

| §3 acceptance item | Result |
|---|---|
| 1. Existing Binance Testnet adapter; actor is opt-in | PASS: `--runtime actor`; serial stays the default |
| 2. Release binary verified at start | PASS: receipt hash and Rust inputs matched; clean tree |
| 3. ≥ 90 min with real orders and fills | PASS: 5,704 s, 5,251 closed candles, 506 orders (IDs 1–506, all unique), 496 fills, 11 cancels, 0 REST errors |
| 4. Market data keeps flowing during slow execution; queues bounded | PASS: 11 market inputs were applied while a venue call was outstanding (out of 517 executions); input high-water 3, effects 1; WS event lag p99 48 ms |
| 5. ≥ 1 recovery, no duplicate send, no unexplained uncertainty | PASS: 2 drills + 5 rotation barriers = 13 reconciliations across 8 coordinators; 0 reconciliation failures; 0 uncertain orders in the final segment |
| 6. Final reconciliation agrees with the venue | PASS: shutdown reconcile left the engine Healthy at 100 lots with the base-balance identity verified. A later `--resume --reconcile-only` followed the 5-segment chain using the sidecar baseline and reconciled again |

**Retention on Testnet.**

- 5 rotations, each at 100 hot orders.
- Core, mirror, audit and runner each held ≤ 100 hot orders; the final segment holds 6.
- The cold index `events-5.jsonl.cold` (500 orders, 490 fills) passes `verify-cold`.
- Each rotation barrier took about 75 s: the Binance REST reconcile of 100 orders at the fixed
  0.25 s pacing.
- As a result the engine was Healthy for 92.1% of the run. The soak's gate check (≥ 99%) FAILs.
- Shutdown reconciliation of the final 6-order segment took about 7 s.

**Observations, not performance claims.** This is one run, and the Rust provenance of the
2026-10-11 serial run is incomplete.

| Metric | Actor run | 2026-10-11 serial run |
|---|---|---|
| Tick-to-trade p50 / p99 | 37 / 484 ms | 500 / 990 ms |
| Shutdown reconciliation | ≈ 7 s | 344 s |

The soak's absolute checks:

| Check | Value | Target | Status |
|---|---|---|---|
| Rust request p99 | 3.84 ms | 0.5 ms (v0) | FAIL |
| IPC p99 | 4.05 ms | 2 ms | FAIL |
| Tick-to-trade p99 | 484 ms | 5 ms | FAIL (REST pacing) |
| Drift | 1.02 | < 2 | PASS |
| REST errors | 0 | 0 | PASS |

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

Offline fault soaks (§5 NP5-AC2) also ran live rotation throughout: 160–161 rotations per seed
at N = 200 with hot orders ≤ 200.

Python coverage (`LiveRetentionTests`):

- Hot orders stay at or below N + 1, and late retired duplicates never gate.
- An open order past the delay is reconciled and the journal rotated.
- No barrier means the session stops without rotating.
- Resume follows the rotation chain.

**Probe** (`ops/retention_probe.py`).

- Real release engine and the actor runner, with `FakeLiveAdapter` (no network).
- Fake candles every 5 ms, about 1,000 orders/min.
- Samples once per second, streamed to disk. The probe holds O(1) state: online slope and
  per-minute maxima.
- In the rotating case the fake venue forgets retired orders, so Python RSS measures the runner.
- In the no-rotation case Python RSS includes the fake venue's own history.
- Summaries are in `comparisons/results/retention-20261012/`.

| Run | Host / commit | Orders | Rotations | Hot orders max | Python RSS (per-minute max) | Python slope after 60 s | Rust RSS | Recovery of final journal |
|---|---|---|---|---|---|---|---|---|
| Rotating N = 200, 30 min | EC2 t4g.micro / `6331a39` | 29,260 | 146 | 200 | 30.0 → peak 31.4 → 30.0 MB | **0.34 MiB/h** | 3.2–7.7 MB, slope −2.1 MiB/h | 8.2 ms (399 KB segment) |
| No rotation (comparator) | EC2 / `80809a2` | 7,553 in 7.5 min | 0 | 7,551 | 31.4 → 58.8 MB | 241 MiB/h | 13.9 MiB/h | 684 ms (53.6 MB journal) |
| Rotating N = 200, 20 min | container (4 vCPU) / `cee0fd3` | 26,093 | 130 | 200 | 29.7 → 31.3 MB | 3.8 MiB/h* | flat | 6.7 ms |

\* This run still kept every sample in memory; that list alone accounts for about 1.8 MiB/h. It
was fixed in `6331a39` before the EC2 run.

**Verdicts against thresholds v1.**

| Criterion | Result |
|---|---|
| Hot object counts | **PASS.** Bounded by N plus open orders, with no growth over 146 rotations. |
| RSS slope ≤ 1 MiB/h | **PASS** on the deployment host: 0.34 MiB/h over 30 min. This is one run; it is not a proof of indefinitely bounded RSS. |
| Recovery | **PASS** under the ADR trust contract: 8 ms for the current segment against 684 ms for one long journal. Archive validation moved to rotation time and the lazily checked cold index. |

The no-rotation comparator failed closed twice on t4g.micro (after 209 s, and after 7.5 min
once a fixture O(history) poll was fixed).

- Mechanism: periodic writer stalls overflowed the 95-slot market lane, and repeated resyncs
  exhausted the reconnect budget (`market_reconnect_budget_exhausted`).
- That is the designed fail-closed path.
- Cause of the stall: suspected to be heap-size-dependent pauses (for example full GC). Not
  proven, and not pursued, because this is the comparator, not the candidate.

`tracemalloc` after `gc.collect()` found no coordinator retained across 12 rotations. Remaining
growth came from the bounded tick-to-submit deque and the fixture's submit log.

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
| NP2-AC3 (replaced by §4.1 item 4) | PASS (one run per host) | hot counts ≤ N + open over 146 rotations; Python RSS slope 0.34 MiB/h on t4g.micro over 30 min; HWM reported, not used as evidence |
| NP2-AC4 replay oracle equality and crash points | PASS (scoped) | journaled verdicts replay without the cold file; crash after Closed/before successor; stray cold before Closed. Not covered: power-loss hardware, ENOSPC during cold write |
| NP2-AC5 cold miss bounded | PASS | O(log n) positioned reads only for retired reports; no queue of lookups |
| NP2-AC6 fast startup | PASS (scoped) | recovery replays the current segment only (§4 probe); this is the ADR trust contract, not a checkpoint of one long journal |
| NP2b bounded hot state (now mandatory) | PASS (opt-in flag) | `--rotate-orders N`; without the flag history still grows |
| NP3 durability candidates | DEFERRED (investigation only) | storage floor measured: journal_sync p99 4.97 ms on gp3 in the Testnet soak, full Rust request p99 4.6 ms in the cohort; no candidate run — every option changes the durability contract or the host (owner decision, §6); v0 0.5 ms stays FAIL |
| NP4-AC1 bounds | PASS | coordinator bounds; one market thread, one poll worker, one venue worker; WS `max_queue=8` |
| NP4-AC2 gap/duplicate/reconnect/late epoch/lost ACK fixtures | PASS | `tests/test_live_actor.py`, `tests/test_runtime.py` |
| NP4-AC3 cancel ≠ external cancel; late result reconciled; no replacement while busy | PASS | `test_late_sdk_result_*`, `test_unresolved_venue_call_fails_closed_*` |
| NP4-AC4 actor/serial state parity | PASS (existing) | `tests/test_runtime_parity.py` (offline); live parity is not claimed |
| NP4-AC5 capability gaps listed | PASS | Binance adapter: REST polling for private state, local delivery sequence only, 4 req/s budget |
| NP5-AC1 verify | PASS | `scripts/verify.sh` → `VERIFY OK` on each commit (one intermittent pre-existing timing test failure observed once, then 33/33 passes; not loosened) |
| NP5-AC2 three 90-min offline fault soaks | PASS (reliability only) | `ops/actor_soak.py`, seeds 7/17/29 concurrently at `c671a76` (clean, release receipt `4b8dc267…`): each 5,400 s, ≈ 32,200 orders, 160–161 rotations, 46–55 injected faults (late result 10–14, lost before send 9–13, poll failure 11–17, stream drop 10–13, drill 11–13); all checks pass — no error, shutdown reconciled, position = venue, no resubmission, only injected latch reasons. `comparisons/results/actor-soak-20261012/` |
| NP5-AC4 ≥3 real rotation cycles, retained counts and RSS per segment | PASS | Testnet smoke 3, Testnet soak 5, EC2 probe 146, offline soaks 160–161 each; §4 |
| G2 Testnet soak (§3 items 1–6) | PASS | §3; single run |
| Soak absolute checks (Rust p99, IPC, tick-to-trade, healthy ≥ 99%) | FAIL | §3: storage floor, REST pacing, rotation barriers |

## 6. Needs the owner's decision

- **Architecture contract change.** `docs/architecture.md` previously said automatic live rotation
  is refused. It now describes opt-in live rotation under the ADR.
  - Startup validates only the current segment.
  - Old segments are trusted via rotation-time validation and the checksummed cold index.
  - The proposal (v1.2 §6) made live compaction conditional on §4.1/§4.2 being answered and
    tested. That condition is met, but the trust contract itself changed.
  - CLAUDE.md asks for explicit human approval of a weakened invariant. Please confirm or reject.
    The feature is behind `--rotate-orders` and is off by default.
- **v0 absolute targets.** Rust request p99 ≤ 0.5 ms is out of reach on gp3 with per-order fsync
  (sync stage p99 ≈ 5 ms). Reaching it needs a durability or storage decision, such as a
  non-burstable host with faster storage or group commit. Under thresholds v1 that host requires
  your price approval.
- **Actor CPU.** The slow-I/O guardrail (+25% per event, +15% per market input) is unresolved.
  Reducing per-event thread and loop handoffs would mean batching, which ADR2 forbids. It needs a
  design decision.
- **Rotation barrier cost on Binance.** About 75 s gated per 100 orders, because REST
  reconciliation runs at the fixed 0.25 s pacing. Options:
  - a larger N;
  - rotating only when flat or idle;
  - WebSocket user-data reports, which change the adapter contract.
- **EC2 host.** `i-015087878f29f14cd` is still running (≈ USD 12.69/month fixed).
