# Cluster-PCA stat-arb：研究還原、事件回測與 paper／Testnet proposal v1.0

Created: **2026-10-13T06:13:54+08:00 (Asia/Hong_Kong)**

Status: **DRAFT — 已找回討論，尚未重跑研究或部署**。本次交付是 proposal。以下新門檻是下一階段建議，實作 agent 須在看新結果前凍結 manifest；不能把它們當作舊研究已通過的驗收。

## 1. 目的與研究來源

目標：還原既有 cluster-local PCA／AR(1) residual mean-reversion，補齊資料偏差和 perpetual 成本，使用 MiniNautilus 的事件處理／風控／恢復契約重新回測，再按證據進入 live-market paper，最後才是獨立 USD-M Futures Testnet。研究失敗並得出 NO-GO 也是完整交付，不以盈利作程式完成條件。

原討論：[Build PCA stat-arb module](codex://threads/019da666-2f5a-78c3-9b88-0284f58ac2b5)，project `quant-research-agent-skill`，原路徑 `/Users/kachunpang/quant-research-agent-skill`。本次讀取全部三頁、24 turns 的 user／assistant 記錄，另看到部分原命令輸出。這是討論證據，不等於本次核實了原始 Parquet、程式版本及結果 hashes。該本機 project 目前不在 cloud filesystem；不可虛構已 clone／讀到原檔。

可見討論主要為 2026-04-19～20；未證實它就是使用者提及更早於 2026 年初的全部研究。尤其 funding 作 alpha factor 的另一段原始討論尚未定位：本 thread 最新結果明確說 **沒有 funding cost model**。不得把「曾討論 funding」寫成「已完成 funding 因子」。

### 已還原的研究演進與來源定位

| Thread item | 已知內容 | 本次使用方式 |
|---|---|---|
| item-1、12、31 | 1h USD-M perp；上市≤2023-01-01；最初50幣；6bps；global PCA baseline return -78.66%、Sharpe -1.61 | 保留為 rejected baseline，不重新當首選 |
| item-56 | half-life 單位修為 bars；要求小於 PCA window/2；invalid half-life 優先退出 | 核對最新程式是否仍保留 |
| item-78 | dollar/factor neutrality；最終 projection 必須在 turnover／exit 後仍滿足 | 加入實際整數下單及 partial-fill 檢查 |
| item-135 | 110 symbols、3,181,310 hourly rows；2023-01-01至2026-04-20；current-TRADING universe；KNCUSDT壞 candle repair | 還原資料及 repair provenance；修 survivorship bias |
| item-169 | pending exits 時不再開新名字，修 max_positions 漏洞 | 不把較早 handoff 的「未修」當最新狀態；重新測最終 cap |
| item-182、188 | rolling correlation clusters → local PCA；4h、10 clusters、turnover cap .05；全期報告+91.66%、Sharpe1.465 | 已被看過／用來選模型，不是新 OOS |
| item-192 | 2025-09-01切開既有 equity curve | 事後切圖，不能稱 untouched forward |
| item-193、204 | 80/20 train/forward；只選 PCA window、entry threshold，共12 configs | 作 reproduction 主版本 |

最新 reported config：4h bars、10 clusters、min cluster assets6、每 cluster 1 PCA factor、AR window60days、exit score .35、max_positions20、optional turnover cap .05、commission6bps。Train grid為 PCA days={90,180,270} × entry={1.25,1.5,1.75,2.0}，選中270days／1.50。

| 原討論 reported results | Train | Forward |
|---|---:|---:|
| 分界 | <2025-08-22 00:00 UTC | ≥2025-08-22，至2026-04-20 |
| Total return | +22.97% | +27.79% |
| Sharpe | .85 | 2.15 |
| Max drawdown | -13.26% | -5.33% |
| Annual turnover | 20.48x | 20.05x |
| Average gross | .559 | .601 |

以上是舊 assistant／命令的報告值，**未在本次重現**。原結論仍有 survivorship、spread/slippage/funding 缺漏，且架構／cluster_count 已在更早全期實驗中選過；兩參數 search 不會消除這些先前選擇偏差。

## 2. Scope、expectation 與明確不包括

本階段先回答兩件事：修正後 residual signal 是否仍有可觀察、成本後的 OOS edge；同一套 signal／portfolio 語義能否在事件 replay 與 live paper 正確運作。

包含：原研究重現、point-in-time universe、funding cashflow、bounded signal cache、portfolio/perp accounting、事件回測、paper feed、風控及故障驗收、可選 Futures Testnet readiness。

不包括：真實資金下單、Hyperliquid/Kraken adapter、cross-venue arbitrage、autoencoder/Koopman等新模型搜尋、毫秒級 alpha、既有 cold-index 性能優化。不得用Spot Testnet驗證perpetual funding或short semantics。

先交 R0–R3 的研究 gate，再做 R4–R6 大幅 Core 擴展；資料 recorder／無下單 signal shadow 可較早做。若研究 NO-GO，保留報告並停止為此策略擴大 execution 投入。

## 3. 現有系統與缺口

已讀：`docs/architecture.md`、`docs/runtime-v2-design.md`、`python/mininautilus/venue.py`、`src/backtest.rs`。Registry.require_v0 明確拒絕 multi-instrument／margin。現有 backtest 是單一 integer-tick/lot instrument 的 Core+PaperExchange，沒有 portfolio collateral／perp funding／liquidation model。現有 quote=bar price 的成交假設也不等於真實 bid/ask。

可沿用：single writer、typed events、durable-before-external、idempotency/uncertainty、actor I/O隔離、重播、metrics和證據工具。不能把110個獨立Spot engine共用同一筆USDT，就稱為portfolio backtest。

舊 `trading-system-reference/src/post-trade/.../pnl/delta_neutral_utils.py` 有 USD-M candle、funding history collectors；只作API/ledger參考。其 full-history list 模式不可放入 live strategy。現有生命周期v2.1尚未總驗收，相關失敗不能因新策略而抹掉；新工作須記錄使用的 runtime SHA及適用故障證據。

## 4. Signal、data 與 runtime 如何協作

```text
Public historical partitions / live public streams
  → recorder + point-in-time normalizer + closed-bar synchronizer
  → bounded numeric ring + immutable model snapshot
  → cluster PCA → residual path → AR(1)/OU s-score
  → constrained portfolio target (model_id, input_watermark, expiry)
  → one account-domain writer: reservations + collateral checks + durable journal
  → executor (event simulator / paper / Futures Testnet)
  → fills + funding + balances → same writer → reconciliation
```

昂貴 clustering/PCA 放 bounded worker，不能阻塞市場輸入或 account writer。writer 接受帶版本和watermark的結果；過期結果丟棄並記錄，不沿用到錯誤bar。舊模型在其有效期內可用；逾期停止新增風險。

### 所需市場資料

| 資料 | 生成 signal／執行的用途 | 歷史來源／live方式 |
|---|---|---|
| Universe全體已收市1h OHLCV、quote volume | UTC對齊4h bar、returns、liquidity、clusters、PCA | cached partitions；live kline WebSocket；REST bounded補洞 |
| 上市／下市／symbol變更、tick、lot、min notional、contract multiplier | 時點正確的membership及有效訂單 | 歷史instrument master；live metadata refresh／變更事件 |
| 全部持倉／候選的mark/index price | MTM、margin、basis與funding估算 | historical mark/index；live mark stream |
| Funding公布／settlement時間、rate、下一次時間 | 實際持倉現金流；可選獨立因子 | historical settlements；live announcements/stream及REST校核 |
| Bid/ask及數量 | spread、可成交價格、paper fill | live bookTicker；需要depth時才加boundeddepth，不把mark當成交價 |
| Private order/fill/account/margin events | Testnet訂單狀態、對帳、collateral | Futures private stream＋REST snapshot/backfill；paper用獨立ledger |

實作時核對當時官方 USD-M Futures 文件並存 URL／日期／schema；本proposal沒有驗證最新 endpoint hosts。Spot 的 `BINANCE_TESTNET_*` credentials 不推定可用於 Futures。公共主網行情不使用主網交易金鑰；遇地區政策不繞過。

每筆資料保存 exchange_event_at、received_at、available_at、instrument_id、source、sequence（若venue提供）、payload hash。Historical只有event time時，明確採保守availability假設，不能捏造當年的receive time。

Signal universe與execution basket不同：最多20持倉，不代表只取20幣的PCA輸入。先凍結universe上限及membership規則。

### 有界cache與clock契約

還原主版本270days×6根4h/day =1620 return rows，需要至少1621 prices；另保留最多4根未聚合1h bars／symbol及明確的AR／refit工作區。若核對程式發現AR需要超過PCA窗的殘差context，必須在manifest修正公式，不能默默不足warmup。

採 contiguous NumPy ring或等效fixed-size storage；append/evict O(1)。PCA refit本身不是O(1)，須另計時。冷啟動从本地/S3讀bounded warmup；缺多少補多少，分頁fetch，不把2023至今全歷史灌進strategy。離線reader可stream全期。

以UTC 00/04/08/12/16/20收市bar決策；watermark只接受complete、available的bar。缺一個required symbol不forward-fill製造return；記錄skip並停止依賴該panel的新風險。duplicate冪等；late correction另存版本，不追改已執行的signal。

### 模型契約

R0先逐行還原原版：standardization ddof、correlation distance/linkage、PCA refit cadence、eigenvector convention、residual units、AR fit slice、signal/position lag、vol target、stop/half-life defaults都列清楚，不能從聊天補猜成確定值。

新因果版以≤t已知資料fit；固定當次loadings在整個AR fitting window重算residual，避免把不同PCA bases的殘差直接拼接成OU path。AR(1) X_next=a+bX+noise；0<b<1才用 OU mapping，m=a/(1-b)，sigma_eq=sigma_noise/sqrt(1-b²)，s=(X-m)/sigma_eq，half_life_bars=-ln2/lnb。零variance、NaN、退化rank、invalid b全部明確non-tradable。核對原版是否同定義，差異單獨版本化。

Funding先作**必需成本**：linear perp cashflow由settlement時持倉、venue指定mark/notional、rate、rounding計算，positive rate一般long付short收，但按venue規格及fixture驗證。不能把結算後才知道的final rate回填作結算前signal。Funding alpha暫列獨立待還原研究，不能在沒有原公式時偷偷加入新的search維度。

## 5. 詳細任務、原因、依賴與steps

### R0 — 鎖定研究版本及重現原報告

原因：thread中多個策略曾被替代，甚至「cap未修」的handoff早於後來fix；直接重寫容易重做被否定的模型。

需要：原project code／dirty diff、uv.lock、tests、110-symbol資料manifest、train_forward_report、config、weights、equity和data repair記錄。讀本地AGENTS及handoff後記sourceSHA+dirtydiffhash，不把gitignoreddata誤認已在Git。

Steps：取得可讀source snapshot → 保存12-config search與selected270/1.5設定 → 在原env重跑 → 對照逐期weights/equity及reported metrics → 所有差異分類data/code/numerics。只拿到聊天則標RECONSTRUCTED，禁止稱REPRODUCED。

交付：`docs/pca-research-source-map.md`、`comparisons/stat-arb/reproduction-manifest.json`、原code/data hashes、重現差異表。原 funding factor discussion缺失列為UNRESOLVED，但成本修正可獨立繼續。

### R1 — 時點正確資料與perp ledger

原因：current-TRADING universe刪掉失敗／下市幣；commission-only可能高估edge。

Steps：保存原110幣作reproduction → 建歷史可交易master及delist policy → 分區cache hourly/mark/funding → append-only repair ledger → 明確時間與units → funding/fee/MTM獨立oracle。不要無條件inner join刪掉所有缺值timestamp，也不要把真正zero-volume bar補成trade。

KNCUSDT 2023-11-10 15:00 UTC原repair需保留raw；對照隔離該bar與widen OHLC兩種資料政策，不把repair當原交易所真值。沒有歷史delisted資料時restricted-universe可作探索，promotion gate標BLOCKED。

交付：partition hash、schema、quality report、universe timeline、cashflow fixtures；完成精確成交／結算事件順序及integer monetary quantization ADR。

### R2 — 共享streaming signal及可行portfolio

原因：研究signal與live分叉，以及最後projection破壞cap/turnover，是既有研究已遇到的問題。

Steps：抽出純signal module → bounded rings/model snapshots → batch/stream parity → constrained target → tick/lot rounding後重新risk check。

目標約束：1^T w=0、Beta^T w=0、gross≤1、最終非零names≤20；optional one-way traded-notional turnover定義為sum(abs(target_weight-drifted_pretrade_weight))≤.05，不可混用half-turnover。費用依实际traded notional收，sell到buy穿零兩邊都計。原版不同定義須先重現再另報修正版。

不要用「projection→scale→truncate」反覆破壞其他約束。先選有足夠rank的active set，再做聯合constraints；不可行時不新增風險並輸出reason。强制risk exits可超optional turnover，但單獨統計；target不可行不能憑空把既有positions歸零。pending exits／unknown fills仍佔資金和20-name worst-case union。

### R3 — 成本修正版與真正後續驗證

原因：舊80/20已被查看；重新切同一區間不會還原untouched OOS。

Steps：先重現commission-only → 同weights加真funding／cost attribution → 再以event fills重算策略 → walk-forward12month train/3month test、step3month；270day warmup在train之前另供context，不計入train績效；每fold只原12configs。跨foldcapital/positions與costs連續，不能reset消除虧損；切model的處置明確記錄。

2026-04-20之後資料只在確認未被其他研究使用時稱new holdout；否則標已見，使用未來paper期。freeze dataset end（完整UTC bars）、split、fee/slippage、parameter selection objective及tie-break後才讀holdout。不能跑到滿意才換end date。

報告：gross/net equity、commission/spread/slippage/funding分拆、daily returns Sharpe及autocorrelation-aware uncertainty、drawdown、turnover、active utilization、percluster/long-short PnL、s-score horizons1/2/3/5/10bars。IR必須命名benchmark；zero-cash時與Sharpe同值要明說。僅兩個optimized參數也要列過往模型/cluster選擇的全部research trials。

成本scenario固定：實際可核實fee tier，reproduction6bps/side；stress12及18bps/side；quoted halfspread另加，unobserved slippage採2/5/10bps每side的假設矩陣，標scenario不標實測。容量採每單≤前一完整1h quote volume的0.1%，Testnet minimum造成違例則skip，不放寬risk。

### R4 — MiniNautilus portfolio/perp最小擴展及事件backtest

依賴R3研究gate；先寫ADR再改core/model/journal。一個account risk domain一個writer，所有instrument共享collateral reservation；保留spot相容測試，不移除require_v0而不補替代保障。

新增typed instrument_id於quotes/targets/orders/fills，per-instrument ticks/lots/position，account settlement asset、fees、funding、mark MTM、initial/maintenance margin及reduce-only。第一版只linear USDT perps／one-way position mode／明確margin模式；不聲稱支援inverse或portfolio-margin。

多腿不是atomic exchange transaction：durable記basket plan+childIDs+version，先預留所有可能的worst-case orders；partial fill/reject/timeout記leg imbalance。取消未確認保留reservation，unknown submit不能盲重送。risk-reducing unwind也是durable有上限的動作，不假裝rollback交易所成交。

Simulator與live共用account transitions／portfolio checks；獨立oracle驗cash。Replay以decision_available_at之後下一個可執行quote成交；只有OHLC時採下一bar open+保守cost，不能同收市signal用同價無延遲成交，也不能宣稱orderbook-realistic。funding跨fill順序、fee asset、forced liquidation/adverse gap需fixtures，無資料標scenario。

### R5 — Public live market shadow與paper

Recorder/shadow可早做；完整paper等R4。先15min transport/fault fast iteration，再7天自然signal paper。4h策略15min可能零signal，不能壓縮時間／強迫下單來冒充alpha validation；7天也只測integration，並不足證明alpha。

Production public行情＋paper fills可評估signal及cost assumption；paper wallet與market data環境明確分離。每個decision重播同recording，檢查modelhash、signal、target和risk outcomes。分開market delivery delay、signal CPU、writer latency、exchange latency，不以5ms一刀切要求每4h一次PCA。

### R6 — 可選Futures Testnet及AWS交接

依賴R4/R5 correctness和Testnet instruments/credentials/network preflight。Futures Testnet自己的行情＋成交只驗execution/recovery；若用production signal配Testnet執行，明標cross-environment plumbing test，PnL不作策略證據。

兩次15min fast-fail sessions可重播固定basket測多腿故障，但清楚標synthetic test intents，不能算自然策略訊號。另行自然時間paper收集signal。start/end都做orders/positions/collateral/funding reconcile，不以spot wallet替代。

AWS先沿用現有Tokyo環境；無本輪新deploy授權。未來部署以CloudFormation管理，public subnet+固定IP（若必要）、無NAT/ALB、S3 gateway endpoint、small compute；核算含EBS/IPv4/logging月費≤USD20，不能假定ECS1–2CPU符合。7day運行之前記成本、artifact retention、終止條件及owner，憑證走secret storage。

## 6. 可量化acceptance criteria（執行前凍結）

| ID | 數字與判定 | Evidence |
|---|---|---|
| AC0 source | 必需source/config/data/repair hashes覆蓋100%；原report逐barweights abs diff≤1e-8、return abs diff≤1e-8；缺原檔則NOT_REPRODUCED | reproduction_diff.json |
| AC1 data | duplicate/conflicting rows=0；invalid accepted OHLC=0；每個使用bar/funding有availability與membership依據100%；缺洞與排除原因覆蓋100%，不把缺資料標0 | data_quality.json、partition manifest |
| AC2 causal parity | ≥10,000 panel decisions（可含明標synthetic），batch/stream s-score abs diff≤1e-8；integertargets／gate outcomes100%相同；future-data mutation後所有過去decisions不變 | parity.jsonl、leakage tests |
| AC3 targets | 所有final floating targets abs(net)≤1e-8、max abs(Beta^T w)≤1e-8、gross≤1+1e-8、names≤20；optional turnover≤.05+1e-8；risk exit exceptions100%理由化 | targets/constraints.jsonl |
| AC4 executable risk | 整數化後gross≤1、per-name≤10%NAV、abs(net)≤1%NAV；factor exposures以manifest凍結的unit-loadings計max≤.02NAV；超標拒絕新風險100%；worst-case names≤20含pending | risk_decisions.jsonl |
| AC5 accounting | ≥100 deterministic fixtures及10 seeded×10,000 mixed events；cash/position/funding/fees與獨立oracle在每event最小settlement quantum內一致；duplicate charge/submit=0 | ledger_diff、journal replay |
| AC6 research delivery | 原12configs全部保留；≥4完整non-overlap 3month WF folds，否则INSUFFICIENT；成本矩陣與fold/year/cluster attribution覆蓋100%；不要求盈利才算交付 | research_results.json |
| AC7 alpha promotion | 建議：base全成本OOS net return>0、daily365 Sharpe≥1、MDD≤15%、≥100 closed round trips；12bps+5bps slippage+spread/funding stress net return>0；任何資料偏差BLOCKED不得PASS | promotion.json；這些是研究篩選非盈利保證 |
| AC8 bounded runtime | 原110symbol profile及PIT universe實際最大值均測；cache每symbol≤manifest rows；兩天replay後再加十倍歷史cache容量不變；runtime process-tree RSS≤512MiB；15min後10min RSS線性斜率≤1MiB/min；零OOM | cache_counts、1sresources；不宣稱長期leak通過 |
| AC9 signal timing | EC2獨佔測試負載、固定BLAS threads=1；≥100 scheduled refits p99≤30s；完整panel ready→target p99≤60s；30s內缺required close bar不送該期新風險 | stage traces；首次暖機另報 |
| AC10 safety | 每種：feed gap、duplicate/latebar、stale model、one-leg rejection、lost ack、writer crash、funding duplicate，3seeds×各3次；0未journaled external send、0blind resend、0reservation超配 | fault ledger；有oracle的recorded/fake環境 |
| AC11 paper | 15min smoke正常結束；之後7×24h自然paper，≥42 scheduled4h decisions全部有outcome、≥99%complete或明確skip；0 unexplainedreconcile mismatch；recorded replay target parity100% | paper/replay report；少量trade不強求補足 |
| AC12 Testnet | 2×900s；每次≥10 deterministic basket attempts、含≥1partial/reject scenario及1recovery drill；unknowns0或durable fail-closed明確FAIL；finalreconcile差0；不足樣本NOT_MEASURED | 獨立integration報告，不使用testnetPnL驗alpha |

持倉partial fills可暫時不neutral：建議正常執行30s限時、失衡net≤5%NAV；30s未完成立即停止新增風險並啟動有界cancel/reconcile。異常不能保證30s平倉，記time-over-limit及未解決risk，不以return-zero target隱藏。Testnet前凍結更保守實際notional、maxloss與unwind政策。

所有acceptance輸出含 status PASS/FAIL/BLOCKED/NOT_MEASURED/INCONCLUSIVE、observed、limit、unit、sample_count、reason、source_commit、config_hash、data_hash、run_id、EC2 type/region、raw_uri。Research failure與engineering failure分欄；舊thread metrics不得填入新run。

## 7. Deliverables、依賴與下一agent起手式

建議新增位置（尚未建立程式）：`python/mininautilus/strategies/cluster_pca.py`共享signal；research repo保留search/validation；`src/portfolio*.rs`與perp events按ADR決定，不先大幅拆檔；`ops/stat_arb_*.py`負責replay/paper/evidence；`comparisons/stat-arb/`放小manifest與結果；大Parquet/raw在ignored runs/S3。

執行次序：**R0 → R1 → R2 → R3研究gate → R4 → R5 → R6**。可獨立提前做public recorder；不得先deploy然後才追補margin/reconciliation。

下一agent先讀本文件、MiniNautilus CLAUDE/architecture、原研究AGENTS/handoff；記兩repo SHA與dirtydiff；取得原data/report檔；列出已知config與未決項。先重现270days/1.5版，不另開global PCA或nonlinear search。完成每個task要附可執行命令、原始證據、失敗項、下一gate。所有程式改動依repo verification規則；本次僅文件，沒有聲稱跑過新backtest或部署。
