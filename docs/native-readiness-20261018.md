# Native readiness: acceptance and remaining work

Created UTC: 2026-10-18. Final review UTC: 2026-10-18.
Machine record: [native-readiness-20261018.json](native-readiness-20261018.json).
Scope: Hyperliquid **Testnet**, native Rust runtime, bounded BTC SMA diagnostics.
This is an acceptance record, not production approval or strategy-profit evidence.

## Accepted results

| Gate | Result | Measured boundary |
|---|---|---|
| Active rotated-root takeover | **PASS**, three fresh trials | 79.658 / 68.854 / 74.614 seconds, all ≤300s. Exposed successor after real rotation; old EC2 terminated before replacement. Same retained EBS/mount; original financial prefixes, seals, identities, exact cash and unique executions checked. Surviving EBS only, not EBS/AZ loss. |
| Terminal polling cache | **PASS**, three EC2 release fixtures | 800 verified terminal +4 active orders require exactly four regular status RPCs; complete fill-history checks remain. Terminal fixture orders are canceled/unfilled, not 804 live trades or an 800-filled-order recovery benchmark. |
| Current partial-open proof | **PASS for exercised case**, v5 | 27 proof observations on **one distinct live order**; exact original-minus-current remaining equals complete fills, with immutable known fills preserved. First root had zero execution recoveries. Not 27 independent trials. |
| Actual accounting and original recovery | **PASS for audited roots** | V4:115 unique fills across54 segments. V5:64 across32 segments, including the failed second root after original-session recovery. Position and gross execution cash discrepancies zero. Failed experiments retain FAIL. |
| Final task cleanup | **PASS**, independent AWS + venue reads | All15 continuation task tokens have zero active instances, remaining volumes or temporary parameters. Actual Testnet positions/open orders empty. User host `i-015087878f29f14cd` remains running. |

Live market data, execution and OMS remain Rust. Python supplies bounded eight-bar
SMA strategy and cold backtest analysis; no live Python network/OMS bridge was
added. Single writer, durable-before-effect, no blind resend, uncertainty retention
and strict financial tolerances remain intact.

## Closed-loop correspondence: not accepted

The simulator consumes public depth/trades/candles and public settled funding,
separates venue-event and agent-receive clocks, and feeds its **own simulated fills**
into Core inventory and future orders. Actual private fills and actual order
schedules do not supply modeled executions. Funding uses simulated physical
inventory at settlement. Each flat segment resets SMA. Once-only, target-independent
initial bootstrap readiness is a conditional operating input; this is not autonomous
startup-network modeling.

Arrival960ms / report390ms / cancel520ms were frozen before these holdouts.
No holdout delay tuning, threshold relaxation or removal of adverse groups.
Quantity/count error ≤5%, maximum grouped VWAP ≤2bps, ≥100 actual **and** modeled
orders, and execution-class coverage remain required.

| Attempt | Actual / modeled orders | Grouped lot error | Max grouped VWAP | Verdict |
|---|---:|---:|---:|---|
| v3 run1 |55 /53|11.6236%|1.10863bps|**FAIL**; later runs stopped |
| v4 run1 |56 /56|0%|1.47872bps|**INCONCLUSIVE**; sample and class coverage |
| v4 run2 |56 /54|8.13008%|1.10967bps|**FAIL**; run3 stopped |
| v5 run1 |54 /54|0%|0.86332bps|**INCONCLUSIVE**; three uncovered modeled orders / three actual non-full ACKs, sample below100 |
| v5 run2 |Not scored|Not measured|Not measured|**FAIL**; lifecycle interrupted at segment4; original recovery passed separately |

V5 run1 completed904.036s,26 rotations,54 orders /53 unique actual fills,
zero execution recoveries and40.875MiB peak process-tree RSS. Actual net cash
−0.428528USDC; modeled net−0.429520USDC; difference−0.000992USDC.
These small forced-rotation SMA diagnostics do not establish alpha or general
market-simulator fidelity. Pooling runs cannot erase a per-run failure.
**No24h run was started.**

## Corrections and evidence

- **Cash presentation:** exact execution loss−0.00175USDC agrees with wallet;
  API displayed closedPnl−0.00174 is separately validated and never booked as cash.
  Tolerance stays one micro-USDC. [Cash ADR](native-pnl-presentation.md).
- **Dust and original recovery:** refuse below-USD10 opening deltas before Core
  order allocation; strictly reducing dust remains eligible. Explicit original-session
  no-attempt proof can resolve locally unsent intents. Default unknown handling
  stays unresolved; regular polling retains reconciled rejection while rejecting
  contradictory fills. Earlier failed cleanup/recovery verdicts remain unchanged.
- **Initial admission:** record bootstrap readiness independently of targets.
  Same-data v4 run1 ablation changes maxVWAP2.21762→1.47872bps; lot/count error
  stays zero in both. Different market periods are not causal comparisons.
  [Admission ADR](native-initial-admission-replay.md).
- **Partial-open proof:** all80 lots of v4 run2 grouped error occurred in three
  premature recovery/cancel episodes. Older status said20 remaining, current
  openOrders said15 and complete fills proved5. Strict identity, terms, quantity
  and immutable-fill checks now accept this snapshot; v5 exercised it naturally.
  [Proof ADR](native-partial-open-snapshot.md).
- **Cloud cleanup:** non-XML EC2 discovery interrupted v4 cleanup after account
  cleanup/host termination. Follow-up deletion and independent verification passed.
  Cleanup now attempts independent known volumes/secrets despite discovery errors;
  incomplete inventory cannot produce fully verified cleanup.

## Remaining failure: acknowledged order temporarily unqueryable

V5 root2 received filled ACKs, while original-cloid orderStatus returned
`unknownOid`. Segment1 recorded80 such replies, the last23.323s after ACK;
visibility then returned and in-place recovery completed. Segment4's reducing
order had25 unknown replies, the last4.911s after ACK; its five-second visibility
budget expired. Core still held−20lots locally although the venue had executed
the close. No successor started. Cleanup recovered the **original journal**
in1.095s after visibility returned, without resending; final position/open orders
zero. Original lifecycle/controller remains **FAIL**.

This is independent of partial-open quantity verification. A filled ACK alone
cannot synthesize terminal Core state or final execution economics. Repeated
visibility failures stop this iteration under the repository's
[verify-change skill](../.claude/skills/verify-change/SKILL.md):
“If any step fails twice for the same reason, stop and report the failure with output.”

```text
native-live: segment failed; recover original current pointer, no successor started
cleanup unresolved: order visibility deadline; absence unresolved
```

No additional signed retry or longer global timeout concealed this failure.

## Next implementation sequence

1. **Read visibility, with admission closed.** Candidate: use the acknowledged
   exchange OID as a lookup hint when cloid is unknown. Currently that hint is
   discovered only from subsequently visible fills. Numeric responses must still
   match original cloid/instrument/side/quantity/limit and complete immutable fills
   before any Core report. Neither ACK nor unknown proves completion/absence.
   Lost ACK/restart retain conservative reconciliation. Numeric lookup may also
   lag: keep that case unresolved. Isolate bounded recovery waiting from market
   ingestion; do not increase global timeouts without a frozen end-to-end budget.
2. **Cause-specific proof before another holdout.** Sanitized regressions:
   cloid-unknown/numeric-visible, both unknown, contradictory OIDs, partial ACK,
   missing/changed fills, late completion and restart. Require zero blind resends,
   zero uncertain new-risk sends, exact identity/quantity checks, and no successor
   until the original root is flat/terminal. Full verify + unique-journal replay.
3. **Fresh frozen correspondence.** Keep old manifests/results. Freeze new source,
   binary and workload before sequential15-minute guards; stop on native or
   quantitative FAIL. Require ≥100 held-out actual/modeled orders, unchanged
   ≤5% quantity/count and ≤2bps price gates, zero unexplained accounting differences.
   Validate passive/cancel classes separately. An immediate-cross/IOC experiment
   must declare a new execution policy; it cannot relabel failed GTC evidence.
4. **Operations qualification.** Preserve separate original gates for persistent
   stop, alert receipt, immutable upgrade/rollback, archive/retention, damaged/stale
   backup refusal, and24 elapsed hours with expiry renewals/restart. Short takeovers
   do not prove these. State-volume CloudFormation draft remains undeployed;
   no unattended production service was installed.
5. **Cluster-PCA shadow.** Original data/provenance, causal panel/refit, bounded
   cache and shared-collateral multi-instrument Core remain strategy prerequisites.
   SMA results do not validate PCA returns or portfolio safety.

See [original readiness scope](native-production-readiness-proposal.md).
Mainnet, EBS/AZ disaster recovery and indefinitely bounded cold-history cost remain
unaccepted. Conditional24h qualification remains **BLOCKED**, elapsed0hours.

## Performance and verification

V5 run1:28 strategy submits; writer p99 **3.841185ms**, sync p99 **3.830142ms**.
Overall writer p99 **0.050379ms** is dominated by non-order traffic and is not
order latency. Operational flatten submits are outside this strategy-submit trace.
No frozen A/A plus repeated A/B: relative improvement/regression **NOT_MEASURED**.

Runtime source: `2093ba6d3d68dfd015c90423738c9263faafc21e`.
Clean bundle SHA256: `4eac07e2eb5be03cf68274b477f0d74d66cadd938f6298694fcfe0f861dc28a4`.
`scripts/verify.sh`:220 Python tests,49 native library unit tests and enabled
integration/format/lint gates passed. Journal replay: position0, grossPnL12,
two orders /three unique fills. Verbatim verify tail:

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
VERIFY OK
```

Source/binary/manifest hashes, raw S3 archives, preserved failures/recoveries and
write-once verification receipts are indexed in the machine record. Evidence is
private; credentials, signed request bodies and signatures are excluded.
