# Runtime v2 lifecycle acceptance: implementation and evidence

Created at: 2026-10-12T19:30:00+08:00 (Asia/Hong_Kong).
Updated at: 2026-10-12T19:34:33.538260+08:00.
Scope: [LC proposal](runtime-v2-lifecycle-acceptance-proposal.md), thresholds LC-v1.
Implementation: `15bc8ae`; baseline probe: `9939d32`; first streaming candidate: `03048ad`.

**Overall acceptance: FAIL.** Compatible implementation and diagnostic EC2 short probes
are delivered. Cold first-use verification and full-history rewrite still violate
LC-v1 budgets. The W2/W3 long soaks are **not started**, following proposal §9.6
(short gates first). Live rotation's changed trust contract also awaits explicit
owner approval; elapsed time or an opt-in flag does not provide approval.

## Delivered code

- Same MNCOLD01 byte format: merge cold history through bounded read/write buffers;
  do not materialise all archived orders/fills or a full-file serialization Vec.
  Full checksum verification remains; first lookup is still O(history).
- Fix the predecessor anchor after Closed/before Genesis crash: retry publishes
  the same genesis. Add `repair-retaining` for an already Closed predecessor only.
- Optional `reclaim_superseded_cold`: successor publication and verification precede
  disposal; shared reader locks pin old files. Journals are retained. A pinned
  generation is skipped; this is not a complete retry catalogue or global bound.
- Actor blocks new order admission at N on the writer, and also rotates quote-only
  segments at 8 MiB by default when rotation is enabled. Disk preflight reserves
  the planned cold output plus max(2 GiB, 20% capacity). A 0.5s monitor gates on
  low headroom and records a durable stop marker. Uncertainty is retained.
- Bounded opt-in Binance batch reconciliation discovers up to 1,000 orders and
  10,000 fills. Missing discovery rows still use exact client-ID lookup; identity,
  cumulative quantity, commissions, wallet balance, absence fence and quiescence
  checks remain. The offline 100-order oracle returns the same snapshot with
  2 bulk data requests instead of 300 individual data requests (account/open-order
  requests are outside those counts). This is not a live latency claim.
- Constant-space admission timing includes rotation pending, recovery and drills
  in downtime. Record full pause to fresh eligible quote, not disk rotation only.
  Report current-segment versus total cold+current history counts separately.
- Sidecar retries re-sync identical survivors; partial/conflicting survivors fail
  closed and are never overwritten. A dead writer no longer masks the failure
  with another shutdown BrokenPipeError.
- Raw resource recorder, independent W1 analysis and fresh-process startup probe.
  Earlier sampler's 100ms steal and coverage errors are disclosed; corrected
  collector uses complete >=1s tick intervals and a final sample. Old runs remain.

## EC2 W1 short evidence

Tokyo `i-015087878f29f14cd`, t4g.micro, 2 vCPU, 1 GiB, gp3 12 GiB,
3,000 IOPS / 125 MiB/s. Baseline A/A: 10 probes per H; interleaved original/
streaming: five per side per H (60 probes total). No venue calls were made.

| H | Baseline rotation median | Streaming rotation median | Baseline Rust HWM max | Streaming Rust HWM max |
|---|---:|---:|---:|---:|
| 1k | 15.49 ms | 14.82 ms | 2,564 KiB | 2,540 KiB |
| 100k | 118.62 ms | 44.03 ms | 12,816 KiB | 2,544 KiB |
| 1m | 1,091.02 ms | 325.54 ms | 105,044 KiB | 2,544 KiB |

At H=1m, first lookup median remains 113.42 ms. At least ~49 MB is read;
the LC first-read growth budget is 1 MiB. Rotation exceeds 250ms and grows more
than 2x versus 1k. Thus LC1-7 and LC2-3 FAIL despite the useful memory change.

These are **scoped observations**, not complete performance adoption proof:
short probes did not satisfy the 99% sampler coverage gate, one old 100ms steal
sample exceeded 5%, and W0 300s cohorts/CPU/throughput guardrails were not run.
Rust HWM is one process, not the full actor-group peak. The original probe's
replay duration is in-process recovery, not fresh-process startup or full resume.

Authoritative reviewed analysis:
[W1 report](../comparisons/results/lifecycle-20261012/w1-short-report-final.json).
Earlier report-generation versions are retained and superseded by this file.
[Host](../comparisons/results/lifecycle-20261012/host.json),
[credit samples](../comparisons/results/lifecycle-20261012/host-cloudwatch.json),
[archives](../comparisons/results/lifecycle-20261012/archives.json).
The original resource manifest source described the recorder checkout even for
an archived baseline binary; use the separate build command evidence and binary
hashes, not that field alone, to identify baseline source. Full LC0 provenance
acceptance remains INCONCLUSIVE.

## Twenty-cycle disk and fresh-process supplement

Final code `15bc8ae`, separate EC2 cohort. N=200, one million prebuilt retired
orders/fills, 20 actual rotations with explicit GC. No venue connection.

- No pinned readers: cold allocated maximum **49,197,056 bytes**; normal steady
  budget passes for this scoped exercise.
- Twenty pinned readers: **1,031,102,464 bytes** versus **213,561,344 bytes** steady
  budget: **LC1-5 FAIL**. Immediate-predecessor reclamation safely skips readers
  but has no bounded retry catalogue; this does not deliver a global disk bound.
- New payload **196,000 bytes**, cold logical writes **982,058,640 bytes** versus
  **35,514,432 bytes** budget: **LC1-6 FAIL**. Streaming did not solve write amplification.
- First lookups about **118 ms**, still over 100ms.
- Normal 20-cycle cgroup peak **113,250,304 bytes**, resource coverage **99.94%**,
  complete-interval steal p99 **0.92%**. The pinned case peak was **637,235,200 bytes**,
  coverage **99.90%**, steal **5.53%** (host gate FAIL); no latency adoption claim.
- Five fresh-process engine IPC startups per normal history: 1k **3.24–14.71 ms**;
  1m **3.18–3.32 ms**. This is engine startup, not venue resume. The supplement
  used current segment states/tails rather than the exact frozen tail=200 matrix;
  LC2-4 remains INCONCLUSIVE rather than a full AC PASS.

[Raw-derived disk report](../comparisons/results/lifecycle-20261012/disk-report.json).

## Correctness validation

`scripts/verify.sh` on the implementation: **VERIFY OK**, 78 Rust / 138 Python /
4 Node tests and 20 parity seeds. Prometheus was placed on PATH; no existing
optional test was disabled to get a pass. See
[validation index](../comparisons/results/lifecycle-20261012/validation.json).

The fault-injection build exercises 13 publication/GC crash boundaries three
repetitions each (39 process exits), preserving position and exact late dedup
or failing closed. Normal builds compile out these crash hooks. Real Linux
`/dev/full` write and `/dev/null` sync failures leave predecessor unchanged.
Sidecar publication tests cover four boundaries, three repetitions each; partial
and conflicting files are refused. New actor unit/integration cases inject writer
kill, gap, overflow, executor cancel and old source epoch, three repetitions each.
They supplement, but do not replace, W2 long-soak coverage.

The timeout-budget test now checks three separate budget allocations with
controlled events; 100/100 repetitions passed. Existing delivery timeout cases
remain. Journal replay example: position 0, gross PnL 12, 2 orders, 3 fills.

Verbatim verify tail:

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
VERIFY OK
```

The demo's position 2 demonstrates reservation/uncertainty preservation; it is
not a live position or the replay result. Hardware power loss is not tested.

## Acceptance ledger (24 criteria)

| AC | Status | Evidence / remaining requirement |
|---|---|---|
| LC0-1 | INCONCLUSIVE | Code/binary hashes and build logs exist; original recorder lacked complete per-binary receipts. |
| LC0-2 | INCONCLUSIVE | W1 repeats complete; corrected sampler requires new qualified W0 A/A; no full adoption claim. |
| LC0-3 | PASS | Old next-phase interpretations corrected; raw results/numbers retained. |
| LC1-1 | INCONCLUSIVE | Writer order cap and byte trigger covered; full EC2 actor count/caches matrix pending. |
| LC1-2 | INCONCLUSIVE | Single-component W1 cgroup/process observations exist; complete actor lifecycle peaks pending. |
| LC1-3 | INCONCLUSIVE | Rust HWM history growth removed; full actor process-group sweep and buffer telemetry pending. |
| LC1-4 | NOT_MEASURED | >=90min W2/W3 RSS trend not run. |
| LC1-5 | FAIL | Twenty pinned generations exceed steady disk budget; normal unpinned case passes only its scoped exercise. |
| LC1-6 | FAIL | Twenty cycles write 982,058,640 bytes, budget 35,514,432 bytes. |
| LC1-7 | FAIL | First lookup >100ms at 1m, first-read growth >>1MiB; warm lookup is separately reported. |
| LC1-8 | INCONCLUSIVE | Low-headroom gate/durable-stop test and preflight delivered; W2/W3 normal-path validation pending. |
| LC2-1 | BLOCKED | Testnet rotation trust decision and short gates pending. |
| LC2-2 | BLOCKED | Same; no live pause improvement asserted from request-count reduction. |
| LC2-3 | FAIL | Streaming 1m rotation >250ms and history ratio >2. |
| LC2-4 | INCONCLUSIVE | Fresh-process IPC startups measured; exact fixed-state/tail=200 matrix and approved integrity adoption pending. |
| LC2-5 | NOT_MEASURED | Complete fake/Testnet runner resume not collected. |
| LC2-6 | NOT_MEASURED | W0 CPU/latency/throughput/queue full guardrails not collected. |
| LC2-7 | INCONCLUSIVE | Runtime quiescence bound and durable stop delivered; full 60s/5s deadline matrix pending. |
| LC3-1 | INCONCLUSIVE | Short parity/dedup checks pass; final long-run trace/venue oracle pending. |
| LC3-2 | INCONCLUSIVE | New publication/sidecar/runtime cases delivered; complete corruption/index/anchor matrix still needs expansion. |
| LC3-3 | BLOCKED | Proposal short-gate precondition fails; W2 not started. |
| LC3-4 | BLOCKED | Same plus trust approval; W3 not started. |
| LC3-5 | INCONCLUSIVE | Short actor hard-stop counts checked; full per-source/epoch long-run accounting pending. |
| LC3-6 | PASS | VERIFY OK, replay and 100 deterministic timeout-budget repetitions. |

Disk/fresh-startup supplements amend only the relevant observations/status rows;
no source or result from a failed run is discarded. NOT_MEASURED does not mean PASS.

## Artifact handling and operational limitations

Evidence summaries, source manifests, hashes and build metadata are committed.
W1 raw logs plus generated journal/cold fixtures are stored under the existing
same-region stack bucket `runs/lc-v1/`. Before/AB archives were downloaded and
analysed. Each fixture archive is stream-downloaded and SHA-verified before this
task's local derived fixture is removed. No other session's files are deleted.

An attempted combined archive failed ENOSPC on `/tmp` tmpfs (459 MiB); root EBS
still had ~3 GiB. The partial task archive was deleted; the originals were intact.
Backup was changed to per-run streaming upload/verification, restoring root free
space to ~5 GiB. This failure is recorded; future preflight must inspect the actual
spool mount. Pre-existing Prometheus/tools/data in `/tmp` were not deleted.

Existing bucket retention is not silently changed to 30 days. Requested 30-day
retirement needs a scoped operator cleanup/lifecycle decision; current object
availability and hashes are recorded. No new EC2, IAM, NAT or paid infrastructure
was provisioned. No Binance order was sent by this task.

## Next concrete actions

1. Decide the current-segment/first-use trust contract using the
   [decision record](adr-lifecycle-contract-decision.md). Do not equate opting in
   to an unapproved integrity change. Live runner now requires the explicit
   `--rotation-trust-contract current-segment-v1` operator choice as well.
2. Design the next cold format/retirement catalogue. Streaming fixes RAM but not
   all-history checksum and rewriting. Keep full integrity guarantees or obtain
   a concrete decision before changing verification coverage/timing. Test pinned
   generations, crash retry and reclamation before claiming a disk bound.
3. Complete LC0 receipts/schema and rerun qualified W0/W1 short gates with the
   corrected sampler. Keep historical short-run observations separate.
4. After short gates pass, run final W2 three seeds and W3 two Testnet sessions;
   validate complete runner resume and true trade-enabled downtime.

No multi-instrument, new venue, binary IPC or sub-ms storage optimisation is added
until this lifecycle stage has a defensible acceptance result.

## Host handoff

The EC2 checkout, original engine binary and original receipt were restored to
their pre-task state (`80809a2`). The instance remains running as originally
found. Task fixtures were stream-backed-up and verified before cleanup; old
Prometheus and other pre-existing /tmp artifacts were preserved. Root free space
is about 4.9 GiB. See host-restoration.json and fixture-archives.json.

This report is a partial implementation plus failed short-gate result, not a
claim that every task or acceptance criterion is complete. The ledger is the
source of truth for remaining work.
