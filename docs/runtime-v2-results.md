# Runtime v2 implementation and acceptance record

日期：2026-10-12（UTC）。Scope：[proposal](runtime-v2-proposal.md)。功能及正確量度基線：`f7186be`；舊長 soak source：`4789ebc`，修正後固定 source：`e16496d`。

**必做功能及設計已交付，三個修正後的 90 分鐘可靠性 soak 全部通過；整體性能驗收仍未通過。** 不把功能測試綠燈、單一 metric 改善，或完成測試時間當作 release PASS。Actor 是離線 opt-in；原有 live SMA runner 仍是串行 venue loop，只有 audit 接入增量檢查。本輪沒有交易所連線、下單或 AWS 部署。

## What changed

- `Coordinator` 分開 single writer 和 account executor；inputs、bytes、message、sources、effects、in-flight calls、tasks 有上限。Source FIFO、critical fairness、overflow／timeout／epoch gates、shutdown、queued expiry 和 terminal cancel guard 都有測試。
- `BlockingExecutor` 最多一個 worker。取消 await 不代表 socket 已取消；busy 未解除前不可另開 executor 或重送。
- Python audit 改為 O(delta) 的 always-on indexes／aggregates；full replacement、reconcile、rotation 和 recovery 做獨立 full audit。SMA 保持所需 window 的 O(1) rolling update，不載入全歷史生成 signal。
- Release receipt、binary/source hashes、dirty detection、raw action watermark、exact admission clock、source/kind queue metrics、錯誤 run 保存及 guardrails 已實作。
- Venue/account/instrument、units、client-ID／idempotency／absence／watermark／accounting 契約有 fixtures。Core 仍是一個 Spot instrument；multi-instrument、margin 和真實新 adapter 不在本輪實作。

Core 不變式保持：deterministic single writer、durable-before-external、保留 uncertainty／reservation、exact fill dedup、kill latch、fail closed。Journal schema 1、protocol 2 不變，新增 trace 欄位不改 persisted frames。

## Evidence and reproducibility

[精簡 reports](../comparisons/results/runtime-v2/reports.json)、[source/build identities](../comparisons/results/runtime-v2/identities.json)、[immutable raw index](../comparisons/results/runtime-v2/raw-index.json)、[correctness](../comparisons/results/runtime-v2/correctness.json)。Raw files 留在 index 指明的 `runs/`，不入 Git；所有 run 目錄唯一。Git 保存摘要、hash、manifest 和 profiler 摘要，不能聲稱它保存了完整 journals。

使用同一台 shared Linux cloud host，AMD EPYC、5 CPU affinity，Rust 1.99 release、LTO、locked dependencies、outbox。硬體、Python、compiler flags、binary SHA 和實際 workload 在每份 provenance。並行 reliability soak 會造成 contention；沒有 CPU isolation，也沒有把 cloud-container 絕對值當部署承諾。

三個 source 世代分開：

| Source | 用途 | 限制 |
|---|---|---|
| `9b4ad06` → `4789ebc` | queued-dispatch safety guard 前後，各五次 | 非交錯，primary internal 有 outlier；只作觀察，非完整 release proof |
| `d26b28c` | corrected external-call queue metric，A/A、交錯 A/B、aggregate/trace、I/O cases | 舊 input queue age 是 receive→writer |
| `f7186be` | exact admission→writer，按 source/kind 分組，A/A、交錯 A/B | 正式本輪 metric cohort；不與舊 harness 混比 |

舊 Testnet run 的 dirty provenance 沒有可重建的完整 patch／binary 身分，不能當本輪 baseline。新離線證據沒有 Binance network 或 ACK latency。

## Canonical performance result

每組五個 run；先同版本 A/A，再交錯 serial/actor。Seed 7，100 quotes/s，12 秒，warm-up 2 秒，manifest：[manifest.json](../comparisons/runtime-v2/manifest.json)。Slow case 注入 20 ms async executor delay；另有實際 1 秒 I/O 功能 case。p99 以下均為五個 run 的 p99 中位數。

| Metric | Slow serial | Slow actor | 判定 |
|---|---:|---:|---|
| Internal tick-to-effect | 23.35 ms | 3.11 ms | ranges 不重疊，改善；此 endpoint 的 A/A 通過 |
| Admission→writer queue age | 45.46 ms | 0.81 ms | ranges 不重疊，改善；此 endpoint 的 A/A 通過 |
| Full Rust request histogram estimate | 1.00 ms | 1.47 ms | INCONCLUSIVE；0.5 ms 絕對目標 FAIL |
| Python CPU/completed event | 454 µs | 565 µs | 約 +24%，guardrail FAIL |
| Executor queue age | 0.30 ms | 0.81 ms | 回退；其 A/A 不穩，不能作改善主張 |
| Completed events/s | 144.48 | 131.63 | guardrail PASS；actor 省去 terminal cancel 的回報，不能當行情吞吐下降 |

Immediate case internal p99 是 3.52 → 4.03 ms，INCONCLUSIVE；executor queue age 0.32 → 0.68 ms 回退。RSS guardrails 通過，Rust／IPC／CPU 多項 ranges 跨越 10% guardrail。**整體 RV2 performance gate FAIL／INCONCLUSIVE，不能把上表第一行當成全部通過。**

[Slow A/A](../comparisons/results/runtime-v2/canonical-slow-aa.json)、[Slow A/B](../comparisons/results/runtime-v2/canonical-slow-ab.json)、[Immediate A/A](../comparisons/results/runtime-v2/canonical-immediate-aa.json)、[Immediate A/B](../comparisons/results/runtime-v2/canonical-immediate-ab.json) 保留 ranges、run counts、RSS 和 errors。

先前 guard 前後觀察：實際 external calls 的 executor queue p99 中位數 28.2 → 0.47 ms。它消除對已確認 terminal 訂單的冗餘取消；五次正常 run 的 orders/fills 均為 111，完成事件由 1,149 減至 1,038 是少了 111 個 cancel replies。這不是 network improvement 或完整 A/A release verdict。

Aggregate／full trace 各五次的舊 cohort：CPU/event 中位數 446／494 µs，約 10.7% 差異；internal p99 反向，顯示 noise，不能把 instrumentation 成本視為已精確扣除。它們不同 trace 設定，普通 compare 會拒絕混比。

## Final scheduling experiment: rejected

第三次、最後一次 runtime 單因素候選（`eedf924`）：以獨立 `asyncio.timeout` contexts 取代每個 effect 三個 `wait_for` tasks。每個 stage 的 timeout 預算及 durable barrier 不變；harness 和 manifest 與 `f7186be` 完全相同。新增 guard-timeout 不送單、各階段預算獨立的 regression tests。

先做 slow/immediate actor A/A 各五次，再交錯 baseline/candidate 各五次。CPU 的 A/A stability PASS；latency A/A 多項 INCONCLUSIVE。Slow A/A 初段與必要 verify 重疊，因此不能把 latency 當安靜主機的證據。

| Metric | Slow change | Immediate change | 判定 |
|---|---:|---:|---|
| Python CPU/event | +1.43% | +5.43% | INCONCLUSIVE，沒有 ≥10% 改善 |
| Executor queue age | +13.52% | +58.60% | INCONCLUSIVE，ranges 重疊，沒有改善證據 |

所有 A/B run 正確性成功，但性能門檻未通過；候選在 `88def66` 撤回，保留安全測試。**沒有採用這項 timeout 優化，也不以更改 metric denominator、durability 或 harness 換取 PASS。** 詳見 [decision](../comparisons/results/runtime-v2/runtime-v2-final-scheduling-decision.json)／[confirmed adoption](../comparisons/results/runtime-v2/runtime-v2-final-scheduling-adoption.json) 及完整 A/A/A/B rows。

## Recovery, retention and conditional decisions

Recovery 每 case 五次，fresh copies，完整 prefix 驗證。下面是 startup 中位數，含 process launch、replay、snapshot 核對及強制 Disconnect；Python checksum/decode probe 是重複工作的 attribution，不是 Rust stages 的精確分解。

| Dimension | Archive quote records | Active orders | Tail | Bytes read | Startup |
|---|---:|---:|---:|---:|---:|
| Archive | 1,000 | 0 | 20 | 162,784 | 3.05 ms |
| Archive | 100,000 | 0 | 20 | 16,171,142 | 142.88 ms |
| Active | 1,000 | 100 | 20 | 186,776 | 4.36 ms |
| Active | 1,000 | 1,000 | 20 | 404,368 | 16.68 ms |
| Tail | 1,000 | 0 | 2,000 | 423,809 | 6.80 ms |

真實 paper journals 的 0／1,000／100,000 terminal history actor case 全通過。新單、晚到 duplicate fill、lost ACK、quiescent reconcile 無 blind retry；100,000 case Python lifetime HWM 約 537 MiB、Rust recover HWM 約 129 MiB。Full snapshot／reconcile／audit 重建及 retained dedup evidence 仍隨歷史增長。

增量 audit 空 delta 的 100,000:0 batch-mean p99 比值 1.40 < 2，entries checked = 0；原 quadratic full audit 與 incremental 的 ranges 分開。這個 p99 是 1,000-call batch averages 的 p99，不能說是個別事件的 tail。

| Task | 決策 | Evidence / preserved contract |
|---|---|---|
| RV4b | DEFERRED | 不讀 archive 的 checkpoint startup 不能保持交易前完整舊歷史 corruption 驗證；沒有批准信任 checkpoint／背景驗證。沒有 venue retirement horizon，不啟用 live compaction。 |
| RV5 | DEFERRED | 沿用 protocol 2 compact delta 和既有 batch。五次 non-durable sim／durable paper probe 有 noise，沒有 binary candidate 的改善證據。 |
| RV6 | DEFERRED | 555 個相同 request 的 SendOrder traces：Python codec/apply fraction 中位數 5.56%、journal encode 0.285%、sync 98.47%。沒有採用未驗證的 flusher／group commit／新 journal。 |

[Correlated stage fractions](../comparisons/results/runtime-v2/stage-fractions.json)、[transport probes](../comparisons/results/runtime-v2/transport-probe.json)、[profiler](../comparisons/results/runtime-v2/profile.txt)。Fraction 來自同一 request 的 durations，沒有相加／相減不同分布的 p99。cProfile 是 diagnostic，thread attribution 不分離、Rust 不在 profiler 裏，不能把 CPU 百分比當 wall time。

## Fault matrix and long soak

Functional load addendum：[load-matrix.json](../comparisons/runtime-v2/load-matrix.json)。保守 serial 400 quotes/s calibration 有零 rejection/pending；100／300／600 quotes/s，各 serial/actor 10 秒均成功。400 不是飽和極限，600 burst 不必 overflow；容量邊界另以 barrier tests 和 scripted flood 證明。這是後加的功能 coverage，不冒充優化前的性能 manifest。

1 秒 I/O、明確 reject、duplicate、reordered cases 成功。Never／accepted-but-ACK-lost cases 正確返回 failure、關閉 gate、保留 uncertainty；不是漏掉的成功樣本。真實 lost-ACK journal 和大歷史 fixture 驗證之後的 reconciliation。

Linux descriptor tests 覆蓋 ENOSPC (`/dev/full`)、successful write 後 fsync EINVAL (`/dev/null`)、nonblocking pipe 的 partial write 後 EAGAIN；Core 不 commit、不發布 effects、poison sticky。既有 durable-prefix、process crash、corruption、snapshot、rotation tests 保留。OS/power-loss model 是截取最後 successful sync prefix，不是假稱 kill -9 證明硬體斷電。

舊 seed 7／17／29 各完成 5,400 秒（`4789ebc`），但都 FAIL：非預期磁碟停頓 gate，以及收尾 `BrokenPipeError`。Seed 29 另有 timeout。每個 run 有九次成功 recovery；最終 `killed=false`，兩個 seed 倉位仍為 2，不能冒充完整 shutdown 驗收。

追到 driver 的確切邊界錯誤：producer 在 sleep 到第 5,400 秒後仍注入 `writer_kill`，而 shutdown 在 actor 發現 writer 死亡前檢查 gate。紅燈測試證明 deadline 後仍 armed fault；修正 `4eff63f` 在 sleep/recovery 後重查 deadline，先 drain 再檢查 gate/process，再決定是否 reopen/reconcile。Scripted fault 使用獨立 counter，非預期 recovery 不再改變覆蓋順序；非預期 gate 仍算失敗。

`e16496d` 補齊 bootstrap/recovery/shutdown 取樣；紅燈測試證明舊版缺少這些 phase。另一次 bootstrap 被 receipt/source mismatch 正確拒絕，三個 run 未開始；保留拒絕紀錄，換入剛 verify 的 release binary/receipt 後才重試。沒有以 allow-dirty 繞過檢查。

修正後三個 90 分鐘 run 全部 exit 0、unexpected = []，各有八次 scripted recovery；截止後沒有再 armed fault，收尾對帳、fixture flatten、Kill/restart 都成功。舊 [sync stalls](../comparisons/results/runtime-v2/sync-stalls.json) 保留。舊 cohort 的 queue metric 是 receive→writer，不能 relabel 作 exact admission。新 cohort 只作 reliability coverage，並行主機 contention 不作性能 A/B。

[分段 resource/fault 報告](../comparisons/results/runtime-v2/soak-resources.json) 保存 phase、每 15 分鐘 RSS/HWM、object counts、queue bounds、pending 和 final fill parity。沒有 retention／live rotation，RSS 增長不能宣稱 leak，也不能宣稱 H4 完成。

### Fixed 90-minute cohort (`e16496d`)

| Seed / full report | Wall seconds | Orders | Unique fills | Final position | Killed after restart | Unexpected errors |
|---|---:|---:|---:|---:|---|---:|
| [7](../comparisons/results/runtime-v2/soak-fixed-seed7-report.json) | 5,400.649 | 7,839 | 7,838 | 0 | true | 0 |
| [17](../comparisons/results/runtime-v2/soak-fixed-seed17-report.json) | 5,400.682 | 7,815 | 7,814 | 0 | true | 0 |
| [29](../comparisons/results/runtime-v2/soak-fixed-seed29-report.json) | 5,400.655 | 7,847 | 7,846 | 0 | true | 0 |

全部 Core/venue final fills、position 相符，health = Healthy，window = 8。[獨立 read-only journal replay](../comparisons/results/runtime-v2/final-journal-replay.json) 另外核對 Kill、flat、zero uncertain/nonterminal orders 及 Rejected tombstone。Orders 比 fills 多一筆是 executor cancellation 留下的 pending intent，對帳以 fake venue 的 fenced absence proof 確認 Rejected；不是遺失 fill。每個 run input high-water = 128（scripted overflow），effect high-water = 1（上限 32）。每個 run 在 writer_failed hard-stop 有兩個 admitted-but-uncommitted inputs，保留為 pending；沒有改寫成零 loss 或假裝已完成。每個 session 都符合 offered = admitted + rejected、admitted = completed + pending；重啟不重送歷史 effects。

| Seed | Active Python RSS, first→last (KiB) | Active Rust RSS, first→last (KiB) | Shutdown Python HWM (KiB) | Shutdown Rust HWM (KiB) |
|---|---:|---:|---:|---:|
| 7 | 30,616 → 69,712 | 2,368 → 12,184 | 80,584 | 43,804 |
| 17 | 30,644 → 70,664 | 2,496 → 12,872 | 80,176 | 43,700 |
| 29 | 30,616 → 69,632 | 2,496 → 12,152 | 79,940 | 43,720 |

Active 欄位使用 warm-up reporting cutoff 後的實際取樣；完整 bootstrap/recovery/shutdown、每 15 分鐘 retained-order/fill counts 及 RSS/HWM 見 resource JSON。Rust HWM 每個 process 重設；Python 是整個 session 的 lifetime HWM。終端歷史和 dedup indexes 繼續增加，沒有 rotation/checkpoint/retention，也沒有 leak 或 H4 穩定性的主張。有限三次 soak PASS 只代表這些負載及故障腳本的 coverage。

## 47 acceptance criteria checklist

PASS 是該項具體契約／coverage 通過，不代表整體 release。條件任務 DEFERRED 不冒充實作驗收。舊 cohort 的 FAIL 永久保留；新 cohort 的 final facts 已核對並保存。

| AC | Status | Test / artifact / limitation |
|---|---|---|
| RV0-AC1 | PASS | `RuntimeEvidenceTests` missing/duplicate/invalid watermark；aggregate/full trace 分開五次；canonical admission/source metrics。 |
| RV0-AC2 | PASS | Python-only dirty、binary tampering、workload/sync mismatch、compiler/Python/affinity mismatch tests。 |
| RV0-AC3 | PASS | Original fixed manifest、release receipts、absolute budgets、source/build identity；load addendum 明確非 retroactive baseline。 |
| RV0-AC4 | INCONCLUSIVE | Canonical primary slow endpoints A/A 通過、交錯各五次；其他 A/A/guardrails 不全穩定。 |
| RV0-AC5 | FAIL | 必報全部 guardrails；CPU/event 超過 10%，Rust 絕對目標未達。 |
| RV1-AC1 | PASS | [ADR](runtime-v2-design.md)，counts/bytes/in-flight/service/timeout/shutdown ownership。 |
| RV1-AC2 | PASS | `Input` schema、epochs、event/receive time、source vs engine seq、correlation 欄位；沒有偽造 native sequence。 |
| RV1-AC3 | PASS | FIFO/fairness、actual journal replay tests；不承諾 concurrent runs 同 arrival order。 |
| RV1-AC4 | PASS | `VenueContractTests` 三個 venue、同 symbol 不同 identity、exact units、fees/balance authorities。 |
| RV1-AC5 | PASS | client-ID scope、idempotency、watermark、absence contract validation；unsupported operation fixtures。 |
| RV1-AC6 | PASS | shared-account independent risk domain 拒絕；multi/margin v0 拒絕，future ADR 邊界明確。 |
| RV1-AC7 | PASS | schema 1/protocol 2 不變；quiescent rollback，沒有重發歷史 effects。 |
| RV2-AC1 | PASS | `test_slow_execution_does_not_block_writer` controlled barrier；bounded blocking worker test。 |
| RV2-AC2 | PASS | `RuntimeParityTests` 每 commit state/effects/refusal 與 serial 一致；inspect/recovery/soak replay。 |
| RV2-AC3 | PASS | source FIFO/critical fairness worst-position test，最多四 critical 然後 market。 |
| RV2-AC4 | PASS (offline) | byte/count/message/source limits、critical overflow/gap tests；真實 reader buffer integration 未包括。 |
| RV2-AC5 | PASS | effect overflow、timeout、lost ACK、executor cancel；durable intent/reservation 保留、無 resubmit。 |
| RV2-AC6 | PASS | 一個 executor；late epoch、queued expiry、private commit barrier、terminal cancel guard tests。 |
| RV2-AC7 | PASS | graceful drain、hard abort、real lost-ACK recovery；fixture shutdown/restart Kill latch。 |
| RV2-AC8 | FAIL | Canonical fixed runs 無 unexpected loss/gate、queue bounds 正確；CPU/executor guardrails 有回退。 |
| RV3-AC1 | PASS | `IncrementalAuditTests` seeded oracle、partial/duplicate/conflict/cancel/reject/full replace/rotation/recovery。 |
| RV3-AC2 | PASS | NoScan operation-count test；0/1k/100k 五次，empty delta entries 0、batch-mean ratio 1.40。 |
| RV3-AC3 | PASS | existing bounded SMA/warm-up/gap/zero-volume parity；沒有改 signal 定義。 |
| RV3-AC4 | PASS | window/queues 有界，Core/mirror/audit/Fake terminal history 仍增長，獨立披露。 |
| RV4a-AC1 | PASS | archive/active/tail 五次 probe；read/decode/checksum/replay/snapshot/startup/RSS 分列。 |
| RV4a-AC2 | PASS | full-before-trade vs trusted checkpoint vs background validation ADR；不放寬契約。 |
| RV4a-AC3 | PASS | retention 列 risk、uncertainty、kill、ID floor、dedup、epoch/horizon；無限精確去重不宣稱有限 RAM。 |
| RV4a-AC4 | PASS | 清楚 DEFERRED fast startup/live retention，保留 O(history) 限制。 |
| RV4b-AC1 | DEFERRED | 沒有選用新的 publication/GC 實作，不能標新 crash matrix PASS。 |
| RV4b-AC2 | DEFERRED | 舊 corruption/rotation fixtures 保留；不冒充新 checkpoint 格式 coverage。 |
| RV4b-AC3 | DEFERRED | 沿用完整 replay；沒有新 cold index／fast-start equality claim。 |
| RV4b-AC4 | DEFERRED | bytes/startup 隨 archive 增長；不宣稱 archive independent。 |
| RV4b-AC5 | DEFERRED | live retirement horizon 缺失；hot full-session history 無固定上限。 |
| RV5-AC1 | PASS (regression) | Existing scalar/batch protocol parity；失敗 prefix/不得整批 retry 語義見 operations。沒有新 batching 採用。 |
| RV5-AC2 | DEFERRED | 新 live batch 未採用；actor 使用逐筆 receive-time-aware requests。 |
| RV5-AC3 | DEFERRED | 沒有 binary codec，無 framing/negotiation adoption claim。 |
| RV5-AC4 | DEFERRED | non-durable/durable probe、correlated stages 支持先保留協議；沒有新 codec 收益證據。 |
| RV6-AC1 | PASS (current contract) | positive durable watermark、missing traces、write/sync/partial/ENOSPC failures；沒有新 durability 優化。 |
| RV6-AC2 | PASS (model) | `uncertain_send` successful-sync prefix/process-crash model；不宣稱硬體 power loss。 |
| RV6-AC3 | PASS (unchanged format) | schema不變、complete corruption/torn tail/rotation tests；未拆 market/order journals。 |
| RV6-AC4 | DEFERRED | 沒有 group commit/flusher candidate 採用，預設 sync policy 不變。 |
| RV7-AC1 | PASS | `scripts/verify.sh` → `VERIFY OK`；66 Rust、107 Python、4 Node、20 independent parity seeds；fresh replay 0/12。 |
| RV7-AC2 | PASS | 47 項 checklist、160 completed reports、1,571 raw-file hashes、source/build identities、full new-soak reports。 |
| RV7-AC3 | PASS (fixed cohort) | seeds 7/17/29 各 90min、exit0/unexpected[]、六類 fault/eight recoveries、fill parity、flat/Kill restart；舊三次 FAIL 保留。 |
| RV7-AC4 | PASS (reporting) | bootstrap/recovery/shutdown + 每5秒 active、15分鐘 segments、RSS/HWM/retained counts、100k history peak；無 retention，H4 未完成。 |
| RV7-AC5 | PASS (honest evidence) | immutable hashes、manifest、A/A/A/B、profiler；不穩/回退明確 INCONCLUSIVE/FAIL，不宣稱全部更快。 |
| RV7-AC6 | PASS | [operations](runtime-v2-operations.md) startup、bounds、gates、stop/recover/rollback；offline 無 keys。 |

## Exact next work

1. 三次優化迭代已用完；後續 actor CPU/guard 性能工作先在隔離主機建立穩定 A/A，再提出獨立單因素實驗。現有功能 opt-in 不代表性能通過；不能用改 denominator 或刪 durability barrier 解決 guardrail FAIL。
2. 下一輪性能驗收使用穩定、符合目標的磁碟／CPU 主機，重建 A/A 和完整 guardrails。本輪三個修正後的 reliability soak 已通過；舊長 run 的 FAIL 保留。
3. 若需要 live actor runner，另做 adapter stream buffering、quiescence/reconciliation、late SDK result 和 capability integration；本文件不批准 live 下單。
4. Checkpoint／retention／binary IPC／durability candidates 依 ADR 和 evidence gate 推進，不為完成清單而放寬不變式。
