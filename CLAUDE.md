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
- Before starting or resuming signed venue tests, check for orphaned sessions and actual venue positions/open orders. `--flatten` is a best-effort normal shutdown path, not protection against process/container loss. Recover an interrupted session using its original journals before starting another; never report cleanup complete without a fresh venue check. Explain that long/short diagnostics temporarily hold exposure, and preserve interrupted runs as incomplete even if recovery succeeds.

## Skills

- `verify-change` — every code change.
- `perf-investigation` — any latency/throughput/CPU question or optimization.

When an agent makes a mistake a human had to catch, add a rule here or to the skill
so the next session does not repeat it.

## Strategy data and AWS costs

- Streaming strategies must cache only their required observation window. Fetch a bounded warm-up window and incremental updates; never fetch full historical datasets into a strategy merely to generate a signal. Offline historical readers may stream the full dataset, keeping strategy state bounded.
- Use a fixed-size ring or bounded deque with O(1) append/eviction and rolling aggregates. Do not rebuild/slice the full window on every tick. Specify warm-up, duplicate/out-of-order and missing-bar behavior; preserve signal parity. A Python list ring stores references, not contiguous numeric values: do not claim CPU cache or latency gains without measurement.
- AWS default region is ap-northeast-1. Target under USD 20/month including compute, EBS and public IPv4; verify current prices before creating persistent resources. Prefer small EC2 with standard CPU credits, no NAT Gateway/ALB/paid interface endpoints. A public subnet plus Elastic IP supports Binance whitelisting; no inbound ports are required. Same-region S3 traffic should use a gateway endpoint. ECS, if needed, should stay around 1–2 vCPU and 2–4 GiB; do not assume that fits the monthly budget.

- EC2 cloud-init needs IMDS to retrieve user data. Require IMDSv2 instead of disabling metadata when using bootstrap scripts; confirm the script actually ran and verify cleanup after failed launches.

- Temporary benchmark EC2: launch only after the exact build and runnable experiment are ready. Wrap each bounded experiment in controller-side `finally` termination and verify instance termination plus deletion of task EBS volumes. A guest shutdown timer is a fallback, never evidence of cleanup; do not leave an instance idle while implementing code. Persist resource IDs before waiting, and check them first after any interrupted turn.
