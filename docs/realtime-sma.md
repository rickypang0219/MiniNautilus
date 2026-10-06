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
per-order cap 200 USDT, duration 120 seconds, six durable orders across the session, including after resume. Limits
are checked against tick/lot and minimum notional before persisting a submit. Exchange
filters, free balance and account restrictions can still reject an order.

To recover and verify without new signals, add `--resume --reconcile-only` to the
original Testnet command.

## Reliability boundaries

- Candle reception is WebSocket; best bid/ask and private order discovery are REST.
  REST blocks the coordinator, so this is **not** isolated actor execution yet.
  Quote timestamps include REST request latency; stale quotes cannot become fresh by
  sitting in the input buffer. Private freshness is refreshed after successful polling and an authenticated account
  read, even when there are no active orders. A silent market stream reconnects after 15 seconds.
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
  is the durable Rust journal; `summary.json` contains the latest invocation counts and final inventory. Each invocation
  also has an immutable `summary-<id>.json`, so resume does not erase prior results.
  Summaries include total session orders, injected faults and bounded IPC p50/p99/max
  samples (up to the latest 10,000 engine calls; not exchange latency).
- Paper fills are optimistic touch fills, not exchange queue simulation. Fees are checked
  in reconciliation but not deducted from the Rust gross position/PnL. Base fees or dust
  can make the available sell quantity differ from gross inventory, particularly without
  pre-existing test inventory. Before selling, the runner checks free base inventory and
  blocks insufficient sells without creating a durable submit. Reconciliation records
  commissions by asset and net base change, including when gross position is flat. It
  does not yet resize sells or implement a fee-aware net-position strategy ledger.
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


## Repeatable fault soak

```sh
.venv/bin/python examples/sma_spot.py --run-dir runs/sma-fault-soak --mode paper --interval 1s --fast 3 --slow 8 --seconds 600 --max-orders 20 --faults
.venv/bin/python examples/replay_sma.py runs/sma-fault-soak
```

`--faults` is paper-only: drop one finalized candle after 15 seconds, inject one
identical duplicate after 30 seconds, age a message by 60 seconds after 45 seconds,
and close the WebSocket after 60 seconds. Every injection is labeled in the observations;
these are synthetic faults on a real public stream, not spontaneous venue incidents.
Backfill verifies the exact expected timestamps and close times before mutating the
strategy; matching row counts alone are insufficient. Four failed REST reads keep the
gate closed. Reconnection allows three consecutive failures with 1/2/4-second backoff; 30 seconds
of fresh stream progress resets that budget. Total runtime remains bounded by `--seconds`
plus bounded request/cleanup time. Reconciliation retries discovery at most three times,
without resubmitting orders; credential/signature errors stop immediately.

GitHub Actions runs formatting, Clippy, Rust tests and offline Python integration tests
on Linux. It does not load secrets or contact the exchange. Local verification uses
`cargo build && python -m unittest discover -s tests -p 'test_*.py'` after installing
`requirements.txt`.


## Follow-up findings

- The first fault soak stopped safely after about 281 seconds: two injected reconnects
  and two additional connection failures exhausted the old total-retry limit. It had
  processed 255 closed bars, 20 simulated fills, 1,285 invariant checks, and ended flat.
  Replay reproduced all 294 recorded candles. We changed the limit to consecutive
  failures so healthy operation between transient failures restores the budget.
- A 20-second authenticated resume processed 18 stream bars with zero new submissions
  at a session cap of two orders. Its shutdown reconciliation failed; order/fill/balance
  queries afterward were consistent, with no open orders. The original error lacked
  detail, so its exact cause is unconfirmed. Reconciliation errors now record safe
  adapter reasons and use bounded rediscovery retries. A subsequent reconciliation-only
  restart completed successfully without new orders.
- The actual Testnet round trip charged 0.00000100 BTC on the buy and 0.08530724 USDT
  on the sell. Gross position was zero, while net base change was -0.00000100 BTC.
  This is now explicit in reconciliation observations, rather than calling gross flat
  an unchanged wallet. A free-base preflight blocks unaffordable sells before submitting.
- Private heartbeat previously advanced even when polling an empty order list made no
  authenticated request. The coordinator now verifies an account read before advancing
  that heartbeat.
- Summary history is immutable per invocation; the latest pointer is replaced atomically.
  Durable orders, including those recovered after restart, count toward `--max-orders`.
