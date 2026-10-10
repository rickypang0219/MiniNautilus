# Agent guide

Read `docs/architecture.md` before touching `src/core.rs`, `src/journal.rs` or `src/model.rs`.
Its invariants are the contract; a change that weakens one needs explicit human approval.

## Definition of done

A change is done only when `scripts/verify.sh` prints `VERIFY OK` (same gates as CI).
Report the command and its last lines. Never say "should work" without that evidence.

## Hard rules

- Never skip, ignore or loosen a test to get green. Fix the cause or stop and report.
- Never claim a performance change from one run: use the `perf-investigation` skill.
- Never touch `.env*` or run anything against Binance with real keys; Testnet only, and only when asked.
- Run artifacts go in `runs/` (git-ignored). Evidence is never overwritten.

## Skills

- `verify-change` — every code change.
- `perf-investigation` — any latency/throughput/CPU question or optimization.

When an agent makes a mistake a human had to catch, add a rule here or to the skill
so the next session does not repeat it.
