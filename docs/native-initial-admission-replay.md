# Initial admission in causal replay

Created UTC: 2026-10-18T15:10:25.340280+00:00.

## Problem and evidence

The failed `clock-v2b` root retains all13 segments. A diagnostic of its12 completed
segments found one2.567833604bps discrepancy, above the frozen2bps limit. In that
segment the model submitted at1792314737402, but the live Core submitted at
1792314738112:710ms later. The model correctly delayed arrival after submit,
but admitted a queued strategy target before the native bootstrap had finished
its initial source/reconciliation gate. No delay is refitted from this outlier.
The failed root and original v2/v3 manifests/results remain unchanged.

## Minimal correction and boundary

The native writer records `initial_admission_ready` exactly once when the source,
reconciliation and executor admission conditions first permit dispatch. This
happens **before checking whether a strategy target exists**. It requires an empty
local order book and flat bootstrap, so it cannot encode a later trading outcome.
The lossless observation recorder stores only kind, wall time and `empty=true`.
Recording failure stops the runtime through its existing circuit handling.

The v4 replay waits for that operating event before dispatching its own queued
targets. Quotes and target inputs remain causal. Actual orders, fills, private
funding and all later live gate changes are excluded from simulation inputs;
simulated executions determine its inventory and future order quantities. This is
per-segment replay **conditional on observed startup readiness**, not an autonomous
simulation of startup network-service times or complete across-segment scheduling.
It does not fix or validate passive queue priority, market impact or all later
coordinator service times.

## Frozen next test

`comparisons/native-readiness/closed-loop-v4.json` is a distinct pre-collection
manifest. Arrival960ms, report390ms, cancel520ms, fees, opening minimum10USDC,
3x900s/30s rotation, at least100actual and100simulated orders,5% lot/count limits,
2bps maximum grouped VWAP and all coverage restrictions are unchanged. No scoring
of old records with inferred readiness is allowed. v3 continues on its original
immutable guest bundle; v4 must use fresh native recordings and a verified release.

Acceptance for this correction: no modeled submit before the initial ready event;
changed later private facts cannot change a prior target or readiness input;
duplicate/nonempty ready inputs fail; native readiness can be recorded with no
target and no order; all existing financial tests and full verify remain enabled.
A full fidelity PASS still requires the original quantitative and coverage gates.
