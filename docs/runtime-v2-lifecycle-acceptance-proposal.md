# Runtime v2：長期資源、輪換可用性與恢復驗收 proposal

- Created at：**2026-10-12T15:32:31+08:00（Asia/Hong_Kong）**
- Created at UTC：2026-10-12T07:32:31Z
- Version：1.0；Status：**DRAFT，交給下一位 agent 實作，所有新任務 TODO**。
- 審閱基線：`b0223f3`，`codex/cross-platform-backtest-audit`。
- 前置文件（應閱讀上述基線或其後版本）：[proposal v1.2](runtime-v2-next-phase-proposal.md)、[results](runtime-v2-next-phase-results.md)、[rotation ADR](adr-live-rotation.md)、[thresholds v1](perf-thresholds.md)、[architecture](architecture.md)、[CLAUDE.md](../CLAUDE.md)。
- 本文件代號 **LC**；新驗收預算版本 **LC-v1**。保留 RV、NP、v0/v1 的原始結果，不覆寫舊 FAIL。建立時間不變，修訂另加 Updated at 與變更摘要。

## 1. 本輪要回答的三個問題

1. 歷史訂單增加時，輪換與舊成交查詢是否仍受記憶體、磁碟與延遲預算約束？
2. 輪換與恢復期間，系統多久不能交易？能否在保持對帳正確的前提下達到可用性目標？
3. Crash、磁碟不足、遲到回報後，是否仍能精確去重、保留風險，並恢復到正確狀態？

本輪是既有 actor/live rotation 的收斂與驗收，不擴展 multi-instrument 或新交易所。輸出包括實作、可在 EC2 執行的量度工具、機器可讀 evidence、結果報告與營運交接。

## 2. 現況與需要補證的部分

已完成：actor 接入 Binance Testnet、release provenance、95 分鐘 live run、三次離線長 soak、日常 hot state 限制。重用這些成果。

尚未完成的生命周期問題：

- Cold index 每次輪換讀出全部歷史並建立完整輸出 buffer；hot state 有界不等於輪換峰值 RAM 有界。
- 累積 cold index 被反覆重寫，舊副本沒有自動生命週期管理；磁碟副本與寫入成本隨輪換數增加。
- 第一次 cold lookup 先做全檔 checksum；後續 binary search 的 O(log n) 不能代表首次查詢。
- 約 8 ms 是 current-segment engine startup，未包含 runner resume、venue reconcile 或首次 cold lookup。
- Live 每 100 張單輪換約停頓 75 s，Healthy 比例 92.1%，未達既有 99% 目標。
- 新格式 cold-write ENOSPC、完整 publication crash matrix、新 actor 下部分 fault-soak 情境仍欠證據。
- 只驗 current segment 的信任契約尚有待確認。文件寫 ACCEPTED、功能 opt-in、已有 run，均不能自行代替明確契約決策。

## 3. Scope、expectation 與依賴

| ID | Scope | Expectation / deliverable | 依賴 |
|---|---|---|---|
| LC0 | 校正舊結論、建立 evidence contract 與 manifest | 原始結果保留；可程式判定的新 AC；可重現基線 | 無 |
| LC1 | Cold index 建置／查詢與磁碟生命週期 | 輪換峰值 RAM 不再按全部歷史配置；有 crash-safe 回收及磁碟預算 | LC0；契約相容設計 |
| LC2 | 輪換、對帳、resume 的端到端行為 | 限制停止交易時間；恢復時間含真正重新開 gate | LC0；整合最終 LC1 |
| LC3 | 故障、歷史矩陣、EC2 soak 與交接 | 所有結論可追溯至 commit、run、metric、threshold | LC1、LC2 |

必要設計先完成，功能故障測試先於長 soak。LC1/LC2 可獨立開發，但正式性能量度在同一主機上串行執行。每個優化候選只改一個可驗證因素，每輪最多三個候選；沒有性能證據不採用性能優化。

不包括：新 venue adapter、主網交易、multi-instrument/margin/funding、PyO3/shared memory、Core 全面重寫、新 AWS stack／機型升級、以移除 fsync 達標。完整 Rust request 0.5 ms 舊目標繼續報告；不是本輪必須解決的交付。

## 4. 決策與授權邊界

這是草案，數字是**工程驗收預算，不是已量得的能力或獲批准的 SLA**。下一位 agent 開始執行本 proposal 時，將 LC-v1 manifest 在候選修改前 commit 凍結。若 calibration 顯示預算不可行，交付原因與替代預算；新預算另升版，不能回改已收集資料的門檻或移除失敗 run。

| 決策 | 本文件預設 | 影響 |
|---|---|---|
| 歷史完整性 | 保持現有已獲批准契約；查找是否已有具體信任變更批准記錄 | 若找不到，只可繼續離線設計／測試；改變信任語義的 live adoption 標 BLOCKED |
| 信任變更提案 | 列出 startup 不讀 archive、lazy cold validation 的漏檢窗口、驗證時機、失敗處理及替代方案 | 依 CLAUDE.md，具體削弱須明確批准；不能以「請實作 proposal」推論所有契約退讓已批准 |
| 主機 | 既有東京 t4g.micro、2 vCPU、1 GiB、gp3；使用前核對實際規格與可用性 | 不新建／升級資源；既有主機不可用時完成離線工作，EC2 驗收 BLOCKED |
| 額外成本 | 新基建預算 USD 0；既有主機沿用已授權配置；增量 evidence 儲存／API 預算上限 USD 1 | 執行前按實際價格估算，超額提出具體方案，不自行擴容 |
| Testnet | 只用隔離的 Testnet account/symbol scope；執行前確認已有 Testnet 授權適用 | 不碰其他 session 訂單；本次草案編寫不啟動任何交易 |

相容的 batching 可以保留每筆 input 的順序與轉移；batching 不等於 coalescing。是否需要契約變更取決於實際語義，不以名詞推論。

## 5. EC2 量度環境與統一判定

### 5.1 Host gate

- Release binary/hash/receipt、Rust 與 Python source commit、harness commit、clean tree、threshold SHA 必須 100% 可驗證。記錄 kernel、filesystem、EBS size/IOPS/throughput、CPU affinity、sync policy。
- 不與 build、verify、profiler、另一個 benchmark/soak 共用量度主機。取樣工具是同一個凍結 harness 的一部分。
- `/proc/stat` 每秒讀取：1 秒 steal% 的 p99 ≤ **5%**；CPU credit balance 用 CloudWatch 所有可得 5 分鐘樣本，minimum ≥ **20 credits**，記錄 datapoint timestamp。只記首尾不能聲稱中途未耗盡。
- 必要 host/resource 樣本 coverage ≥ **99%**，最大缺口 ≤ **5 s**；CloudWatch credit 首末邊界距 run ≤ **5 min**，中間缺口 ≤ **10 min**。超限或無權讀取時該性能 cohort 為 INCONCLUSIVE，不拿缺值當 0。
- CPU process seconds 包含 Python、Rust、worker/helper 子程序；以 **100% = 一個 vCPU** 報告，另報占整台機百分比。資源 sampler／Prometheus 另報，且納入 host memory headroom。

### 5.2 Repeatability 與判定

- 針對要作比較的 endpoints，A/A 每邊 ≥ **5 runs**；median 差異 ≤ **10%** 且 run-level ranges 重疊才可宣稱穩定。每 run warm-up **60 s**、steady measure ≥ **300 s**。
- 性能候選 A/B 每邊 ≥ **5 runs**，交錯執行，固定 workload/event mix/harness。改善須 median ≥ **10%** 且 ranges 不重疊，所有適用 guardrails 回退 ≤ **10%**；跨界／A/A 不穩為 INCONCLUSIVE。原始 serial 不穩的 endpoint 只報觀察值。
- 絕對預算逐 run 驗收；不得用五次的 median 隱藏一次超標。PASS 要每個 required run 通過；沒有比較候選仍可完成量度調查。
- Event p99 使用原始 duration 的 nearest-rank `ceil(0.99*n)`；n < **1,000** 不作 p99 驗收，改報每次值與 max。Histogram 估值另列，不能混作 raw percentile。
- 任何正確性錯誤均 FAIL；沒有資料 NOT_MEASURED；外部條件缺失 BLOCKED；噪音 INCONCLUSIVE；不得把以上狀態轉 PASS。

## 6. 凍結的 workload matrix

| Profile | 固定輸入與重複 | 用途 |
|---|---|---|
| W0 baseline/candidate | 100 market inputs/s；同一 immutable logical trace；每 10 inputs 一張 terminal order、每單一 fill；immediate／20 ms executor 各一組；active orders cap=8、strategy window=8、N=200 | CPU／latency、event mix 相同的 A/A及 A/B |
| W1 history sweep | 預建有效 history H=1k、100k、1m terminal orders，各一 fill；每 case 固定 N=200、active=0、tail=200 inputs；每 H 五次 fresh-process repetitions | 不能靠短 soak 代替的輪換、cold lookup、recovery 成本 |
| W2 offline reliability | seeds 7/17/29，各 ≥90 min；N=200；100 inputs/s；每 run ≥5,000 submitted orders、≥20 成功輪換 | 故障、長期 resource、完整 lifecycle |
| W3 Testnet | 同一最終 source；兩次各 ≥90 min；Binance Spot Testnet、1 s candles、SMA 3/8、N=100；每 run ≥300 accepted orders、≥100 fills、≥3 輪換、≥1 controlled recovery | 真實 actor/reconcile/rotation 可用性；不是性能 A/B |

預建 W1 可用 streaming fixture，但須符合 production format、cross-record 關係並經 validator 驗證。另有 ≥1 end-to-end 正常產生、跨 ≥3 輪換的 fixture 作一致性對照。Baseline/candidate 使用同一 fixture 副本；不能比較不同歷史量或不同 simulator 記憶體策略。

W1 累積 counts、prices、IDs 用固定 seed 和 manifest；查詢分布：50% exact duplicates、25% absent retired IDs、25% contradictory payloads，後兩者於獨立 fault sessions 驗證 gate，不污染正常 latency cohort。每 H 至少 1,000 warmed duplicate lookups；首次查詢及 rotation 用五次獨立值/max，不能報 p99。

若市場訊號不足，W3 最多延長至每 run **180 min**；仍不足標 NOT_MEASURED 並解釋，不改策略強行製造交易。W3 保持同一 strategy/config；不要以提高 N 逃避輪換驗收。

## 7. 可量化 acceptance criteria

### LC0 — Evidence 與結果修正

| AC | 數字／判定 | EC2 收集方式 |
|---|---|---|
| LC0-1 身分與完整性 | 100% runs 有全部 provenance；0 hash/receipt mismatch；每個檔案 SHA-256 | Git/build receipt、manifest、artifact index |
| LC0-2 可重複性 | §5 host gates；A/A 每邊5次；所有用作採用的 endpoints 穩定 | process/procfs、CloudWatch、run summaries |
| LC0-3 舊結論修正 | 0 個把不穩 A/A 標正式 improved 的結論；0 個以 segment replay 代替完整 resume；G3 hot-state／rotation-peak／disk 分開三欄 | 報告與 machine-readable verdict mapping；不刪原始 evidence |

### LC1 — 記憶體、cold index 與磁碟

| AC | LC-v1 預算 | 收集方式／適用範圍 |
|---|---|---|
| LC1-1 Hot state | W0–W3 `hot_orders ≤ N+8`；controlled one-fill workload `hot_fills ≤ N+8`；live fills 另有 hard cap **10,000**；strategy window ≤8；所有 cache 有 bytes/count cap | 每秒各 component counts，加每次 admission/rotation boundary 的精確 high-water；到 cap 停止新增風險，不能丟 dedup |
| LC1-2 Lifecycle RAM | 整個 runtime process group RSS peak ≤ **384 MiB**；專用 cgroup `memory.peak`（含 file cache）≤ **640 MiB**；host MemAvailable minimum ≥ **192 MiB**；OOM=0、swap-in/out=0 | W1 含 rotation、first lookup、startup、shutdown；W2/W3 全程。每秒 RSS，transition 期間100 ms；cgroup peak 捕捉短暫尖峰 |
| LC1-3 History scaling | 固定 hot/tail，H=1m 的 process-group peak RSS ≤ H=1k + **32 MiB**；max rotation working buffer ≤ **16 MiB**，不隨 H 增大 | 五次 repetitions 取各 H 最大值；實作記錄 buffer high-water，禁止全歷史 Vec／buffer 路徑 |
| LC1-4 Steady growth | W2/W3 首10分鐘後，每5分鐘取 RSS median 作 OLS，斜率 ≤ **1 MiB/h**；最後三段 median 的平均 ≤ 最初三段 + **8 MiB**；每 run 都通過 | 每秒 process-group RSS；短於90分鐘的 run 不驗收此項；不使用 HWM 回落 |
| LC1-5 Cold disk footprint | 設 `L=17*retired_orders+32*retired_fills+32` bytes；穩態 cold/index/temp 總 allocated bytes ≤ **4L+16 MiB**，publication 期間 peak ≤ **6L+32 MiB** | `st_blocks*512`，包含舊代、temp、lookup index；格式可改，L 只是固定比較基準。安全回收受目前及讀取中引用約束 |
| LC1-6 Write amplification | 預建 H 後連續 **20 次**輪換，新增總 retired payload bytes 為 B；cold/index 的 app logical bytes written ≤ **10B+32 MiB** | 所有 compaction/GC/maintenance 寫入計入；不得延期到量度結束才處理。另報 `/proc/PID/io` 及 EBS write bytes，不用它們代替 logical counter |
| LC1-7 Cold lookup | 首次 verified duplicate lookup max ≤ **100 ms**；warm duplicate lookup p99 ≤ **5 ms**；H=1m 相對1k首次讀取 bytes 增量 ≤ **1 MiB** | W1 fresh process；已含 checksum、open、page verification、實際讀取。架構若需完整驗證，照實FAIL，不省略驗證達標 |
| LC1-8 Disk exhaustion | 正常 W2/W3 0 ENOSPC；disk free < max(**2 GiB**, capacity **20%**) 後 **1 s** 內拒絕新增風險 | 每秒 statvfs；離線隔離 quota/image fault injection，不填滿 EC2 root。保留journal，controlled stop；0錯誤刪除仍被引用的資料 |

Cold footprint 不含原始 journal 或 evidence；兩者不得被忽略：每 run 額外總檔案輸出上限 **2 GiB**（不含共享唯讀W1 fixture），主機所有本輪 raw runs 合計 ≤ **8 GiB**。超額停止產生新負載並保留完整已產生資料，不偷偷刪除。原始 journals 長期保留／移出策略另列，不宣稱總磁碟永久有界。

cgroup peak 若不可用，不降低權限／不自行改主機系統設定；列明等價可靠量度方式，否則 LC1-2 peak 部分 NOT_MEASURED。Reader cold index 損壞、缺頁／檔或驗證失敗時，新風險拒絕與 gate 必須 **100%**，不能僅記 warning。

### LC2 — 可用性、輪換及恢復

| AC | LC-v1 預算 | 定義／收集 |
|---|---|---|
| LC2-1 Live 可用性 | W3 每 run trade-enabled ratio ≥ **99.0%**；unexpected gate=0；unexpected reconnect=0 | 分母：初次完成 startup reconciliation 到發起最終 shutdown；分子：實際允許新增風險的時間。包含 rotation pending、rotation barrier、controlled drill downtime；只標 Healthy 不足夠 |
| LC2-2 Rotation pause | W3每次完整 pause max ≤ **10 s**；同一 run所有 rotation pause union ≤ 分母 **0.8%** | 從首次因 rotation 阻止新風險，到 successor 完成對帳並開 gate；waiting TTL 亦計入，不只量 disk rotate |
| LC2-3 Offline rotation | W1每次 durable rotation max ≤ **250 ms**；H=1m max ≤ **2×** H=1k max | 含 cold publication／必要維護、Closed/Genesis、fsync；不含預建fixture。記錄每階段，不相減不同 p99 |
| LC2-4 Engine replay | W1 current-segment startup max ≤ **100 ms**；H=1m max ≤ **2×** H=1k max | 五次 fresh-process；相同hot/tail；另報 bytes/events read。只在完整性契約允許時適用，否則 BLOCKED並保留O(history)結果 |
| LC2-5 Full resume | FakeVenue W1完整 resume max ≤ **1 s**；Testnet正常 W3 resume max ≤ **15 s** | 從啟動runner命令到venue reconcile完成且首次允許新風險；必須包含chain resolution/sidecar/index需要的工作。每個live run前後各一次，共≥4 observations；位置可非零但須一致 |
| LC2-6 CPU/latency guardrails | W0 total process CPU ≤ **30% of 1 vCPU**，1 s CPU樣本p95 ≤ **80% of 1 vCPU**；internal tick-to-effect p99 ≤ **5 ms**；候選CPU/event、executor queue、throughput相對回退 ≤ **10%** | 固定logical input/order mix。Rust完整request 0.5ms舊目標另外報，不從metric中扣fsync；network tick-to-trade不冒充internal latency |
| LC2-7 Unable to rotate | rotation pending後 **1 s** 內不再admit新開倉；等待quiescence上限 **60 s**；超時後 **5 s** 內進入不可交易且記錄durable stop原因 | 離線以持續uncertain／不返回SDK測試；active reservations與uncertainty不釋放。不能強制取消真實side effect來宣稱shutdown完成 |

LC2-2 為明確待驗證目標；若當前10s TTL已使等待逼近預算，須量出等待、poll、cancel、reconcile各階段，再提出相容方案。不能刪除必要對帳、排除等待時間或調大N來達標。

正常完整恢復與無限延遲的外部服務分開：後者不要求強行開gate，驗收fail-closed deadline。報告同時保留engine Healthy ratio和trade-enabled ratio，解釋差異。

### LC3 — 正確性、故障與整合

| AC | 數字／要求 | Evidence |
|---|---|---|
| LC3-1 Correctness | duplicate external submit=0；unexplained position/cash/fill mismatch=0；durable-before-external violations=0；unexplained uncertain=0 | trace、venue/client IDs、replay oracle；不能只靠counter自報 |
| LC3-2 Fault matrix | 下列每類／每publication邊界至少 **3次** deterministic repetitions；所有case invariant violations=0 | 每次expected gate/exit/recovery與actual記錄 |
| LC3-3 Offline soak | W2 三次全部完成；每次≥20輪換、≥5k送單；每種runtime fault每run至少 **3次** | seeds7/17/29；最終replay與FakeVenue position/fills/risk一致；不是要求一律flat |
| LC3-4 Live soak | W3兩次皆滿足樣本量、LC2可用性、resource與最終對帳；REST unexpected errors=0、重複送單=0 | 每個run完整source/receipt；單次成功不能替代第二次；外部錯誤保留并報FAIL/BLOCKED原因 |
| LC3-5 Accounting | 每source/epoch：offered=admitted+rejected；admitted=completed+pending；無法分類events=0 | 正常與hard-stop各報；pending不是completed，也不能冒充零loss |
| LC3-6 Verify / timing flake | 最終commit `scripts/verify.sh` exit0且VERIFY OK；既知timeout-budget測試以deterministic同步覆蓋＋stress **100/100**通過 | 歷史失敗保留。不能只重跑到綠燈；重跑100次本身不證明根因已修復 |

必測runtime faults：lost ACK／late SDK result、SDK never returns、stream gap、queue overflow、writer kill、executor cancel、old epoch。長soak以單一fault排程和復原後下一fault為主；fault之間保持≥30s，不以重疊故障令結果不可解讀。

Publication/storage matrix：cold寫入前、中、fsync後、rename後、directory fsync後、Closed durable後、Genesis durable後、sidecar publication前後、GC前後。每個適用邊界測process crash；另測cold write ENOSPC、partial write、sync failure、missing/truncated/corrupt cold page、wrong anchor、讀取中舊代被回收。恢復要麼得到唯一正確state，要麼明確fail closed，不能默認丟紀錄。硬體power loss未測即明示，不以kill-9代替。

## 8. AI agent 可直接讀取的 evidence contract

以下是**待實作輸出格式**，不是聲稱已有工具。新schema版本固定為 `lc_evidence_v1`；所有 JSON 嚴格格式，未知數值用null並附missing_reason，禁止NaN/Infinity。時間用UTC ISO8601，duration用同process monotonic clock；報告可另顯示香港時間。

每個run使用唯一`runs/lc-v1/<run_id>/`，至少包含：

| 檔案 | 必填資料 |
|---|---|
| `manifest.json` | schema/run_id/profile/seed；source/harness/binary/receipt/threshold SHA；完整非secret command；instance ID/type/region；EBS設定；workload、caps、sync、trust_contract_id；start/end；exit_code |
| `resources.jsonl` | timestamp、monotonic_ns、phase、process_generation、RSS sum、各PID RSS/HWM、cgroup current/peak、MemAvailable、swap、CPU seconds、steal、I/O counters、disk free、hot/cache counts |
| `lifecycle.jsonl` | rotation_id、history orders/fills、pending/gate/publish/reconcile/reopen時間、stage durations、bytes read/written、buffer peak、cold bytes、GC引用、結果／reason |
| `requests.jsonl` | request/order/client ID、input seq、epoch、durable watermark、stages、first/warm cold lookup、result；不要寫keys/signatures或無關account資料 |
| `faults.jsonl` | fault_id/type/seed/boundary、scheduled/injected/recovered時間、expected、observed、state hash、external submit evidence |
| `host-cloudwatch.json` | 所有credit datapoints及timestamps、period、查詢範圍／errors；不得只有彙總 |
| `summary.json` | totals、raw percentiles和n、phase peaks、ratio分子分母、各AC observed/threshold/unit/status、sample coverage、unexpected[] |
| `artifact-index.json` | 每檔size/SHA256、archive SHA256、S3 URI（若已保存）、保存期限、驗證方式；log/stdout/stderr亦納入 |

每項verdict至少包含：`ac_id, threshold_version, scope, metric, statistic, observed, limit, comparator, unit, n, run_ids, status, reason, evidence_paths`。比較結果再加baseline/candidate、A/A verdict、run-level values、median/ranges、effect_pct。0 events 時比例為null，不以0%通過。

獨立分析器從raw重新計算summary；兩者所有整數counts完全一致，浮點duration誤差≤ **0.1 ms**、ratio誤差≤ **0.01 percentage point**，否則evidence FAIL。分析器自身用缺值、亂序、重複ID、hash mismatch、zero denominator fixtures 驗證拒絕錯誤資料。

Raw先壓縮；使用既有同region bucket保存，SHA驗證後才清除本輪本機副本。S3保存預設 **30日**，摘要/manifest/hash留Git；不得修改bucket全域lifecycle或刪他人資料。無S3權限時保留local並清楚標handoff未持久化，不把hash視為backup。

## 9. 執行次序與停止條件

1. Fetch、檢查HEAD/dirty；閱讀instructions與前置文件；不reset或覆蓋他人改動。記錄實際基線，不假設仍是b0223f3。
2. 建立LC0 ledger與contract decision記錄；修正過度結論。完成量度harness並獨立commit，凍結manifest後收baseline。
3. 先跑W1最小／最大歷史短測，暴露rotation peak、cold first lookup和write amplification；不要先花數小時跑soak。
4. 實作LC1／LC2，每項獨立驗證。GC與新format先做crash matrix；必要契約決策未完成則只做相容部分／離線實驗。
5. 跑A/A、符合條件的A/B與W1 sweep；一次最多3個性能候選，不改harness和implementation後混比。
6. 所有短測通過後，EC2串行執行W2，再跑授權範圍內W3。總體分多個可恢復階段；每cohort最多3小時，不讓任一shell工具的timeout決定測試語義。
7. 首次correctness failure、OOM、disk floor違反即停止新增負載、保留證據及可恢復state。性能超標記FAIL，不能無上限延長／重跑挑好結果。

## 10. 最終交付與handoff

新增 `docs/runtime-v2-lifecycle-results.md`，逐項列LC0–LC3 AC及run links。維護ledger：owner/status/start/end datetime/base/candidate/harness commits、命令、verify最後輸出、evidence、已知限制、下一步。

交付必須分別回答：

- 功能與故障正確性是否通過？信任契約是否批准？
- Hot state、輪換peak、cold disk、journal archive四類資源各是否通過？
- Engine replay、首次cold查詢、完整resume各是多少？
- Testnet trade-enabled ratio與rotation最大pause是否達標？
- 哪些是重複驗證結果、哪些只有觀察？哪些仍FAIL/INCONCLUSIVE/BLOCKED？

**全部適用AC通過才稱本輪驗收PASS。** 調查完成但目標未達，可以交付清楚的FAIL與下一步；不能以已跑滿時間或功能opt-in替代验收。通過本輪仍不代表主網或新交易所production readiness。
