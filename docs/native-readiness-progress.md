# Native reliability and market correspondence

Created: **2026-10-17T01:55:05Z (UTC)**.

This implements the owner's two priorities together: test whether simulated execution corresponds to live facts, and close the bounded session/host recovery gaps. It extends [the existing readiness scope](native-production-readiness-proposal.md); it does not replace its stricter gates or treat short tests as 24-hour qualification.

## Scope and implementation

1. **Lossless observations:** native `observations.jsonl` records received L2 depth, private wakeups, successful REST replies, every bounded SMA input/answer, and public funding/mark/oracle context. Signed request bodies/signatures and keys are excluded. The recorder has no lossy telemetry queue; append failure stops its producer and closes trading through the circuit. Writes are buffered by the OS and synced at normal completion; this is not a power-loss guarantee for all market observations. Financial input-before-effect and fsynced action-attempt journals remain authoritative and unchanged.
2. **Correspondence:** `ops/live_correspondence.py` first asks Rust to validate/replay the journal. It independently recomputes bounded SMA and rational average-cost position/PnL, fees and settled funding, compares every raw venue fill with Core, and makes separate displayed-depth predictions. Live fills never supply simulated quantities/prices. Available-at ordering, delayed arrival, depth consumption, stale books and partial fills are explicit. This is conditional replay of the recorded live order schedule, **not** closed-loop strategy/backtester validation. Passive queue, market impact, closed-loop portfolio PnL and simulated funding remain unvalidated. Actual private funding is never reused as simulated funding for a different position.
3. **Flat native segmentation:** `native-live lifecycle` owns the local account lock across segments. At 800 orders, 64MiB, or 30 minutes, it stops new strategy dispatch and requests a flat/terminal boundary; waits at most 60s; stops without a successor if unavailable. The whole data directory must be retained. Original signed cleanup verifies venue facts before existing journal rotation carries cash, ID floor, epoch and cold dedup evidence. Each successor gets a distinct cloid namespace and bounded session history. Nonce floor, account/agent, original-run loss floor and expected ending cash are persisted, so rotation cannot reset the loss allowance or hide cash changes. No arbitrary old action is resent.
4. **Publication recovery:** checksummed atomic `current.json`, immutable prepared facts and sealed predecessors distinguish active from rotating. A proven interrupted publication can be completed without venue mutation; changed/missing facts, non-flat state, corrupt history or a non-genesis successor stop. A partial/unmatched pointer temp or torn successor requires explicit repair; it is not silently removed. `recover-lifecycle` recovers the original current session, or independently verifies a prepared untouched successor has no signer/flat account. It does not automatically restart trading after recovery.
5. **Host replacement drill:** the EC2 controller uses a task-owned encrypted data EBS with DeleteOnTermination=false, terminates and confirms the old task signer, then mounts the **same** EBS on a same-AZ replacement without formatting. It verifies original journal/action/session hashes before recovery. Local `flock` is not cross-host fencing. A guest shutdown timer is only fallback; controller `finally` verifies task termination and volume deletion. Unresolved account cleanup retains state rather than deleting evidence.

The original SMA never naturally targets flat. `--rotation-flatten` is an explicit infrastructure drill/operational option; the default does not liquidate merely to rotate. These extra trades/costs cannot be used to assess strategy alpha. This does not solve non-flat PCA portfolio carry or multi-instrument collateral.

## Acceptance and commands

Frozen numeric experiment: [lifecycle-drill-v1.json](../comparisons/native-readiness/lifecycle-drill-v1.json). Conditional execution assumptions: [correspondence-v1.json](../comparisons/native-readiness/correspondence-v1.json). Both were written before AWS collection. A displayed-depth result cannot receive fidelity PASS merely because a few samples fit; independent accounting and execution-fidelity results are separate.

```sh
cargo build --locked --release --features native-runtime --bin native-live
target/release/native-live lifecycle-fixture --fixture-orders 10000 --rotate-orders 800 --run-dir runs/retention-new
# Signed TESTNET, dedicated flat account only; diagnostic exposure is temporary.
target/release/native-live lifecycle --python-strategy --seconds 900 --rotate-orders 4 --rotate-seconds 180 --rotation-flatten --flatten --run-dir runs/lifecycle-new
target/release/native-live recover-lifecycle --flatten --run-dir runs/lifecycle-new
python3 ops/live_correspondence.py runs/lifecycle-new/segment-000000 --manifest comparisons/native-readiness/correspondence-v1.json --output runs/comparison-new.json
```

For EC2, package the clean verified release using `ops/native_bundle.py`, then use `ops/aws/native_host_drill.py --bundle-receipt ... --config ... --verify-log ... --output ...`. The non-secret config must contain `ami,subnet,existing_sg,existing_profile,bucket`. The worker enforces Testnet and retrieves credentials only from the controller's temporary encrypted SSM parameter. No production stack or persistent paid service is created.

## Invariants and tests

- Durable-before-effect, no blind resend, uncertainty retained: existing native venue/expiry and durable-engine tests remain enabled.
- Flat alone is insufficient: `flat_terminal_is_not_enough_without_a_reconciliation_barrier` rejects rotation without a fresh barrier, without changing the predecessor.
- Economic carry and bounded hot state: `repeated_real_core_rotation_preserves_economic_state_and_bounds_active_orders` traverses 12 real Core rotations.
- Crash publication: `interrupted_rotation_is_completed_only_from_unchanged_flat_facts` covers before/after Closed and rejects altered sealed actions. Existing `live_rotation` fault-injection tests exercise cold/Closed/genesis/catalog boundaries.
- Nonce/loss carry: `carried_nonce_survives_equal_clock_and_loss_budget_does_not_reset`.
- Simulator counterexamples: independent ledger cross-zero/fees/rebates/funding, changed duplicates, future-book perturbation, displayed liquidity reuse, partial/passive fills and wrong strategy targets in `test_live_correspondence.py`.

## Result status

Pending final source/build identity and AWS evidence. Do not infer PASS from implementation. Full production readiness still requires the original L/O/S matrix, repeated host drills, long qualification, operations/alerts/rollback and real strategy shadow. No mainnet actions are enabled.

### First frozen attempt: FAIL, recovered (2026-10-17)

Build `d07cc87`, bundle SHA256 `77d2b73a97715f10e54441887b0f40382d8abdd439ce4f7ac5f955737d8818d8`, failed after **35.447 active seconds**, before rotation. An order filled 19 lots while the separately read open-order view still showed its original 40 lots remaining. Strict validation correctly stopped, but the adapter only retried missing fills, not the opposite visibility race. Both directions now receive at most three read-only attempts; impossible quantities and changed duplicate facts still fail immediately.

Cleanup canceled the resting remainder, leaving **-1 lot = -0.00001 BTC**. The adapter rejected a dust close below USD10 before signing, leaving a durable Core intent with **no action attempt**. Bounds now permit sub-minimum orders only when fresh account state proves strictly reducing and the actual signed action has `reduceOnly=true`. Normal risk-increasing minimum/notional/position/collateral checks remain. Recovery refuses missing action history. Explicit `recover --resolve-unsent` can additionally prove a never-acknowledged/unfilled pending intent was never signed using the original complete action journal; default unknown-OID behavior remains unresolved. No old client ID is reused, no failed mutation is retried. This proof relies on the existing complete-files trust contract; whole valid suffix deletion still requires an external high-watermark to detect.

The original session was recovered from its preserved archive. A 1-lot reduce-only close filled despite an HTTP response timeout; a second recovery reconciled that same order, without resend. Independent fresh account check: flat, no open orders. Final ledger: realized **-0.004480 USDC**, fees **0.014642**, funding **0**, net cash **-0.019122**; Core **4 orders / 3 fills**, including the locally proved unsent rejected ID. The original failure and first timed-out recovery remain FAIL. S3 prefix: `native-readiness-20261017/mini-lifecycle-1792202502/` in the existing artifact bucket. Original task host `i-076ae452348ec4643` terminated; root `vol-094a112e1cb404786` deleted. Retained data `vol-002a2a762e7ca697c` was deleted only after original-session recovery, independent flat check, and uploaded recovery archive (`ef4e38c56cd6f92ce0637804c4ee4dcab4192cf512e98ecb4377cd8f241b05b2`). This is **not** a completed retained-EBS replacement drill.

Additional fault-injection tests exposed inherited-descriptor lock lifetime in old cold GC and journal ownership. Deterministic clone-descriptor tests reproduced failures. Logical-owner teardown explicitly releases only its own lock; independent readers/owners still exclude reclamation/writers. The unchanged 18-test rotation matrix subsequently passed 20 consecutive runs. No assertions were removed or serialized.
