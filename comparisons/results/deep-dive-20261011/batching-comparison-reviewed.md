Repetitions: 5 / 5; compatible successful experiments: True.
Verdicts require >=5 runs per side, non-overlapping ranges and >=10% median effect.
| Metric | Before median [range] | After median [range] | Verdict |
|---|---:|---:|---|
| rust request p99 ms | 0.280360655737709 [0.1957672955974835, 0.6839903846153679] | 1.8265909090909092 [1.7111764705882302, 2.4038888888889076] | inconclusive |
| rust journal_sync p99 ms | 7.549999999999987 [4.793999999999999, 9.981250000000001] | 7.549999999999981 [4.996842105263159, 8.737500000000008] | inconclusive |
| python ipc p50 ms | 0.04346628978224455 [0.04160140657669645, 0.07163646348160352] | 0.08706843874121602 [0.08528415783274441, 0.10950299125632765] | inconclusive |
| python ipc p99 ms | 0.9065168539325804 [0.8449642857142868, 3.1386206896551725] | 1.9790434782608672 [1.8651960784313786, 2.937500000000029] | inconclusive |
| tick_to_trade p99 ms | 8.383333333333326 [4.998499999999999, 10.0] | 8.762500000000005 [7.5249999999999915, 21.50000000000006] | inconclusive |
| loop p99 ms | 4.301931034482747 [4.0660869565217315, 9.769801980198018] | 3.3043076923076806 [2.8446875000000005, 3.7506249999999928] | improved |
| journal syncs | 107.30955159193199 [101.43200000000002, 107.622125] | 105.20833333333334 [103.27566666666667, 107.65216666666666] | inconclusive |
| requests | 25316.276375 [23726.871145190868, 26143.0] | 5296.566333333334 [5178.8475, 5307.356333333333] | inconclusive |
| healthy fraction | 1.0 [1.0, 1.0] | 1.0 [1.0, 1.0] | inconclusive |
| market IPCs per update | 5.0 [5.0, 5.0] | 1.0 [1.0, 1.0] | improved |
| overruns per 1000 updates | 5.194369973190349 [3.345600535296086, 42.584434654919235] | 2.677376171352075 [2.0013342228152102, 3.6912751677852347] | inconclusive |

rust request p99 ms: different events per request; not comparable.

python ipc p50 ms: different events per request; not comparable.

python ipc p99 ms: different events per request; not comparable.

Some ancillary queries failed; inspect query_errors in the source reports. Missing metric values receive no verdict.
