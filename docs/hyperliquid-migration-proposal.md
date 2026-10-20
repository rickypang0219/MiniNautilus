# Hyperliquid Perps migration v1.1

Created: 2026-10-13 (original draft date; precise creation time was not recorded).
Updated: 2026-10-13T03:36:41.560405+00:00. Status: frozen implementation scope; acceptance is evidence-based, not automatic on merge.

## Outcome and boundaries

Move the next executable venue experiment to Hyperliquid native USDC-settled perps with the smallest compatible change. Preserve Rust single-writer OMS, durable-before-send, uncertain reservations and recovery. Stop Binance-specific performance work. Keep Binance regressions passing; do not delete a working reference adapter.

H1 delivers one dedicated Testnet account, one native perp (BTC default), one writer, flat genesis, conservative fixed notional and collateral limits, long/short SMA as an **execution diagnostic**. It reuses the existing Core's signed gross inventory; it does not call that Core a margin model. Venue-authoritative collateral/funding/fees are independently checked by the adapter before sends and at reconciliation. No foreign positions/orders, withdrawals, account abstraction, vaults, HIP-3, spot, liquidation automation, runtime leverage changes, automatic transfer or mainnet mutation.

H2 is the user's actual cluster-PCA strategy: multiple perps sharing USDC collateral, a single portfolio reservation authority, partial basket fills and per-instrument recovery. H2 cannot be simulated by independent H1 accounts each spending the same collateral. It follows the research reproduction and portfolio work in [the PCA proposal](pca-stat-arb-research-to-paper-proposal.md). H1 success is not H2 completion or evidence of alpha. A new venue cannot fix the reported PCA survivorship/cost biases.

This change authorizes implementation and Testnet testing, not funded mainnet orders. Network access and an exclusively operated funded Testnet account are external prerequisites. No new AWS infrastructure is needed for offline tests. Public production market data may feed paper, clearly labelled separately from Testnet.

## Tasks: how and why

1. **Transport and instrument contract.** Read official SDK wire types. Explicit production-public vs Testnet destinations; no redirect or mutation retry. Decimal price/size validation (szDecimals, perp decimal ceiling, five significant figures with integer-price exception), not Binance filters. Persist stable 128-bit cloid from session+local order ID. Use official signing, Testnet domain only, bounded timeout and expiresAfter; serialize nonces and durably record each outgoing action before networking. Unknown outcomes stay unknown; orderStatus unknownOid never proves absence.
2. **History and reconciliation.** Match orderStatus cloid/coin/side/size/limit to durable intent; cap/paginate fills without losing equal-timestamp records; deduplicate trade IDs, reject payload changes. Quiesce sender, discover all account open orders, cancel only owned orders, prove every known order terminal, prove cumulative fills, compare signed position and USDC equity (realized PnL - fees + funding + unrealized). Reject foreign account activity, history truncation and unsupported schemas. No fabricated exchange watermark: barrier is local, single dedicated writer plus terminal-order checks.
3. **Perp admission.** One configured contract, no shared account positions. Finite positive NAV, cross-margin mode and configured leverage 1 only. Explicit empty-account Testnet setup may set cross 1× once using a durable signed action; runtime never adjusts leverage. Fixed maximum position lots and order notional, available margin buffer, fresh account/mark data. A persisted intent is not permission to bypass final venue checks. Any discrepancy gates. This does not guarantee against market gaps or prove a portfolio-margin model.
4. **Actor/market migration.** Default actor remains spot-safe; add an explicit adapter-backed short capability. Validate private account outside writer I/O, final execution checks before signing. Closed candles only, bounded warmup/backfill, no synthetic zero-return gaps, WebSocket receive limits/timeouts; retain current bounded queues and late-result handling. No full history fetch into signal.
5. **Paper and Testnet runner.** Separate runnable entry point, durable session identity/config and summary, explicit --resume, release identity optional for offline but required for live acceptance. Paper SMA is plumbing; fees/funding-realistic PCA remains H2. Prevent environment/account/instrument/session changes on resume. Do not automatically flatten unknown exposure on shutdown.
6. **Evidence and handoff.** Unit fixtures + real Rust integration + fault tests + public protocol probes + 15-minute paper/Testnet sessions when reachable. Immutable run directories; include actual durations/counts, source/binary hashes, failure reasons and unrun cases. Never turn a network-blocked probe into a PASS.

## Acceptance criteria

| ID | Measurable acceptance | Evidence |
|---|---|---|
| HL1 wire | ≥20 boundary/identity/schema checks; zero accepted invalid Decimal/price/size; production mutation requests=0 | unittest output and offline signing vector |
| HL2 send | 3 failure stages (before network, lost reply, explicit reject); zero automatic resend; nonce strictly increasing across restart; duplicate local intent dispatched≤1 | journal/transport fault tests |
| HL3 history | equal timestamp page saturation fails closed or splits safely; duplicate identical fill counted once; changed duplicate rejected; order/fill quantity mismatch blocks100% | deterministic fixtures |
| HL4 account | long/short/partial close/reversal, fee and funding cases match independent wallet within 0.000001 USDC; foreign position/order/fee token/activity, insufficient collateral and stale data block100% | account/risk tests |
| HL5 recovery | 3 deterministic crash/recovery cases; terminal orders and position agree with real Rust replay; unknownOid never maps to Rejected/absent; duplicate external order IDs=0 | real Engine tests |
| HL6 strategy | SMA bounded slow≤500 observations; closed-candle duplicates idempotent, corrected duplicate/gap rejects; paper and replay targets identical for fixed input | strategy/adapter tests |
| HL7 regression | scripts/verify.sh ends VERIFY OK; original journal replay position0/PnL12; no ignored/skipped tests used to claim coverage | retained logs |
| HL8 connectivity | production-public and Testnet meta/l2/candle responses + bounded WS subscription each decoded; signed Testnet action requires real Testnet credential | timestamped probes; missing prerequisites BLOCKED |
| HL9 soak | 900s paper and 2×900s Testnet, each actual runtime within ±5s excluding startup/cleanup; zero unexplained account mismatch/duplicate sends/unresolved mutation at successful shutdown; queue≤configured cap; RSS≤384MiB | run reports and raw traces |
| HL10 meaningful execution | ≥2 accepted orders and ≥2 fills across Testnet runs, ≥1 long and1short transition, ≥1 controlled reconnect/recovery; natural-signal shortfall NOT_MEASURED, never force trades under strategy label | venue reports; separate synthetic diagnostic allowed |
| HL11 H2 readiness | exact shared-collateral reservation, ≥2-instrument basket recovery and research parity before cluster-PCA deployment | PCA proposal ACs; NOT satisfied by H1 |

No latency improvement target: purpose is correctness on the venue the user intends to use. Fifteen minutes tests operational plumbing, not alpha or long-run memory stability. No production-reliable claim until live venue gates pass; no PCA-ready claim without HL11.

## Dependencies and secrets

Public HTTPS/WS hosts: api.hyperliquid.xyz and api.hyperliquid-testnet.xyz. Testnet signer: HL_API_PRIVATE_KEY; account identity: HL_MAIN_ADDRESS. Agent signing key may differ from account address; approval must already exist on Testnet. Signing requires actual securely injected key material locally, not an HTTP proxy replacement token. Do not display values or write them to run files.

Use pinned official hyperliquid-python-sdk signing support; public/paper and core tests must work without signer dependencies. Future setup must install the optional pinned requirements for Testnet signing. Do not install a mainnet trading SDK path by default or provide fund-transfer commands.

Handoff statuses: IMPLEMENTED/TESTED/BLOCKED/NOT_MEASURED independently for each HL ID. Source evidence includes SDK revision/package version and read date. The existing lifecycle FAILs remain visible and are not overwritten by this venue migration.

## v1.1 pre-run corrections (before signed venue testing)

Use `HL_NETWORK=testnet`, `HL_MAIN_ADDRESS`, `HL_API_ADDRESS`, `HL_API_PRIVATE_KEY`. Verify the signing key derives the configured agent. Classical account modes are `default`/`disabled`; reject unified and portfolio margin. The observed empty Testnet account has cross 20× as its default: the separate diagnostic `--configure-leverage` step sets cross 1× once, only without exposure. This is an explicit setup operation, not automatic runtime risk changes.

For BTC the diagnostic uses 0.0002 BTC, maximum position 0.0002 BTC, maximum single order USD40 (including a two-sided SMA reversal), minimum collateral USD100 and maximum loss USD10. These limits are configuration, not a guarantee against market gaps. One account writer per machine is enforced by a lifetime lock; exclusive use across machines remains required. No extra AWS infrastructure.

## Pre-rerun clarification — 2026-10-13T08:45:52.525203+00:00

The first actor soak failed on CandleGap; the original log did not retain its exact reason, so its root cause is not proven. New runs retain bounded candle-window diagnostics. A not-yet-published trailing bar is handled without inventing data: warmup reads one extra observation, accepts at most one missing trailing interval, and live signal delivery waits for a newer published bar. Interior gaps and longer delays still fail closed. No acceptance duration/RSS/queue threshold changes. Closeout now reconciles the disconnected writer before sending its bounded closing order. The original failed run remains FAIL even after a successful separate resume/closeout.
