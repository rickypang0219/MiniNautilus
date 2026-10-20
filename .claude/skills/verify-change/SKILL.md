---
name: verify-change
description: Prove a code change in MiniNautilus is correct before reporting it done. Use after any edit to src/, python/, tests/ or benches/.
---

# Verify a change

1. If the change touches order/fill/position/risk/journal logic, list which invariants in
   `docs/architecture.md` it affects, and name the test that covers each. If none covers it,
   write that test first and show it fails without the change.
2. Run `scripts/verify.sh`. It must end with `VERIFY OK`.
3. For journal/recovery changes also run `python3 examples/replay.py --journal runs/verify-<id>.jsonl`
   and confirm the README's expected result (position 0, gross PnL 12 tick-lots).
4. Report: what changed, invariants affected, tests added, and the verbatim tail of the verify output.

If any step fails twice for the same reason, stop and report the failure with output.
Do not iterate blindly.
