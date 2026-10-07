# Order churn, latency and cross-ledger verification

This experiment tests whether delayed updates can cause the wrong order quantity,
stale direction, double dispatch, duplicate fill accounting or premature replacement.
Signals deliberately alternate long/flat; they are labeled synthetic stress targets,
not SMA forecasts. The normal SMA runner now uses the same Rust target guard.

## What must agree

At submission time, the order must reference the current target revision and the
current engine position. Its signed quantity must equal target minus position, and
all previous orders must be terminal and certain. Client IDs are never reused.

While reports are delayed, the engine and venue position **can legitimately differ**.
We check that the difference is exactly the signed quantity of independently known
venue fills not yet delivered to the engine. Actual venue inventory must remain inside
the engine's pending-order exposure bounds and inside the Spot long-only limit.

At a quiescent reconciliation barrier, all owned orders are terminal. Venue fills,
engine fills, gross position and gross cash must agree exactly. Spot wallet changes
must also account for base-asset commissions. Duplicate reports must not change cash
or position. A new signal cannot undo fills on a previously accepted order; cancellation
races are handled by incorporating those fills before calculating a replacement.

## Rust guard

`SetTarget` records a monotonic revision, target position and expiry. `SubmitTargeted`
carries that revision plus the position used to calculate the order. Rust validates
both before applying ordinary freshness, ID and risk checks. Disconnect invalidates
the active target but retains the revision high-water mark. A duplicate/older target
cannot resurrect an old plan.

The low-level `Submit` event remains available for existing reference examples and
manual order semantics. It enforces risk, not a strategy target. Target-driven callers
must use `SubmitTargeted`; both new runners and `sma_spot.py` do so.

A regression test demonstrates the distinction: target 5, risk cap 10, then a partial
fill of 2 and cancellation. Reusing the old buy-5 plan would reserve position 7 and pass
the generic risk cap. The targeted API rejects that plan; the valid replacement is buy 3.

## Adversarial paper execution on real quotes

```sh
cargo build
.venv/bin/python examples/order_stress.py --run-dir runs/order-stress-new --seconds 60 --max-orders 200 --seed 7
.venv/bin/python examples/verify_order_run.py runs/order-stress-new
```

An independent actor reads public Testnet best-bid/ask REST snapshots, coalescing quotes
and preserving request-start timestamps. It does not share execution state with Rust.
The coordinator continues processing its delayed-message queue while that actor waits
on HTTP. Quotes older than five seconds stop the experiment safely. We initially tried
bookTicker and partial-depth streams; quiet streams triggered conservative stale-feed
stops, so the completed experiments use explicit REST refreshes for liveness.

The independent simulated venue schedules partial fills, 70–200 ms ACK delays,
20–250 ms fill-report delays, duplicate fills, cancellation delays, delayed signals and
duplicate intent callbacks. Execution liquidity is synthetic, at the accepted limit
price; these are **not actual exchange fills** or a calibrated queue model. Old-epoch
reports are exercised after reconciliation. Fault RNG is seeded, but wall-clock timing
is not reproducible from the seed alone: replay the recorded journal for exact history.

`trace.jsonl` records every engine input, effect, target, engine position, venue position
and exposure bounds. `venue-ledger.json` is the separate simulated order/fill ledger.
The verifier checks trace inputs against the durable journal, validates every accepted
order against its logged target/position, detects duplicate dispatch IDs, replays Rust,
and checks final fills/position/cash against the independent ledger.

## Real Spot Testnet cancel/requote cycles

```sh
.venv/bin/python examples/testnet_order_churn.py --run-dir runs/churn-new --cycles 4 --execute-testnet
.venv/bin/python examples/verify_order_run.py runs/churn-new --exchange

# Recovery without generating new signals
.venv/bin/python examples/testnet_order_churn.py --run-dir runs/churn-new --recover-only --execute-testnet
```

Default quantity is 0.0002 BTC, four cycles, at most eight orders, and a 200 USDT
per-order cap. Each cycle places a passive order, polls its state, updates the target
revision, requests cancellation and confirms terminal state including fills. Only then
can it recalculate and submit a marketable-limit replacement. It deliberately resends
a stale callback into Rust (which must refuse it before any wire request) and delivers
each real fill report twice (which must have no second accounting effect).

`--passive-offset-ticks 1` places the passive order nearer the touch to increase the
chance of a genuine fill/cancel race. `--cycles` is bounded to 1–10. The adapter retains
its request pacing and server-directed rate-limit backoff; this does not attempt to
load-test Binance. Repricing uses cancel-confirm-new, not Binance's `cancelReplace`
endpoint, whose partial-success semantics would require a separate state machine.
[Binance REST semantics](https://developers.binance.com/en/docs/products/spot/rest-api)
and [order-count limits](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/order_count_decrement.md).

Every cycle reconciles complete owned order/fill history and base balance including
fees. The independent `--exchange` verifier makes only GETs, re-fetches every order and
trade, validates trade direction and quantity, and checks gross cash and net base
balance. It writes `exchange-audit.json` and `exchange-verification.json`. Local evidence
under `runs/` is ignored by Git. Cleanup cancels outstanding orders but does not force
liquidation of filled inventory; inspect the final summary.

## Measured results (2026-10-06)

| Experiment | Engine events | Submits / cancel requests | Unique fills | Final engine / venue position | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Paper seed 7, 60.054 s | 8,612 | 190 / 190 | 171 | 19 / 19 lots | All cross-ledger checks passed |
| Paper seed 31, 60.178 s | 8,534 | 177 / 177 | 183 | 8 / 8 lots | All cross-ledger checks passed |
| Testnet, four cycles | 84 | 8 / 4 | 4 | 0 / 0 lots | Five reconciliation barriers passed |
| Testnet, six near-touch cycles | 114 | 10 / 4 | 6 | 0 / 0 lots | Seven reconciliation barriers passed |

Paper runs processed about 142–143 engine events/second, with 1,027 target updates in
aggregate. They exercised 260 cancel-after-partial-fill cases and 227 reconciliation
barriers. Seed 7 rejected 788 stale/unresolved candidate actions, including one whose
position snapshot had changed; seed 31 rejected 817. No unexplained position gap,
exposure-bound violation, phantom fill or duplicate wire ID was observed. The maximum
explained position gap was 20 lots: an entire target can fill before reports arrive.

The four-cycle Testnet run completed four replacements and refused four stale
callbacks. Four deliberate duplicate real fill reports had no second accounting effect.
A separate fresh exchange audit verified all eight orders and four trades, no remaining
open orders, gross position zero, and commissions of 0.00000040 BTC and 0.03435560 USDT.
Gross cash was -980 tick-lots; the base balance change was exactly -0.00000040 BTC.

A second Testnet run used a one-tick passive offset for six cycles. Two passive orders
filled before cancellation was needed, so no replacement was submitted for those
cycles. It finished with ten orders (six filled, four canceled), six stale callbacks
blocked, six duplicate reports ignored, and seven successful reconciliation barriers.
Fresh GET-based audit confirmed gross flat, gross cash 65,520 tick-lots, and commissions
of 0.00000060 BTC and 0.05159065 USDT. Both Testnet runs finished with no open orders.
These actual fills-before-cancel differ from the deliberately injected in-flight
partial-fill/cancel races in the paper tests; we do not claim the same race occurred
on the exchange.

The ordinary SMA paper runner also passed with the new guards: 30 closed bars,
five submissions/fills, 182 state checks, no reconnects, and exact Python/Rust replay.
Its final simulated position was 100 lots; no exchange orders were made by that run.

## Limits of the evidence

This validates defined interleavings at measured reference-engine rates. It is not a
proof of HFT correctness, a throughput benchmark, or evidence of production readiness.
Full-state JSON IPC, reference-state cloning and durable fsync limit the event rate.
Testnet order polling and cancel/fill reads are not an atomic exchange snapshot while
orders remain active; exact equality is asserted only at the terminal barrier.

Independent risk caps and target correctness are different guarantees. Rapid input
rates can also make `max_signal_lag` reject a callback whose wall-clock TTL is still
valid; the recorded refusal reasons expose that trade-off. Private WebSocket execution,
native exchange amendments, fee-aware net targets and fully isolated execution actors
remain future work. No margin borrowing or production orders are involved here.


Validation includes 27 Rust tests and 35 Python tests. The targeted-order regression
explicitly demonstrates that the old generic risk check accepts a target-overshooting
plan while the new targeted entry point refuses it. Logs, raw exchange audit evidence
and verification reports remain locally under `runs/order-stress-03`,
`runs/order-stress-04`, `runs/testnet-churn-01` and `runs/testnet-churn-02`.

Recovery-only restart of the second Testnet run was also verified: zero new submissions
or cancellations, two successful barriers, unchanged six fills and gross flat. The
verifier includes the startup `Disconnect` automatically appended by journal recovery,
so trace/journal coverage remains exact across restart (122 total engine events).
