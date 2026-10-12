# Runtime v2 下一階段：性能、歷史狀態與 adapter 整合 proposal

- Created at：**2026-10-12T11:20:52+08:00（Asia/Hong_Kong）**
- Created at UTC：2026-10-12T03:20:52Z
- 文件版本：1.0
- 狀態：PROPOSED；下列新任務全部 TODO，並非實作成果或上線批准。
- 審閱基線：`c71ec6a4506a4e3b05d158dcb487d45722e52bbe`
- Branch：`codex/cross-platform-backtest-audit`
- 前置文件：[原 proposal](runtime-v2-proposal.md)、[47 項驗收結果](runtime-v2-results.md)、[設計](runtime-v2-design.md)、[操作手冊](runtime-v2-operations.md)。

建立時間永久保留；後續修訂另加 Updated at、版本與變更摘要，不覆寫建立時間。本文件新增 NP 任務編號，不重編或改寫上一輪 RV 驗收結果。

## 1. 目標與現況

目標是改善不依賴交易所的 runtime 成本、長期運行資源使用，以及讓既有 actor 能由真實 adapter 安全接入。優先保持正確性，再以重複實驗決定是否採用性能改動。

既有成果可重用：bounded single-writer coordinator、bounded executor、增量 audit、固定 strategy window、durable watermark、故障測試與三個修正後的 90 分鐘離線 soak。不要重新實作這些功能。

| 已知證據 | 解讀與本輪期待 |
|---|---|
| 慢 I/O internal p99 23.35 → 3.11 ms；admission queue p99 45.46 → 0.81 ms | Actor 有阻塞隔離收益；保留此特性，不能承諾所有負載都更快。 |
| CPU/completed event 454 → 565 µs；immediate case 未證明改善 | 找出額外成本，通過 CPU／queue／throughput guardrails。 |
| Full Rust request p99 estimate 1.47 ms，0.5 ms 目標 FAIL | 查明 durability floor，不重命名或排除 fsync 來達標。 |
| 100k terminal-history case Python lifetime HWM 約 537 MiB | 減少熱狀態對歷史數量的依賴；這不是 strategy 載入全歷史。 |
| Archive 1k → 100k，startup 約 3.05 → 142.88 ms | 分開 RAM retention 與完整 archive 驗證的啟動成本。 |
| SendOrder 的 sync/Rust request duration 中位比例 98.47% | 優先調查 storage；不是整個系統 wall time 的 98.47%。 |
| Actor 仍為 offline opt-in，live SMA 仍串行 | 補 adapter 接入與失敗契約；真實交易另行驗收。 |

上述數字屬舊 cohort 的觀察，不是本輪基線。上一輪 35 PASS、2 FAIL、1 INCONCLUSIVE、9 DEFERRED 不等於 production readiness 分數。

## 2. Task scope 與依賴

| ID | Task scope | 交付性質 | 依賴 | 預期成果 |
|---|---|---|---|---|
| NP0 | 凍結新 benchmark manifest、穩定 A/A、stage attribution | 必做量度 | 無 | 可作判斷的基線，或具體的不穩定原因及限制 |
| NP1 | Actor CPU／queue 開銷調查與最多三個單因素候選 | 必做調查；採用有條件 | NP0 性能 gate | 保留慢 I/O 收益，消除可證實的回退 |
| NP2a | Hot/cold state、dedup、recovery 完整性 ADR | 必做設計與原型調查 | 可與 NP0 調查交錯進行 | 有容量公式、crash model、相容性及遷移設計 |
| NP2b | 持久化 cold index／bounded hot state 實作 | 有條件實作 | NP2a 選定相容方案 | RAM 不隨 terminal history 無界增長 |
| NP3 | Storage floor 校準與 durability 單因素候選 | 必做調查；採用有條件 | NP0；NP2a 格式決策；固定 NP1 版本 | 可證實的 durable latency 改善，或保留 FAIL |
| NP4 | Adapter-neutral actor runner 接入，離線及錄製資料驗證 | 必做實作 | NP1 功能基線與既有 venue ADR | 可測的 bounded reader、recovery、late-result lifecycle |
| NP5 | 整合驗收、長 soak、報告與交接 | 必做 | 所有採用的改動 | 清楚區分功能完成、性能通過及外部驗收未做 |

建議次序：NP0 → NP1；期間可寫 NP2a → 被選定的 NP2b → NP3 → NP4 → NP5。NP0 若無法穩定，仍可推進設計與正確性工作，但不得採用未證明的性能優化。多項工作可分開進行，但性能量度不得與 build、verify、profiler 或其他 soak 爭用同一主機。

### 不包括

- 新的 Kraken／Hyperliquid 真實 connector、Binance rate-limit 改造、主網或 Testnet 下單。
- 多 instrument 正式風控、永續合約 margin／funding／liquidation／reduce-only。
- PyO3、shared-memory IPC、全面 Rust coordinator 重寫、分散式複製。
- AWS 新部署、機型升級或付費資源建立。本文件不授權產生額外雲費用。
- 未經具體契約決策的 trusted-checkpoint startup、歷史資料刪除或 live compaction。

## 3. 不可削弱的條件

沿用 [architecture](architecture.md) 與 [CLAUDE.md](../CLAUDE.md)：deterministic single writer；effects 發布前，intent 及必要因果歷史已 durable；write/sync failure 不發布 effects；timeout 不代表拒單，不釋放 uncertain exposure、不盲目 retry；保留 exact fill dedup、reservation、ID floor、Kill latch、stale feed／intent gates。

Recovery 不重送歷史 effects；完整 journal corruption fail closed，只容許處理最後 incomplete frame。對向 pending orders 不互相抵銷風險。不得刪除或放寬既有測試換取通過。

Strategy 只 fetch 所需 warm-up window 與 incremental data；fixed-size ring／bounded deque 做 O(1) append/eviction，維持 rolling aggregates。禁止將 full historical data 載入 strategy 生成 signal；offline reader 可串流讀全歷史。不得未經量度宣稱 Python reference ring 有 contiguous numeric cache locality。

依 CLAUDE.md，削弱不變式才需要明確人類批准。先完成具體設計、相容替代方案、風險及測試計劃，再提出決策；一般相容實作與可逆調查不用重複詢問。沒有回覆不構成批准。

## 4. NP0 — 基線與量度契約

Scope：檢查現有 `ops/runtime_bench.py`、evidence／compare 工具及 build receipt；必要的 harness 修正先獨立 commit，再凍結 harness。記錄實際 source、release binary SHA、compiler、Python、CPU affinity、storage/filesystem、sync policy、queue limits、seed、quote/order rates、trace mode、run duration。

Expectation：分清 queue admission、writer、durable effect、executor start、外部完成；profiler thread CPU 不能冒充 wall time。保留原 CPU/completed event，另外報 per offered/admitted input、per logical order 等輔助指標及事件組成，不以更換分母隱藏回退。

Acceptance criteria：

- **NP0-AC1**：baseline clean；source/binary receipt 相符。dirty、missing receipt、不同 workload／compiler／sync policy 的比較被拒絕或標為不可比較。
- **NP0-AC2**：manifest 在候選修改前凍結。最低包含 immediate I/O、20 ms slow I/O、1 秒 I/O 故障功能 case；正常負載、burst 與 overload 分開。正式性能 run warm-up 至少 10 秒、量度至少 60 秒；訂單樣本不足則在修改前延長，記錄樣本數。
- **NP0-AC3**：同一版本 A/A 每組至少五次，按既有 evidence stability 規則逐項報結果；不穩定 metric 不可作 adoption 證據。正式 A/B 每邊至少五次交錯執行，維持相同 harness 與配置。
- **NP0-AC4**：以同一 request/order 的 ID、sequence、durable watermark 關聯 stages；不可相減不同分布的 p99。trace/aggregate overhead 獨立披露，不能混成同一 cohort。
- **NP0-AC5**：保留所有失敗／中斷 run、退出碼與原因；報全部 latency、CPU、throughput、RSS、errors、overflow、pending、queue high-water。單一漂亮指標不能代表 release PASS。

性能共同 gate：目標 median 改善 ≥10% 且 run-level ranges 不重疊；相對 guardrail 回退上限 10%，區間跨界或 A/A 不穩定為 INCONCLUSIVE。正確性回退容忍度零。保留 internal tick-to-effect p99 ≤5 ms、完整 Rust request p99 ≤0.5 ms 的原絕對目標；不達標即 FAIL，即使比較有改善也不能改稱通過。

## 5. NP1 — Actor 成本

Scope：profile admission JSON snapshot、queue/fairness、private-report barrier、guard、worker handoff、IPC 等候選；這些不是已證明的根因。不重試已被撤回的 timeout 微調，除非有新的獨立證據與假設。

Expectation：改善立即完成 I/O 時的 overhead，同時保留慢 I/O 隔離；不增加無界 tasks、threads 或 queue。

- **NP1-AC1**：交付 thread/task 可辨識的 profile 與 stages，列出已證實成本及尚未證实假設；profiling run 不當正式 latency benchmark。
- **NP1-AC2**：每個候選只改一個因素，先通過 `scripts/verify.sh`，再按 NP0 比較；每輪最多三個候選。沒有改善或有回退則 revert，保存決策及 evidence。
- **NP1-AC3**：同時比較新 actor 對修改前 actor，以及新 actor 對同機 serial reference。不可只證明比舊 actor 快，便宣稱上一輪 actor/serial guardrail 已修復。
- **NP1-AC4**：source FIFO、critical fairness、private commit barrier、stale intent、late epoch、blocking worker、overflow、shutdown parity 測試全部保持通過。
- **NP1-AC5**：功能結果與性能結果分別列出；未達共同 gate 時維持 opt-in，不宣稱新預設 runtime 或整體性能 PASS。

## 6. NP2 — Retention 與 recovery

NP2a Scope：盤點 Core、bridge mirror、audit、FakeVenue、strategy 各自持有的 order/fill/index，區分 active、uncertain、terminal、dedup evidence。提出固定 active/window 下的 RAM 容量公式，以及 cold index 的 disk growth、lookup 與 cache eviction 規則。

Expectation：先分開兩件事：bounded RAM 不必等同快速 checkpoint startup；保留交易前完整 archive 驗證的方案仍為 O(history) 啟動，不可宣稱 archive-independent。

- **NP2-AC1**：ADR 比較完整 archive 驗證、可信 checkpoint、背景驗證三種契約；選定方案明列 checksum/anchor、watermark、版本、atomic publication、GC 順序、writer exclusivity、migration/rollback。
- **NP2-AC2**：terminal IDs 不可單純 TTL 丟棄。無 venue retirement proof 時，保留可精確查詢的 durable cold evidence；證據缺失或 index 損壞 fail closed。Live compaction 保持 disabled，除非另有符合原契約的 retirement 決策。
- **NP2-AC3**：實作前凍結 hot-state 容量與 lookup latency budget。固定 active orders、window、tail，以 0/1k/100k terminal histories 測試；hot object counts 不隨 archive 線性增長，RSS、bootstrap/recovery/shutdown HWM、disk/index bytes 分別披露。不得只用 allocator RSS 下降代替結構上限證明。
- **NP2-AC4**：完整 replay oracle 與新方案在 state/risk/dedup/Kill/ID floor 上一致；測試晚到 duplicate/new fill、uncertain order、missing index、corrupt index，以及 append/index publication/eviction/GC 前後 crash。未 durable 的 index 不能令已 durable fill 被再次接受。
- **NP2-AC5**：cold miss 不造成無界 I/O backlog；有界 cache、工作量、timeout/gate 及恢復語義具測試，且符合 NP0 性能 guardrails。
- **NP2-AC6**：只有在獲准不讀 archive 的契約下，固定 checkpoint/tail 的 bytes read/events replayed 不隨 archive 增加，且 100k:1k startup 比值 <2，才可宣稱 fast startup。否則明確 DEFERRED，bounded RAM 可以獨立驗收。

NP2b 進入條件：NP2a 完整、方案保持現有契約，或具體改變已獲批准。未進入時交付設計與 prototype evidence，不標 production retention PASS。

## 7. NP3 — Durability 與 storage

Scope：在目標測試儲存上量度 sync 的 bytes、frequency、sequence、durable watermark 與耗時；分開 idle/storage-only 校準及 runtime 負載。根據 NP2a 格式決策，最多三個單因素候選，例如 preallocation、平台支援的 fdatasync、bounded group commit。

Expectation：保留因果歷史完整持久化，找出可採用收益；group commit 在低頻可能增加等待，背景 flusher 不視為必然有效。

- **NP3-AC1**：逐筆 request 的 Rust stages 與端到端 internal latency 關聯；註明 fraction 分母。Storage microbenchmark 不能代替 runtime 驗收。
- **NP3-AC2**：候選定義 max group bytes/count/wait、低頻 flush、shutdown flush、error propagation；不以無限等待或未 durable dispatch 達標。
- **NP3-AC3**：ENOSPC、partial write、sync failure、intent persist 前／後、dispatch 前／後、ACK 前 crash、完整 corruption／torn tail 測試通過。Process kill 與 durable-prefix model 不冒充硬體 power-loss 測試。
- **NP3-AC4**：依 NP0 採用或 revert；絕對 Rust 0.5 ms 未達仍 FAIL。若 storage floor 阻礙目標，交付量度與成本選項，不自行購買新主機或降低目標。

## 8. NP4 — Adapter-neutral runner 整合

Scope：提供 actor runner 與 adapter lifecycle 接口，使用現有 fake/replay/錄製 fixture 實作 reference integration；重用現有 bounded coordinator。生產連線、新 venue adapter、真實下單不在本輪。

Expectation：行情、private reports、timer、strategy 與執行 I/O 分離；禁止把 blocking strategy/SDK 放進 event loop 造成無界阻塞。SDK buffer 與 OS/transport buffer 的控制能力及限制要分開披露。

- **NP4-AC1**：每個 producer/reader/worker 的 count、bytes、max message、in-flight、timeout、overflow/backpressure、shutdown ownership 都有明確上限或外部限制說明；慢 consumer/flood 測試驗證 application bounds。
- **NP4-AC2**：duplicate/out-of-order/gap、snapshot 與增量交界、斷線重連、late epoch、queued stale intent、lost ACK 有 deterministic fixtures；reconciliation 未完成不得重開交易 gate。
- **NP4-AC3**：取消 await 不視為取消外部 side effect；late SDK success 回報可被記錄與對帳，busy worker 未 quiescent 不建立可重複送單的 replacement。
- **NP4-AC4**：相同接受 input 總序下，actor 與 serial/reference state/effects/risk 一致；保留 bounded strategy warm-up/window 及 signal parity。
- **NP4-AC5**：文件明列每項 fake venue 證明與真實 adapter 尚欠的 capability/absence proof。Runner 維持明確 opt-in；不將 fixture PASS 稱作 Kraken／Hyperliquid 或 live 認證。

## 9. NP5 — 整合驗收與交接

- **NP5-AC1**：採用的 code changes 完成 `scripts/verify.sh` → `VERIFY OK`，保存命令、source、exit code、最後輸出與實際 test counts；不沿用舊 commit 的測試作新 commit 證據。
- **NP5-AC2**：最終候選以三個不同 seeds 各跑至少 90 分鐘離線 soak，涵蓋 lost ACK、gap、overflow、writer kill、executor cancel、old epoch。先以短測驗證 harness deadline/shutdown，再啟動長 run。並行 soak 只作 reliability，不能作獨立性能 A/B。
- **NP5-AC3**：每段 offered=admitted+rejected、admitted=completed+pending；預期 recovery 與 unexpected errors 分開。最終 independent replay 驗證 position/fills、zero uncertain/nonterminal、Kill restart；保留 pending，不能偽稱全部 input 已 durable。
- **NP5-AC4**：若採用 retention，soak 必須觸發至少三次實際 eviction/publication cycle（可安全 GC 時亦測 GC），量度 retained counts、cold lookup、每段 RSS、所有 recovery/shutdown peaks。沒有 retention 則只驗收 reporting，不能聲稱 bounded RSS。已有 retention 但未觸發，不算該項 coverage。
- **NP5-AC5**：建立 `docs/runtime-v2-next-phase-results.md`，逐項列本文件 AC 的 PASS/FAIL/INCONCLUSIVE/DEFERRED/BLOCKED、commit、證據路徑、限制與下一步。TODO 不能被當作 DEFERRED；後者必須符合條件任務並有理由。
- **NP5-AC6**：raw artifacts 使用唯一 `runs/` 路徑，不覆寫；Git 保存 manifest、摘要、hash、比較輸出與 profile 摘要。列出 raw data 的可取得位置、保存期限或尚未持久保存的風險；只有 hash 不代表資料已備份。更新 operations 的啟動、回滾、恢復及已知限制。

## 10. 後續 agent 工作規則與 handoff

1. 先讀 CLAUDE.md、architecture、原 proposal/results、本文件、perf-investigation skill。檢查 branch、HEAD、dirty files；fetch 後閱讀差異，不 reset 到本文件基線或覆蓋其他人的工作。沿用現有 checkout，不自行建立 worktree。
2. 在結果文件建立 task ledger：ID、owner、status、start/end datetime（ISO 8601＋timezone）、base/candidate commit、dependency、commands、evidence、decision、next action。接手前先核對 ledger 與實際檔案，避免重跑已完成實驗。
3. 每項假設和改動分開 commit；harness 先凍結，implementation 後修改。驗證環境不足時只完成不受影響部分，記錄具體 blocker，不臆測性能或 secret 狀態。
4. 不讀 `.env*`、不將 secret 寫入文件或 log；本輪不需要交易所 credentials。不要因過往 AWS 授權推論本文件允許部署或下單。
5. 三次優化候選用完就保存結果並交接，不無限追加實驗。條件任務未採用可交付有證據的 DEFERRED；必做實作尚未完成則維持 TODO/BLOCKED/FAIL。
6. 最終分別回答：功能是否完成？哪些性能 gate 通過？哪些安全契約改變？記憶體是否真正有界？是否測過真實 adapter？哪些事情沒有測？不可用一個「全部完成」掩蓋差異。

### Handoff 記錄模板

```text
Task / AC IDs:
Status / owner:
Started at / finished at (ISO 8601):
Baseline / candidate / harness commits:
Hypothesis and expected metric:
Exact commands and environment manifest:
Verification exit code / final lines:
Evidence paths / raw artifact availability:
Observed result / adoption or revert decision:
Remaining blockers / next concrete action:
```

## 11. 本 proposal 的交付與 release 判定

本文件交付的是可執行 scope，不表示授權範圍外的 live/AWS 工作已批准。未來實作應分三層報告：

- **功能完成**：必做實作／設計 AC 通過，所採用方案正確性與整合測試通過。
- **性能驗收通過**：穩定 A/A、有效 A/B、全部適用 guardrails 與原絕對目標通過；否則保留 FAIL/INCONCLUSIVE。
- **Production readiness**：本輪離線驗收不足以給出此結論；另需選定 venue 的真實 lifecycle、能力、風控與營運驗收。

任何一層未達標，都必須在結果與 handoff 中明示；不能以延後條件優化、測試數目或 soak 時長替代實際驗收。
