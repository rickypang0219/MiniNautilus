# Native readiness continuation

Created UTC: 2026-10-18. Machine-readable evidence: [native-readiness-20261018.json](native-readiness-20261018.json). This record retains failures and distinguishes implementation, executed checks and acceptance.

| Gate | Result | Evidence and scope |
|---|---|---|
| Rotated-root host takeover | PASS,3 fresh trials |79.658s /68.854s /74.614s, all≤300s. A real exposed successor after rotation; old EC2 terminated before replacement, same retained EBS/mount, original prefixes preserved, unique fills and exact cash checked. |
|800 terminal+4 active polling | PASS,3 EC2 fixtures | Exactly4 regular status RPCs each, complete fill-history/proof checks retained. Fixture terminal orders are canceled/unfilled; this is not804 real orders or a filled-history throughput benchmark. |
| Repeatable recovery attempts | PASS for exercised failure | Original failure logs/results remain immutable; new attempts use new names. Both interrupted original volumes subsequently recovered and were deleted only after native reconciliation and fresh actual flat/no-order proof. |
| Exact wallet vs API PnL presentation | Captured regression fixed and original root recovered | Execution loss−0.00175USDC agrees with the wallet; reported closedPnl−0.00174 is separately validated. The0.00001 display delta is never booked. Financial tolerance remains0.000001USDC. |
| Closed-loop market correspondence | FAIL retained; original account recovered | Separate venue event time/agent receive time; own simulated private fills drive Core inventory and future orders. Frozen arrival960ms/report390ms/cancel520ms. Next frozen v3: three900s sessions,30s rotation;≥100 actual and modeled orders,≤5% lot/count differences,≤2bps max grouped VWAP. |
|24h qualification | Not started | Conditional on correspondence and operations evidence. A short soak cannot replace24 elapsed hours. |
| Relative performance improvement | Not measured | No A/A or repeated A/B; RPC count and takeover RTO are functional/absolute gates. |

The live path remains Rust. Python depth replay uses the existing backtest Core protocol only on the cold path; it does not add a live Python network/OMS bridge. Strategy inputs retain their bounded eight-bar window and publish only the final warmup target. Simulated funding uses physical settlement inventory, independently of delayed local fill knowledge.

Original failed experiments remain in the machine record and S3. V1 stopped on an HTTP non-success without a captured status. Its first recovery attempt could not invoke native recovery because old evidence filenames collided. V2 then found the distinct PnL presentation/cash issue. Their later recovery successes prove cleanup, not successful original soaks. Partial `filled` acknowledgements are not treated as full order completion for model coverage.

See [clock scope and criteria](native-correspondence-clock-v2.md), [cash regression](native-pnl-presentation.md), [cache contract](native-terminal-cache.md), and [original readiness proposal](native-production-readiness-proposal.md). Persistent human stop, alert receipt, immutable upgrades/rollback, verified lifetime archive/retention, EBS/AZ disaster recovery and Cluster-PCA shadow retain their separate acceptance requirements. No production/mainnet approval follows from this record.


## Continuation: partial-fill topup and delayed original recovery

The `clock-v2b` holdout failed in segment12: buy20 lots filled18, then the
2-lot target topup was below USD10. This was a local pre-send validation failure,
not evidence of a venue-rejected or lost order. HTTP transport also interrupted
its original cleanup. The original host was terminated, and the same EBS was
retained. A fresh query still found long0.00018BTC and no open orders.

First delayed recovery failed before launching a host because EC2 no longer
returned the previously terminated instance. The controller now requires both a
successful current query showing no instance and the original termination receipt;
any visible running/stopped signer or API failure still blocks takeover.

The next recovery reached native reconciliation but twice could not find the
locally unsent order25. The existing strict `--resolve-unsent` proof is now exposed
through `recover-lifecycle` and a recovery-only controller flag. It is opt-in:
original journals/action hashes, zero fills, no action attempt, no contradictory
open order and complete fill/wallet reconciliation remain mandatory. No change
to default absence handling or financial tolerance. The first explicit recovery failed while polling a locally rejected order after sending the flatten. Its final cleanup re-reconciled the original session and succeeded; the controller result remains FAIL, with cleanup_verified=true. Fresh actual position/open orders are zero; both task volumes and the temporary secret were deleted. A regression now requires regular polls to retain that reconciled no-attempt proof, while still rejecting any unowned fill.

Live target preflight now refuses such an opening delta once per target, before
allocating an order. Small pure reductions remain eligible. Replay uses the same
policy in **closed-loop-v3.json**; old frozen manifests/results are untouched.
Arrival/report/cancel delays, fees, error thresholds, sample requirements and
passive-coverage restrictions are unchanged. This is a functional correction,
not a measured performance improvement.
