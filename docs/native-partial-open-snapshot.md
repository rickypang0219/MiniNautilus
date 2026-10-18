# Verified current remaining quantity for partial open orders

Created UTC: 2026-10-18T16:11:22.537977+00:00.

## Captured defect and scope

The retained v4 run2 failed its model volume gate. All80lots of absolute grouped
error occurred in three segments that triggered native recovery with
`cumulative fill history incomplete: expected=0 observed=5` (or10).
In segment8, the first orderStatus at1792337711359 and another at1792337712866
both reported open/original20lots. The current openOrders at1792337712776
reported15remaining; complete unique fills independently proved5executed.
The runner canceled at1792337712965, then its canceled status reported15remaining.
The model had left the order open. This identifies a concrete false recovery/cancel,
not a reason to change the frozen model latency. It does not establish all API
lag/convention behavior from one case. Captured sanitized fixture:
`tests/fixtures/native/partial-open-snapshot.json` (original source hash included).

## Correction and invariants

Regular polling still queries the complete original-session fill history and
original order identity. Only when an **open** status has inconsistent cumulative
quantity does it fetch a current openOrders snapshot (at most once per poll).
There must be one matching oid/cloid, identical symbol, side, original quantity,
limit and creation timestamp. Remaining must be positive, within original size,
and must not increase relative to the already observed status. Full fills must
exactly equal original-minus-current-remaining, including fill cloid validation.
Previously known Core fill IDs and payloads must remain present and unchanged;
equal aggregate quantity cannot hide a lost/replaced immutable fact.
Foreign open orders, duplicate identities, changed terms, missing fills and
contradictions of durable no-attempt proof still fail closed. Terminal statuses
are never overwritten. Missing current rows never prove absence. Existing bounded
visibility retries, full reconciliation, risk/nonce/order caps and wallet quantum
are unchanged. No new orders or cancels are sent by this read/proof path.

A successful `open_remaining_proof` is recorded losslessly. Only the durable writer
applies returned fills; remaining risk stays reserved. The per-poll terminal-cache
proof remains unchanged. The800terminal+4active fixture still requires4statusRPCs;
this extra global snapshot is conditional on a nonterminal quantity disagreement.

## Regression and next frozen forward check

The captured20/15/5 case is RED before the change and must return a5lot fill without
mutations after it. Missing snapshot remains unresolved; a snapshot cannot regress
known cumulative quantity or override terminal facts. Tests reject altered oid,
cloid, symbol, side, original size, limit, timestamp, invalid remaining, duplicates
and incomplete fills. Existing fill-ahead retry tests keep their assertions.
The test server now handles peer EOF before/during headers instead of spinning;
its financial assertions and deadline tests remain enabled.

`closed-loop-v5.json` retains v4 model parameters and all acceptance thresholds.
Collect fresh3x900s at most, stopping on a per-run native/quantitative FAIL. At least
one natural proof is required to call the new branch live-measured; otherwise mark
NOT_MEASURED. Counterfactual previous results remain FAIL/INCONCLUSIVE.
Passive queue assumptions, later network-service modeling and24h/operations
qualification are still independent gates; this change cannot certify them.

## Forward evidence,2026-10-18 UTC

Frozen v5 first900s root naturally exercised27 successful proofs on one distinct
order; every proof independently matched complete fills and original remaining
quantity. The root completed26 rotations with zero execution recoveries.
This meets the natural-case proof condition, not broad API-behavior coverage.
Correspondence stayed INCONCLUSIVE (54/54 orders, zero grouped-lot error,
0.86332bps max VWAP, three uncovered cases). A second root failed a separate
acknowledged-but-unqueryable order deadline; its original recovery succeeded.
See [final acceptance](native-readiness-20261018.md) for preserved failures,
source/binary hashes and cleanup receipts. No model thresholds were changed.
