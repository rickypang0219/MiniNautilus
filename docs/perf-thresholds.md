# Performance thresholds

- Created at: 2026-10-12T12:05:00+08:00 (Asia/Hong_Kong)
- Version: **1** (frozen before any next-phase candidate comparison)
- Decider: repo owner. Agent-proposed under the owner's instruction of 2026-10-12 to execute
  [the next-phase proposal](runtime-v2-next-phase-proposal.md) §2.1. The owner may veto at any
  time; a change creates version 2 with a reason and never edits this version after results exist.

Results always show two columns: **v0** (original RV / proposal appendix A targets) and **v1**
(this file). A v0 FAIL is never rewritten by a v1 verdict.

## Host

| Item | v1 value |
|---|---|
| Measurement host | Deployed `MiniNautilusHost` EC2 t4g.micro, ap-northeast-1 (2 vCPU Graviton2, 1 GiB, gp3 root, burstable, standard credits). Already paid for; no new resource. |
| Steal | Per-run `host.steal_pct` from `/proc/stat`. A run with steal > 5% or unknown steal is unusable (`ops/runtime_compare.py` marks the cohort incomparable). |
| Background load | 1-minute loadavg recorded at start/end of each run; no build, verify, profiler or other soak may run during a cohort. |
| CPU credits | Burstable host: credit balance is read from CloudWatch before and after a cohort and recorded with the results. A cohort that exhausts credits is unusable. |
| Scope of conclusions | Results hold for this host only. A non-burstable benchmark host needs owner approval of its price first (budget below is USD 0) and does not imply t4g.micro results. |

## Workload (`comparisons/runtime-v2/manifest-v1.json`: the RV manifest with seconds 70, warm-up 10 s)

| Case | Definition |
|---|---|
| Normal, immediate I/O | rate 100/s, warm-up 10 s, 70 s, `fake_mode immediate` |
| Normal, slow I/O | same, `--fake-delay-seconds 0.02` |
| Burst / overload | `--rate 2000 --seconds 10`; functional only in v1: expected outcome is bounded queues and recorded rejections, not a latency verdict |

## Metrics and budgets

| Metric | Definition | v0 target | v1 rule |
|---|---|---|---|
| Internal tick-to-effect p99 | receive → durable effect, per request (`internal_tick_to_effect`) | ≤ 5 ms | ≤ 5 ms (unchanged) |
| Full Rust request p99 | Prometheus `mini_request_seconds` estimate, includes fsync | ≤ 0.5 ms | Reported; absolute target stays the v0 value. No v1 relaxation. |
| CPU / completed event | `total_cpu_ns_per_event` = Python process CPU + Rust child utime+stime, per completed input. `python_cpu_ns_per_event` still reported for comparison with RV results. | n/a (RV guardrail used Python only) | Relative guardrail: candidate regresses ≤ 10% vs baseline |
| Stage durations | per-request rows from `orders.jsonl` (`MINI_ORDER_TRACE`); never p99 subtraction | — | Reported |
| Memory | hot object counts (orders/fills held by Core, mirror, audit) and sampled RSS per segment, plus lifetime peaks reported separately | — | Hot counts must not grow with terminal history; RSS slope ≤ 1 MiB/h over a fixed-load soak after warm-up |
| Recovery time | journal replay at startup | — | Reported as O(history) unless G3 adopts a design that does not read the whole archive; then ≤ 2 × the 1k-history baseline |

## Evidence rules

- A/A first: ≥ 5 runs per side, same binary and receipt, interleaved; a metric whose A/A medians
  differ > 10% or whose ranges do not overlap is unstable and cannot support adoption.
- A/B: ≥ 5 interleaved runs per side; adoption requires ≥ 10% median improvement in the target
  metric, non-overlapping run-level ranges, and all guardrails within 10%.
- "Measurement complete" (A/A stable or instability explained, comparison valid, all runs kept)
  is a finished result even when no candidate improves.

## Budget

| Item | v1 |
|---|---|
| New benchmark host | USD 0 (needs owner approval) |
| One cohort | ≤ 3 h wall time |
