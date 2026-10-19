# Native feed consistency and admission

Scope: native Rust Hyperliquid Testnet, one account / BTC. Base:
`2ae0f642eb1096e9d37f6f815076e0190abd041d`. Verification uses offline fault
simulations; this does not establish exchange freshness or live acceptance.

## Contract

A later REST response is not necessarily a later venue fact. A subscription ACK
does not prove a usable market view. Arrival order is not an exchange-wide
sequence or atomic account watermark.

WebSocket/execution workers publish bounded inputs; only the writer journals and
changes Core. Private WS messages still wake REST verification. This patch does
not promote private WS payloads into fills, solve acknowledged-but-unqueryable
orders or increase query timeouts. Uncertainty retains original identities and
closes admission rather than resending.

### Public market view

`l2Book` is a self-contained best-bid/ask snapshot, not an incremental book. The
writer retains one accepted view per connection generation. It rejects obsolete
generations, older exchange timestamps, future timestamps, over-age snapshots and
over-age queue residence. Stream wall/monotonic receive times are captured before
recording/normalization. Exact duplicates at the same timestamp cannot renew quote
lifetime. Different prices in the same millisecond remain eligible in arrival
order: timestamps cannot prove continuity or order equal-time events.

Core receives the existing `QuoteObserved` with conservative freshness time
`min(exchange_time, receive_time)`; journal `EventTime` retains the separate source
and receive times. The five-second market budget is unchanged. This assumes enough
venue/local clock agreement; future timestamps fail validation. No exchange
sequence is invented, and silent loss of intermediary full snapshots is unproven.

Expiry closes the circuit, invalidates the Core quote via `MarketUnavailable`,
clears the target and resets the view cursor. Even before the writer dequeues the
expiry control, the market handler checks the atomic source generation. Recovery
still drains old execution results before advancing the epoch. New subscriptions,
account reconciliation and a fresh current-generation quote are required before
admission can reopen.

An expired view closes admission, journals `MarketUnavailable` and clears its
target. Fresh data alone cannot revive that cleared target. Each submit job also
carries the quote deadline and monotonic receive age. The executor rechecks both
before dispatch and caps signed intent expiry at the source deadline. An already
handed-off request may remain uncertain and cannot be undone locally.

### Strategy input

The bounded REST candle/SMA batch retains the epoch sampled before fetching.
A reconnect during that batch cannot relabel results with the new epoch. Existing
epoch/revision/deadline checks reject obsolete results. Continuity, unchanged
overlapping closes and bounded warmup remain enforced. The historical SMA window
can advance during an obsolete batch; only later fresh input can authorize a new
target. This does not model REST caching or guarantee candle publication latency.

### Private facts

Every regular poll checks every collected path against committed Core facts,
including ordinary active orders and the first poll with an empty terminal cache.
Each known execution ID must remain with exactly the same order, quantity and
price. Equal cumulative quantity with a replacement ID is insufficient. A terminal
lifecycle cannot become active or change to another terminal lifecycle under the
supported no-correction model.

Missing/regressing evidence uses the existing three-read poll budget; exhaustion
becomes execution uncertainty. Changed immutable fill payloads fail immediately.
No reports from an incomplete multi-order poll are published. An order-indexed
count pass avoids scanning the whole Core fill map per order. Existing ownership,
terminal economic proofs, ledger checks and financial tolerances remain. Trade
busts/corrections require separate semantics and remain unsupported.

## Invariants and tests

| Architecture invariant | Regression |
|---|---|
| Stale market data cannot submit | `older_or_duplicate_books_do_not_overwrite_or_refresh_the_core_quote`, `a_recent_receive_does_not_make_an_old_exchange_snapshot_fresh`, `queued_market_backlog_does_not_acquire_a_new_receive_time` |
| Source recovery cannot reuse old inputs | `source_pause_fences_market_before_the_expired_control_is_dequeued`, `expiry_drains_old_fill_then_requires_reconciliation_and_new_subscription` |
| A REST candle response cannot acquire a later epoch | `candle_response_crossing_reconnect_keeps_its_original_epoch` changes epoch during a localhost HTTP request and verifies writer rejection |
| Subscription ACK is insufficient | `subscription_ack_without_a_current_book_cannot_open_admission` |
| Timestamps are not sequences | `equal_millisecond_books_can_change_without_claiming_a_sequence` |
| Pricing expiry is checked at executor handoff | `queued_order_cannot_outlive_its_quote_even_with_a_fresh_intent` |
| Invalid market state and cleared targets survive replay | `stale_book_closes_admission_and_clears_target_until_fresh_data` |
| Known fills are immutable; terminal state cannot regress | `regular_poll_cannot_drop_replace_or_regress_already_committed_facts`: missing, changed, replacement-ID and terminal-to-open cases |
| No blind resend or synthetic rejection | Queued-order regression replays the original pending intent without reports; existing lost-ACK/unknown-order/recovery tests remain enabled |

Before implementation, the book-order test failed because a duplicate renewed
quote receipt time. The REST test failed because an old open/empty-history response
was accepted despite a committed partial fill. REST simulations use localhost HTTP,
synthetic data and zero exchange mutations.

The pure Core event schema and transition rules are unchanged. Adapters constrain
which existing events reach it; journals remain deterministically replayable.
Multi-instrument routing, portfolio risk, shared rate budgets and stream-first
private execution remain separate work.

## Verification result

`scripts/verify.sh` passed on frozen sources: 59 native-feature library tests,
enabled integration/comparison/lint checks, 220 Python tests (one skipped) and
four dashboard tests. An earlier verification attempt rejected changed build
inputs because a test was added during verification; the frozen rerun passed.
The two original regressions failed before the implementation change as recorded
above. Independent `examples/replay.py` returned position 0, gross PnL 12 tick-lots,
two orders and three unique fills.

Verbatim final verification tail:

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
VERIFY OK
```
