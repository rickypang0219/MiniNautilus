# Hyperliquid Testnet operating guide

Created: 2026-10-13T03:49:46.038518+00:00. Scope: one dedicated account and one native USDC perpetual, BTC by default. This is the H1 execution migration in [the proposal](hyperliquid-migration-proposal.md). It does not deploy PCA or a shared-collateral portfolio.

## Install and verify

```sh
python3 -m pip install -r requirements.txt -r requirements-hyperliquid.txt
scripts/verify.sh
```

The official SDK is used only for Testnet signing. Public data, paper and ordinary unit tests do not require it. `verify.sh` builds the release executable and writes `target/runtime-v2-build.json`; signed evidence also requires a clean Git checkout. No secrets enter build receipts.

Secure environment bindings: `HL_NETWORK=testnet`, `HL_MAIN_ADDRESS` (account), `HL_API_ADDRESS` (approved agent), `HL_API_PRIVATE_KEY` (raw agent signing key). The key must derive the API address. Startup and every signed mutation verify that `userRole` binds that agent to `HL_MAIN_ADDRESS`; do not rebind an agent while a session is running. Never paste keys into commands, code, logs or chat. Agent approval must exist on Testnet. The adapter cannot submit mainnet orders.

Only use an exclusive account: no other machine, bot, manual orders, transfers or non-BTC positions during a session. A local process lock prevents duplicate local writers; it cannot lock other machines. Fresh sessions require no positions/open orders. Classical `default`/`disabled` account modes only; unified/portfolio margin, vaults and HIP-3 are unsupported.

## Probe and paper

Every fresh run needs a new directory:

```sh
python3 examples/hyperliquid_perp.py --mode probe --run-dir runs/hl-probe-new
python3 examples/hyperliquid_perp.py --mode paper --seconds 900 \
  --recovery-drill 300 --run-dir runs/hl-paper-new
```

Market data uses WebSocket candle liveness plus bounded REST reads of finalized bars. There is no Binance `x` close flag. Warmup/backfill is capped; gaps/corrected closed candles fail closed. The adapter can wait for one unpublished trailing minute, using one extra warmup observation. It never manufactures a bar, and new signals wait for a newer finalized observation. A missing interior bar or longer publication delay still fails closed. SMA keeps a fixed ring of at most 500 observations. Execution/private reports and accounting use bounded REST polling. Private WebSocket connectivity does not imply private-stream replay/recovery is implemented.

Paper uses the same Rust OMS but an optimistic touch simulator, including one touch opportunity after submitting a new order. This avoids expiring all minute-bar orders before the next simulated trade. Fees/funding/queue position are not modeled in paper; its PnL is not strategy evidence.

## Bounded execution diagnostic and live actor

Before the first run, configure BTC cross 1× in Testnet, or explicitly run the setup diagnostic below. It refuses setup with any position/open order and writes the signed setup attempt durably. Runtime never changes leverage.

```sh
python3 ops/hyperliquid_diagnostic.py --configure-leverage \
  --run-dir runs/hl-diagnostic-new
python3 examples/hyperliquid_perp.py --mode testnet --seconds 900 \
  --recovery-drill 300 --flatten --receipt target/runtime-v2-build.json \
  --run-dir runs/hl-live-new
```

**These commands send real Testnet orders.** The diagnostic intentionally trades long → flat → short → flat, killing the Rust writer after the first venue fill and before its report. It proves execution/recovery, not alpha. Defaults: 0.0002 BTC target, at most USD40/order including a reversal, USD100 minimum equity, USD10 loss limit. Recheck minimum notional against current prices. Collateral admission and loss checks cannot guarantee against market gaps.

The live runner uses SMA(3,8), with signals only from completed 1-minute bars. It has a 100-order session cap, 10-second order TTL, 900-second maximum session and a 384 MiB sampled combined RSS budget. Summary records queue caps (128 inputs/32 effects), runtime separately from setup/closeout, reconciliations and source/binary identity. A successful 15-minute run is not proof of 24/7 operation.

`--flatten` is explicit: after a successful quiescent reconciliation, send one bounded order to close known exposure and reconcile again. Without this flag, shutdown cancels owned resting orders but preserves filled positions. Uncertain exposure is never automatically flattened. If closeout fails, the summary is FAIL and operators must recover the same saved session.

## Recovery and stop conditions

Use identical risk/strategy parameters and the original directory:

```sh
python3 examples/hyperliquid_perp.py --mode testnet --resume --reconcile-only \
  --flatten --receipt target/runtime-v2-build.json --run-dir runs/hl-live-new
```

Recovery queries the original cloids, cancels only owned orders, proves terminal quantities, deduplicates complete fills and checks position plus realized PnL, USDC fees/funding and account cash. Core cash is gross tick-lots, not a perpetual margin ledger. The adapter's separate wallet check supplies this restricted account contract.

A missing `orderStatus` result is **unresolved**, never proof of rejection. The same local action cannot be dispatched twice, even after a lost HTTP reply or process restart. Unknown lifecycle, history truncation, unsupported schemas, foreign activity, stale account data, changed duplicates or cash mismatch stop admission. Re-running `--resume` retries reconciliation, never submission. Some unknown orders require operator investigation; there is no fabricated absence proof.

Preserve `events.jsonl`, `actions.jsonl`, `session.json`, `config.json`, provenance and summaries together. Never delete the action journal to retry an order. Credentials are not stored there. `runs/` is Git-ignored; durable acceptance summaries and artifact hashes belong in documentation, raw artifacts need separate retention.

No Hyperliquid live rotation is enabled yet. Retained fills/actions are bounded by session limits; do not extend these diagnostic runners into unattended long-lived services. Multi-instrument portfolio margin, realistic PCA backtests, funding-factor research and mainnet risk approval remain separate tasks.
