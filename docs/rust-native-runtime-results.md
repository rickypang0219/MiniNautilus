# Rust native migration: acceptance record

Created: 2026-10-15T21:21:55+09:00 (Asia/Tokyo).
Original controlled comparison: `2db3faa`; expiry recovery build: `8d58574`; proposal: [frozen scope](rust-native-runtime-proposal.md).

## Confirmed corrections

- Cancellation acknowledgement is not terminality. The native adapter now queries the original order until terminal visibility (bounded five-second polling), without resending cancellation. `cancel_ack_waits_for_terminal_visibility_without_resending` exercises open → open → cancelled with exactly one mutation.
- Diagnostic and recovery flatten orders use the existing H1 bounded 0.1% collar, rounded outward to whole USD. Strategy resting limits and the USD40 admission cap are unchanged. This addresses the first crash drill's resting unfilled order, without relaxing fill/recovery assertions.
- Market journals retain exchange event time separately from local receipt time. Python reference and native controlled benchmark both write the same metadata; Core decision-time semantics are unchanged.
- Previously fixed queued-intent expiry and absolute Python partial-response deadlines remain covered by RED/GREEN evidence. Native signed commands require release builds.

## Verified local results

`scripts/verify.sh` passed, including existing Rust/backtest contracts, native fault/signing/boundary tests and 183 Python tests (the latest run adds the evidence identity regression test). Exact final output:

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
VERIFY OK
```

The original replay check separately produced position 0 and gross PnL 12 tick-lots. This result must not be confused with the fault scenario above.

Real Testnet diagnostic `diagnostic-live-1` completed four accepted orders/fills (long/flat/short/flat), with final position 0 and verified cleanup. Fees were 0.029306 USDC and realized PnL −0.0036 USDC; this was an operational diagnostic, not strategy performance.

`diagnostic-crash-2` genuinely exited with code 86 **after the venue fill and before local fill persistence**. Original-session recovery took 5.233156357 seconds, accounted for two fills including flattening, and finished with position 0. Independent venue checks confirmed no positions or open orders. The interrupted run remains incomplete; successful recovery does not relabel it a completed trading session.

The first post-recovery process scan incorrectly included a concurrent *offline* `native-live bench` process. Its wrapper therefore failed despite flat venue state. After that benchmark exited, a fresh scan confirmed zero trading processes as well as zero positions/orders. Preserve both observations.

## Failed and incomplete evidence retained

- `native-probe-1`: subscription acknowledgement defaults differed from the initial request; explicit matching fields fixed it. `native-probe-2` passed.
- `diagnostic-crash-1`: the initial limit order did not fill before the deadline. Cancellation acknowledgement preceded terminal query visibility. It never reached the injected crash point. Explicit recovery cancelled the original order and verified flat. The two corrections above were followed by the genuine second crash drill.
- Dirty/short local benchmark smoke runs only establish that harnesses run; they are not performance acceptance.
- The first c7i.large was launched prematurely and left idle. It produced no performance evidence. Instance `i-0d3ed06e6d581495e` and volume `vol-056a7825943a24c40` were subsequently confirmed terminated/deleted. This operational failure remains part of the record.

## AWS evidence

The second experiment uses an immutable package of `2db3faa`, bundle SHA256 `cd9517f9aa9d40b806e9b3f75c02e19f1d68eee8dc5fd02cb1e53d38f5db4fa1`. Raw artifacts are under `runs/rust-native-20261015/aws-experiment-2/`; private S3 prefix is `rust-native-20261015/mini-native-1792066730` in the existing artifacts bucket.

Local verification archive: `local-verification-3.tar.gz`, SHA256 `bb33a60eef951b7222333b3b0eb357c230a201c06bf32c290d64d0975c1f50de`, stored under the same S3 prefix. It includes original failed/interrupted diagnostics, recovery journals and verification logs.

All 20 controlled runs completed: five per A/A side and five per interleaved A/B side, each 120,000 offered/admitted/completed market events, zero rejected/inflight, and 120 matched orders/fills. Excluding warm-up leaves 115,000 market and 115 order samples per run. Input/action identities and executable provenance match.

A/A **INCONCLUSIVE**: request-to-simulated-handoff p99 range 6.3029–8.0652 ms, median 6.6587 ms; spread/median 26.465%, above the preregistered 20% gate. All relative performance conclusions below therefore remain **observations, not accepted improvement**.

Medians of five run-level measurements per side:

| Controlled metric | Python actor/reference | Native Rust | Observed change |
|---|---:|---:|---:|
| Application-frame → simulated order handoff p99 | 6.6453 ms | 6.1814 ms | −6.98%; also below the required 10% effect |
| Market receive → writer complete p99 | 0.37459 ms | 0.01841 ms | −95.1% |
| Market queue wait p99 | 0.22318 ms | 0.00666 ms | −97.0% |
| Market receive → writer complete p99.9 | 5.1861 ms | 4.2460 ms | −18.1%; this is **not order p99.9** |
| CPU / completed market event | 275.607 µs | 83.375 µs | −69.75% |
| RSS peak indicator | 74.594 MiB | 6.375 MiB | −91.45% |

RSS for the two-process Python reference sums the observed process high-water marks (an upper bound, not necessarily simultaneous); native controlled mode has one process and no Python strategy. These numbers must not be substituted for live combined RSS. CPU includes the reference's Rust child. At this fixed 1,000 events/s workload, CPU/event corresponds approximately to 27.6% versus 8.3% of one CPU, including benchmark work.

The native five-run queue p99 maximum was 0.01732 ms and simulated action p99 maximum 6.28862 ms, within the scoped 1 ms / 10 ms budgets. These do not satisfy the complete signed-handoff gate because signing/admission/TLS are excluded.

Per-submit trace attribution (115 retained submits/run) finds median **99.80–99.82% of each individual submit request** in the sync stage. Submit request p99 ranges 5.7279–5.9078 ms; sync p99 5.7183–5.8995 ms. The share is computed from paired stages of each request, not a sum or ratio of unrelated percentiles. This explains why removing Python routing costs barely changes the durable order path. No durability relaxation was made.

AWS paper acceptance **FAIL**: stopped after 684.0367 s with `unexpected WebSocket frame`, four simulated fills, position0, no queued messages, no telemetry drops, cleanup verified. It did not reach 900 s; signed Testnet was therefore **NOT_RUN**. The original error lacks frame type/close status, so an exchange close cannot honestly be distinguished from another unsupported frame retrospectively. A regression test now requires bounded close-code/reason diagnostics and binary-frame length without payload disclosure; all such frames still stop admission. The following experiment isolated the expiry cause; see the lifecycle follow-up below.

The experiment controller downloaded original evidence and confirmed instance `i-0ff2cbc6e041fda4c` terminated, EBS `vol-0e599b8eb1c2413a6` deleted and its temporary SecureString parameter deleted. The original t4g.micro was not touched.

## Lifecycle follow-up: diagnosed expiry and fresh release acceptance

Experiment 3 (`553c824`, S3 prefix `rust-native-20261015/mini-native-1792085193`) stopped after **678.800630215s**, this time with explicit **WebSocket close 1000 / Expired**. Three simulated orders/fills, flat and cleanup verified; signed acceptance NOT_RUN. Original artifacts remain under `aws-experiment-3/`; instance `i-07c30e0a44acfff3e`, EBS `vol-01f833ad52cfc9d47` and temporary secret were confirmed removed. The first run's generic error does not retrospectively prove its close reason.

`8d58574` adds bounded expiry renewal, synchronous admission pause, source generations and draining in-flight results in their original epoch before reconciliation. New subscriptions plus matching successful reconciliation are required; stale market/strategy results cannot reopen admission. Other errors still fail closed, and the budget is three renewals per invocation. See [expiry recovery contract and tests](rust-native-runtime-expiry-recovery.md). No Core or financial journal semantics changed.

Full local verify and optimized release build passed. Repeated original replay: position0 / gross PnL12. The negative test with the synchronous pause removed fails; restoring it passes. GitHub Actions run [38907752684](https://github.com/rickypang0219/MiniNautilus/actions/runs/38907752684) passed with both native and default feature suites.

Experiment 4 is bound to `8d58574ab259fb072e83fbae53409e54e7488adb`, bundle SHA256 `fd07f00a114eae5bf338dda4568f71910190ca8b9de531a8ef56f09357833912`, prefix `rust-native-20261015/mini-native-1792156825`. Order frozen before launch: paper900 → A/A 10 runs → interleaved A/B 10 runs → signed900. The same manifest/thresholds remain unchanged. No concurrent performance and live workloads.

Fresh paper: **PASS, 900.003230206s**, one expiry renewal, four simulated orders/fills, final position0, cleanup verified, queued messages0 and telemetry drops0. Trace records generation1 expiry at 1792157503858ms, matching reconciliation at 1792157503859ms and replacement subscriptions at 1792157504917ms. These wall timestamps establish observed ordering, not a formal monotonic gate-latency measurement. All 20 controlled runs subsequently completed with matching identities/accounting. **A/A PASS**: p99 range 6.3220–7.0325ms, median6.5883ms, spread10.7836% ≤20%. Five-run medians:

| Scoped metric | Python reference | Native | Verdict |
|---|---:|---:|---|
| Simulated action p99 | 6.56479ms | 6.13455ms | INCONCLUSIVE: 6.55% effect is below10% |
| CPU/completed market event | 279.284µs | 87.102µs | IMPROVED: −68.81%, nonoverlapping ranges |
| RSS peak indicator | 76.594MiB | 6.348MiB | IMPROVED: −91.71%, nonoverlapping ranges; same upper-bound caveat as above |
| Market receive→writer p99 | 0.46130ms | 0.01968ms | Secondary observation, separate from action latency |
| Market queue p99 | 0.27097ms | 0.00719ms | Secondary observation |

Native per-submit median sync/request share remains 99.731–99.819%. Order p99.9 and complete signed transport-handoff remain NOT_MEASURED; this controlled workload excludes venue/signing/TLS. Numbers belong to `8d58574`, not a later binary.

**Signed acceptance FAIL** after121.815479220s with `unknown order; absence unresolved`, before any expiry. Original-session recovery succeeded (and final independent recovery took1.3064s); final ledger has4 fills, fees0.024605USDC, realized0.01192USDC, funding0, cash change−0.012685USDC, position0. Fresh independent account inspection on2026-10-16 found positions0/open-orders0/local trading processes0. This does not relabel the short run PASS. The SSM supervisor returned Success after successful recovery, so its exit status alone was not an acceptance verdict.

Instance `i-03ce080b840ba2119` terminated; EBS `vol-074c313c9c6c99670` and temporary SecureString deleted. The user's existing small host was untouched.

The native query path previously stopped on the first non-`order` response. The old failure did not retain the raw status, so its exact value cannot be established retrospectively; `unknownOid` is a reproducible visibility-lag case covered by the correction, not a proven reconstruction of that old response. A RED/GREEN test reproduces unknown→unknown→valid original order. The follow-up change retries **only original-cloid reads**, under a5s absolute deadline including request time/pacing; no mutation is resent and no absence is inferred. The execution owner remains busy, preventing another strategy action. Permanent unknown, malformed response and wrong identity still fail closed. Full verify passed; the fresh signed900 acceptance below passed. This change affects REST visibility handling, which is excluded from the controlled workload; it does not remeasure or relabel the earlier performance binary.

Local verification archive `local-verification-5.tar.gz`, SHA256 `14ccc6de6523e972ba45580624c1caaa8168a099a579b9db1da3f75a78c9f5b9`, contains tests, negative experiment, replay, frozen controller/supervisor and public shadow preflight, under the experiment4 prefix.

## Corrected signed release: experiment5

Signed build `5ee07f2aec4e2f1bd9b428646718c8e3da87a023`, bundle SHA256 `548e145ef8a90a21d3d88bca7ed8f1efa2428e22927979dddc0dc3a8610a8d63`; immutable evidence prefix `s3://mininautilushost-artifacts82dd59a1-jimhxpumwqyb/rust-native-20261015/mini-native-1792178996/`. This bounded follow-up tests the REST visibility correction. The controlled comparison remains bound to experiment4's `8d58574`; no later binary is relabeled as that measurement.

**Signed900 PASS**: active900.005929342s, exit0,5 owned orders/5 unique fills,172 market observations, no queued messages at stop, no telemetry drops and no runtime failure. One real `1000/Expired` renewal occurred: generation1 expiry at1792179703566ms, successful reconciliation at1792179704471ms and new subscriptions confirmed at1792179704664ms. Core advanced to epoch2; subsequent submits at1792179781180ms and1792179901273ms demonstrate trading after renewal. These are wall-time trace ordering observations, not a formal monotonic10ms gate-latency proof.

Runtime process-tree RSS had896 samples, peak**32,236KiB /31.480MiB**, below384MiB. The initially sampled minimum precedes full child initialization; it must not be used to infer a leak. Fifteen minutes is a fast-fail integration gate, not a long-term memory bound.

Shutdown cleanup and an additional original-session recovery both succeeded; the latter took1.686279740s. Independent venue inspection after termination confirmed positions0/open orders0, with no local trading processes. Offline inspection of the actual recovered journal independently reproduced position0,5 orders/5 fills. Session accounting: realized−0.0002USDC, fees0.058526USDC, funding0, cash change−0.058726USDC. This diagnostic SMA session is not alpha performance; real funding settlement remains NOT_MEASURED here.

The supervisor now retains a failed signed exit as failure even when recovery succeeds. This corrects the experiment4 orchestration ambiguity without altering its original report. No failed attempt was dropped or relabeled.

Task instance `i-0dd2d835b0767992b` terminated, EBS `vol-02b136a02e5db78d2` deleted and temporary SecureString deleted. A fresh AWS inventory found zero remaining task instances. Existing user host was preserved. Raw signed archive SHA256: `ff32106e759bcd8149fbcd650970d82a385b6cc630d92353b2caa530b8487eb9`. Local verification/source-preparation archive `local-verification-6.tar.gz` SHA256: `b05845c92077b6906f6b4f0880f1badedf88cb07f35057164584be48a14a66cb`, under the same prefix.

Full `scripts/verify.sh` finished VERIFY OK, including183 Python tests; native venue tests7/7 passed, including the RED/GREEN visibility regression, permanent unresolved-order preservation and wrong-identity rejection. Original replay remains position0/grossPnL12. CI [38955374834](https://github.com/rickypang0219/MiniNautilus/actions/runs/38955374834) passed on the tested signed source commit.

## Acceptance matrix

| Gate | Status | Evidence / limit |
|---|---|---|
| RN-A1 ownership | PASS for the native invocation | Rust direct DurableEngine; optional Python strategy only; production ownership documented separately from old compatible entry points |
| RN-A2 / A7 regression contracts | PASS for executed suites | Full verify, existing backtest/reference suites, replay position0/PnL12; no Core financial or journal format changes |
| RN-A3 protocol | PASS for covered fixtures and real diagnostic | Pinned SDK signing vectors, action-journal corruption/reuse, binding/mode/cap rejection tests, real four-fill diagnostic |
| RN-A4 fault recovery | PASS for covered cases | Deterministic lost-response/no-resend, cancellation visibility, expired intent, unknown absence tests; real exit86 original-session recovery |
| RN-A5 optional work isolation | PASS for tested delay/overflow scenarios | One-second delayed strategy/full telemetry private-progress test, absolute response deadline and stale-result tests |
| RN-A6 live integration | PASS for the recorded integration slice | Earlier real4-fill/crash drills; paper900 on8d58574; corrected signed900 on5ee07f2, real expiry/resume, peak31.480MiB and final flat/open0; original failed runs retained |
| RN-P0 quiet reference | PASS in experiment4 | Ten valid A/A runs; 10.784% spread ≤20%; earlier failed A/A retained |
| RN-P1 complete signed local handoff | NOT_MEASURED | Available comparison excludes signing/admission/socket handoff, so its scoped result cannot satisfy this whole gate |
| RN-P2 tail/resource guardrails | PARTIAL / NOT_MEASURED | Scoped CPU/RSS IMPROVED in experiment4; full signed path and order p99.9 remain NOT_MEASURED |
| RN-P3 absolute complete path | NOT_MEASURED | Queue and simulated action budgets can be reported separately; signing/admission are missing from that workload |
| RN-P4 workload accounting | PASS for controlled workload | All 20 runs complete with identical input/action identities and zero rejected/inflight |

The local report analyzer was strengthened at `6184319`: mixed baseline/candidate executable identities now invalidate a comparison. A RED test demonstrated the prior false positive; GREEN and full verify followed. This does not change the workload, measured program or numerical thresholds of the frozen AWS experiment. The original remote analyzer output is retained and the collected data were also analyzed locally with this correction; all comparison identities passed.

## Boundaries that remain explicit

Rust now owns ingestion, routing, the durable writer, timers, admission/signing/execution and recovery. Python is optional strategy only. Private WebSocket observations wake verified REST refresh; they do not yet directly apply native venue accounting facts. General reconnect-and-resume trading, live Hyperliquid rotation, multi-instrument collateral, mainnet and PCA alpha deployment remain outside this delivered slice. The exact expiry-only renewal described above is now implemented; it is not general network recovery.

The controlled comparison ends at a simulated executor handoff and excludes TLS, signing and account RPC. Thus even a passing comparison cannot establish the proposal's complete signed transport-handoff RN-P1/RN-P3 gate. Order p99.9 is NOT_MEASURED because each run has fewer than 100,000 orders. Earlier A/A instability made that experiment INCONCLUSIVE. Experiment4 passes A/A, allowing its scoped CPU/RSS conclusions; its action effect still falls below10%.

## Next milestone preparation

[Native production readiness proposal](native-production-readiness-proposal.md) specifies lifecycle fault coverage and safe session rotation, persistent journal/host replacement and deployment drills, and the owner-selected Cluster-PCA shadow path. It has dated scope, implementation steps and numerical AWS gates. These new targets are future acceptance, not changes to this migration's frozen thresholds.

Deployment preparation: [native live runbook](native-live-deployment-runbook.md), including signed preflight, original-session recovery, ABI/architecture checks and explicit pending operational drills.

The deployment preparation also includes a minimal [retained-volume CloudFormation draft](../ops/aws/native-journal-volume.json), accepted by AWS ValidateTemplate. It is not deployed and has no automatic host attachment/service installation. Production blockers remain general lifecycle faults/restarts, safe live rotation beyond1,000 orders, retained-state host-loss/fencing/alerts/upgrade drills, and true portfolio strategy accounting. Those are specified in the next-phase proposal, not silently treated as delivered by a successful15-minute session.

CI follow-up: one later native-feature CI attempt failed with exit101; original test details are currently blocked by the log-host network policy. Passing local/CI reruns do not identify its cause. See the [retained incident and next steps](native-ci-followup.md); this is an open qualification item, not a claimed runtime fix.
