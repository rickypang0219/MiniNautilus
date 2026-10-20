# Historical reports and evidence archive

The working tree contains current architecture, operations, fixtures, acceptance
contracts and machine-readable readiness records. Historical experiment outputs
and superseded reports are preserved in Git rather than copied into each checkout.

- Archive tag: [`archive/pre-cleanup-20261020`](https://github.com/rickypang0219/MiniNautilus/tree/archive/pre-cleanup-20261020).
- Pinned commit: [`7524d895d618cb912572921afdefea8d39fe9cd7`](https://github.com/rickypang0219/MiniNautilus/tree/7524d895d618cb912572921afdefea8d39fe9cd7).
- [Complete source snapshot](https://github.com/rickypang0219/MiniNautilus/archive/7524d895d618cb912572921afdefea8d39fe9cd7.tar.gz).
- [Per-file inventory](archive-manifest.json): original path, byte count, SHA256,
  Git blob identity, status markers, summary and a commit-pinned source link.

The inventory covers **121 files / 3,716,832 original bytes**: all 103 tracked
historical files under `comparisons/results/`, two LC-v2-fast output indexes and
16 superseded reports/proposals. Status markers are a discovery aid, not a new
overall verdict. Cleanup does not change any original PASS, FAIL, INCONCLUSIVE,
BLOCKED, rejected optimization or failed/interrupted experiment.

## Current entry points

| Need | Read |
|---|---|
| Core ownership, risk and durability invariants | [Architecture](architecture.md) |
| Current native acceptance and unresolved read visibility | [2026-10-18 acceptance](native-readiness-20261018.md), [machine record](native-readiness-20261018.json) |
| Quote/epoch/REST consistency | [Feed contract and offline regressions](native-feed-consistency.md) |
| Native commands and recovery | [Operations](rust-native-runtime-operations.md), [deployment runbook](native-live-deployment-runbook.md) |
| Lifecycle and strategy qualification still required | [Readiness scope](native-production-readiness-proposal.md) |
| Offline comparison reproduction | [Comparison tools](../comparisons/README.md) |

## Historical findings and limits

These are summaries of the original releases, not acceptance of the cleanup commit.
Links open the complete original document and its original relative references.

| Cohort / scope | Original result and limitation | Frozen report |
|---|---|---|
| Cross-platform backtest audit | Mini order/PnL corrections and version-specific external engine discrepancies; synthetic model differences are not platform rankings or live fidelity | [Audit](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/backtest-comparison.md), [post-refactor audit](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/post-refactor-audit.md) |
| 2026-10-11 cloud, live and memory diagnostics | Latency/drift failures remain; bounded SMA allocation and short-run observations do not prove whole-runtime performance or long-run readiness | [Cloud](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/cloud-validation-20261011.md), [deep dive](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/deep-dive-20261011.md), [live diagnostics](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/live-testnet-20261011.md) |
| Runtime v2 synthetic reliability | Three corrected 90-minute synthetic fault runs completed; absolute latency/CPU failures and inconclusive measurements remain. Final timeout performance candidate was rejected and rolled back | [Results](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/runtime-v2-results.md), [rejected candidate](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/comparisons/results/runtime-v2/runtime-v2-final-scheduling-decision.json) |
| LC-v1 / LC-v2-fast storage qualification | Overall FAIL; scoped storage observations cannot override failed host/swap gates. Formal long fake/Testnet qualification was BLOCKED; performance adoption INCONCLUSIVE | [LC-v1](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/runtime-v2-lifecycle-results.md), [LC-v2-fast](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/runtime-v2-lifecycle-fast-results.md) |
| Hyperliquid H1 diagnostics | Scoped H1 gates PASS, with recorded transient errors and interrupted attempts; H2 multi-instrument/PCA capability was not implemented | [H1 record](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/hyperliquid-migration-results.md), [connectivity](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/hyperliquid-connectivity-results.md) |
| Native Rust migration | Original failed paper/crash attempts, subsequent scoped Testnet recovery successes and A/A INCONCLUSIVE measurements remain attached to their releases | [Migration record](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/rust-native-runtime-results.md) |
| Native lifecycle continuation | Earlier fidelity/lifecycle FAILs and original-session recovery remain; current acceptance is in the retained 2026-10-18 record, with correspondence unaccepted and no 24-hour qualification | [Historical progress](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/native-readiness-progress.md) |

Every other archived report and output has its own original link and digest in
the inventory. The source tag retains the complete tree. Raw journals already
outside Git remain at their original artifact locations; this cleanup does not
claim to archive or verify those external objects.

## Retrieve and verify

Normal CI validates metadata and local Markdown links entirely offline, including
in shallow clones. A complete original-blob check additionally needs the archive
tag and its reachable objects:

```sh
git fetch origin refs/tags/archive/pre-cleanup-20261020:refs/tags/archive/pre-cleanup-20261020
python3 scripts/check_archive.py --verify-blobs
```

The checker verifies the tag still resolves to the pinned commit, then reads every
original path and checks its Git blob, byte count and SHA256. Nothing is restored
over the current working tree. To inspect an individual file:

```sh
git show 7524d895d618cb912572921afdefea8d39fe9cd7:comparisons/results/runtime-v2/reports.json
```

Download links use the commit SHA, not a moving branch. No Git history was rewritten.
Git history retains its size; the reduction is in checked-out files and review noise.
New experiments write unique outputs under ignored `runs/`; `comparisons/results/`
is also ignored to prevent accidentally recommitting historical bulk output.

## What remains local

Fixtures, executable/frozen manifests, lockfiles, ADRs, current acceptance records
and operational documents remain. In particular,
`docs/runtime-v2-lifecycle-acceptance-proposal.md` is consumed verbatim by
`ops/lifecycle_evidence.py` as the default threshold manifest. Its original bytes
and its prerequisite documents remain; it is not redundant prose that can simply
be deleted. Tests never fetch evidence from GitHub to run.

## Cleanup verification

All 121 original blobs passed the explicit restoration/hash/status-marker check.
Seven archive regressions passed, including shallow-checkout operation, immutable
links, corrupt metadata and failure-marker preservation. `scripts/verify.sh`
completed on frozen build inputs: 59 native-feature library tests, enabled Rust
integration/lint/comparison checks, 227 Python tests (one skipped), and four
dashboard tests. Core, strategy, fixtures and executable manifests were unchanged.

Verbatim final tail:

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
VERIFY OK
```
