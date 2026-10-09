# MiniNautilus 跨平台正確性與性能審查

後續更新（2026-10-08）：本文數據保留為原審查基線。Core／Journal 的 full-clone
瓶頸已完成第一輪改善，新結果及剩餘限制見 [prepare／commit 改寫](core-state-transitions.md)。

日期：2026-10-07。這次實際執行 MiniNautilus、Backtrader、NautilusTrader、vn.py 原生引擎，逐筆比對成交、持倉、現金流及估值，並檢查原生帳戶／portfolio 報表。**已修正 Mini 的三類正確性問題；另重現 Nautilus 指定版本的 PnL 錯誤及 L1 成交量刷新問題。** 撮合模型差異與性能瓶頸另列，避免把不同預設當成 bug。

## 已修正的 MiniNautilus 問題

| 優先級／問題 | 最小重現與影響 | 成因 | 已落地的修正 |
|---|---|---|---|
| P1：拒單後的矛盾回報未觸發對帳 | 訂單先 `Rejected`，其後收到 `Accepted` 或 `Canceled`。原本前者被忽略並維持 Healthy；後者可把 Rejected 改成 Canceled，讓本地看似正常但與外部狀態不一致。 | `report` 把 terminal state 當作可以直接忽略的 late ack，Canceled 分支也欠缺 Rejected 衝突檢查。 | 兩類回報均視為衝突，保留 Rejected／現金／持倉，產生 Alert、QueryState，進入 Reconciling 並拒絕新單。有效的 fill-before-ack 流程仍保留。見 [Core](../src/core.rs) 及 [回歸測試](../tests/reliability.rs)。 |
| P2：replay 空單估值偏高 | 賣出 2 lots @100，最後 bid=94、ask=97；原報告 PnL=12，按平倉側應為 **6**。 | `replay.py` 一律使用 bid；空單回補需使用 ask。 | 多單用 bid，空單用 ask。新增實際 paper/replay 測試。見 [replay](../examples/replay.py)。 |
| P2：沒有行情時產生虛構估值／崩潰 | 已持倉後收到 MarketUnavailable，analyze 原本以平均成本當 mark，顯示 unrealized=0；replay 直接讀不存在的 quote 會失敗。空白 replay 也受影響。 | 把「未知」當成「零變動」，且假設任何狀態都有 quote。 | 有未平倉部位且沒有 quote 時，未實現及總 PnL 回傳 JSON `null`；已實現 PnL 仍可計算。空倉時總 PnL 等於已知 cash。見 [analyze](../examples/analyze.py) 及 [五項 Python 回歸案例](../tests/test_comparison_regressions.py)。 |

額外修正 analyze 的 SQLite connection 生命周期，使用明確關閉；原本 connection context manager 只負責 transaction。上述估值輸出是 API 行為變更：消費者需接受 `null`，不可再把沒有 mark 當成零損益。已同步 [架構文件](architecture.md)。

## 外部平台：已重現的錯誤與修正方向

### NautilusTrader 1.230.0：相同 PnL 的不同持倉週期被誤認為同一週期

使用原生 NETTING 帳戶、零費用，先買 2 @100，賣 5 @103 反手，再買 3 @101 平倉。兩個週期各賺 6，因此成交帳本和原生 account 均為 **12**，但 `portfolio.total_pnl` 回傳 **6**。顯式傳入 account／最終價格重新計算仍是 6，並非單純顯示快取過期。

| 兩個已平倉週期 | 帳戶／成交帳本 | 原生 portfolio PnL | 以週期身份彙總 |
|---|---:|---:|---:|
| 6 + 6（反手） | 12 | 6 | 12 |
| 2 + 2（分開 round trips） | 4 | 2 | 4 |
| 6 + 9 | 15 | 15 | 15 |
| -6 + -6 | -12 | -6 | -12 |
| 0 + 0 | 0 | 0 | 0；仍辨認到兩個週期 |

成因定位於 `portfolio.pyx` 的 `update_position` 與 `_calculate_snapshot_contribution`：以 realized PnL 金額是否相等判斷 snapshot／current position 是否屬於同一週期，兩個不同週期恰巧賺一樣多就會漏算。金額不具唯一性。對照 [1.230.0 官方原始碼](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/nautilus_trader/portfolio/portfolio.pyx)。

修正方向：以 account、instrument、opening execution/order、開倉時間或明確 cycle generation 識別週期，按同一週期的最新狀態去重；快取失效判斷及 snapshot contribution 必須一併修改。不能只在結果加回 6，也不能只比較 position ID，因為 NETTING 會重用 ID，archive 又可能附加後綴。

[重現程式](../comparisons/reproduce_nautilus_pnl.py) 不依賴 Mini 二進位檔；[週期身份診斷](../comparisons/nautilus_cycles.py) 已在五個案例中回復正確彙總，並確認把每個 snapshot 重複兩次也不會 double count。[完整證據](../comparisons/results/nautilus-pnl.json) 保留原生成交及診斷。**這是已驗證的修正思路，並未修改／編譯外部 Cython 套件，也未聲稱其他版本有同一問題。**

### NautilusTrader 1.230.0：L1 tick 成交量消耗的刷新問題

預設 `liquidity_consumption=False` 時，兩張 buy 2 的訂單可在一筆 sell trade volume=2 上合共成交 4。開啟 `liquidity_consumption=True` 能令這個案例只成交 2，但另有問題：**新 trade 的價格及數量都與上一筆相同時，可成交量未刷新**。所有 tick 都有不同 TradeId 和時間戳，並非輸入重複。

在 buy 6 @100 已掛單後，輸入連續 sell ticks，價格均為 100：

| 各筆 tick 數量 | 累計提供數量 | 啟用消耗後實際成交 | 解讀 |
|---|---:|---:|---|
| 1, 2, 2 | 5 | 3 | 最後一筆同量 tick 未補充 |
| 1, 2, 3 | 6 | 6 | 每次 size 改變才正常刷新 |
| 2, 2, 2 | 6 | 2 | 只成交第一筆 |
| 2, 1, 2 | 5 | 5 | size 每次改變可刷新 |

原始碼路徑：`process_trade_tick` 重置 trade consumption，但 L1 不進入非 L1 的 seed 分支；`_apply_liquidity_consumption` 按價格記錄 `(original_size, consumed)`，僅當 level size 改變才重置。這與實測一致，形成**以逐筆成交量作補充來源時的 underfill**。見 [1.230.0 官方 matching engine](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.230.0/nautilus_trader/backtest/engine.pyx)。

修正方向：區分「重複的 quote／book snapshot」和「新成交事件」。對新的 L1 TradeId 建立本次共享成交量預算，同 tick 內多張訂單共同消耗；新 tick 即使同價同量也可刷新。保留 duplicate-trade 去重，以及 L2/L3 book depth 本來的消耗規則。尚未 patch 外部引擎；現階段不應把開啟這個參數當成全面解決。可用 [remedies.py](../comparisons/remedies.py) 和 [保存結果](../comparisons/results/model-remedies.json) 重現以上控制組。

## 撮合模型差異：會改變回測結果，但不等同程式錯誤

所有案例都有固定輸入與逐筆結果，見 [fixtures](../comparisons/cases.py)、[結果](../comparisons/results/fixture-results.json) 和 [版本語義斷言](../comparisons/check_semantics.py)。下表只適用本次指定配置。

| 情況 | 實測差異 | 為何不同／如何處理 |
|---|---|---|
| 跳價穿過限價 | buy limit100 在97成交，sell limit105在110成交：Mini／Backtrader／vn.py PnL=26；Nautilus bar及tick PnL=10。 | 前三者給予價格改善，Nautilus 這個 maker 模型按限價成交。先確定策略的成交價格契約；不能只調 PnL 到相同。 |
| 分批成交 | buy5，後續 volume1、2、2：Mini、BT FixedSize、NT tick 預設為1+2+2；BT預設、vn.py 第一筆就成交5；NT bar只成交1。 | bar matcher／filler 並不自然等於逐筆 trade liquidity；NT bar 的不變 OHLC 點並非三筆獨立成交。需要逐筆模型時用 tick 控制組，另核對共享消耗。 |
| 零成交量 | Mini／BT filler／NT tick 等到下一筆有量才成交；BT預設、vn.py 在零量bar成交2；NT bar的合成數量可成交1。 | bar 價格觸發與流動性假設不同。加入 volume=0 拒絕成交規則，並驗證資料的零值是否代表真零量或缺資料。 |
| 同一筆行情觸發下單 | 看到100後才下 buy@100，下一筆103：Mini／BT／vn.py未成交；NT在同一事件可成交2，最後估值盈利6。 | 事件處理、立即市場化訂單和 latency 配置不同。使用共同的下單生效時點／延遲契約；真實成交可用性不能由結果相等證明。 |
| aggressor 方向 | 只有 buy-aggressor trades 向下到買單價：Mini／NT tick不成交；bar平台可成交。 | OHLC 未包含 aggressor side。這是輸入資訊損失，不是單靠改參數便能還原。 |
| 共享成交量 | 兩張buy2，同筆量2：Mini合共2；BT預設／FixedSize／vn.py／NT tick預設可4。 | 每張訂單各自取得容量或忽略volume。BT 自訂 shared-per-bar filler 已驗證只成交2；NT消耗控制仍有上述刷新問題。 |
| ID／時間優先 | 先送id20再送id10，同價競爭量2：Mini成交id10；按送單順序消耗的控制組成交id20。 | Mini用 BTreeMap ID順序，**不是 FIFO**。這是目前明文契約；如要模擬交易所，應保存 acceptance sequence 而非用ID當時間。 |
| 價格優先 | 先buy@99再buy@100，sell trade@99量2：Mini先填較低價的舊單；NT先填buy@100。 | Mini沒有 price-time queue。修正模型需按 side／price／acceptance sequence 排序，另設舊ID順序模式及回歸fixture。 |

Backtrader 的 volume filler 是可替換 extension；本次已提供共享預算的修正示範，但仍不代表真實排隊、雙邊 aggressor 或市場衝擊。其擴充介面見 [官方 filler 文件](https://www.backtrader.com/docu/filler/)。Mini 的 ID優先／trade-price 成交仍原樣保留；直接改掉會改變既有 replay 契約，應作明確可選模型。

50 組隨機 passive-order 測試中，所有平台的成交時點、ID、方向、數量均一致；Nautilus 的價格模型仍會帶來現金和估值差異。完整 65 個案例中，所有 fills/checkpoints/position/cash 同時與 Mini 一致的個數為：Mini 65、BT 58、BT FixedSize 61、vn.py 58、NT bar 7、NT tick 10。**這些是模型相同的次數，不是平台品質排名或 bug 數量。**

Nautilus margin 帳戶按 USD cents 儲存損益；隨機案例相對精確成交帳本的差額介乎 -0.03 至 +0.04 USD。比對以每筆平倉損益最多半分、最後未實現估值最多半分建立上界，另保留有理數平均成本 rounding 參考。浮點 tie 可造成不同的最後一分；這種量化差異與上面漏算整個交易週期分開處理。

## 性能與已定位的瓶頸

環境：macOS 15.8.1 arm64、Python 3.12.0、Rust 1.96.0 release。Backtrader 1.9.78.123、NautilusTrader 1.230.0、vnpy 4.5.0、vnpy_ctastrategy 1.4.1；[依賴鎖定](../comparisons/requirements.txt) 及 [metadata／source hashes](../comparisons/results/metadata.json)。這是本機樣本，非隔離硬件上的通用排行榜。

每組先 warm up 一次，再取三次中位數。相同四點價格循環，每四筆兩次成交，每次1 lot；10,000 observations 合共5,000 fills。各 timed run 也必須通過逐筆成交／帳本檢查。建構輸入、import及初始engine setup排除於純 replay timer；Mini 包含 checkpoint 收集，其他原生平台各自保留正常 run 內部工作，功能成本並不相同。[計時邊界](../comparisons/README.md#timing-boundaries) 有完整說明。

| 原生 replay 路徑 | 1,000筆／500fills | 5,000筆／2,500fills | 10,000筆／5,000fills | 10,000筆／無下單 |
|---|---:|---:|---:|---:|
| Mini Core + PaperExchange | 0.021744 s | 0.577807 s | 2.447507 s | 0.002313 s |
| Backtrader | 0.119218 s | 0.608355 s | 1.224922 s | 0.701763 s |
| vn.py CTA | 0.002833 s | 0.015555 s | 0.031300 s | 0.007079 s |
| Nautilus bar | 0.197122 s | 1.481942 s | 4.400275 s | 0.094448 s |
| Nautilus trade tick | 0.180882 s | 1.418019 s | 4.348714 s | 0.041751 s |

[每次測量及中位數](../comparisons/results/performance.json)。在這個成交密集 workload，Mini 由1,000增至10,000筆，耗時約增至 113 倍；這不能用單一「每秒幾多筆」概括。

Mini 成交密集案例的時間隨歷史增長顯著惡化。`Core::apply` 每個事件 clone 全部 Core，包括所有歷史 orders／fills；完整 durable 路徑又有 clone、journal fsync 和全量狀態 JSON。這些是 v0 的簡單 transactional 實作，不能把無訂單的小迴圈速度推論成長時間 trading 吞吐量。

固定歷史，只做5,000次 quote update 的控制實驗：[原始數據](../comparisons/results/history-scaling.json)。

| 保留的 orders + fills | 每次 quote 中位數 |
|---|---:|
| 0 + 0 | 22.7 ns |
| 100 + 100 | 2.18 µs |
| 1,000 + 1,000 | 24.98 µs |
| 5,000 + 5,000 | 167.76 µs |

即使不撮合、不序列化、不寫 journal，歷史越長每次 quote 越慢，與 full-state clone 路徑相符。累計大量成交時，這會形成近似二次增長的成本。無歷史的極短測量另受計時／最佳化影響，不應拿空歷史的納秒數作交易延遲承諾。

完整 `paper` + Python IPC + fsync 模式，1,000筆行情／500fills 約20.22秒（49.5 observations/s），無訂單約8.93秒。每次 run 另核對 journal replay 與最後 runtime state 完全一致。[durable 原始數據](../comparisons/results/durable-performance.json)；它的 durability 保證與前表純記憶體 run 不同。observations/s 也不是 exchange events/s 或 live latency。

建議改善次序（未在本輪作大幅架構重寫）：

1. 以驗證後提交的 delta／undo transaction 取代每事件 full clone；以現有 rollback、重複成交、矛盾回報和 replay 測試守住原子性。
2. 分開 active order index 與歷史／去重資料，避免每次風控和撮合掃描所有已終結訂單；不可直接刪去 execution-ID 去重依據。
3. Python IPC 傳 bounded delta，必要時才取完整 snapshot；保留可核對的 state sequence。
4. 提供明確的 offline batch backtest 模式與獨立 durability 計時。不能為了跑分快而悄悄關掉 live journal fsync。

## 驗證範圍及重現

| 檢查 | 結果 |
|---|---|
| 15個固定案例 + 50組隨機案例，六種profile | 65個輸入完整保存；共同契約fixture通過，模型差異另有明確斷言 |
| Mini 獨立模型 fuzz | 1,000 seeds ×200 observations = **200,000**；每筆成交及每次現金／持倉／估值全部一致 |
| Durable與純Core | 18案例，完整journal replay = runtime state |
| SMA指標 parity | 20 seeds ×1,000 observations = **20,000**；Mini、BT、NT、TA-Lib 的統一warm-up／tie規則下target一致 |
| 本倉庫回歸 | Rust **40**、Python **43**、JavaScript **4** 全部通過；fmt、clippy通過 |

SMA 檢查是指標／target層，不宣稱已驗證所有平台預設 crossover helper，或成交反饋後的完整動態策略一致性。主比較使用共同訂單指令，以隔離撮合差異。vn.py 的 TA-Lib 指標與原生 CTA backtest 分開驗證；本次沒有模擬 futures hedge book 或 close-today offsets。

所有比較皆為離線合成資料、單一標的、整數ticks/lots、零費用、零滑價、充足起始資金；原生平台 matcher 沒有被共同 simulator 取代。獨立 Python oracle 僅驗證 Mini 已宣告模型，不算外部平台證據。未覆蓋真實資料、fees／funding／borrow、公司行動、交易日曆、多標的／多幣、order-book queue、市場衝擊、live latency或峰值記憶體，故不能據此宣稱所有未知bug已排除。

重現命令在 [comparisons/README.md](../comparisons/README.md)。CI已加入不依賴第三方套件的Mini對獨立oracle檢查；完整外部平台比較使用獨立Python3.12環境。[結果摘要](../comparisons/results/audit-summary.json)、[fuzz摘要](../comparisons/results/liquidity-fuzz.json)、[SMA結果](../comparisons/results/sma-parity.json) 隨程式保存；完整隨機輸入／trace位於本機 `runs/platform-audit-complete`，可用相同seeds重建。

本輪仍需後續落地的項目是：Mini 可選 price-time 撮合、消除全量clone的架構優化，以及外部Nautilus修正的正式patch和上游驗證。本文已提供重現及具體修正方向；沒有把未實施的修正標成完成。
