---
name: perf-investigation
description: Measure, explain and improve MiniNautilus performance with evidence instead of guesses. Use for any latency, throughput, CPU-profile or "make it faster" task.
---

# Performance investigation

Humans used to run the benchmark, read the numbers, explain them and decide the next change.
This loop does that, with the human reviewing only the final report.

## Loop

1. **Hypothesis.** One sentence: what is slow, why you think so, what metric should move.
2. **Baseline.** On the unchanged commit:
   `python3 scripts/evidence.py collect --label <topic>-before --reps 5`
   Reps ≥ 5 (compare refuses a verdict below that). Noise is large here: an A/A run (same
   commit twice, 5 reps, MINI_SAMPLES=20000, 4-CPU cloud container) moved medians by up to
   ±195%, and the gate correctly called all 24 rows `inconclusive`. If you change the machine
   or settings, run your own A/A first.
3. **Profile (optional, when the cause is unknown).** `valgrind --tool=callgrind` on the bench
   binary, or `perf record -g` if installed. Save outputs under `runs/evidence/<label>/`.
4. **One change.** Minimal, one idea. Then `scripts/verify.sh` must pass — a faster wrong engine
   is a regression.
5. **Measure.** `python3 scripts/evidence.py collect --label <topic>-after --reps 5`
   then `python3 scripts/evidence.py compare <topic>-before <topic>-after`.
6. **Decide.** Keep the change only if the target metric is `improved` and nothing it touches is
   `regressed`. `inconclusive` means no claim: raise reps or revert.
7. Repeat from 4 at most 3 times, then report.

## Report

- Hypothesis, change, verify result.
- The compare table rows for the target metric (verbatim).
- Environment from `env.json` (cpu, loadavg, dirty flag). Note that cloud-container numbers are
  only valid for before/after comparison on the same machine, never as absolute latency.
- What you did not test and what the next experiment would be.

## Don't

- Don't report a number from a dirty tree (`"dirty": true`) as the baseline.
- Don't compare evidence collected on different machines or different `MINI_SAMPLES`.
- Don't change the benchmark harness and the code under test in the same step.
