# Runtime v2：task scope 與 acceptance criteria

日期：2026-10-11。審閱基線：`d65eeddb25ffc929ed88a78f6c3dfa5a6fa1bfcf`，branch `codex/cross-platform-backtest-audit`。

**這是交給實作 model 的 proposal，不是已完成的架構改動或性能驗收報告。** 所有下列新任務目前均為 TODO。開始實作時先 fetch、記錄實際 HEAD、閱讀差異，再確認本文件的現況描述是否仍成立；不要倒退到這個基線覆蓋其他人的工作。

## 1. 目標與邊界

本輪優先改善不依賴交易所的 runtime：I/O 阻塞隔離、可控的排隊與過載行為、增量狀態檢查、可重現的性能量度，以及 recovery／retention 的明確契約。為 Hyperliquid、Kraken 等未來 adapter 留下可用的介面，但不在本輪直接實作它們。

交付分為三層，不能混稱「全部完成」：

- **必做實作：RV0、RV2、RV3、RV7**。建立量度、bounded coordinator、增量 audit，並完成離線故障及 soak 驗收。
- **必做設計與調查：RV1、RV4a**。涵蓋多 venue/account/instrument、風險邊界，以及 checkpoint／歷史驗證的相容性。設計通過不代表多交易所或快速恢復已實作。
- **有條件實作：RV4b、RV5、RV6**。只有契約相容、baseline 顯示值得做，且重複量度支持時才採用。否則交付具證據的 DEFERRED／INCONCLUSIVE 結果，不以未證明的改動交差。

**不包括：** Binance rate limiter、REST polling／user-data stream 優化；真實交易所連接器；perpetual margin、funding、liquidation 的正式實作；跨 writer 的風險預留協議；共享記憶體 IPC、PyO3 遷移、跨機器複製；AWS 新部署或規格升級；實盤／Testnet 下單。上述工作需要獨立 scope。現有 adapter 只做接入新 runtime 所需的相容修改。

現有 bounded SMA window、bounded warm-up、合成行情 batch、零成交量 bar parity、paper rotation 已有實作，不重做；把它們保留為 regression coverage。自動 live rotation 並未完成，不能從 paper rotation 推論已支援。

## 2. 不能削弱的契約

以 [architecture.md](architecture.md)、[CLAUDE.md](../CLAUDE.md) 為準，以下是本 proposal 的必要條件：

1. Rust Core 仍然是 deterministic single writer；Core 不取得時鐘、不做 socket／檔案 I/O，也不呼叫 Python strategy。時間、行情、回報、timer 均由明確 input 提供。
2. 對外 SendOrder／SendCancel 之前，動作及其必要因果歷史已 durable。append／sync 失敗不得發布 effects；async queue 不可繞過這個 barrier。
3. Recovery 不重發歷史 effects。不確定是否送達不等於拒單；timeout 不釋放風險，也不盲目重送。保留 client ID、fill 去重、cancel pending reservation、kill latch、stale intent／feed gates。
4. 價格與數量沿用整數單位。對向未完成訂單不能互相抵銷風險；準入與 reservation 在同一次狀態轉移中完成。
5. 完整 journal corruption fail closed；只可截除最後 incomplete record。checkpoint／rotation 必須有明確 crash-safe publication、writer exclusivity 和版本規則。
6. Strategy 只保留所需 window；O(1) append／eviction 和 rolling aggregates。warm-up／補資料按 window 或有界分頁取得，不向 strategy 載入全歷史。Backtest 可以串流讀完整資料集。
7. 現有 frozen correctness／parity tests 不得刪除、降低斷言或改成跳過來換取綠燈。

若選定方案真的需要放寬契約，先完成相容性分析、替代方案和具體 diff／設計，再列出需人類決定的改動；依 CLAUDE.md 取得明確批准後才實作該部分。其他不受影響任務繼續。本文件不預先批准任何不變式退讓。

## 3. 現有證據如何使用

[90 分鐘 Testnet 記錄](live-testnet-20261011.md) 可用來產生假設，但不能直接當新 runtime 的 release baseline：

- 文件標示 source `bb41137`；[report.json](../comparisons/results/live-testnet-20261011/report.json) 的 provenance 卻是 `d5820b5…`、`dirty: true`。先嘗試找回實際 source patch／binary 身分；找不到就標示不可重現，重新量度，不猜測。
- `python/mininautilus/bridge.py` 預設使用 `target/debug/mininautilus`。這不證明舊 run 一定用了 debug，但新 benchmark 必須明確選擇及驗證 release binary。
- 現有 `ipc` timer 包含 Rust 處理、可能的 fsync、pipe 等待、response decode 和 mirror update；不是純 transport 成本。request encode 還有一部分在 timer 之外。
- 990 ms 是 Prometheus histogram 的 p99 估值，且包含 venue delivery 路徑，不能當作 system-only latency。CPU profile 的百分比也不能直接當 wall-time 佔比。
- 單次 RSS 上升不能證明 leak；單次 drift PASS 不能證明長期記憶體有界。`precondition_check` symbol 不能單獨證明 build profile。

不要相減不同分布的 p99 來估算某一階段，也不要相加各階段 p99 來聲稱端到端 p99。以同一 event／request／order 的 trace 關聯計算。

## 4. 任務及依賴

| ID | 交付 | 依賴 | 完成標準位置 |
|---|---|---|---|
| RV0 | Release provenance、離線 harness、metric contract、凍結 benchmark manifest | 無 | §5 |
| RV1 | Runtime／venue／risk／recovery ADR 與介面 fixtures | RV0 的現況調查；可先寫設計 | §6 |
| RV2 | Bounded coordinator、effect executor、過載及 shutdown 語義 | RV0、RV1 coordinator ADR | §7 |
| RV3 | 增量 audit、bounded strategy regression | RV0；接入 RV2 delta lifecycle | §8 |
| RV4a | Recovery／retention 調查、完整性契約與測試設計 | RV0、RV1 | §9 |
| RV4b | 相容且經選定的 checkpoint／retention 實作 | RV4a 決策完成 | §9 |
| RV5 | 有證據支持的 batching／binary IPC 改善 | RV0；以穩定 RV2 作新 baseline | §10 |
| RV6 | 有證據支持的 journal／sync 改善 | RV0、RV1、RV4a；RV2 穩定 | §11 |
| RV7 | 整合故障測試、soak、報告、handoff | 必做實作及所有被採用的條件任務 | §12 |

建議執行順序：**RV0 → RV1 → RV2 → RV3 → RV4a → 被選定的 RV4b／RV5／RV6 → RV7**。每項分開 commit；每項優化只改一個可檢驗因素。不要把 scheduler、serialization 和 durability 同時重寫後才比較。

## 5. RV0 — 可信的 baseline 和量度契約

建議修改位置：`python/mininautilus/bridge.py`、`ops/order_bench.py`、`ops/compare.py`、`ops/soak_evidence.py`，新增離線 runtime benchmark 與 fixtures。以下新命令／manifest 是待實作交付，不是假裝目前已有。

### Scope

- 提供明確 binary 選擇，benchmark 缺少所指定的 release binary 時直接失敗，不能偷偷 fallback debug。不要因此破壞開發／test 使用 debug 的流程。
- 每次 run 保存 commit、完整相關工作樹 dirty 狀態、source/config/lockfile hashes、binary 路徑與 SHA-256、build command/profile/features、rustc/Python 版本、CPU／OS／磁碟／sync policy、seed、負載與 instrumentation 設定。不要記錄 secret 值。
- 現有 `scripts/evidence.py collect` 是 SPSC benchmark，不能當作完整 runtime harness；其 dirty 檢查只涵蓋部分路徑，不能漏掉 Python、ops、examples、config 或 dependency lockfile。
- 正確性測試用 virtual clock、可控制的 fake venue／executor；性能測試用 monotonic clock 和獨立 open-loop producer。fake venue 至少支援 immediate、1 秒延遲、拒絕、永不回覆、接受後丟 ack、duplicate／reordered reports。
- 記錄 offered、admitted、completed、rejected、coalesced、pending／lost，以及 queue depth／age、event bytes、missed deadlines。定義可核對的計數等式，不能只報完成事件以隱藏過載。

### Metric contract

| Metric | 起點 → 終點 | 必須交代 |
|---|---|---|
| Admission delay | producer 預定產生時間 → input admission | 包括 producer overrun；不能從成功 dequeue 才開始計 |
| Input queue age | admission → writer 開始處理 | 依來源／事件類別分開 |
| Internal tick-to-effect | 行情在本機接收 → durable effect 可派送 | 包含 strategy、queue、IPC、durability；排除 venue network／ACK |
| Executor queue age | durable effect 可派送 → 開始 adapter call | 與 network time 分開 |
| Core／journal stages | 同一 request 的 prepare、encode、write、sync、commit | request ID、seq、effect ID、sync bytes／durable watermark |
| IPC round trip | Python request write 前 → response mirror apply 完成 | 額外分開 encode／decode；不能稱為純 IPC overhead |
| Recovery local | process 啟動 → 本地狀態驗證完成 | 和 venue reconcile → trading gate open 分開 |
| Resource use | CPU time、RSS、queue／retained-state counts、I/O | warm-up、active、recovery、shutdown 分段，另保留全程結果 |

跨 process 時鐘必須證明可比較，否則只關聯各 process 的 duration；不直接相減不同 clock epoch。raw trace percentile 與 histogram estimate 分別命名。異常和 incomplete run 必須出現在報告，不能當 missing data 丟棄。

### Acceptance criteria

- **RV0-AC1**：fixture 可驗證每筆 effect 的來源、request、seq、durable watermark；注入缺失／重複 trace 時報告 FAIL，不能用猜測值補齊。低開銷 aggregate 模式與逐筆 trace 模式分開量度 instrumentation 成本。
- **RV0-AC2**：dirty tree、binary mismatch、不同 workload／sync policy 比較被拒絕或明確標成不可作性能結論；測試包括只修改 Python 檔案的情況。
- **RV0-AC3**：提交 benchmark manifest 與一條可重跑命令。固定 seeds、事件比例、負載、history size、queue capacities、batch 上限、warm-up、run duration、硬體與以下 acceptance budgets，再開始候選優化。
- **RV0-AC4**：同機 A/A 先通過穩定性檢查，再做每邊至少 5 次交錯 A/B。改善需 run-level 目標 metric 的範圍不重疊且 median 改善至少 10%，沿用 perf-investigation／evidence 規則。以 run 為單位，不能把同一 run 的事件當獨立重複實驗。
- **RV0-AC5**：必報 guardrails：internal tick-to-effect p99、Rust request p99、completed throughput、CPU/event、RSS、error／overflow counts。預設 guardrail 可接受的相對回退上限為 10%；用重複 run 的 median 及範圍判斷，跨越界線或 A/A 噪音太大則 INCONCLUSIVE，不能視為通過。正確性回退容忍度為零。

絕對目標必須在 manifest 列出數字，未填即 RV0 未完成。現有 Rust request 0.5 ms、internal tick-to-trade 5 ms、舊 L1 500 µs 的端點及 workload 不完全相同：先逐項對齊，保留原結果，不可偷換名稱當作通過。5 ms 可作 internal tick-to-effect 的初始目標；若目標磁碟的 durable floor 已超標，報 FAIL／需要目標決策，不靠去掉 fsync 取得 PASS。以上是本輪 proposed budgets，不回寫篡改舊驗收紀錄。

## 6. RV1 — Runtime 與未來資料模型 ADR

交付 `docs/runtime-v2-design.md` 及可機器驗證的 schema／fixtures。先比較 Python asyncio＋受控 blocking-I/O workers 與 Rust I/O runtime，根據 RV0 選一個實作；不要預先把整個 coordinator 搬到 Rust 視為已證明的最佳選項。

### Acceptance criteria

- **RV1-AC1**：畫出 input producers → bounded admission／sequencer → single writer → durable effects → bounded executors → reports 的所有權及失敗邊界；列出每條 queue 的容量（count 及 bytes）、優先次序、backpressure、timeout、overflow、shutdown 規則。
- **RV1-AC2**：event envelope 定義 venue/account/instrument ID、source epoch／sequence（可缺省但不能偽造）、event time、receive time、engine seq、schema version、correlation ID；venue sequence 與本地 delivery sequence 分開。
- **RV1-AC3**：定義 source 內順序、跨 source admission 順序，以及 priority／coalescing 的合法範圍。錄下實際被接受的總序後能精確 replay；不承諾不同 concurrent run 有相同到達順序。
- **RV1-AC4**：multi-instrument unit conversion、同名 symbol 跨 venue 的 identity、account ownership、fees／balances 的責任均有 fixtures。以目前 Spot adapter 加兩個能力不同的 fake venue 驗證介面，不需真實 Hyperliquid／Kraken 連線。
- **RV1-AC5**：capability contract 包括 order/client-ID 語義、idempotency、sequence／gap、snapshot watermark、history retention、absence proof、amend／reduce-only 等 supported／unsupported 狀態。unsupported 操作在建立交易 intent 前明確拒絕，不默默降級。
- **RV1-AC6**：預設一個 account risk domain 由一個 writer 管理；共享 balance／collateral 的 instruments 不能獨立分片而各自花同一筆資金。現有 gross strategy inventory 不宣稱 account margin risk；跨 domain risk、perpetual accounting 留作後續 ADR。
- **RV1-AC7**：列出舊 journal／protocol 兼容策略、遷移和 rollback 路徑。多 venue 正式 Core 實作獨立拆單，不在 RV2 順帶改寫所有 risk state。

`src/queue.rs` 是 in-process SPSC 實驗；不直接當作跨 process shared-memory queue，也不直接承接多 producer。若採用它，設計必須說明 fan-in 與記憶體所有權。

## 7. RV2 — Bounded coordinator 與 effect executor

### Scope

分離行情接收、private reports、timer、strategy 與 venue I/O。Engine／Core 仍只有一個呼叫者；blocking REST／SDK 和 strategy 計算不能在 coordinator event loop 中無界阻塞。只提供必要的 adapter shim，保留原有 order／reconciliation 語義。

Queue 必須同時限制 count、bytes、單筆 message 大小、in-flight calls 和 task 數量，不能以 unbounded executor futures 把 backlog 搬到另一處。所有 await／worker cancellation 都要有送出是否已發生的語義。

### Acceptance criteria

- **RV2-AC1**：控制 fake venue call 保持未完成，其他合法 input／timer／private report 仍可由 writer 處理；用可控制 barrier 證明，CI 不靠偶然 sleep 長短判斷。共享 risk domain 的 submit 可以因風險而被 gate，不能為了證明吞吐量繞過 gate。
- **RV2-AC2**：固定串行 fixture 與舊 runtime 每個 committed input 後的 state、effects、error 完全一致。並行 case 對實際 journal 順序 replay 得到相同 state／effect 計算；recovery 本身不派送這些 effects。
- **RV2-AC3**：market 洪峰不能讓 kill、private reports、cancel control、timer 永久飢餓。scheduler 提供有限服務配額／最大連續 market batch，數值寫入 RV1；測試最壞排隊位置。不能靠任意重排同一 source 的 reports 來達成優先級。
- **RV2-AC4**：critical report 不可靜默丟棄。無法背壓來源且 buffer 已滿時，明確記錄 gap、停止新交易並要求 reconcile；connection reader 也有有限 buffer。只有 ADR 證明合法的 latest-quote 類別可 coalesce，且有計數；trade、fill、bar observation、recovery barrier 不可任意合併。
- **RV2-AC5**：effect queue 滿、executor crash、send timeout／ack loss 都不丟 durable intent、不釋放 reservation、不自動重新送單。禁止無界產生待送 effects；寫明 gate 關閉前已排隊訂單如何停止派送、標成 unresolved 並恢復對帳。
- **RV2-AC6**：effect 順序有明確契約；同一 order／account 需序列化的 submit、cancel、reconcile 不會被並行 executor 顛倒。新 epoch 後的遲到結果不能污染目前狀態；過期策略 intent 重新檢查／拒絕。
- **RV2-AC7**：graceful shutdown 先停止新 intent、處理或保留 in-flight uncertainty、寫下最後 durable 狀態；hard stop 後走正常 recovery。不能為了退出而把 unresolved order 改為 terminal。
- **RV2-AC8**：在 RV0 固定負載下，queue 上限不突破、事件計數可核對、無非預期 gate／loss；通過 guardrails。主要改善目標是 injected slow-I/O 下的 queue age／internal latency，正常 immediate-I/O 亦不得掩蓋回退。

本任務不保證任何阻塞磁碟下 writer 仍能進步：durability barrier 仍可能阻塞決策。報告須區分被隔離的外部 I/O 與刻意保留的 journal barrier。

## 8. RV3 — 增量 audit 與 bounded strategy state

將 `examples/sma_spot.py` 每 request 的全歷史 audit 改為由 order／fill delta 更新的 indexes／aggregates。always-on safety checks 保留；完整 audit 可在啟動、reconciliation、checkpoint 及測試 oracle 執行。只把舊 audit 改成每 N 秒跑一次，不算完成。

### Acceptance criteria

- **RV3-AC1**：增量結果與獨立 full-scan oracle 在每筆事件後相同；涵蓋 partial fill、duplicate／conflicting fill、cancel/fill race、rejection、uncertainty、full-state replacement、rotation、recovery。
- **RV3-AC2**：固定 active orders、window 和每 input delta 數量，對 terminal history 0／1,000／100,000 做操作數及性能比較。hot path 不掃歷史；預期工作量只與該次 delta 和必要 active state 相關。CI 用操作數／oracle；時間只在受控 benchmark 評估，p99 的 100,000:0 比值須 < 2。
- **RV3-AC3**：長串流不增加 strategy window 長度；warm-up 不足、duplicate／out-of-order／missing bars、zero-volume bars 行為有 parity fixtures。不得因新的 async 調度重複套用 observation 或改變已定義的 signal。
- **RV3-AC4**：strategy bounded 不等於整個 Python mirror bounded。報告分別列出 window、active indexes、terminal orders／fills、queues 的容量及增長；history retention 問題交給 RV4，不能漏算。

## 9. RV4 — Recovery、checkpoint 與 retention

### RV4a：必做設計和量度

現有 snapshot 要完整 journal-prefix replay 後核對，不能直接加一個 seek 就聲稱安全的快速啟動。調查並分段量度檔案讀取、checksum、decode、Core replay、snapshot load、venue reconciliation，以及 peak RSS。

- **RV4a-AC1**：固定 checkpoint state 與 tail，增加 archived history 量；指出每段是否仍 O(history)。再固定 archive、增加 active state／tail，量度真正恢復成本。
- **RV4a-AC2**：ADR 明確區分完整舊歷史在交易前驗證、信任已發布 checkpoint、背景驗證 archive 三種契約。背景驗證不等於現有 fail-closed startup；若改契約須遵守 §2 的批准關卡。
- **RV4a-AC3**：retention 列出 unresolved orders、balances、risk、kill latch、ID floor、dedup evidence、epoch、水位與 reconnect horizon。不能為有限 RAM 而忘記任意晚到 fill，亦不能宣稱有限集合可永久精確去重無限 ID。
- **RV4a-AC4**：交付清楚選擇：相容方案可實作 RV4b；或哪些保證／資料缺失令 fast startup 暫時不可做。只有串流 replay 降 RSS、但仍讀完整歷史時，必須保留「啟動時間 O(history)」限制。

### RV4b：有條件實作

採用的 checkpoint／retention 方案必須處理版本、checksum／chain anchor、durable watermark、atomic publish、segment closure、checkpoint 與 segment GC 的先後次序、讀取中的檔案生命週期，以及舊版工具兼容性。

- **RV4b-AC1**：crash 注入覆蓋 snapshot 寫入、sync、rename、directory sync、segment close、successor publish、GC 前後；每一點 recovery 得到合法 durable prefix 或 fail closed，不能帶錯倉位啟動。
- **RV4b-AC2**：bad checksum、wrong version／anchor、missing segment、重複／倒退 seq、完整 frame corruption、torn tail、舊 checkpoint 和 invalid successor 均有 fixtures；符合選定完整性契約。
- **RV4b-AC3**：same valid checkpoint＋tail 的 recovery 與完整 replay state／risk／dedup 結果一致；pending／uncertain orders、kill、ID floor 跨重啟不遺失。Cold-history lookup 不可令未知成交被錯誤接受；查不到足夠證據時 gate。
- **RV4b-AC4**：只有在已批准的契約下不需讀 archive，且固定 checkpoint／tail 的 bytes read、events replayed 不隨 archive 增長時，才可宣稱啟動成本與 archive 無關。量度 1,000／100,000 歷史，時間比值 < 2；完整驗證仍需 archive 的方案不標此項 PASS。
- **RV4b-AC5**：固定 active set、window、tail／checkpoint 間隔，hot retained state／queues 有可驗證上限；archive／cold index 容量獨立披露。未具備 venue history horizon／retirement proof 時，不啟用自動 live compaction，不能以 paper 成功代替 live 安全性。

## 10. RV5 — IPC：先 batch，再決定是否 binary

以 existing protocol 2 compact deltas 和既有 batching 為起點。先量 serialization／response apply／syscalls 的成本；只有它們仍是主要成本才做 binary codec。batch 必須有事件數、bytes 和最大等待時間上限；低流量不能無限等滿 batch。

- **RV5-AC1**：unbatched／batched 在相同 input 順序下 state、每筆 effects、error、seq／time 完全一致；明確定義 batch 中途失敗的 committed prefix 與回覆，不能盲目 retry 整批。
- **RV5-AC2**：live receive time 不被 batch timestamp 抹去；stale intent／private events 不因等 batch 被延後到失效。batch 不繞過每個 external effect 所需的 durable causal prefix。
- **RV5-AC3**：若採 binary，需 framing、版本協商、長度上限、unknown fields／schema、truncated／malformed input 處理及 JSON 路徑 differential fixtures；不能增加 unbounded allocation。
- **RV5-AC4**：在非 durable transport probe 與完整 durable runtime 分別量度；符合 RV0 改善規則及 guardrails 才採用。無收益則保留既有協議並提交結果，不為完成清單而換格式。

## 11. RV6 — Durability 優化：不得靠減少保證通過

候選包括 frame encoding、preallocation／fdatasync、bounded group commit。背景 flusher 曾測過但未證明 latency 改善，不能把重做它當作必然收益。先決定 journal／checkpoint 格式契約，再一次測一個假設。

- **RV6-AC1**：逐筆 trace 證明 external effect 發布前所需 seq 已達 durable watermark；測試 write／sync failure、short write、disk full。poisoned writer 不繼續發單。
- **RV6-AC2**：對最後一次成功 sync 的 prefix 做故障模擬，所有已派送動作仍可恢復並保留 uncertainty。process kill 測試與 OS／power-loss model 分開，不宣稱普通 kill -9 證明硬體斷電安全。
- **RV6-AC3**：journal format 有 backward compatibility／migration、完整 corruption fixtures；若拆 market／order journal，明確保存 replay 所需因果資料和跨檔案水位，不能只刪行情紀錄而保留原 replay 承諾。
- **RV6-AC4**：group commit 有最大等待及 batch 上限，涵蓋單筆低頻訂單與 burst。除 internal tick-to-effect 外，Rust request p99 亦需符合 guardrails；主目標依 RV0 重複比較通過才合入。

不改預設 sync policy；不實作「第二台機器收到 RAM 就取代本地 durable storage」。遠端持久化、quorum、fencing 是另一份設計。

## 12. RV7 — 整合驗收與交接

### 固定驗證矩陣

| 維度 | 最少案例 |
|---|---|
| Event mix | market-only；strategy→submit→ack→partial/full fill；cancel race；recovery |
| History | 0、1,000、100,000 terminal orders；固定 active set，另列 active-set scaling |
| Load | 同機 baseline 可持續負載的 25%、75%；150% 的 10 秒 burst；絕對 rates 在 RV0 凍結 |
| I/O | immediate、1 秒 slow call、永不回覆、accepted-but-ack-lost、明確拒絕 |
| Interruptions | writer／executor crash、feed gap、duplicate／late report、journal write／sync failure |
| Durability windows | intent 前；persist 後／dispatch 前；send 後／ack 前；fill 收到但未 durable；checkpoint／rotation 中途 |

負載率用 baseline 校準後固定，不能用候選版本自己的上限重算以隱藏回退。無法在小 host 跑 100,000 history 時記錄資源限制，另在有足夠 RAM 的主機跑該 case；不能把兩台主機的絕對 latency 拿來做 A/B。

### Acceptance criteria

- **RV7-AC1**：`scripts/verify.sh` 輸出 `VERIFY OK`，新增 correctness suites 接入同一 CI gate；報告命令、commit、最後輸出。變更 journal／recovery 時另執行 verify-change skill 指定的 fresh journal／replay 檢查。
- **RV7-AC2**：所有必做及已採用條件任務的 AC 有測試名、命令、artifact／摘要對應。fake venue 接受的每張單都能由 durable intent 和最終 state／unresolved 集合核對；不得宣稱網絡 exactly-once delivery。
- **RV7-AC3**：完成至少 3 個不同固定 seeds 的 90 分鐘離線 soak，固定 active state／window、持續產生 terminal history，包含過載與故障腳本。每個 run 分開列出預期 gate、非預期 errors、遺失／重複 events、queue 上限及 recovery 結果。這是可靠性 coverage，不代替性能 A/B 的每邊至少 5 次。
- **RV7-AC4**：記憶體報告包含 warm-up 後逐段 RSS／object counts、high-water marks、rotation／checkpoint peak。只在 RV4 retention 已完成且實際觸發多次時評估其穩定性；沒有 retention 的 run 明確報 remaining growth，不能宣稱 H4 已完成。
- **RV7-AC5**：所有性能主張附 immutable raw artifacts、manifest、比較結果及 profiler 證據；無穩定 A/A、sample 不足、host credit／swap／I/O 噪音過高時標 INCONCLUSIVE。受控主機資料與 t4g.micro 部署資料分開；不為本 proposal 自動增加 AWS 成本。
- **RV7-AC6**：操作文件說明啟動、queue／batch／timeout 配置、gating、graceful stop、crash recovery、roll back；migration 不可使舊 binary 誤讀新 journal。整合完成前以 offline fake venue 驗收，不需要 API keys。

## 13. 給實作 model 的工作與回報規則

1. 先讀 CLAUDE.md、architecture.md、verify-change／perf-investigation skills 和本 proposal；對最新 HEAD 做 gap check，只做尚未完成部分。
2. 建立 AC checklist。RV0 凍結 baseline manifest 後，每次只推進一個可審閱的 task／subtask。新增不變式測試先證明能抓到問題，再實作。
3. 必做項目持續完成；條件任務先交付決策證據，不需為 routine implementation choices 反覆問人。遇到真正的契約退讓才依 §2 停止該部分。
4. 原始 artifacts 放唯一 `runs/` 目錄，不覆寫；精簡結果、hash、重跑命令放 `comparisons/results/runtime-v2/` 及 `docs/runtime-v2-results.md`。機密、API keys、完整環境變數不入 repo。
5. 每項回報使用以下格式；「code 完成」與「驗收通過」分開，沒有數字不得寫「更快」。

```text
Task / AC IDs:
Source commit / binary SHA-256 / manifest:
Changes and preserved invariants:
Correctness command + result + verify tail:
Performance primary metric / guardrails / repetitions:
Fault and soak artifacts:
Status: PASS | FAIL | INCONCLUSIVE | NOT RUN | DEFERRED
Remaining limitation / decision / exact next task:
```

本輪完成意義：必做實作與設計有完整證據，條件任務有可追溯決策，未完成限制清楚列出。它不等於 Hyperliquid／Kraken production-ready、live recovery 已驗證、或所有硬體上都能達成 5 ms。
