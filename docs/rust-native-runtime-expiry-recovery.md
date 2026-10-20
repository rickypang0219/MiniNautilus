# Native WebSocket expiry recovery addendum

Created: 2026-10-16T02:56:41+09:00 (Asia/Tokyo).
Scope: repair the failed RN-A6 integration gate without changing Core, journal durability or financial invariants.

## Evidence and decision

The initial AWS paper run stopped after 684.0367 seconds with an unclassified WebSocket frame. After adding bounded frame diagnostics, a second independent run stopped at 678.8006 seconds with **close 1000, reason `Expired`**. Both runs safely finished flat; neither reached 900 seconds and neither started signed acceptance. Their instances, EBS and temporary SecureString parameters were deleted. The first close code was not captured and cannot be established retrospectively.

A normal transport expiry needs a new subscription plus account reconciliation. It is not evidence that the account is unchanged, an order was rejected or an old strategy answer remains valid.

## Implementation and invariants

1. Only exact close `1000/Expired` can request renewal. Other close codes/reasons, malformed frames, transport failures and unknown action outcomes still stop the session. Allow at most three renewals, with one-second backoff; connect and subscription deadlines remain bounded. This is not automatic recovery from process/host loss and is not an indefinite 24/7 policy.
2. The WebSocket producer advances a **local source generation** and pauses the admission latch before publishing expiry to the writer. This generation is not an exchange sequence. Queue overflow and failed/halted latches remain fail-closed.
3. The single writer clears its old strategy target and blocks dispatch. It consumes any in-flight executor result in the original Core epoch before durably applying `Disconnect` and `Reconnect`. Unknown or unsent durable actions are not invented as rejected; ambiguous cases stop and retain the original journal.
4. The same execution owner performs existing quiescent reconciliation: original IDs, owned-order cancellation, complete fill/funding history and independent cash/position checks. A correct snapshot and confirmed replacement subscriptions are both required to reopen admission. Failed reconciliation cannot be overridden by a later readiness message.
5. Queued market observations carry source generations; mismatches are discarded with an explicit counter/trace. Old-epoch strategy answers are discarded, while the bounded candle cache remains intact. Current/future malformed strategy answers retain their existing validation failures. A fresh target and quote are required before another order.
6. `source_expired`, `source_ready` and `source_reconciled` trace events record generation/epoch/timestamps. Reports expose reconnect and stale-market counts. Core replay and financial schemas remain unchanged.

## Required verification

- A queued order cannot reach even the simulated executor after a source pause, and remains Pending rather than becoming Rejected.
- A fill delivered after expiry but before epoch advancement is accounted exactly once; snapshot recovery preserves the resulting position.
- New subscription alone, old subscription readiness, an old strategy answer or a second expiry cannot reopen admission prematurely.
- A bad snapshot fails closed permanently; a subsequent correct snapshot cannot clear the failed circuit latch.
- A loopback WebSocket peer proves only `1000/Expired` is classified as renewable; `1000/maintenance` remains an error. Production URLs remain fixed; no user-selectable mutation endpoint is added.
- Full `scripts/verify.sh`, release build and the same frozen AWS A/A + interleaved A/B workload. Keep earlier results bound to their original commit; do not label them measurements of this changed dispatch gate.
- Fresh >=900-second paper and >=900-second signed Testnet sessions, observing expiry/reconciliation, zero unaccounted actions and final venue flat/open-orders0. Keep failed attempts and cleanup receipts.

A targeted fault-injection test removes the synchronous pause latch to demonstrate that the queued-send test fails; restoring the latch must pass. The initial negative-test harness waited on an intentionally open sender after the injected fault; it was stopped and corrected to close the producer, with the original log retained. This is test-harness evidence, not a production hang.
