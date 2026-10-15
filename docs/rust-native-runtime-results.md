# Rust native migration: acceptance record

Created: 2026-10-15T21:21:55+09:00 (Asia/Tokyo).
Implementation under test: `2db3faa`; proposal: [frozen scope](rust-native-runtime-proposal.md).

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

AWS paper acceptance **FAIL**: stopped after 684.0367 s with `unexpected WebSocket frame`, four simulated fills, position0, no queued messages, no telemetry drops, cleanup verified. It did not reach 900 s; signed Testnet was therefore **NOT_RUN**. The original error lacks frame type/close status, so an exchange close cannot honestly be distinguished from another unsupported frame retrospectively. A regression test now requires bounded close-code/reason diagnostics and binary-frame length without payload disclosure; all such frames still stop admission. A fresh paper/signed acceptance is pending with that diagnostic change.

The experiment controller downloaded original evidence and confirmed instance `i-0ff2cbc6e041fda4c` terminated, EBS `vol-0e599b8eb1c2413a6` deleted and its temporary SecureString parameter deleted. The original t4g.micro was not touched.

## Acceptance matrix

| Gate | Status | Evidence / limit |
|---|---|---|
| RN-A1 ownership | PASS for the native invocation | Rust direct DurableEngine; optional Python strategy only; production ownership documented separately from old compatible entry points |
| RN-A2 / A7 regression contracts | PASS for executed suites | Full verify, existing backtest/reference suites, replay position0/PnL12; no Core financial or journal format changes |
| RN-A3 protocol | PASS for covered fixtures and real diagnostic | Pinned SDK signing vectors, action-journal corruption/reuse, binding/mode/cap rejection tests, real four-fill diagnostic |
| RN-A4 fault recovery | PASS for covered cases | Deterministic lost-response/no-resend, cancellation visibility, expired intent, unknown absence tests; real exit86 original-session recovery |
| RN-A5 optional work isolation | PASS for tested delay/overflow scenarios | One-second delayed strategy/full telemetry private-progress test, absolute response deadline and stale-result tests |
| RN-A6 live integration | FAIL / NOT_RUN | Local diagnostics passed; AWS paper stopped at 684 s; signed soak not started; corrected-diagnostic retry pending |
| RN-P0 quiet reference | INCONCLUSIVE | Ten valid A/A runs; 26.465% spread exceeds 20% |
| RN-P1 complete signed local handoff | NOT_MEASURED | Available comparison excludes signing/admission/socket handoff, so its scoped result cannot satisfy this whole gate |
| RN-P2 tail/resource guardrails | INCONCLUSIVE / NOT_MEASURED | CPU/RSS/market tails observed lower, but A/A fails; order p99.9 remains NOT_MEASURED |
| RN-P3 absolute complete path | NOT_MEASURED | Queue and simulated action budgets can be reported separately; signing/admission are missing from that workload |
| RN-P4 workload accounting | PASS for controlled workload | All 20 runs complete with identical input/action identities and zero rejected/inflight |

The local report analyzer was strengthened at `6184319`: mixed baseline/candidate executable identities now invalidate a comparison. A RED test demonstrated the prior false positive; GREEN and full verify followed. This does not change the workload, measured program or numerical thresholds of the frozen AWS experiment. The original remote analyzer output is retained and the collected data were also analyzed locally with this correction; all comparison identities passed.

## Boundaries that remain explicit

Rust now owns ingestion, routing, the durable writer, timers, admission/signing/execution and recovery. Python is optional strategy only. Private WebSocket observations wake verified REST refresh; they do not yet directly apply native venue accounting facts. Automatic reconnect-and-resume trading, live Hyperliquid rotation, multi-instrument collateral, mainnet and PCA alpha deployment remain outside this delivered slice.

The controlled comparison ends at a simulated executor handoff and excludes TLS, signing and account RPC. Thus even a passing comparison cannot establish the proposal's complete signed transport-handoff RN-P1/RN-P3 gate. Order p99.9 is NOT_MEASURED because each run has fewer than 100,000 orders. A/A instability means INCONCLUSIVE, irrespective of attractive medians.
