| Metric | controlled-every-5m | controlled-outbox-5m | after/before |
|---|---:|---:|---:|
| rust request p99 ms | 4.216 | 0.345 | 0.08 |
| rust journal_sync p99 ms | 4.116 | 11.700 | 2.84 |
| python ipc p50 ms | 0.748 | 0.041 | 0.06 |
| python ipc p99 ms | 4.416 | 2.851 | 0.65 |
| tick_to_trade p99 ms | 4.908 | 26.300 | 5.36 |
| loop p99 ms | 17.291 | 44.105 | 2.55 |
| journal syncs | 72766.667 | 83.325 | 0.00 |
| requests | 72490.731 | 68794.510 | 0.95 |
| healthy fraction | 1.000 | 1.000 | 1.00 |

| Check | before | after |
|---|---|---|
| L1-rust | FAIL | PASS |
| ipc | FAIL | FAIL |
| tick-to-trade | PASS | FAIL |
| drift | PASS | PASS |
| errors | PASS | PASS |
| gate | PASS | PASS |
