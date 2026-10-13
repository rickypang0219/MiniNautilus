# Hyperliquid H1 migration acceptance

Created: 2026-10-13T14:45:46.058655+00:00 (UTC). Runtime implementation: `c23d154`; later documentation and CI-reporting commits do not change the acceptance runtime. Verdict: H1 diagnostic gates PASS, with the operational and unexplained transient-failure limits below; H2 is not implemented.

## What is delivered

One dedicated Hyperliquid Testnet account, native BTC/USDC perpetual, Rust durable single writer, Python actor, bounded SMA(3,8) diagnostic with both long and short targets. Native Decimal grids, stable cloids, monotonic durable nonces, official Testnet-domain signing, agent/account binding before every mutation, conservative collateral checks, cancellation, complete bounded fill/funding history, and independent account reconciliation are implemented. Mainnet mutations remain disabled. Binance regression coverage remains intact.

No new Rust journal or core model is introduced. New venue-specific code is contained in the adapter, actor boundary and diagnostic entry points. Market data uses candle WebSocket liveness plus bounded REST finalized bars; private execution/account state uses REST. A separate private WebSocket probe does not change that implementation.

This is H1 execution plumbing, not a PCA deployment, proof of profitability, multi-instrument margin engine, unattended live service or performance improvement claim. The 15-minute gate is deliberately short; no A/B latency improvement is claimed.

Machine-readable companion: [acceptance JSON](hyperliquid-migration-results.json).

## Final cohort

| Run | Runtime (s) | Peak sampled RSS (MiB) | Input / effect queue peak | Orders / fills | End position |
|---|---:|---:|---:|---:|---|
| `hl-paper-20261013-r7` | 902.062 | 46.06 | 2 / 1 | 5 / 5 | +20 simulated lots |
| `hl-testnet-20261013-r5` | 901.707 | 90.44 | 3 / 1 | 4 / 5 | 0 |
| `hl-testnet-20261013-r6` | 902.422 | 89.35 | 3 / 1 | 5 / 5 | 0 |

These runs use matching runtime source hashes and the same release binary SHA-256 `54e6d33ad497d3cf38c1d871b677a038151495e58a86a72fcf7f3ae3c090fde6`. Durations exclude startup and final reconciliation/flattening. RSS is combined Python + Rust sampled every two seconds, not an allocator peak or proof of long-run stability. Queue limits are 128 inputs / 32 effects. Order/fill counts come from final Rust journal replay, including explicit closeout; the actor's submission counters exclude those closeout orders. Paper positions are simulated and its optimistic touch model omits fees/funding.

Testnet r5 recorded one `private_poll_failed` gate in addition to its recovery drill; quiescent reconciliation succeeded and it completed flat. The precise transient cause was not recorded. R6 recorded only its planned recovery drill. These results must not be described as zero-error sessions. The measured trade-enabled fractions were 5.43% / 5.56% / 5.92%; minute-bar quote refresh and the three-second freshness guard limit admission between observations. No continuous-admission SLO was accepted.

Final venue positions/open orders: zero positions and zero open orders, independently queried at `2026-10-13T14:44:26.409475+00:00` after all signed diagnostics stopped.

## Acceptance mapping

| ID | Result | Evidence and limits |
|---|---|---|
| HL1 wire | PASS | 32 Hyperliquid test methods including Decimal/price/size boundaries, schema and identity rejection. Official SDK 0.24.0 offline vector matches Testnet signing and differs from mainnet; wrong agent rejected. No mainnet mutation. |
| HL2 send | PASS | Signing failure before network, lost response, explicit rejection, durable restart nonce floor and no automatic resend tests. Action journal precedes signing/network; unknown results require reconciliation. |
| HL3 history | PASS | Timestamp pagination/saturation, duplicate/changed fills and funding, partial-cancel cumulative quantities and mismatched history fixtures. Saturated single-millisecond pages fail closed. |
| HL4 account | PASS in H1 scope | Independent long/short/reversal/partial-close ledger tests within 0.000001 USDC; foreign activity and insufficient/stale collateral rejected. Actual interrupted-session recovery also accounted for funding accrued during downtime. |
| HL5 recovery | PASS | Real Rust integration covers crash before send, after send and after fill for both directions. Actual Testnet diagnostic kills Rust after an exchange fill but before its report, recovers without duplicate sends, then closes. UnknownOid remains unresolved. |
| HL6 strategy | PASS | Captured paper observations replay: 31 observations, two warmup resets, zero signal mismatch, eight-slot SMA window. Changed closed bars/gaps reject; bounded publication delay never fabricates candles. |
| HL7 regression | PASS | `scripts/verify.sh`: 92 Rust, 180 Python (including 32 Hyperliquid), four dashboard tests; VERIFY OK. Original replay position 0 / gross PnL 12 tick-lots. No skipped tests. |
| HL8 connectivity | PASS | Current cloud mainnet-public and Testnet meta/l2/candle/WS subscription probes passed. Separate Testnet private WS probe: 902.262s, three subscription acknowledgements, two distinct live fills, four order updates, one reconnect, 42 pongs. This proves receipt/re-subscription, not authoritative private-stream recovery. |
| HL9 soak | PASS | 900±5s paper and two sequential Testnet runs; exact duration/RSS/queues, final journal positions, terminal orders, unique cloids and increasing nonces checked by retained collector. Interrupted runs excluded explicitly below. |
| HL10 execution | PASS | Current-runtime actual Testnet diagnostic `hl-execution-20261013-r2`: four orders/fills, position path +20 → 0 → -20 → 0 lots, zero final open orders, Rust SIGKILL recovery. Strategy runs remain labelled separately from forced diagnostics. |
| HL11 H2 readiness | NOT_IMPLEMENTED | Shared collateral portfolio reservation, basket execution/recovery and fee/funding-realistic PCA research parity remain separate scope. |

GitHub CI on `8cfff36` failed in the Python step; the original detailed logs redirect to a network-blocked blob host. A diagnostic annotation was added without changing the test command/assertions. Local rerun passed all 180 Python tests and [CI on `30f41cb`](https://github.com/rickypang0219/MiniNautilus/actions/runs/38415101373) passed with unchanged runtime. The original failure remains unexplained; this is not a demonstrated bug fix. Retained metadata: `hl-acceptance-20261013/ci-results.json`.

Exact final runtime verification log: `hl-acceptance-20261013/verify-provenance.log`. Its last lines are:

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
VERIFY OK
```

## Failed and interrupted runs are retained

- Paper r1 failed on an incorrect live-adapter recovery call; paper now reconciles through the Rust simulator. Paper r2/r3 failed; minute-bar order TTL/simulator touch and candle diagnostics were corrected. Their failed artifacts remain.
- Testnet r1 failed after 766.01s on CandleGap/reconnect exhaustion. The original log recorded only the error class; its exact cause is unproven. A later bounded candle-publication fix accepts at most one unpublished trailing interval, with no fabricated observation; interior gaps and longer delays still fail closed.
- That run exposed a closeout bug: the writer was Disconnected when flattening attempted Submit. Closeout now performs proven reconciliation first, with a regression test. A separate original-session recovery subsequently closed its long position, including five funding entries. Recovery success does not turn the original run into a soak PASS.
- Paper r4/r5/r6 and Testnet r2/r3 supplied successful intermediate evidence but are not substituted for the final matching-runtime cohort.
- Testnet r4 was interrupted with its final resource sample at 792.448s, no original completion summary and no surviving runner. Its final logged market failure was OSError; the precise process termination cause is not established. Order 4 sold 0.0004 BTC, reversing long 0.0002 into short 0.0002. On resumption the account still held that short with no open orders. Original-session reconcile-only recovery filled order 5 (buy 0.0002), reconciled fees/funding and returned flat. A fresh venue check at 2026-10-13T14:10:55.887Z confirmed no positions/open orders. The zero-runtime recovery summary is not accepted as a 900-second run. Replacement runs are r5/r6.
- An earlier private WebSocket observer disconnected at 698.8s; the later 902.262s observer passed. Both are retained.

The short-position incident exposes an operational limit: `--flatten` is normal cleanup, not host-loss protection. A process/container can disappear while exposure remains at the exchange. No independent watchdog is implemented. The operating guide and agent rule now require orphan-session inspection, original-journal recovery and a fresh venue flat/open-order check before another signed session. Do not use these diagnostic scripts as an unattended service.

## Next work, in order

1. **Supervision and restart ownership.** Before unattended operation, design a persistent single-owner service and external stale-session alert. Test host/process loss, restart ownership, original-journal recovery and failed closeout. Do not promise automatic flatten on unavailable/uncertain venue state; exchange dead-man order cancellation does not close filled positions.
2. **Strategy and account scope.** For PCA, implement shared-collateral multi-instrument reservations and basket partial-fill recovery, then reproduce research with realistic fees, funding and bias controls. H1's single-account/single-instrument checks intentionally reject that scope today.
3. **Reporting and long-lived retention.** Generic `examples/analyze.py` must not infer fill chronology from Hyperliquid execution IDs; use venue `(time, tid)` ordering for fee/funding-aware reporting. Live Hyperliquid rotation is not enabled; session history is bounded by the diagnostic limits, not by proven 24/7 retention.
4. **Failure diagnostics and private streams.** Add stable, redacted cause codes for private polling gates and investigate any recurrence of the unexplained CI failure. **Private-stream integration requires a recovery contract.** User streams are reachable, but replacing REST requires duplicate/gap/snapshot semantics and tests. Optimize only after measuring the chosen strategy's actual bottleneck. Current minute-bar freshness makes the overall trade-enabled fraction low; soak PASS does not certify continuous admission availability.

## Artifact provenance and deployment boundary

Existing private Tokyo S3 objects (181 files, 2,578,362-byte gzip archive; existing bucket retention applies):

- `s3://mininautilushost-artifacts82dd59a1-jimhxpumwqyb/runs/hyperliquid-migration/hyperliquid-evidence-20261013T144426Z/evidence.tar.gz`
  SHA-256: `3d99005e803129e4e8f368a45ce34754147040b6fde48d8ea561697af84cb4f2`.
- `s3://mininautilushost-artifacts82dd59a1-jimhxpumwqyb/runs/hyperliquid-migration/hyperliquid-evidence-20261013T144426Z/index.json`
  SHA-256: `6614962cf40ef8812adcd1287727ec3e68e19c5af273e62fa74755c44fbf1f0c`.
- `s3://mininautilushost-artifacts82dd59a1-jimhxpumwqyb/runs/hyperliquid-migration/hyperliquid-evidence-20261013T144426Z/manifest.json`
  SHA-256: `ecc5b18f39059572a52a4490d944e35bca958b31941107404c50a409f1f4d3e0`.

All accepted and failed run files, red/green test evidence, probe outputs, source/binary identities and raw journals are retained privately; the archive has an independent per-file SHA-256 index. Raw account data is not committed to Git. Secrets/signatures are not included.

Signed Testnet sessions ran in the current cloud Linux environment. The existing Tokyo EC2 `i-015087878f29f14cd` performed only a read-only public connectivity probe (SSM command `2a030ba7-03bb-4e1f-a32d-7741a0af6f41`). No Hyperliquid trading service, ECS stack or new AWS instance was deployed by this acceptance task.
