# Spot Testnet SMA experiment

Python computes fast/slow SMA from **finalized** WebSocket candles. Fast > slow targets
`quantity` BTC; fast < slow targets zero; equality holds the previous target. Warmup
uses closed REST bars. Integer sums avoid floating-point crossing ambiguity. Rust owns
risk checks, order lifecycle, fills, and the durable journal. A single pending order
prevents repeated target submissions. This is a correctness lab, not HFT.

## Setup

```sh
cargo build
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.testnet.example .env.testnet
```

Fill in `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET` locally. The runner
loads this file without evaluating shell commands. Never commit credentials. Use a
dedicated Spot Testnet account/symbol with no competing account writer. Existing base
inventory is recorded as a baseline; engine position measures this session's trades.

```sh
# Public live candles, simulated fills; no credentials needed
.venv/bin/python examples/sma_spot.py --run-dir runs/sma-paper --mode paper --interval 1s --fast 3 --slow 8 --seconds 120

# Actual orders using Spot Testnet virtual balances
.venv/bin/python examples/sma_spot.py --run-dir runs/sma-testnet --mode testnet --interval 1s --fast 3 --slow 8 --seconds 120 --max-orders 6

# Verify Python signal replay and Rust journal replay, without placing orders
.venv/bin/python examples/replay_sma.py runs/sma-testnet

# Recover the same identity and journal before continuing; use identical parameters
.venv/bin/python examples/sma_spot.py --run-dir runs/sma-testnet --mode testnet --interval 1s --fast 3 --slow 8 --seconds 120 --max-orders 6 --resume
```

Default interval is 1 minute, fast/slow 5/20. The 1-second configuration accelerates
flow testing; it is not a profitability recommendation. Default quantity is 0.001 BTC,
per-order cap 200 USDT, duration 120 seconds, six submissions per invocation. Limits
are checked against tick/lot and minimum notional before persisting a submit. Exchange
filters, free balance and account restrictions can still reject an order.

To recover and verify without new signals, add `--resume --reconcile-only` to the
original Testnet command.

## Reliability boundaries

- Candle reception is WebSocket; best bid/ask and private order discovery are REST.
  REST blocks the coordinator, so this is **not** isolated actor execution yet.
  Quote timestamps include REST request latency; stale quotes cannot become fresh by
  sitting in the input buffer. Private freshness is refreshed after successful polling.
- Small bounded WebSocket buffers, message-age checks and contiguous bar checks gate
  new orders. REST can lag WebSocket at startup: missing intervals are fetched with a
  bounded retry. Persistent gaps stop or reconnect with a new warmup.
- Connection/transport failure clears the market gate. Testnet recovery cancels owned
  orders, queries complete order/fill history, and reconciles base balance including
  base-asset commissions before reopening. Unknown orders and absent ambiguous submits
  remain blocked; no automatic resubmission.
- Resting orders are canceled after the order TTL. On shutdown owned orders are canceled
  and reconciled. **Filled inventory is retained, not automatically liquidated.**
- Resume replays the Rust journal without sending historical effects, reconciles, then
  warms the SMA from current closed bars. It does not replay missed signals as orders.
- Each engine response checks fill/order totals and 0 <= session position <= limit.
  `observations.jsonl` captures candles, signals and operational events; `events.jsonl`
  is the durable Rust journal; `summary.json` contains counts and final inventory.
- Paper fills are optimistic touch fills, not exchange queue simulation. Fees are checked
  in reconciliation but not deducted from the Rust gross position/PnL. Base fees or dust
  can make the available sell quantity differ from gross inventory, particularly without
  pre-existing test inventory. This remains a limitation requiring a fee-aware ledger.
- Kill/recovery and durable fsync are present; funding, liquidation, margin borrowing,
  private WebSocket order reports and non-blocking REST workers are outside this version.

## Margin

Spot Testnet supports `/api` only and cannot test `/sapi` margin borrow/repay. This runner
therefore supports long/flat only, with no borrowing and no production endpoint option.
See [Spot Testnet FAQ](https://testnet.binance.vision/) and
[Margin borrow/repay](https://developers.binance.com/docs/margin_trading/borrow-and-repay/Margin-Account-Borrow-Repay).

## Observed experiment (2026-10-06, local run artifacts)

- Initial 1-second paper stream exposed REST history lag at startup, producing a missing
  candle between the warmup and WebSocket. Added bounded interval backfill, then reran.
- Paper: 34 closed stream bars, six submissions/simulated fills, final flat,
  175 state-invariant checks. No reconnects after the backfill fix.
- Spot Testnet: 65 closed stream bars, two submissions and two exchange fills
  (buy then sell), final flat, 206 state-invariant checks. Duration including cleanup
  about 70 seconds. Independent balance/fill reconciliation passed, no reconnects.
- Offline replay reproduced all 76 recorded candles (warmup/backfill included), SMA
  targets, and Rust final position/fill count. The initial candle-gap failure was real;
  network outage/lost-ACK behavior was not exercised by this successful live run.

These observations establish end-to-end connectivity and bookkeeping on this short run;
longer soak tests, private stream integration and additional injected faults remain useful.
Raw local artifacts are under `runs/sma-spot-testnet-01/` and excluded from Git.
