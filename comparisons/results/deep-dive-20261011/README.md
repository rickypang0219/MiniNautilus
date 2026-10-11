# Deep-dive evidence, 2026-10-11

See ../../../docs/deep-dive-20261011.md for methods and limitations.

- repeated-evidence.json: five repetitions per measurement variant and comparisons.
- strategy-memory.json: five before/after streaming SMA allocation measurements, identical signal hashes.
- rotation-sustained.json and rotation-bounded-sma.json: single matched 10-minute soaks; do not infer statistical improvement from this pair.
- real-bars.json: full ledger parity over 599 recorded public Testnet candles, including 351 zero-volume bars.
- order-attribution.json and comparison Markdown: fsync experiment and repeated driver results.

Synthetic order timing is local engine/paper timing, not exchange acknowledgement latency. Missing network metrics are not zero latency. The <=5 ms tick-to-trade goal remains unmet. Baseline and prototype Git hashes and workload provenance are included in the reports. Raw artifacts are retained under ignored runs/deep-dive and in an external compressed archive; no credentials are included in these summaries.

- tokyo-public-probe.json: actual Tokyo public REST/WS reachability, recovered from the EC2 console. No credentials/orders; wall-clock differences are not one-way latency.
- aws-cleanup.json: read-back of EC2 instance/security-group cleanup after the probes.
