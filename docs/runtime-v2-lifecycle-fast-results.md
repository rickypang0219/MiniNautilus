# LC-v2-fast implementation and acceptance results

Created at: **2026-10-13T02:01:15+08:00 (Asia/Hong_Kong)**. Threshold: **LC-v2-fast**, unchanged from proposal v2.1. Branch: `codex/cross-platform-backtest-audit`. Implementation checkpoint: `74a27d7`.

**Overall: FAIL / not fully accepted.** Numerical storage and fresh-runner budgets are within limits on Tokyo EC2. Host steal and fresh swap I/O fail qualification. Formal three-seed 900s fake integration and two 900s Testnet runs remain BLOCKED by short gates. No Testnet orders were sent in this phase. Owner approval for root/path verification is recorded and is not the blocker. Performance adoption remains INCONCLUSIVE; no qualified A/A or interleaved A/B was completed.

Sources: [proposal v2.1](runtime-v2-lifecycle-fast-iteration-proposal.md), [format ADR](adr-cold-pages-v2.md), [operations](runtime-v2-operations.md), [machine ledger](../comparisons/runtime-v2/lifecycle-fast-ledger.json), [raw index](../comparisons/runtime-v2/lifecycle-fast-raw-index.json). Ledger PASS rows are explicitly scoped observations, not an overall AC or rollout decision. Older LC-v1 results remain unchanged.

## Implementation delivered

| Task | Concrete change | Acceptance boundary |
|---|---|---|
| F0 | Separate clean engine/example receipts; historical engine verified against every Rust input in its immutable Git commit; actual/planned sampling and missed deadlines; independent resource/latency/storage/fault analysis | Tooling improved; complete eight-class schema/exact working-buffer telemetry and qualified A/A remain incomplete |
| F1 | MNCOLD02 immutable 4KiB ordered pages, batched copy-on-write, bounded tree fanout; legacy read/write defaults retained | Root/path mode explicitly approved for isolated Testnet; unread-page damage can be discovered later; manual full scrub available |
| F2 | Durable retirement catalogue, one-second actor retry, budget/debt preflight, per-root pins and lifetime store leases | Single old reader and20-generation pins stay readable. Unregistered readers also prevent shared-page deletion. One missing catalogue registration can be repaired from bound parent; larger unknown gaps fail closed |
| F3 | Five fresh Python interpreters and Rust children per H; launch-to-eligible timing includes provenance checks, engine/root boot, retired duplicate query, reconciliation and SMA3/8 warm-up | Fixed hot0/tail200 fixtures; current journal explicitly supplied. Arbitrarily long sidecar-chain resolution and aggregate buffer high-water remain unqualified |
| F4 | Page/root/catalogue/GC/migration crash matrix; independent fake cash/position and durable-submit oracle; bounded source/epoch accounting; hard10,000 fill cap; resource guard and truthful stop-flush status | New fault recovery requires matching gate evidence before another fault. Corrected60s diagnostic covers all21 faults; it is not a900s acceptance run |
| F5 | Testnet actor flags, contract record and release artifacts prepared | Two live runs not executed because short gates fail; no secret values copied into artifacts |
| F6 | Immutable raw archives, hashes, per-metric ledger, costs/retention record and host handoff | Restoration and archive receipts below; no new stack, ECS deployment or persistent resource |

Known cold damage now latches a synced integrity-stop marker. Restart/reconciliation cannot forget it; complete successful verification is needed before clearance. New-risk admission and retired dedup preserve single writer, durable-before-external and uncertainty/reservation semantics.

Migration streams and fully verifies old data, preserves the original and atomically publishes a complete root without replacement. `migrate-cold` and `export-cold` do **not** rebind live journals; replacing an old binary after v2 writes is not a rollback. Interrupted/orphan migration files are preserved and counted for admission. No automatic scrub scheduler or automatic live migration/rollback was added.

## Tokyo measurements

Existing `i-015087878f29f14cd`, ap-northeast-1, t4g.micro,1GiB, gp3; EC2 rather than ECS. Release engine SHA256: `8f585586a4045dd27616dc8dd611a81eecf507a4d1f0fb34c997879b8d7d045c`. Native matrix uses engine/example source `e421b0e`; fresh runner uses Python/harness `74a27d7` with unchanged Rust inputs/binary. Disk probe uses `954b525`. Local corrected fault diagnostic uses `213c45d`. Identity is taken from receipts, not names.

W1 has five independent native processes per H,20 production durable rotations per repetition,200 new orders/fills per rotation, then200 explicit tail Tick inputs and hot0. All300 rotations complete. Fixtures begin with generated valid cold history; they do not contain a million historical trading journals. The newly traded4000 orders per case traverse the production journal/rotation path.

| H | Rotation max across5×20 | Fresh engine startup max | Fresh Python+Rust full resume max | Fresh verified lookup including engine boot max |
|---:|---:|---:|---:|---:|
| 1,000 |57.221 ms|4.304 ms|226.952 ms|5.714 ms|
| 100,000 |57.343 ms|4.428 ms|224.364 ms|6.399 ms|
| 1,000,000 |73.181 ms|4.919 ms|249.400 ms|9.542 ms|

Rotation high/low max ratio is1.279 (limit2); engine startup ratio1.143 (limit2). First child logical reads, including startup and pipe reads, max46,668/50,854/50,816 bytes. The conservative growth upper bound is below1MiB. Native1000-query warm p99 remains below5ms. These are absolute scoped observations, not proven A/B improvements.

Twenty-cycle production logical-write upper bounds, including journal/cold/catalogue writes in `/proc/self/io` wchar, max576,542/742,547/742,940 bytes. Each is below35,514,432 bytes (10×196,000+32MiB). Initial-L budgets are used conservatively in analysis. Production allocation samples are after rotation/GC; they are not exact publication peaks. Component probes separately sample allocation before collection.

At H1m, no-pin/single-pin/20-pin component runs complete20 publications. Worst observed allocation is69,652,480 bytes (~66.43MiB), well inside storage limits. Pin-release retry completes in8.075ms for one reader and8.513ms for20 readers; their content remains readable before release. Twenty pins fit the budget, so these runs do not demonstrate admission refusal latency. Budget/debt rejection has correctness coverage; AWS refusal/restart deadline qualification is still incomplete.

Component full verification reads69,136,757 bytes and takes~260–262ms; root/path lookup reads12,661 bytes and takes~0.065–0.076ms in the same program. This illustrates the changed verification scope and I/O; it is not repeatable performance adoption evidence. Largest single buffer allocation was recorded, but it is not the aggregate16MiB working-buffer criterion.

## Failures and diagnostics retained

The first360s W0 candidate run has3,600 external submits and17 rotations, final reconciliation consistent. Raw-derived internal p99 is5.034757ms (limit5); mean CPU20.153% of one vCPU,1s CPU p9522.999%. Host steal p997.407% exceeds5. Fresh swap counters change by4 in /204 out pages. Thus a latency adoption conclusion is unqualified even though CPU and sampled memory are within budgets.

After backing up and releasing task-owned fixtures, the guarded seed7 AWS diagnostic detects fresh swap-in after~6s and stops at64 submissions, before the first planned fault at60s. Cash/position, dedup, durable traces and accounting remain consistent. It is a stopped/incomplete run, not a15-minute PASS. Later native runs also show steal violations (up to11.159%) and fresh swap-in; this was not just one bad observation. CPU credits remain well above20 in available samples; credit exhaustion is not the observed cause.

A first local60s diagnostic revealed a harness error: a normal eligibility flicker could declare a delayed SDK fault recovered before its call returned, allowing overlapping faults. Failure artifacts remain. The corrected run has508 submissions,2 rotations, all7 faults three times, matching gate/recovery records, zero duplicate external IDs or unproved durable submissions, matching cash/position and source accounting. Spacing~1s, duration60s and small sample counts deliberately fail formal AC-F. SDK-held calls are explicitly released after fail-closed; permanent held-submit behavior is separately tested. Generic permanently hung polling/reconciliation/market SDK coverage is not established by this test.

An isolated3GiB ext4 loop image, never the root filesystem, tests disk floor. Actual reserved extents reduce free space below2GiB. Gate response upper bound429.465464ms (limit1000), zero dispatches after gate, worker quiescent, synced stop marker, final Disconnected. The image/loop mount are removed; journals/config/boundary evidence are archived. Unwritten reserved extents are reconstructible scratch data, excluded from export.

The5c5fe90 baseline is genuinely rebuilt on the same host, source/receipt bound, binary SHA `38818b25fdca75eea0b4e2bae8864fc0ac40319742fa9848579706b5570e8aa4`. A22s protocol compatibility diagnostic completes220 submits, one legacy rotation and final reconciliation. This prepares baseline tooling; it is not a qualified W0 repetition.

## Verification

Final code checkpoint `74a27d7`: `scripts/verify.sh` prints `VERIFY OK`;148 Python tests,92 ordinary Rust tests,4 dashboard tests; no skips. Feature matrix:13 cold-page plus18 live-rotation tests, serialized, covering111 process crash exits (39 legacy publication +42 paged/catalogue/GC +30 migration), plus injected I/O failures. Hardware power loss is not tested.

The serialized crash suite is distinct from the ordinary parallel suite. Two parallel crash-suite attempts encounter transient `WouldBlock` journal locks while other test children spawn; their logs are retained. All assertions/repetitions execute in the serialized matrix. Timeout-budget deterministic stress passes100/100. Journal replay confirms position0 and gross PnL12 tick-lots.

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
VERIFY OK
```

## Handoff and next steps

1. Qualify an idle host under the unchanged gates: zero new swap I/O, steal p99≤5%, credits≥20, coverage≥99% and maxgap≤5s. Inventory existing memory/swap owners; do not delete other sessions' `/tmp` data or disable protections. A fresh benchmark host would require a separate scope/cost decision because this proposal permits no new infrastructure.
2. Complete exact aggregate working-buffer/cache telemetry and complete8-class availability metadata (null+reason where genuinely inapplicable). Recollect compatible baselines with the frozen collector; older runs without new sampling fields remain incomplete. Include all reader/restart refusal deadlines and arbitrary sidecar-chain recovery cases.
3. Run qualified A/A and interleaved A/B (five each side, both executor delays), then seeds7/17/29 each900s and two isolated Testnet900s runs. Keep natural SMA signals, N10 live/N200 fake, thresholds and all failed runs. Never turn a short diagnostic into formal PASS.
4. Keep legacy/full defaults until acceptance is complete. Retention/page mode is opt-in. Long-term drift,99% availability, multi-instrument Core, other venues, IPC codecs and durability changes remain separate phases.

Archive/restore completion and actual usage are recorded in the ledger and raw index. S3 retention is the existing90-day `runs/` rule, not a newly configured30-day policy. Costs are estimated from bounded artifact/API usage, not a billing readout. No new EC2/ECS/VPC/NAT/ALB or bucket policy was created or changed.


Host restoration verified: checkout `80809a2fc5a58df5f53db4f125d625b45c3a0703`,
binary `e8ab3699b2fb7e95ba7babf07e4b044493989184f2686eef0c0eb69bc6c61a11`,
original receipt identical, clean Git status, zero task runtime PIDs, no quota
image/mount and root free~5GiB. The first post-restore verifier used system
Python3.9's unavailable `hashlib.file_digest`; restoration had already completed.
The corrected read-only SHA256 verification succeeds. That failed verifier is
retained as an operational error, not an application failure.

Four full task archives are downloaded and SHA256-verified: prior fixtures,
final native fixtures/journals, fresh-runner/baseline/disk supplement, and local
failed/corrected diagnostics plus verification. Compressed total is about163MiB;
raw URI/size/hash receipts are in the ledger. Artifact/API/storage/transfer cost
is conservatively estimated belowUSD0.20 at ordinary S3 rates; billing/prices were
not independently read, so this is not a hard spending-cap certification.
Existing host/storage charges continue; no new persistent infrastructure exists.

The next instrumentation change should replace `Stats.working_buffer_bytes`
(largest single allocation) with ownership-aware simultaneous buffer accounting:
track live Vec capacities, page/ancestor buffers, root serialization/parser
buffers and journal merge inputs through success/error paths; emit current and
peak bytes into raw traces. Measure Python IPC/decode buffers separately and
combine without double-counting hot state. Do not infer a16MiB aggregate bound
from a10KiB largest allocation or sampled RSS. Add overflow/error-unwind cases,
then rerun the unchanged H matrix before marking this criterion PASS.
