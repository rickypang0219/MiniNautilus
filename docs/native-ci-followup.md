# Native CI failure follow-up

Created: 2026-10-16T19:57:00Z (UTC).

Run [38957643132](https://github.com/rickypang0219/MiniNautilus/actions/runs/38957643132), commit `95635b3`, failed in `cargo test --locked --features native-runtime` with exit101. The API annotation contains only the exit code. The underlying failing test is **not established** and this incident is not marked fixed.

The signed runtime source passed full local verify, CI [38955374834](https://github.com/rickypang0219/MiniNautilus/actions/runs/38955374834) and the900s AWS acceptance. The subsequent result-document commit also passed CI [38957575191](https://github.com/rickypang0219/MiniNautilus/actions/runs/38957575191). Those successes do not erase the failed run.

Local investigation retained six repetitions of the20-test native unit suite with8 test threads and three complete native-feature suite runs, all passing. This does not identify the original cause and must not be called a repaired flake.

Full log retrieval is blocked by the environment proxy: HTTPS CONNECT to `results-receiver.actions.githubusercontent.com` returns403. No TLS verification, authentication or network policy was bypassed. A network draft preserving all previous custom domains and adding that exact host was saved; environment review/save and publication are required before retrying the affected download.

Commit `ad357ed` makes native Cargo failures additionally available through GitHub check annotations, following the existing Python-test pattern. It invokes the same command, keeps the same assertions and exit code, prints complete output, and adds an escaped last16000-character failure annotation. It does not skip tests, change timing limits or turn failures into successes.

Next agent: once log access is available, retrieve the original failed run, identify the exact assertion/error and reproduce it before fixing. If the failure recurs, read the new structured annotation through the GitHub API. Keep this item open until the original cause is known or explicitly dispositioned; do not infer host noise solely from passing reruns. The live-deployment proposal's operational qualification remains separate from the completed short Testnet acceptance.
