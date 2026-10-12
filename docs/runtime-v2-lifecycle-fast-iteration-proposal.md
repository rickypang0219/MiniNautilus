# Runtime v2 Lifecycle：增量歷史與15分鐘快速驗收 proposal

Created at：**2026-10-12T20:05:09+08:00（Asia/Hong_Kong）**  
Version：**2.1 DRAFT**；threshold version：**LC-v2-fast**。  
Updated at：**2026-10-12T20:19:05+08:00**。  
變更摘要：補齊F0–F6問題、解法、理由、前置條件、steps、validation與dependency；AC數字及15分鐘window不變。  
Baseline：`5c5fe90`，`codex/cross-platform-backtest-audit`。  
Owner：下一位 implementation agent；使用者批准具體 integrity 退讓。  
來源：[本輪檢收](runtime-v2-lifecycle-review-v2.md)、[LC-v1 proposal](runtime-v2-lifecycle-acceptance-proposal.md)、[成果](runtime-v2-lifecycle-results.md)、[待決 ADR](adr-lifecycle-contract-decision.md)。

## 1. Scope、expectation 與完成定義

本輪三個 outcome：

1. Cold storage 不再每次重写完整歷史；reader pin 不令磁碟無限制累積。
2. 完整輪換、查詢與恢复在固定歷史量下有可信的數字、清楚的完整性契約。
3. 每次 **15分鐘** 整合 soak 可快速揭露 crash、resource、accounting 問題，輸出 AI agent 可獨立重算的 evidence。

預期交付為 production implementation、格式／migration ADR、故障測試、frozen harness、machine-readable manifest/ledger、raw archives 與結果文件。這份草案不聲稱下列數字已達成。

In scope：provenance、cold 格式及增量 publication、retirement catalog/reader lifetime、bounded caches、compatible migration、完整 resume、15分鐘 fake/Testnet integration。
Out of scope：新 venue、multi-instrument、binary IPC、移除 fsync、主網、ECS 搬遷、新 AWS stack、長時間 leak 或 production readiness 宣稱。
保留 single writer、durable-before-external、uncertainty/reservation、exact dedup；strategy window 使用 bounded ring/deque，O(1) append/eviction，window≤8。

**全部本輪適用 AC 通過才能稱 LC-v2-fast PASS。** 功能完成但 integrity 決策未定、資源超標或 Testnet 樣本不足，仍分別標 BLOCKED/FAIL/NOT_MEASURED。Offline 與 live 分開 verdict。

## 2. 與 LC-v1 的明確差異

- W2 從每 seed 90分鐘改成每 seed **900秒**；W3 每次也固定900秒，不自動延長到180分鐘。
- 日常開發先 seed7 一次；候選定版才依次跑7/17/29；Testnet定版兩次。不得反覆重跑只挑成功結果。
- 舊 LC1-4（90分鐘後記憶體趨勢）、LC2-1（99% live availability）與原長測驗收保留原狀。本輪另報15分鐘 resource envelope與 downtime，不將舊 AC 改成 PASS。
- Fake N=200；fast Testnet **N=10**，用較少自然訂單覆蓋輪換。這是新 workload，必須凍結及標示，不能與舊 N=100/200 的 downtime 直接比較；不更改 SMA 訊號製造訂單。
- 15分鐘是 steady observation window；build、fixture、warm-up與最終安全清理另計並分開報時。發現正確性錯誤立即停止新增負載，保留退出／復原證據。
- 15分鐘縮短 soak，不縮減 W1 歷史量或20次輪換壓力測試，也不替代正式 A/A、A/B。

## 3. 決策與部署邊界

現有量度是東京 EC2 t4g.micro / 1GiB / gp3，並非 ECS。本輪預設重用已授權主機；不新建資源。若已有可用 ECS task，可跑相同900秒 profile，須記錄 task definition、image digest、launch type、底層主機/CPU架構、task CPU/memory limit、volume及cgroup；沒有可靠 peak/host資料則對應 AC 不可 PASS。A/B 必須同一部署方式、機型、storage與harness。

`CLAUDE.md` 明示：**“a change that weakens one needs explicit human approval.”** 因此：

- 相容 GC、catalog、offline 新格式原型可直接做。
- Live 採用目前 current-segment/first-use 契約仍須記錄明確批准。
- 若新格式改成 root + requested-page verification，ADR 必須逐項列出 startup、first query、背景 scrub、corruption detection 的時機。不得把 hash tree 等同全檔檢查。
- 未批准時維持原完整驗證模式，照實報 lookup FAIL/BLOCKED；不要為達100ms省略完整性。新 ADR 不能沿用舊批准作更廣退讓。

本草案可先完成設計及離線工作，不要求現在中斷草擬等待決策。Testnet使用既有隔離 scope；不得清理其他 session 的單。新基建成本0，增量 evidence/API≤USD1；不建立 NAT/ALB、不更改 bucket 全域保留政策。

## 4. Tasks 與實作順序

| Task | 工作與 expectation | 交付／完成依據 |
|---|---|---|
| F0 | 修補 source/binary/harness receipts、獨立分析器及完整進程取樣 | AC-E；先獨立commit harness，再凍結manifest及baseline |
| F1 | 設計增量 immutable blocks/tree 或 bounded segments；估算1m與20輪換 read/write成本；比較完整性時機 | ADR、byte layout、migration/rollback、fault boundaries；保留完整驗證相容模式 |
| F2 | 實作增量 publication、bounded maintenance、durable退休catalog、pin重試與admission | AC-S、AC-P；crash後不得遺失被引用檔案／去重證據 |
| F3 | 補完整 runner resume、固定tail matrix、resource/accounting高水位 | AC-L、AC-R；不能以engine IPC startup代替resume |
| F4 | 完整故障矩陣及15分鐘fake integration | AC-C、AC-F；先seed7，定版再三seed |
| F5 | 已批准契約後，兩次15分鐘Testnet integration | AC-T；不足樣本不延長、不強迫交易 |
| F6 | 整理每AC ledger、raw hashes、成本、host restore與後續事項 | 獨立 analyzer重算；結果文件及操作runbook |

F1 必須解決 unbounded segment count / read fanout，不能把寫入問題轉成每次查詢開啟全部歷史檔。
Maintenance 的所有寫入與前景干擾都算入預算；不能把compaction延期到測試結束。若 maintenance 有債務，manifest凍結上限及每cycle計數；超限 fail closed，不能稱 sustainable throughput PASS。
Migration 只讀原始 MNCOLD01、寫新檔、驗證後原子發布；原始資料保留到新版本驗證及rollback策略完成。不同版本並存期間也計 allocated bytes；一次性migration成本單獨報告，不混入steady rotation改善。

## 4.1 Task 詳細規格與 agent 實作 context

以下解法是有根據的建議路線，尚不是已驗證可達預算的實作。Agent 可提出替代方案，但須先更新 ADR、成本模型及 frozen manifest，不能收完數據後才改設計分母或門檻。每項 task 交接須附：problem → hypothesis → design → steps → invariant/tests → measured result → remaining work。

### F0 — 可信 evidence 與可重複 baseline

**問題與根因。** 原 baseline binary 與 recorder checkout 不同，部分 manifest 只記 recorder HEAD；短測 coverage 不足，舊 steal 取樣不是完整1秒。結果即使看起來快，也無法作完整採用結論。

**建議解法與理由。** 把 executable、Python package、harness 三種身分分開記錄。量度使用同一已凍結 collector；先取得合格 A/A，才比較候選。不要把「修量度工具」與「改 engine」放進同一 A/B。

**所需東西。** `ops/lifecycle_evidence.py`、`ops/lifecycle_report.py`、`ops/lifecycle_startup_probe.py`、`scripts/evidence.py`、現有 raw archives、release build receipt、CloudWatch credit read access、可可靠捕捉整組 process peak 的 cgroup。缺少資料保留 null 與 reason。

**Steps。**

1. 核對 HEAD、dirty tree、AWS主機是否空閒；列出本輪各 binary/source/harness 的實際身分，禁止從檔案名稱推測 commit。
2. build receipt 記錄 source commit/tree、Cargo.lock hash、toolchain/target/profile/flags、binary SHA；Python source/dependency identity 另列。啟動時重新驗證執行中的 binary。
3. 以固定 monotonic deadline 取樣，記錄予定及實際 sample 時刻、missed samples、PID/starttime generation。包含 worker descendants，正確处理已退出進程CPU及PID重用。
4. 每秒記錄原始 CPU/steal ticks；CloudWatch抓完整時間範圍。RSS、cgroup、hot state、buffer、bytes counters各有定義，不互相代替。
5. Analyzer測缺值、hash mismatch、重複run/event、亂序、zero denominator、路徑逃逸；raw可重算所有判定，summary不能自行宣稱PASS。
6. 單獨commit harness，凍結 LC-v2-fast manifest；收未改動engine的W0 A/A、W1 baseline，將不合格run保留並標原因。

**驗證／完成。** AC-E及host gates；重算counts完全一致。交付CLI用法、receipt schema、baseline run IDs與A/A verdict。工具準備完成不等於A/A穩定；不穩時繼續做正確性工作，性能採用標INCONCLUSIVE。

### F1 — Cold v2 資料結構、完整性與 migration 設計

**問題與根因。** `src/cold.rs::extend` 雖只使用小buffer，仍把所有舊records讀寫一次；`ColdIndex::open` 仍驗全檔checksum。這兩項成本隨H增加，單靠background thread或更大buffer不會消除49MB首次讀取及982MB累計寫入。

**建議解法。** 優先評估 **immutable、按頁 copy-on-write 的有序索引**：order及execution兩個索引，generation manifest只引用root及其完整性摘要。固定大小頁，輪換一次批量加入本segment資料，只寫變動leaf及共同ancestor；舊generation共用未變頁。

**為何選這個方向。** 可以把查詢fanout限制到tree高度，支援非連續execution ID，避免每次掃全部segment；sharing也減少pinned generation完整複製。單純append一個檔容易寫得少，但其索引、bounded lookup及重啟仍要解決。LSM也是候選，但必須將compaction寫放大、lookup fanout及maintenance debt一起建模。

**所需東西。** `docs/architecture.md`、`src/cold.rs` 的order/fill byte layout與lookup verdict、`ColdRef`及其journal序列化、`src/journal.rs`的Closed/Genesis流程、現有dedup測試；新增versioned格式ADR與validator。現有order記錄為id/filled/terminal lifecycle，fill為execution_id/order_id/qty/price，不能刪掉用來辨認contradiction的欄位。

**Steps。**

1. 列出lookup oracle：相同retired report為duplicate、相同ID不同payload為contradiction、absent key不能錯認duplicate；對order/fill兩種key獨立定義。
2. 比較COW tree、bounded sorted segments與LSM，列出H=1m、每cycle200order+200fill、20cycles的read/write/space估算。初始評估4KiB/16KiB頁；明列fanout、頁填充率、兩索引及metadata成本。
3. 批量update須排序新key、同輪共同路徑只重寫一次。禁止每record獨立複製root路徑而假設寫入自然達標。把預期pages/cycle換算bytes，先檢查≤10B+32MiB的可行性。
4. 設計格式magic/version、byte order、key ordering、length/overflow bounds、page/root hashes、generation identity、missing/invalid page處理。區分偶然損壞檢測與惡意修改防護；未有可信root來源不宣稱authenticated。
5. 明列兩種模式：完整掃描驗證相容模式；root+visited-page模式。後者未讀頁的損壞可延後發現，需要具體批准；背景scrub不會消除發現前的窗口，亦不能在計時外偷偷完成首次驗證。
6. migration streaming讀MNCOLD01並驗全檔，建新索引、做count/content parity、durable publish；保留原檔。新reader支援舊格式，writer不暗中改歷史；rollback有新寫入時不能只換回舊binary，須具體定義可驗證的重建/export路徑。
7. 在ADR畫出所有durability boundaries、crash survivors、pin/GC reachability、ID floor與root binding；產出小fixture驗證格式設計。

**驗證／完成。** ADR有成本表、integrity比較、migration/rollback及fault matrix；prototype輸出逐page bytes與lookup parity。F1完成不代表格式已採用。若算出門檻不能達成，先交付原因與替代設計，不能降低budget裝作成功。

### F2 — 增量 publication、安全回收與 backpressure

**問題與根因。** `remove_superseded`只嘗試當前的直接前代，被pin時跳過，以後沒有retry；20個reader因此保留20個完整cold副本。刪檔不是唯一問題：publication、live roots、GC與reader registration必須避免race。

**建議解法與理由。** 實作durable retirement catalog及可重入sweeper，配合F1共享頁；設reader generation admission及disk reservation。安全性先由reachability/pin決定，資源不足時明確拒絕新reader或publication，不能強制失效仍在使用的FD。

**所需東西。** `src/cold.rs`、`src/journal.rs`、`src/main.rs`、`python/mininautilus/bridge.py`、`live_actor.py::_rotate/_disk_guard`；generation catalog schema、跨process reader lock協定、system crash hooks、disk accounting。

**Steps。**

1. 固定publication協定：reserve headroom → write新pages/root → fsync及必要dir sync → durable Closed/Genesis binding →發布新current generation →登錄退休候選。具體Closed先後順序須符合現有修復流程，ADR列明每一步失敗如何重試。
2. 增量writer僅寫新records、受影響pages及metadata；app counter計成功、partial、retry、catalog、GC/compaction全部bytes。必要background maintenance同一run內計入。
3. Reader在與GC互斥的registration窗口取得generation pin，然後驗root/讀頁；GC在相同協定下決定live roots。禁止「掃到沒有pin→reader隨後pin→unlink」race。
4. Catalog至少包含generation/root、superseded relation、retire state及checksum；current/live pinned roots可達的頁一律保留。若共用container檔內有dead pages，計整個allocated bytes；需要rewrite才能釋放時算入寫放大。
5. Sweeper持久化待回收狀態、bounded批次處理，unlink後dir fsync；每≤5秒重試，restart重建可驗證catalog並恢復。不能靠掃目錄猜「老檔一定可刪」。
6. Manifest明訂cache bytes、reader count、metadata/batch cap與maintenance debt cap；disk reserve計migration並存、pinned live sets及planned publication，不只看當前L。
7. 預估會越budget時≤1秒拒絕新增pin/publication，對actor阻止新增風險並保留uncertainty。單一長reader與20個不同generation壓力分開測；禁止用無限pin測試承諾全部輪換都完成。
8. 保持舊格式預設可讀與explicit opt-in；新增格式、GC和integrity模式在command/receipt中都有身分，不能由flag名稱推定owner已批准。

**驗證／完成。** AC-S、AC-P、AC-D；20cycles無pin全成功、單pin可讀且釋放後回收、multipin拒絕有證據。每次publication/GC crash後state唯一或fail closed，0被引用頁誤刪。交付bytes階段表及remaining GC debt，不只看最後磁碟size。

### F3 — 固定 workload 的 lookup、startup 與完整 resume

**問題與根因。** 現有startup supplement雖用fresh process，但hot/tail不完全匹配；in-process replay與IPC ready不能代表runner已經可以交易。Actor的waiting、reconcile、first fresh quote都會影響恢復。

**建議解法與理由。** 建一個production runner resume harness，明確記下每個邊界，用同一signal-independent fixture評估history成本；固定hot/tail，才可將差異歸因於H。

**所需東西。** `ops/lifecycle_startup_probe.py`、`examples/lifecycle_probe.rs`、`python/mininautilus/live_actor.py`、`lifecycle.py::AdmissionClock`、FakeVenue、bridge/sidecar recovery及前述新格式validator。

**Steps。**

1. 為H1k/100k/1m生成一致有效fixtures，tail固定200inputs、active=0、固定carry及strategy window。至少一份fixture經production路徑正常生成並跨3次輪換，與streaming fixture對照。
2. 每rep複製唯讀baseline到唯一run目錄，驗hash；fresh Engine/runner process，不重用engine、index handle或cache。每H五rep，不全機drop caches。
3. 記錄launch、chain/sidecar resolve、engine ready、root/page驗證、reconcile完成、strategy ready、fresh quote及first-new-risk-enabled；FakeVenue等待不省略coordinator。
4. First duplicate query涵蓋必要open/verification/read；另測1000warm lookup。讀取所有index/journal/metadata bytes分開記，不能只算payload或/proc physical I/O。
5. 每H做duplicate、absent retired ID及contradictory payloadoracle；後兩者在独立fault session，避免污染正常latency cohort。
6. 收process tree RSS/cgroup、buffer/cache精確high-water、hot counts、per-source/epoch totals。不能將冷頁cache藏在worker而只報RustRSS。
7. 若啟動仍須全archive驗證，保留其時間；未批准的新模式不得出現在live receipt。失敗root/sidecar不允許reconcile後直接開gate。

**驗證／完成。** AC-L、AC-R、AC-C；交付每repstage值/max、bytes、同hot/tail證明及完整resume oracle。舊13msstartup等數字只作context，不能混入本輪新dataset。

### F4 — 故障矩陣與15分鐘 FakeVenue 整合

**問題與根因。** 單元crash hooks提供邊界覆蓋，但不能證明catalog、新索引與actor長路徑協調正確。縮短測試後仍需足夠輪換及fault coverage；run duration本身不是正確性證據。

**建議解法與理由。** 分兩層：短deterministic boundary tests測所有新storage邊界；900秒integration測實際writer/executor/stream/recovery互動。將fault排程、expected invariant及oracle一起凍結，fail立即停新風險。

**所需東西。** `tests/live_rotation.rs`、`tests/test_live_actor.py`、`tests/test_runtime.py`、fault-injection feature、FakeVenue external-submit oracle、F0raw recorder、F2/F3實作。

**Steps。**

1. 先為每個受影響invariant寫可在缺修正時失敗的test：exact dedup、unique root、durable-before-external、reserve preservation、pin安全及recovery一致。
2. 每新page/root/catalog/Closed/Genesis/sidecar/GC邊界做3次process crash；另測wrong root/anchor、partial/sync fail、corrupt/missing/shared page。恢复不能默默回退到會丟新records的舊root。
3. SDK永不返回或不可恢復不確定狀態用獨立deadline test驗證fail closed；integration若注入此項，須有explicit release/restart/reconcile安排，不能虛構「自動復原」。
4. 凍結7類runtime faults各3次，共21次；第一項最早steady第60秒，間隔≥30秒；900秒內預留recovery時間。先做schedule dry-run證明可行，任何fault尚未復原就停止後續注入並FAIL，不能壓縮間隔。
5. Fake run seed7開發smoke；最終clean candidate串行7/17/29，各900秒，100inputs/s與固定order mix。至少5000submitted及20成功輪換以counter和trace雙證明。
6. 每故障核對external IDs、venue/replay pos/cash/fills/reservations；hard-stop列pending與epoch，不以「沒有exception」作oracle。
7. 15分鐘結束後停止新增風險，依bounded protocol清理與最終對帳；觀察window與cleanup duration分開記錄。測試error保留first cause，避免secondary broken pipe掩蓋。

**驗證／完成。** AC-C、AC-F、AC-D及resource envelope；最終verify.sh、journal replay、已知timeout deterministic coverage/stress。三seed有任一correctness error即總FAIL，不因另外兩次成功抵消。

### F5 — 15分鐘 Testnet actor／rotation 整合

**問題與根因。** Offline request-count減少不保證live pause改善；Testnet有自然訊號、TTL、network/reconcile差異。900秒可能樣本不足，必須預先說清楚何時未量得。

**建議解法與理由。** 用已批准的最終artifact及既有隔離Testnet scope，保持策略不變，用N10提高自然輪換覆蓋；兩次短run評估整合，不將其當長期可用性證明。

**所需東西。** 明確trust approval reference、Testnet授權/credentials安全注入、既有東京主機可達REST/WS、release receipt、SMA actor runner、batch reconcile（若使用需同一frozen config）、安全resume/drill runbook。

**Steps。**

1. 檢查所有短gate、isolated account/symbol、existing orders/session ownership；只驗secret presence，不輸出值。契約未批准就標BLOCKED，繼續交付offline結果。
2. 使用已驗證release binary/image；啟動同一source/config：1秒candles、SMA3/8、N10、explicit sync/trust模式。不要順手更改poll/TTL等造成難以歸因的多變量實驗。
3. Start-up量完整resume；ready後觀察900秒。Drill建議安排在第300秒，按已驗證lost-ack/runner restart流程，保留order ownership及uncertainty，重新對帳才准開gate。
4. 每rotation量pending、quiesce、cancel/poll、reconcile、cold publish、successor ready及fresh eligible quote；區分disk時間與總交易pause。不同階段p99不能相減作成本拆解。
5. Run1、Run2串行，無並行profiler或build；每run至少20accepted/10fills/2輪換，未足900秒不能完成定版測試，900秒自然訊號不足NOT_MEASURED且不延長湊数。
6. 結束做scope內完整對帳及安全交接；非零但一致position不等於錯誤，是否flatten依已授權runbook。Unexpected network/exchange問題保留分類，不能移出分母令結果PASS。

**驗證／完成。** AC-T、AC-C、AC-R；4個完整resume值、每rotationpause及trade-enabled時間。Owner approval、sample insufficiency及外部阻礙獨立列出；未部署ECS就報EC2，不包裝成container驗收。

### F6 — 驗收分析、營運交接與下一輪界線

**問題與根因。** 先前容易混淆單組件、engine ready、完整resume，以及功能存在和數字達標；下一位agent需要可追溯的結論及可恢复現場。

**建議解法與理由。** 以raw重算的AC ledger為source of truth，逐項mapping舊FAIL、新scope及remaining work；保留所有failed runs與immutable artifacts。

**所需東西。** F0–F5run outputs、analysis CLI、same-region bucket權限、host原checkout/binary/receipt inventory、run ownership與費用明細。

**Steps。**

1. Analyzer從raw重算各AC，不直接複製runner summary；對每result列commit、profile、n、scope、threshold、hostgate及trace link。
2. 分別回答：storage writes/disk/pin是否達標；完整性是否批准；lookup/startup/resume是否達標；15分鐘correctness與resource是否達標。
3. 壓縮並串流備份本輪raw/journals/fixtures；重新下載SHA驗證，才清本輪derived副本。實際spool mount先查容量，避免再次/tmp ENOSPC。
4. 保留報告與manifest到Git；原始artifact URI/size/hash/retention留索引。恢復先前host checkout/binary/receipt，只停本輪自己的process，不刪他人檔案或更改AWSstack。
5. 出具fixed/failed/inconclusive/blocked清單，每項指定下一個實驗及所需決策。若短窗通過，後續長期drift及availability仍需獨立proposal，不能靠外推填PASS。

**驗證／完成。** 檔案hash及result mapping一致、verify evidence指向最終implementation、host restore有檢查輸出、backup可下載。交付results/runbook與machine ledger；本輪fast PASS仍保留原LC-v1未完成狀態。

## 4.2 Dependency、commit 與停止規則

- 主依賴：F0 → F1 → F2 → F3 → F4 → F5 → F6；F6的ledger/backup工作從F0開始持續更新，不等最後才整理。
- F0凍結harness後再收baseline；F1設計與approval記錄獨立commit；F2格式/publication、GC、migration可拆commit，但每個可執行checkpoint須通過適用correctness測試。F3修改harness後需升identity並重收可比baseline。
- 每個code checkpoint按 `verify-change`：記invariant/test mapping、執行verify.sh；journal/recovery再replay。文檔修改不需要以長soak替代文檔review。
- 每項性能假設最多3個候選。不能達預算時交付確切cost及替代方案；不偷偷升CPU、改N、縮H或把驗證工作移出計時。
- F5受具體approval及short gates制約；其餘相容offline工作繼續。任何stage發現會削弱既有invariant，先完成具體ADR與alternative再請owner決定。

## 5. 可量化 acceptance criteria

所有數字在候選修改前凍結；每 required run 都需通過，不能用median掩蓋單次超標。MiB=1,048,576 bytes。以下是新工程預算，不是既有 SLA。

| AC | 數字與定義 | AWS 收集／證據 |
|---|---|---|
| AC-E evidence | 100% run source、release binary、receipt、harness、threshold hashes相符；0缺失必填欄。整數重算誤差0，duration≤0.1ms，ratio≤0.01pp | build receipt、artifact index、獨立 analyzer；錯誤/缺值 fixtures 必須拒絕 |
| AC-S storage | W1 H=1k/100k/1m，連續20輪換每輪200新增terminal orders及fills；cold/index/temp穩態≤4L+16MiB、publication peak≤6L+32MiB；總logical writes≤10B+32MiB | L=17×retired orders+32×retired fills+32，B=新增retired payload；包含所有維護、catalog、index；st_blocks與app write counters |
| AC-P reader/GC | 無pin20輪換全部成功。單舊reader跨20輪換保持可讀，釋放後≤5秒重試回收；restart也≤5秒重新啟動回收。20次不同generation pin壓力不得越disk budget：可明確拒絕新pin／publication，拒絕≤1秒；誤刪活躍引用=0 | 每file generation/refcount/lock、admitted/rejected、allocated high-water、GC crash oracle；遭拒測試不冒充20次成功輪換 |
| AC-L latency | 每H五次fresh process、相同hot=0/tail=200inputs：durable rotation max≤250ms，1m max≤2×1k max；engine startup max≤100ms且同ratio≤2；首次verified lookup max≤100ms、read bytes增量≤1MiB；warm lookup1000次p99≤5ms；Fake完整resume max≤1秒 | 計時包含所有必要驗證及maintenance；完整resume從啟動runner至venue reconcile完且首次准許新風險；原始單次值 |
| AC-R resource | 全runtime進程RSS peak≤384MiB，專用cgroup含cache peak≤640MiB；host MemAvailable≥192MiB；OOM=0、swap IO=0；H1m RSSpeak≤H1k+32MiB；rotation buffer≤16MiB；hot orders≤N+8、fake fills≤N+8、live fills≤10000 | 每秒process tree；transition100ms+cgroup peak、exact count/buffer high-water；包括worker，sampler/Prometheus另列並算hostheadroom |
| AC-C correctness | duplicate external submit、position/cash/fill mismatch、durable-before-external違反、丟失uncertainty均0；每source/epoch offered=admitted+rejected、admitted=completed+pending；未知事件0 | raw trace對venue/replay oracle；hard-stop pending不得當completed；最終verify.sh exit0及VERIFY OK |
| AC-F fake soak | seeds7/17/29各900秒；100market inputs/s，每10inputs目標1terminal單，N200；每run≥5000submitted、≥20成功輪換；每種runtime fault≥3次且oracle全部正確 | 固定trace/seed；fault後恢復才下一個，間隔≥30秒；未恢復就fail並停止，不能縮短間隔湊數 |
| AC-T Testnet soak | 兩次各900秒；1秒candles、SMA3/8、N10；每run≥20accepted orders、≥10fills、≥2成功輪換、1controlled recovery；每rotation完整pause≤10秒；每run前startup及drill完整resume各≤15秒，共≥4值；最終reconcile mismatch=0、unexpected REST error/reconnect/gate=0 | 待契約批准後執行；controlled fault單獨分類。自然訊號不足NOT_MEASURED；raw trade-enabled numerator/denominator及pause union必報，15分鐘不驗收舊99% SLA |
| AC-D deadlines | disk free低於max(2GiB,20%capacity)≤1秒停止新增風險；rotation pending≤1秒停止新開倉；quiescence上限60秒，timeout後≤5秒durable stop；uncertainty不得釋放 | 隔離quota/image fault，禁止填滿root；SDK永不返回/uncertain oracle；write停止失敗也記錄，不偽造durable marker |

15分鐘memory drift只作快速警報：第12–15分鐘RSS median≤第2–5分鐘median+8MiB，各run皆需通過；取完整時間窗口、包含drill，不挑低點。這是 AC-R 的短窗條件，**不能證明1MiB/hour長期leak上限**。

Fault matrix：lost ACK/late result、SDK never returns、stream gap、overflow、writer kill、executor cancel、old epoch；另有cold write/partial write/ENOSPC/sync failure、missing/truncated/corrupt page、wrong anchor、migration中斷、pinned GC。每個適用publication邊界至少3次deterministic crash，state唯一正確或明確fail closed。新格式每個新增durability邊界納入矩陣；process kill不是hardware power loss。

## 6. 量度與 fast-fail 次序

1. 本機正確性及最小fixture → AWS 1k/1m短probe → 20輪換普通/pin測試。功能迭代可在性能目標未達時跑15分鐘fake smoke以找整合錯誤，標DIAGNOSTIC；不取代失敗的短gate。
2. 定版驗收須短gate通過，再三個fake15分鐘，最後兩個Testnet15分鐘。每個run串行；不與build/profiler/其他soak同host。
3. A/A每邊5次，warmup60秒+measure300秒；endpoint median差≤10%且ranges重疊。A/B每邊5次交錯，改善≥10%且ranges不重疊，CPU/event、queue、throughput回退≤10%。最多3候選；不穩就INCONCLUSIVE，不無限重跑。
4. W0沿用100inputs/s、每10inputs一terminal單、active cap8、window8、N200，immediate及20ms executor兩組；CPU平均≤30%單vCPU，1秒CPU p95≤80%，internal tick-to-effect p99≤5ms。完整Rust request含fsync另報。此guardrail是正式性能採用必要條件，不是每次開發smoke都需重跑。
5. Host：1秒steal p99≤5%；CloudWatch credit全部可得5分鐘樣本min≥20，邊界距run≤5分鐘、gap≤10分鐘；resource coverage≥99%、maxgap≤5秒。不合格只使performance INCONCLUSIVE，不抹掉correctness或disk FAIL。
6. n<1000不報驗收p99，改列所有值/max。首次查詢五次fresh process，不靠全機drop caches；記錄pagecache狀態及實際logical read bytes。不得用暖cache掩蓋全檔讀取。
7. 任一correctness/OOM/headroom違規立即停止新增負載；保存原始錯誤、pending、durable state，再安全清理。900秒觀察結束不代表可以kill不確定的side effect；有界shutdown到期fail closed並交接，不宣稱已flat。

## 7. Agent evidence 與 handoff

先commit `comparisons/runtime-v2/lifecycle-fast-manifest-v2.json`，固定上述workloads、fault schedule、caps、integrity mode、host、工具版本及AC。此檔為下一位agent待實作，草案本身未生成可執行harness。
每run唯一 `runs/lc-v2-fast/<run_id>/`；沿用LC-v1八類輸出，但schema升為 `lc_evidence_v2_fast`：manifest、resources、lifecycle、requests、faults、host-cloudwatch、summary、artifact-index。UTC ISO8601+monotonic durations；未知null+reason，禁止NaN/Infinity、secret、簽名。

新增欄位：cold format/trust_contract_id/approval reference、catalog generation、pin lifetime/admission、maintenance debt/writes、logical/allocated bytes、container image/task metadata（若適用）、900秒window與cleanup分界。
每AC必有 `ac_id, threshold_version, scope, metric, statistic, observed, limit, unit, n, run_ids, status, reason, evidence_paths`；status限PASS/FAIL/INCONCLUSIVE/BLOCKED/NOT_MEASURED。獨立analyzer重算所有判定，不能相信runner自報passed。

原始journal+evidence每run≤2GiB，本輪local合計≤8GiB，且diskfloor更早生效；preflight檢查實際spool mount（包括/tmp tmpfs）。用既有same-region S3串流archive、下载hash驗證後才清理本輪副本，不刪他人資料。記錄實際retention，不把30日願望當已配置。

最終新增 `docs/runtime-v2-lifecycle-fast-results.md`：每AC verdict、fixed/deferred/blocked tasks、base/candidate/harness commits、commands及verify尾段、artifact URI/hash、成本與主機restore。舊LC-v1四個FAIL逐項附新證據mapping，但不得回寫歷史結果。

本輪可以證明「指定history及15分鐘窗口內正確、受資源限制、可復原」。長時間drift、99%長期可用性、主網及其他venue仍屬後續獨立驗收。
