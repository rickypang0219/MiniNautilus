# Native CI lock-lifetime follow-up

Created: 2026-10-16T19:56:25+00:00 (UTC).
Updated: 2026-10-16T20:04:56+00:00 (UTC).

## Captured failure and deterministic reproduction

Run [38958850906](https://github.com/rickypang0219/MiniNautilus/actions/runs/38958850906), commit `259fdfa`, exposed the complete error through the shortened annotation. `attempts_survive_restart_reject_resend_torn_and_corrupt_records` and `action_expiry_cannot_outlive_intent_or_move_after_restart` failed at immediate reopen with **WouldBlock**, after the previous logical owner had left scope. These are lock-lifetime failures, not demonstrated latency regressions or an excuse to disable tests.

An open-file-description lock can outlive the logical owner if another descriptor remains, including during a concurrent fork-before-exec until CLOEXEC. Native boundary tests concurrently start Python processes. `owner_drop_releases_attempt_lock_despite_inherited_descriptor` holds an equivalent cloned descriptor alive: without the fix it deterministically fails at reopen with WouldBlock. This reproduces the lock-lifetime mechanism; the old CI process descriptor table was not captured.

`Attempts::Drop` now explicitly unlocks at logical owner teardown. File ownership stays private. Live owners remain exclusive; an old descriptor closing later cannot unlock the next owner; persisted nonce/action de-duplication and torn/corrupt-record rejection remain intact. The new test asserts each of those conditions. No lock assertion, journal check, timing limit or test was removed/loosened.

RED/GREEN passed, and the unchanged12-test native boundary suite passed **20 repetitions with12 threads** after the fix. Full verify and CI38959672921 passed on the lock fix. A subsequent signed run exposed the independent fill-visibility issue, now corrected and tested in f95a88b. Full verify, CI38962119498 and experiment7 signed900 all passed on that final source; see [runtime results](https://github.com/rickypang0219/MiniNautilus/blob/7524d895d618cb912572921afdefea8d39fe9cd7/docs/rust-native-runtime-results.md). Earlier failed/live evidence stays bound to its original binary.

## Historical evidence and diagnostic improvement

Earlier [38957643132](https://github.com/rickypang0219/MiniNautilus/actions/runs/38957643132) (`95635b3`) failed in the native test step with exit101. Its original detailed log is unavailable, so the exact original assertion cannot be retroactively established. Run38958531035 also failed; the first annotation wrapper was truncated. Passing runs38955374834,38957575191 and38958081922 are retained and do not erase failures. Before the deterministic reproduction, six20-test native unit repeats and three full native-feature repeats passed locally; they were insufficient to diagnose the race.

HTTPS CONNECT to `results-receiver.actions.githubusercontent.com` returns403 in this environment. No TLS/authentication/network policy was bypassed. A network draft preserving existing custom domains and adding that exact host was saved; review/save and publication are required only to retrieve those historical full logs. The new structured annotation provides the captured later error through the accessible GitHub API.

The workflow still invokes the same Cargo command and preserves its exit code/full console output. It additionally strips ANSI codes and puts the final2400 characters in an error annotation;16000 characters exceeded the runner annotation transport limit and lost the actual tail. This is observability, not a passing-test filter.
