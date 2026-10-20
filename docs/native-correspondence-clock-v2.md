# Correspondence v2: separate market and client clocks

Created UTC: 2026-10-18T08:20:18.913915+00:00. Scope and numerical acceptance are frozen in `comparisons/native-readiness/closed-loop-v2.json` before new collection.

V1's first live root failed after four completed 10-second rotations with an HTTP non-success; its status was not captured. Its original failure remains. Initial recovery could not execute because the worker reused existing log/result names. The corrected worker preserved those failures under unique attempt names, recovered the exact retained EBS, verified actual flat/no orders, and deleted the task resources. A high startup request burst is a hypothesis, not an established HTTP429 diagnosis.

## Task scope and method

A market simulator needs two timelines. A venue matches against its physical book at exchange event time, even when a public observer receives that past observation later. The trading agent can use a quote only after its receive time. V1 used the observer's available book for matching too, which mixes market-data delay into simulation error. V2 keeps separate venue and agent books. Offline future availability may reveal a past physical venue fact to the matcher, but cannot change an earlier intent's price, quantity or signal. Future **exchange event times** cannot change past matching. New regression tests prove both directions.

Own simulated venue fills update physical inventory, fees and funding settlement inventory immediately. Explicit modeled private reports reach the same Rust Core after a separately frozen delay; only those delivered reports can change the local position used for subsequent orders. A single modeled executor serializes submission and cancellation. Funding therefore uses the simulated venue's position at settlement, including fills not yet known locally. It never copies actual account cashflows. The existing Rust Core, live strategy, durable-before-effect and uncertainty contracts remain unchanged.

Implementation stays in the Python cold replay's event heap and existing `sim --external-venue` Core path; no additional live IPC or framework is introduced. Keep the eight-bar strategy window, latest-only warmup publication, consumed snapshot liquidity, explicit cancellation and operational flatten, independent rational ledgers and causal public oracle/rate requirements. Initial displayed queue size, missing cancellations/hidden priority, actual-strategy impact embedded in the recorded market and the UTC clock-offset assumption remain limitations. No general passive-queue/PCA/mainnet claim follows from these samples.

## Execution steps and expectations

1. Preserve V1 failures, their raw files and the successful original-EBS recovery receipt. Use the three earlier takeover trials only to freeze timing parameters; the failed V1 holdout does not fit V2 parameters.
2. Test a late public quote whose earlier physical event improves the venue fill: the intent must still use the older client-visible quote. Test delayed private delivery crossing funding settlement: Core still sees zero while simulated venue funding uses the physical position. Retain all earlier partial/cancel/duplicate/future-event tests and full `scripts/verify.sh` gates.
3. Freeze the new model and verify release artifacts before launching Tokyo c7i.large. Three separate 900-second sessions use a 30s segment-age trigger, retaining the production 800-order soft and 1,000-order hard ceilings. The cadence change reduces repeated startup requests; it does not tune price/quantity tolerances. Stop after the first 15-minute lifecycle or quantitative FAIL. Never replace failed runs or change their denominator.
4. Compare every actual and simulated decision/side group, including unmatched groups. Record distinct closed-bar times as well as order counts: repeated warmups and operational trades are not independent strategy observations. Report actual/model gross PnL, fees, funding, inventory, operational flatten and residuals.
5. Recover any interrupted original root before another trading session. Unique attempt logs/results retain old failures. Numeric HTTP status is recorded without reusable signatures or response-body echoes. A previously verified flat phase cannot authorize deletion after later signed activity.

## Acceptance and exclusions

Require at least **100 actual and 100 simulated orders**, lot and order-count differences each **≤5%**, maximum grouped VWAP error **≤2bps**, zero signal mismatches, and independent actual/Core and simulated/Core ledger discrepancies **≤0.000001 USDC**. These limits are unchanged. Every passive/missing-depth simulated order, actual non-filled acknowledgement or missing settlement oracle remains explicit and prevents a complete covered-class PASS. Physical and delivered inventories/cash must agree after all modeled reports drain. A partial dataset remains incomplete even if its few comparisons match.

Use one 2vCPU/4GiB temporary host, no NAT/ALB/paid endpoint, existing S3 and encrypted retained EBS. The 55-minute compute bound plus IPv4/EBS fits the frozen USD0.25 experiment budget using the queried USD0.11235/hour compute rate. Keep controller-finally cleanup and a 60-minute guest fallback. The original user host is untouched.

The 24-hour qualification remains gated on history/takeover, correspondence and operations readiness. It cannot be claimed from these short runs. Persistent stop/alert receipt, immutable upgrade/rollback, verified archival retention, EBS/AZ disaster recovery and owner-selected PCA shadow remain separate evidence requirements.
