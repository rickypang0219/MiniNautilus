# ADR: live journal rotation with a cold dedup index

- Created at: 2026-10-12T12:40:00+08:00 (Asia/Hong_Kong)
- Status: ACCEPTED for implementation behind an opt-in flag (`--rotate-orders N`, actor runner only).
  Answers [next-phase proposal](runtime-v2-next-phase-proposal.md) §4.1 and §4.2.
- Scope: one account domain, one instrument, durable `serve` journal. Paper rotation (H4) is unchanged.

## Problem

Core, the Python mirror, the incremental audit and the runner keep every order and fill of a
session. Hot state and per-cycle work (Binance poll/reconcile scan all known orders) grow with
terminal history: the 2026-10-11 Testnet soak needed 344 s for shutdown reconciliation. Paper
rotation exists but was refused live because a live adapter had no retirement proof and no exact
dedup for late reports about retired orders.

## Decision

Rotation keeps the H4 journal chain (`Closed{successor}` + successor `Genesis` carrying balances,
Kill, epoch, target revision and ID floor) and adds a **cold index**: an immutable, checksummed,
sorted file of every retired order's final `(id, lifecycle, filled)` and every retired fill
`(execution_id, order_id, qty, price)`. It is written next to the successor before the
predecessor is closed, and the successor genesis references it by file name, checksum and counts.

### 1. Quiescence point and reports during rotation

The runner reaches quiescence in this order (`ActorSession._rotate`):

1. Trigger: hot orders in Core ≥ `N` (soft limit). New risk is blocked (strategy may not submit);
   open orders are left to their TTL cancels.
2. When nothing is open, or after `max_rotation_delay` (default 60 s), the coordinator is latched
   (`gate('rotation')`), producers stop, the blocking venue worker must be idle (late result rule:
   wait up to `quiescence_seconds`, else fail closed).
3. Full venue reconciliation through the writer (Binance: cancel owned orders, wait terminal,
   read all trades, check base balance). After it, `reconciled_through == venue_seq`.
4. Rust accepts `rotate_to` for a live journal only if Healthy, nothing open, the request is
   isolated, and **no private report was applied since that reconciliation**
   (`reconciled_through == venue_seq`). Otherwise the request fails and the session stops.

No producer runs between 2 and the new coordinator, so no in-process report can arrive. Reports
the venue produced meanwhile are part of the REST state read in step 3. A report that arrives after
rotation for an order at or below the ID floor is resolved by the cold index (below); a report for a
current-segment order follows the normal path.

### 2. Atomic publication and recovery

Rust `rotate` order, each step durable before the next:

1. Scan and validate the predecessor; require the rotation boundary.
2. Write `<successor>.cold` = previous cold index ∪ this segment's orders/fills (temp file, fsync,
   rename, directory fsync). Deterministic content: a rerun rewrites identical bytes.
3. Append `Closed{successor}` to the predecessor and fsync.
4. Create the successor with `Genesis{carry, predecessor_checksum, cold}` and fsync its directory.

Crash between any steps: the predecessor is either not closed (recovery replays it unchanged; a
stray cold file is ignored because no genesis names it), or closed (rerunning the same rotation
completes 4; recovery of the closed journal refuses and names the successor). Carry, ID floor,
Kill and target revision are one genesis record, so they are published together. Risk state needs
nothing else: with nothing open there are no reservations; position and cash are carried.

Venue-side baseline (Binance base balance and carried position for the balance identity) is
written by the runner to `rotation-<successor>.json` (create-new, fsync) **before** step 1. Resume
uses the file whose name matches the current journal; a file for a successor that never got a
genesis is ignored. Files are never overwritten.

### 3. Where exact dedup evidence lives

In the cold index on disk, not in RAM. The durable engine (outside the pure Core, which does no
I/O) resolves any `Execution` report about an order at or below `id_floor` that is not in the
current segment, and journals the verdict inside the event (`Report::Retired`):

| Report | Duplicate when | Otherwise |
|---|---|---|
| Fill | same execution ID with identical payload | gate (new fill on a retired order, or changed payload) |
| Canceled | cumulative fill equals final filled and final state is Canceled/Filled | gate |
| Rejected | final state Rejected | gate |
| Accepted / CancelRejected | order exists and was not Rejected | gate |
| any | cold file missing or checksum mismatch → verdict `Unavailable` → gate | |

A duplicate consumes its venue sequence and changes nothing else. Replay uses the journaled
verdict, so it never needs the cold file. Callers cannot inject `Retired` themselves: such an input
is refused. Capacity: 17 bytes per order and 32 bytes per fill on disk (≈ 5 MiB per 100 k orders
with one fill each); lookups are binary searches with positioned reads, O(log n), and only happen
for late reports about retired orders.

### 4. What memory is actually freed

Rust: the predecessor Core (orders, fills, indexes) is dropped and the successor starts empty.
Python: the bridge receives full state and replaces its mirror; the audit is reset from it; the
runner drops `sent_at`/`fills_by_order` for retired IDs; the Binance adapter drops retired
`sent_at`/`execution_times`. Evidence for G3 is object counts (Core orders/fills, mirror sizes,
audit sizes, adapter dicts) bounded by `N + max_open`, plus per-segment RSS showing no sustained
growth. HWM is reported, never used as proof of release.

### 5. Trust contract

Before: startup replays the single journal from genesis, so every historical event is validated.
After: startup validates the current segment only (genesis + tail), as offline H4 already does.
Predecessors were fully validated when the rotation scanned them; their content survives as the
cold index, whose checksum is in the successor genesis. The cold file is verified lazily on the
first retired lookup (whole-file checksum, then cached as verified for the process) and by
`mininautilus verify-cold JOURNAL`. A mismatch yields `Unavailable` and gates; nothing is trusted
on a failed check. Predecessor journals are kept on disk for audit and are not re-read at startup.

## Behaviour when rotation cannot happen (§4.2)

- Trigger: Core hot orders ≥ `N`.
- Deferral: up to `max_rotation_delay` waiting for open orders; new risk is blocked from the
  trigger, so hot orders stay ≤ `N + max_open` during deferral.
- If reconciliation for rotation fails, the runner retries it like any recovery (3 attempts) and
  then stops: MarketUnavailable, no new risk, Disconnect, exit non-zero with
  `retention_rotation_failed`. It never drops dedup evidence or rotates without the boundary.
- An unresolved venue call at the quiescence point fails closed (no reconcile, no rotation).
- Hard limit `2N` hot orders (for example if the trigger could not fire): the runner blocks new
  risk immediately and requests rotation; if it is still above the hard limit after a rotation
  attempt it stops as above.

## Consequences

- Startup and reconciliation cost become O(current segment) for Binance; journal and cold files
  still grow on disk (bounded by disk, not RAM). Each rotation rewrites the whole cold index, so
  rotation cost is O(retired history) and the superseded `.cold` files sum to O(rotations ×
  history) bytes. Only the newest is referenced by a writable journal and it contains every
  record; replay of older segments never reads cold files (verdicts are journaled). Operators may
  delete superseded `.cold` files; nothing does so automatically.
- Rotation cancels resting orders (step 3). With the SMA runner's 10 s TTL this is a small cost.
- Retired lookups do file I/O on the writer thread. They are rare (late duplicates); their latency
  is reported separately.
- Not covered: multi-instrument domains, venue retention horizons, deleting old journals.
