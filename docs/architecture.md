# Architecture and contracts

## Ownership and boundaries

The Rust core owns all mutable order, fill, position, cash, and risk state. An event
is processed as one transaction, then produces effects. No wall clock, socket,
filesystem, Python callback, or thread primitive occurs inside the state machine.
Core transitions are identical in paper and live modes; market/execution adapters
and the driver clock differ. The initial core assumes a known empty strategy book;
the live runner verifies no symbol open orders and records the wallet baseline.

Transitions prepare a fixed-size header and an owned write set without mutating
the published Core. Ordinary fills copy one order, not historical maps; quote and
trade validation do not inspect history. A single-use prepared token exclusively
borrows Core until commit or abort. Invalid external events discard their staged
writes/effects, then prepare the original gate behavior with consumed sequence/time.
`DurableEngine` holds this token across journal sync and commits only on success.
Commit performs no recoverable business validation or callbacks; collection inserts
may allocate, so this does not promise OOM or arbitrary panic recovery. Full
reconciliation still builds and validates replacement history. BTreeMaps retain all
orders/fills for ID deduplication; there is no retention/compaction policy yet.
A derived, unserialized index (open orders and `(deadline, id)`) is updated by every
committed order write and rebuilt on deserialization, so risk bounds, timeouts, the
target barrier and disconnect handling walk open orders only. `PaperExchange` keeps a
separate resting set and still matches in ID order. See [acceptance](acceptance.md).
See [the state-transition walkthrough and measurements](core-state-transitions.md).

Python uses JSON-lines IPC, not PyO3. That makes process boundaries explicit and
requires no additional interpreter bindings. Protocol 2 (`src/protocol.rs`) sends the
complete state at startup and after a full reconciliation; every other response is
a fixed-size header plus the orders/fills that request wrote, so response size does
not grow with history. The bridge applies deltas to a mirror equal to the full state.
`sim` runs the same loop with an in-memory Core for backtests; `mininautilus
backtest` runs a precomputed target series fully in-process (see acceptance B3). The live example uses a separate
strategy process; the replay example invokes the same callback synchronously.
Both submit the same typed intent to the same Rust checks. Recorded event ordering,
including strategy results, is replayable; independently rerunning a parallel
strategy is not guaranteed to recreate the same scheduling.

## Risk and OMS invariants

- Price is an integer count of ticks; quantity is integer lots. No binary floats.
- New IDs cannot reuse an existing order, even a terminal one.
- Worst-case long = position + remaining buys. Worst-case short = position -
  remaining sells. Opposing pending orders do not net away reservations.
- Submit check and reservation happen in one state transition, before any effect.
- Pending cancel keeps its reservation; timeout marks uncertainty rather than rejection.
- Lifecycle, pending action, and uncertainty are separate state dimensions.
- A fill ID is counted once; the same ID with a changed payload closes the gate.
- Accepted acknowledgements never regress filled/canceled orders.
- An acceptance or cancel confirmation after a submit rejection is contradictory:
  retain the rejected state and gate the account for reconciliation.
- Canceled reports carrying missing cumulative fills cannot release remaining risk.
- Stream gaps, unknown orders, malformed reports, or inconsistent snapshots gate new orders.
- Stale market data/private heartbeats and expired/stale strategy intents cannot submit.
- Kill is latched; restart/reconciliation cannot clear it. Cancel remains available
  when the connection permits it. Automatic liquidation is not implemented.

This is a **gross strategy inventory limit**, measured relative to session origin,
not an account-wide collateral/margin model. Initial Spot holdings, fees, other
instruments, leverage, and cross-account risk are not part of core pre-trade risk.
Binance enforces its balance/filter checks. Recovery independently compares wallet
base balance against baseline + gross session fills - base-asset fees. Therefore
only dedicated Testnet accounts are supported for execution experiments.

## Time and events

Each input has a contiguous engine sequence and nondecreasing engine milliseconds.
The driver supplies virtual time in replay and elapsed monotonic time in live mode.
Equal timestamps retain explicit arrival order. Strategy intents carry the sequence
they observed plus an expiry; risk always evaluates current state.

Timer delivery is explicit (`Tick`). Core does not run background timers. Resting
accepted orders have no action deadline. Submit/cancel requests do; a quiet resting
order is not automatically stale. Private heartbeat expiry is a separate check.

The generic execution envelope models an epoch and venue sequence. Tests inject
duplicates, gaps, old epochs and reordered reports. The Binance REST adapter's
sequence is only a local delivery sequence; it does **not** claim to detect missing
exchange stream packets. WebSocket feed sequencing is a future adapter extension.

## Persistence and the uncertain-send window

1. Prepare an owned transition while leaving published state unchanged.
2. Append the complete input frame with chained FNV-1a corruption checksum.
3. `sync_all` the journal: after every input (`--sync every`, default), or only
   before an input whose effects leave the process (`--sync outbox`), which also
   covers every earlier unsynced input. See acceptance L3 for crash guarantees.
4. Publish the new state and return effects to the transport.

An I/O error poisons the runtime and returns no effects. The file might nevertheless
contain the input, so restart always replays without sending historical effects
and then closes the trading gate. The caller must never retry an old SendOrder
blindly. Durable intent before sending does not provide exactly-once delivery.

A crash after persistence but before actual send stays unresolved while the venue
merely reports no such order: it could still arrive. Only proof of absence resolves
it: `Reconciliation.absent` lists never-acknowledged, unfilled orders the venue
cannot have received (Binance: queried by client ID after the request's recvWindow
expired). Core marks them Rejected and never reuses their IDs (acceptance L4).

Files are exclusively locked for writer lifetime. Inspection takes a shared lock
and refuses an active writer. Recovery validates all complete records first, then
truncates only a final incomplete line. Complete checksum/sequence corruption fails
closed. FNV detects accidental corruption; it is not authentication. Removal of a
whole valid suffix cannot be detected without an external durable high-water mark.

Snapshots use temporary-file sync, rename, and directory sync. They cannot overwrite
the journal. Snapshot state is checked against full journal-prefix replay before
replaying the tail; v0 snapshots verify a checkpoint but do not accelerate startup.
Rotation (`mininautilus rotate OLD NEW`) closes a healthy, fully resolved journal
and starts a successor whose genesis carries balances, kill latch, epoch and the
highest used client order ID; this bounds retained history (acceptance H4). There
is no binary schema migration beyond schema version 1.

Durable paper sessions also accept an isolated `rotate_to` request. It requires
flat inventory in addition to the resolved/Healthy boundary above. The writer
syncs and closes before offline rotation, recovers the successor gated, then
reconciles a fresh paper venue. The response is full state so Python releases its
old order/fill mirrors and resumes from the carried ID floor. Automatic live
rotation is refused: a live adapter must establish its own venue reconciliation.
`ops/synthetic_session.py --rotate-orders N` waits for this boundary; N is a soft
trigger and does not force liquidation. Rotation scans the predecessor journal,
so transient RSS can exceed the retained-history size.

Optional `MINI_ORDER_TRACE` instrumentation adds request IDs and per-action stage
measurements, journal sequence, bytes written, and bytes pending the action's
foreground sync. It does not change the input/effect persistence barrier.
Background flushing and a new frame encoding were not adopted; see the measured
outcome in `docs/deep-dive-20261011.md`.
Durability depends on filesystem/device sync semantics; no power-loss hardware
test has been performed. Filesystem calls have been exercised on macOS.

## Recovery

Generic reconciliation requires a complete atomic venue history at a watermark:
all known orders, all fills from session origin, and independent position. It checks
identity/terms, missing/unknown orders, duplicate fills, cumulative quantities,
known fill retention, and position. Reports through the watermark are then obsolete.
The adapter is responsible for buffering later reports and establishing the barrier.

The paper venue can return such a snapshot directly. Binance REST cannot, so the
Testnet adapter uses a different way to obtain stable facts:

1. Disconnect/reconnect closes the core gate and starts a new epoch.
2. Discover unknown open orders; stop for investigation if any exist.
3. Cancel each owned nonterminal order using a deterministic cancellation ID.
4. Re-query until the current attempt sees every owned order terminal.
5. Read every order's trades using `orderId + fromId` pagination from zero.
6. Require summed fills to equal each terminal order's executed quantity.
7. Compare gross cumulative position and baseline-adjusted base wallet balance,
   including base-asset commissions; require no open orders remain.
8. Apply the snapshot to Rust. Only fresh subsequent quotes can enable a new intent.

There is no automatic POST retry. HTTP/network ambiguity, absent order history,
testnet resets, rate limiting, schema changes, balance changes, or lagging REST data
can leave the gate closed. A later `--resume` retries recovery, not submission.
Cancellation can race with a fill; terminal history plus complete fills resolves it.
The adapter only cancels IDs belonging to its saved session. It never adopts unknown
orders or cancels another writer's orders.

This REST protocol relies on an exclusive account writer and stable terminal-order
semantics; it is not an exchange-provided atomic snapshot. Fully completed foreign
round trips may be invisible to net balance checks. Do not share the execution
account. Trade busts/corrections, replace/amend, OCO, STP-specific accounting, and
history retention/reset recovery are outside v0's supported model.

## Simulation and cold path

`PaperExchange` accepts limit orders and consumes explicit market-trade liquidity
in client-ID order. It supports partial fills, cancel races, dropped ack, disconnected
delivery, and duplicate fills. Input timestamps control when a report is delivered;
tests reorder delivery explicitly. There is no order-book queue position model,
exchange price/time priority model, endogenous market impact, or sampled latency.

The live paper example converts quotes to optimistic touch opportunities, capped
at one lot per poll. It demonstrates integration, not realistic PnL. Offline replay
uses the explicit fixture trade events and the same Rust matching implementation.

Telemetry uses a bounded channel and separate writer thread. Its overflow counter
is independent of trading state. Event journal records are never sent through this
lossy channel. SQLite projection runs after the session from Rust-validated replay.
It reports gross average-cost realized and marked unrealized PnL, without fees,
funding, FX conversion, or portfolio-wide accounting. Execution ID ordering is the
supported simulator/Binance symbol chronology; other venues need an explicit fill
time/order contract before using the analyzer.

When no current quote is available, an open position's unrealized and total PnL
are unknown (`null`), rather than marked at average cost. Realized PnL is still
known. A flat account needs no mark and keeps its known cash PnL. Consumers of the
SQLite summary JSON must handle these nullable fields.

## Reference sources

- [NautilusTrader architecture](https://nautilustrader.io/docs/latest/concepts/architecture/)
- [Rust CPU affinity example](https://rustmagazine.github.io/rust_magazine_2021/chapter_3/rust_cpu_affinity.html)
- [OMS/PMS architecture discussion](https://orbb.li/blog/20260106-trading-system-architecture/)
- [Rustonomicon atomics](https://doc.rust-lang.org/nomicon/atomics.html)
- [Rust atomic orderings](https://doc.rust-lang.org/std/sync/atomic/enum.Ordering.html)
- [Binance Spot Testnet REST API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api)
- [Binance symbol filters](https://developers.binance.com/en/docs/products/spot/filters)
