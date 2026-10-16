# Native runtime：生命週期、營運保護、真實策略驗收 v1.0

Created: **2026-10-16T13:22:00Z** (UTC).
Strategy selection: **使用者於 2026-10-16 確認 Cluster-PCA stat-arb，先無下單 shadow**。這不代表已批准主網或已有策略驗收結果。

Status: **PREPARATION / NOT ACCEPTED**。這是後續 implementation scope；數字是預先訂定的驗收目標，不是已取得的成果。

## 目的與目前邊界

目標是讓單一專用 Hyperliquid Testnet account 的 Rust runtime 可以安全運行、更新及恢復，再接上有版本的真實策略。Mainnet、無人值守實盤、多主動節點及跨交易所部署不在本次授權交付內。

目前 Rust 已擁有行情、writer、durability、executor、簽名及恢復；Python 只作可選策略。`8d58574` 的 expiry 修正通過本地 verify，AWS 新驗收另見 [results](rust-native-runtime-results.md)。已有一次真實 crash-after-fill 恢復證據，不代表所有 crash window 已覆蓋。

仍有重要限制：只有 `1000/Expired` 可有限度自動續接；其他網絡故障停止 session。每個 session 最多 1,000 張單；account lock 只在單一主機有效。現有基建 root EBS 是 delete-on-termination，不能當 production journal 的保護措施。現有 Core 是單 instrument，不能直接承載多幣 PCA portfolio。

原則：single writer、durable-before-send、unknown 不當 rejected/absent、原始失敗保留、會計以交易所可核實的事實對帳。以簡單函數及現有模組擴充，避免新的通用 actor framework、broadcast hub 或 service mesh。

## 次序與交付邊界

1. **L：生命週期及安全輪換**，先 deterministic faults，再 15 分鐘 AWS fast-fail。
2. **O：持久儲存、單主機替換、部署回滾與告警演練**，使用 task-owned stack；不得對現有使用者主機做破壞性演練。
3. **S：真實策略資料及 signal shadow** 可並行準備；有資金風險的策略整合須等 L/O 及策略會計 gate。
4. 15 分鐘是 iteration gate。完成後進行 24 小時 runtime qualification；4h 策略另需至少 7 天自然 shadow/paper。短測試不能代替跨日驗收。

每項交付包括 source SHA、release hash、manifest、執行命令、原始資料位置、逐項結果 JSON，以及未通過／未量度原因。不得把 pending 寫成 PASS。

## L1：統一關 gate、對帳、恢復契約

**問題與方法。** expiry、EOF、timeout、延遲回覆和 restart 都可能令 local state 落後。沿用 producer 同步關 admission、single writer 消化已在途結果、持久化 epoch 轉換、同一 execution owner 對帳；只有新訂閱、最新 quote、可核實帳戶狀態及新策略版本同時有效才恢復。不要因 TCP 連回便開 gate。未知 mutation outcome 仍須停止及人工處理，不能盲重送。

**所需與步驟。**

1. 讀 `src/native/{runtime,stream,venue,signing}.rs`、expiry addendum、architecture；列明可恢復網絡錯誤和永不自動清除的 integrity/risk failure。
2. 把重連 budget/timeouts 寫入 manifest。建議每次 outage 最多 3 次、backoff 1/2/4 秒；耗盡保持停止。不要沿用「一生最多 3 次 expiry」作 24/7 政策。
3. 為 detection、latch、epoch、inflight resolution、snapshot、subscription、fresh quote、resume 記錄同一 monotonic clock 的事件；交易所時間另欄，不能跨主機相減當 latency。
4. 測不同 fault 與 pending submit/cancel/fill 的交錯；process restart 用原 journal/action journal/session binding，不建立新 session 隱藏不確定訂單。
5. AWS 實測用固定小額 Testnet caps；不能保證已送出訂單在關 gate 後不成交。計數區分「新 transport handoff」與「舊 request 的遲到 fill」。

**L1 acceptance。**

| Gate | 數字及判定 | 證據 |
|---|---|---|
| L1.1 故障覆蓋 | expiry、EOF、read timeout、lost acknowledgement、delayed fill、late strategy、SIGKILL 後 restart 各 3 seeds × 3 次，全部通過 | fault ID、seed、原 journal、mutation spy |
| L1.2 admission | 每次 detected fault → latch closed ≤10ms；關閉後新 risk-increasing handoff **0**，blind retries **0** | monotonic stage trace；inflight actions 另列 |
| L1.3 gate 條件 | 缺任一必要條件的 cases 全部不得 reopen；stale epoch/generation target 被接納 **0** | 狀態表、negative fixtures |
| L1.4 恢復 | 健康 REST/WS fixture 的可恢復故障每次 ≤30s；非可恢復 fault 100% 保持關閉；真實 Testnet超時不得以「交易所慢」改 PASS | detection→ready trace，條件缺失原因 |
| L1.5 經濟正確 | fill 重複入帳 **0**；position 差 **0 lots**；fees/funding/cash 差 **0 venue quantum**，任何 rounding 按事前凍結規則 | 獨立 decimal oracle + venue records |

## L2：1,000 單上限與 live journal 輪換

**問題與方法。** 直接加大 cap 會把歷史量、恢復成本及記憶體問題延後。先支援 **flat + 全部 owned order terminal + 無 in-flight + 最新對帳成功** 的輪換。非 flat 時到達 soft limit 停止增加風險，保留足夠 ID/order budget 做 cancel、reconcile、bounded flatten；不得默默強制平倉改變策略行為，策略 manifest 須選擇「等待自然 flat」或「有界 operational flatten」。預設等待，超時停機告警。

**所需與步驟。**

1. 重用 `DurableEngine::rotate_into_retaining_paged` 和現有 Closed/Genesis crash points。確認既有 cold-page trust approval 的適用範圍；不得把 root/path 驗證擴大成未批准的完整性削弱。
2. 預先設定 soft threshold **800 orders**、hard ceiling **1,000**。800 後停止新風險；預留 200 是安全餘量，不是允許 200 次 blind flatten retry。無靜止點 60s 後進停止／人工處理。
3. 保留全域遞增 ID floor、nonce high-water、account/agent binding、session lineage、歷史 fees/funding/cash carry；所有這些須先 durable 才發布 current-session pointer。
4. **不得只 rotate Core journal。** Venue 的 `owned_oids`、fill lookup、基準 NAV／funding start、action attempt 去重也要以 segment watermark 協調。邊界 timestamp 相同的 fills/funding 必須去重，避免從新 start-time 漏掉舊單遲到事實。
5. successor 出現前 predecessor Closed 已 durable 的 crash，要 deterministic 完成同一 successor；已 Closed 不再寫回。冷歷史保留可查核及離線 scrub，不刪掉原始會計證據。
6. 合併 journal/session/current-pointer crash matrix；恢復後驗證唯一 active successor，再 reconcile 才 open gate。

**L2 acceptance。**

| Gate | 數字及判定 | 證據 |
|---|---|---|
| L2.1 長歷史 | 固定 seed replay **10,000 orders**、≥12 次輪換；ID/nonce 重用0、遺失/重複 fills0、最終 ledger 差0 | chain inventory、ledger oracle |
| L2.2 crash windows | cold publication、Closed fsync、Genesis fsync、dir fsync、pointer swap 前後，每點 **3 次**；全部唯一 successor 或 fail closed，不能猜測 | fault receipt、recovered chain hashes |
| L2.3 上限 | active orders≤1,000；任一輪換不得繞過 terminal/flat/owned-account 條件；非 flat timeout 100% 不新增風險 | admission/rotation traces |
| L2.4 資源 | 指定 2vCPU EC2、1000 events/s 模擬負載，10,000 orders 下 runtime process-tree RSS≤256MiB；最後5個穩態 segment 的各自 peak 最大−最小≤32MiB；recovery 本機驗證≤10s，REST另計 | 1s RSS、segment IDs、recovery stage times |
| L2.5 AWS live | 兩次各≥900s Testnet；各≥1次有實際 owned orders 的輪換。測試可預先將 soft threshold 降至4作同一路徑 drill，不宣稱測到800；hard-cap fixture另測 | venue receipts、rotation chain、原始失敗 |

## O：營運與資金保護演練

**設計選擇。** 第一版採單 active host、受控替換，不建 HA。DynamoDB lease 本身不能阻止仍持有私鑰的舊 host 下單，Hyperliquid 不會替應用程式解讀 fencing token。故 replacement 必須先確認舊 instance 已 stopped/terminated（或可證實其簽名能力已撤銷）；做不到就保持關 gate。單機 `flock` 仍保留。不要把「拿到 lease」寫成跨主機安全保證。

**O1 部署與升級 steps。** 重用 `infra/` 的 CDK→CloudFormation；release artifact 依 CPU architecture 分開 build/verify，不能把 c7i x86 binary 搬到 t4g ARM。建立 service unit 與固定持久目錄、read-only binary、專用 OS user、最小 IAM、SSM secure secret injection。upgrade 先 stop admission、drain/reconcile、確認 old PID 結束，再換 immutable artifact/symlink，verify ledger 後才啟動 strategy。寫入新 schema 後不得盲目換回舊 binary；不相容 rollback 必須拒絕或走可驗證 migration。

**O2 儲存與失機 steps。** journal、action attempts、session、cold pages、current pointer 放 **獨立 encrypted gp3、DeleteOnTermination=false、CloudFormation Retain**。同 AZ 失機演練先 fencing 舊 instance，再 attach retained volume 至 replacement，驗證 hash/chain、原 session 對帳。這可測 host-loss RPO=0，不能宣稱同時覆蓋 EBS/AZ loss。S3 版本化加明確 manifest 備份用於另一種災難；非同步備份有落後窗口，缺失 durable action evidence 時必須保持停止，不可以交易所淨倉位為0猜測可恢復。主機消失演練前確認 Testnet 最小敞口及 original journals 可取回。

**O3 告警／停止 steps。** 定義 gate-closed reason、unresolved actions、reconcile mismatch、feed age、disk free、journal upload lag、heartbeat；低頻送 CloudWatch 或既有監控。先使用測試 receiver 收到警報的證據；對人的 email/Slack 需指定接收者後才啟用。人工 stop 分兩種：stop-new-risk（保留未知在途）及 reconcile-and-flatten（有界、可能失敗）；外部 stop marker 持久化，重啟不得清除。net partition 時不能承諾遠端 kill 即時送到，依本機 feed/heartbeat admission expiry 關 gate並另報故障。

**O4 成本。** 以 Tokyo、無 inbound、public subnet、S3 gateway、無 NAT/ALB 為預設。部署前重新取價；每月估算包含 compute、IPv4、兩塊 EBS、S3、metrics/logs/alarm，目標 **≤USD20/month**。若 RAM 或實測 CPU credits 不足，要報成本與能力缺口，不以 benchmark c7i 的表現推定 t4g 足夠。臨時測試費另列；controller finally 清理 instance、volume、臨時 secrets 並記錄 IDs。演練保留 volume 於恢復成功及證據落地後才刪除。

| Gate | 數字及判定 | 證據 |
|---|---|---|
| O.1 部署／回滾 | install、upgrade、相容 rollback 各3次；每次≤120s（artifact已下載）；任何時刻有效 execution owner≤1；不相容 downgrade拒絕率100% | service PID/instance/build/format receipts |
| O.2 雙主機 | 20次競爭啟動＋3次控制面不可達，replacement 在舊 signer 未被證實停止時送單0 | transport spy/venue action ledger、fencing receipt |
| O.3 host loss | 同AZ保留EBS實測3次；original durable records遺失0；故障注入至恢復ready≤300s；final position/order差0 | EC2 state、attach history、journal chain、venue snapshot |
| O.4 備份災難 | damaged journal、missing last segment、stale S3 snapshot各3次；錯誤自動恢復0；無法證明帳本完整一律BLOCKED | restore report；不可將host-loss結果借用 |
| O.5 停止 | 每種停止指令3次；本機指令被接收後gate≤1s；正常網絡cancel/reconcile/flatten≤30s。斷網則gate仍≤1s，flatten標UNRESOLVED且告警 | stop-marker、latch、venue terminal timestamps |
| O.6 告警 | 每種事件注入3次；測試receiver每次≤120s收到、內容含account alias/run/reason（無secret）；恢復通知與原故障可關聯 | receiver receipt、dedup IDs |
| O.7 qualification | 24h自然Testnet、≥2 expiry renewals及1次受控restart，unexplained economic discrepancy0、unaccounted send0、無未回收task AWS資源 | 跨日run、資源收據；交易樣本不足另標 |

O 的 ≤20USD 是預算 gate，不是現有價格承諾；即使 correctness 通過，超預算也要明確 FAIL，不自行升級 persistent instance。

## S：真實策略驗收（已選定 Cluster-PCA shadow）

**問題。** SMA／強制交易只驗 infrastructure，不能用其 Testnet PnL 推論 alpha。原 [PCA proposal](pca-stat-arb-research-to-paper-proposal.md) 已找到聊天來源但未取得原程式／資料重現；該舊文的 Binance Futures execution scope 已不適用新的 Hyperliquid 方向。本文件僅更新 execution venue，沒有把未重現研究當完成。

**所需與步驟。**

1. 鎖定原 research source/config/data hashes、270day/4h window、cluster/PCA/AR公式、universe與fee/funding定義。若只有聊天 reconstruction，標RECONSTRUCTED；未取得source是明確BLOCKED，不讓 agent 猜選規則。
2. 建立 Hyperliquid instrument/universe mapping、上市時間、tick/lot、mark/oracle、book bid/ask、closed candles、funding settlements、private fills/account schema receipts。Binance舊資料可作研究 baseline，不能當 HL 可成交 universe或HL費用。
3. 先 public recorder及無下單 shadow；所有 timestamp 包含 event/receive/available，不使用事後才知道的 funding 作先前signal。cache只讀 required warmup window，再incremental補洞，fixed-size numeric ring；PCA refit移到bounded worker。
4. 共享純 signal functions 與 immutable model snapshot，offline replay和shadow使用相同data/版本；缺bar、latebar、stale model必須有明確skip，不 forward-fill 虛造return。
5. **多instrument account Core是獨立依賴。** PCA需要多腿、共同collateral、net/gross/factor risk及partial fills；不能以多個獨立BTC Core共用一筆USDC冒充portfolio。先做設計與獨立ledger oracle，研究gate過後再實作，保留Binance compatibility。
6. 成本後回測、未見資料holdout、7天自然paper及最後小額Testnet依序promotion。收益門檻沿原proposal，實際適用時另凍結HL成本/universe manifest；不得依輸出調threshold。工程PASS與alpha GO/NO-GO分欄。

| Gate | 數字及判定 | 證據 |
|---|---|---|
| S.1 來源 | 使用的code/config/data/修補規則100%有hash與來源；缺任一原版依賴不得稱REPRODUCED | source map、dataset manifest |
| S.2 parity/causality | ≥10,000個已收市 decision timestamps；batch/stream signal最大abs差≤1e-8、量化target完全一致；future-data perturbation改變過去target0次 | replay/shadow comparison |
| S.3 cache/資源 | 每symbol價格window≤1621 bars（270days×6 +1；更大AR context需事前說明）；process-tree≤512MiB；10倍歷史replay後cache容量不變 | window counters、1s RSS |
| S.4 signal期限 | ≥100次完整panel refits，單BLAS thread p99≤30s；panel-ready→target p99≤60s；stale/missing panel新增風險0 | model/input-watermark/stage traces |
| S.5 perp會計 | long/short、partial fills、cross-zero、fees、funding、duplicate各3 seeds×3；position差0lots、cash差0venue quantum；實際funding未發生須NOT_MEASURED | 獨立ledger，真實與fixture分列 |
| S.6 自然paper | ≥7×24h、≥42個scheduled4h slots，每個100%有target或明確skip；重播target一致率100%；任何未解釋ledger mismatch0 | slot ledger、raw records、PnL成本分解 |

不為湊15分鐘交易數縮短4h alpha週期。自然trade不足標NOT_MEASURED；強制basket tests單列execution evidence。用公共主網行情做無下單shadow不需要主網交易密鑰；本階段不啟用mainnet signer。

## AI agent 可讀 evidence contract

每個 gate 一筆 JSON：`gate_id, status, measured_at, source_commit, binary_sha256, manifest_sha256, run_ids, sample_count, observed, limit, unit, raw_uri, raw_sha256, reason`。狀態限 `PASS/FAIL/BLOCKED/NOT_MEASURED/INCONCLUSIVE`。

同時保留 session/epoch/source_generation、order/cloid、action nonce、pre-send durable sequence、funding ID；private account用alias，不存secret。至少每秒process-tree RSS/CPU、disk bytes/free，並保留exit code及controller cleanup receipt。stage timing用monotonic；跨process/host時只有已證明共同clock才可相減。不要加不同筆request的p99作一條延遲。

驗收程式須拒絕缺欄、缺raw、錯hash、混build、零sample與不足run；不能用平均值覆蓋安全gate的max violation。所有新code走`verify.sh`；durability/recovery須replay；性能沿用凍結A/A及≥5/side交錯A/B，禁止事後更改分母。

下一個 agent 先讀本文件、results及未完成gate，從L1 fault matrix開始。現在沒有批准用這份proposal的目標數字替換既有migration thresholds；兩套驗收須分開。

## 本次 preparation 取得的證據（不等於 S gate 通過）

2026-10-16 已讀取原研究 thread 最新兩 turns，核對 selected 270days/1.50 及來源路徑。另實際 fetch `rickypang0219/quant-research-agent-skill`：main `924fadb`、codex_refine `c812483`，兩者均未包含 `research/crypto_stat_arb` 或原資料／報告。原 source/data 仍待提供可讀位置，不能稱為重現。

同日以**無密鑰、無下單**方式讀 Hyperliquid public-mainnet `/info`：`metaAndAssetCtxs`、`l2Book`、bounded `candleSnapshot`、bounded `fundingHistory` 四項皆 HTTP200。metadata 234 列含56列 explicit delisted，不能直接把全部列當可交易 universe；BTC 4h 回覆4 bars，其中3 closed、1未收市，需按 `T` 與 availability 過濾。funding回覆是公開rate history，並非私人account實際funding cashflow。沒有啟動7天shadow、沒有生成PCA signal、沒有驗證270天warmup完整性。

小型機讀收據：[pca-shadow-preparation-evidence.json](pca-shadow-preparation-evidence.json)，含request windows、schema、raw hashes及source blockers。這些是connection/schema observations，不可填成 S.2/S.5/S.6 PASS。
