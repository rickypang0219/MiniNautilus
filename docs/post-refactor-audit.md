# 架構改動後的跨平台驗證與剩餘瓶頸

本次使用本機已安裝的 Backtrader 1.9.78.123、vn.py 4.5.0 / vnpy_ctastrategy 1.4.1、NautilusTrader 1.230.0，實際運行 native backtest。日期：2026-10-09（香港）。測試是單商品、整數 tick/lot、零手續費、零滑點、大額初始資金的合成輸入；不能推論所有策略、資產或版本完全兼容。

## 架構改動有沒有改變結果？

新增 54 個案例：50 組各 500 步的多掛單、撤單隨機輸入，加 4 個時序案例。每組都使用改動前 `0580ad1` 的 release binary 與現在的 Mini binary，逐筆比較 fills、每步 position/cash/equity 及最終帳目：**54/54 完全一致**。

在修正下述 adapter 問題後，50 組多掛單隨機案例在六個配置（Mini、Backtrader default/filler、vn.py、Nautilus bar/tick）全部完全一致。全部 54 個案例的精確一致數為：vn.py 54、Backtrader 兩種配置各 53、Nautilus 兩種配置各 52。這是指定輸入及模型的結果，不是平台正確性排名。

原有的 15 個固定案例及 200 組各 500 步的隨機案例另行運行，並包含 200 組流動性輸入對獨立 oracle 的核對。既有的成交價改善、成交量共享、aggressor、同時點落單等差異依然存在，詳見 [原始比較](backtest-comparison.md)。Core 的 frozen-reference 測試另涵蓋 16,384 次混合事件、錯誤及 rollback 行為；native 比較本身不能替代這些檢查。

## 找到並修正的錯誤：我們的 Backtrader adapter 提早讀取成交

位置：`comparisons/adapters.py` 的 `Strategy.notify_order`。

Backtrader 的通知 clone 共用 `order.executed.exbits`，但每個通知另有自己的 pending slice。舊 adapter 讀取整張累積列表，再用「已讀筆數」去重；當 Submitted/Accepted 通知送達時，共用列表可能已包含隨後的成交。這會讓新訂單的成交被提早讀取，導致我們記錄的回報次序錯誤。

最小案例：第一步掛 sell ID 1 @102，第二步掛 sell ID 2 @102，第三步市場到 102。舊 adapter 記錄 `[2, 1]`，正確通知次序是 `[1, 2]`。原先 50 組多掛單比較全部出現「fills 不同但帳目相同」，修正後全部一致。這是 **比較工具錯誤，並非 Backtrader matching bug**。

修正：使用 `order.executed.iterpending()`，只消費該通知的 execution slice。新增 `comparisons/test_native.py`，同時檢查先後次序及 partial fill 不重複。不要對 fills 排序來掩蓋問題：收到 fill 後即落單的策略會依賴真正回報次序。

## 尚未統一的時序語義

同一 callback 內 `submit buy 2 @100` 後立即 `cancel`，當時市場也是 100：

| 平台配置 | 實際結果 | 原因及處理方向 |
|---|---|---|
| Mini / vn.py | 無成交 | 此輸入下，撤單先於下一次撮合生效 |
| Backtrader default / filler | 下一步成交 2 | 新單先進 submitted queue；cancel 只搜尋 pending queue，這時未能撤掉。應追蹤接受/取消狀態，在 Accepted 後送取消；這仍有成交競爭，不能保證等同 Mini |
| Nautilus bar / tick | 當步已成交 2 | 市價可成交限價單在 cancel 前已成交；要比較同一模型，需明確統一送單/撮合/延遲政策 |

另一個雙向成交案例，Mini 在步 1 買、步 2 賣；Nautilus 可在步 1 已完成兩邊。最終 cash/position 相同，途中曝險及成交次序卻不同。因此只比較最終 PnL 會漏錯。這些是觀察到的模型差異，本次沒有強行修改 Core matching policy。

## clone 移除後，仍然隨歷史增長的 code

> 2026-10-09 更新：下面第 1–3 項已改為 open-order index，第 4 項（完整 Core JSON）仍未處理。結果見 [驗收條件](acceptance.md)。

`examples/history_paths.rs` 先建立實際成交、已完全結束的訂單，setup 不計時。對每個 history size 做一次暖機及三次測量，使用 `black_box`；不涉及 IPC、磁碟或 Python。以下是該次中位數，單位 µs/呼叫：

| 已完成訂單 / fills | exposure_bounds | Core Tick | PaperExchange trade，無新成交 | 完整 Core JSON |
|---:|---:|---:|---:|---:|
| 0 | 0.0028 | 0.0261 | 0.0038 | 0.483 |
| 100 | 0.190 | 0.315 | 0.163 | 27.8 |
| 1,000 | 3.32 | 3.88 | 1.67 | 246 |
| 5,000 | 12.2 | 19.4 | 10.7 | 1,335 |

以上是隔離操作的微基準，不是整個 engine 的 latency percentile，也不能把各列直接相加推算實盤延遲。JSON probe 使用 `to_vec`；CLI 使用 `to_writer`，實際還有 stdout、Python decode 和 fsync 成本。

1. **`src/core.rs::exposure_bounds`** 每次風險檢查掃全部 orders，包括 terminal。可維護 outstanding buy/sell totals，但 submit、fill、cancel ack/reject、timeout uncertainty、reconcile、replay 必須一致更新；不能在 cancel request 時提早釋放曝險。先以現有 full scan 作 oracle 驗證增量結果。
2. **`src/core/transition.rs::Event::Tick`** 每個 Tick 掃全部 orders 找 deadline。可加 deadline heap/index；取消舊 timer 或重用 slot 時，需要版本檢查，避免舊 timer 把新 order gate 掉。更新仍須遵守 prepare/commit，journal 失敗不能先改 index。
3. **`src/sim.rs::PaperExchange::trade`** 每個 trade 掃歷史 orders，跳過 terminal 才找可成交單。可另建 active-order index；目前契約是 ID priority，改成 price-time priority 會改變成交分配、PnL 及跨平台結果。刪 terminal order 也不能同時失去 client-ID 去重、snapshot 及 reconciliation 所需紀錄。
4. **`src/main.rs` 每個 response 都序列化完整 Core**。5,000 張歷史單約 1.24 MB；即使 Quote 已不 clone，整份 state 的 JSON 和 IPC 仍隨歷史增長。建議另設 compact response / event delta，保留完整 snapshot API。影響 Python bridge、策略讀取、dashboard 及恢復流程，需 versioned protocol 和一致性測試。

Ring buffer 適合有界事件交接，不能直接替代任意 ID 的 order store。單純 `id % capacity` 會碰撞；需要完整 key / generation，以及 slot 尚被消費或用於去重時不能覆寫的生命週期規則。先消除全歷史掃描，再量連續記憶體是否值得做，可避免改容器卻保留 O(history) 的工作量。

可參考本機 pinned vnpy_ctastrategy 的 `backtesting.py::cross_limit_order`：它遍歷 `active_limit_orders`，成交完成或取消後從 active 集合移除，另外保留完整 `limit_orders`。這支持「歷史帳本與活躍撮合集合分開」的改善方向，但不能據此把平台耗時差全部歸因於單一函數。

每事件 journal 的 `sync_all` 仍保留。上次 durable benchmark 未證明端到端加速，本次純 core/native loop 數據不推翻這點。Batch fsync 或非同步 journal 會改變 crash 後保證，應獨立設計，不能當作無語義影響的性能修補。

曾懷疑 `paper --recover` 會用空 simulator 恢復，但檢查確認 CLI 已明確拒絕這個模式；這不列為新 bug。

## 本輪平台性能測量

以下為 10,000 個市場觀察、5,000 筆成交，三次測量的中位數。所有測量均先核對相同成交及帳目；**Mini 這列沒有 journal/fsync**。計時範圍差異見下節，不能視為同功能排名。

| 平台 / 配置 | 秒 |
|---|---:|
| mini | 0.1086 |
| backtrader | 1.2402 |
| backtrader_volume | 1.3050 |
| vnpy | 0.0319 |
| nautilus | 4.7589 |
| nautilus_tick | 4.5872 |

Mini 的持續交易耗時隨輸入增長：

| 市場觀察數 | 成交數 | 秒 |
|---:|---:|---:|
| 1000 | 500 | 0.001440 |
| 5000 | 2500 | 0.026539 |
| 10000 | 5000 | 0.108571 |

這與上面的全歷史掃描量測一致：移除 clone 後，交易路徑仍非固定成本。優先減少歷史掃描，比單純換 contiguous container 更直接。所有原始三次樣本保留在 JSON；本機為共享 macOS desktop，沒有 CPU 隔離，不宣稱穩定尾延遲或正式性能 SLA。

## 重跑與證據

平台 benchmark 的計時範圍：Mini 是 release Core + PaperExchange loop，包含建立 checkpoint JSON value，但不包括 process 啟動、輸入解析、最後輸出及 journal；Backtrader 是 Cerebro run，包含逐步 native cash/value 記錄；vn.py 是 run_backtesting，最終 daily PnL/report 在計時外；Nautilus 是 engine.run，最終帳目檢查及 canonical ledger 建立在計時外。各自均排除測試資料建立，做一次暖機、三次測量，且每次均驗證成交及帳目。這些數據比較的是具體 adapter workload，不是等功能生產系統的吞吐量。

完整輸入及原始結果在 `runs/expanded-audit/`；可攜摘要與版本/hash 在 `comparisons/results/post-refactor-audit.json`。`runs/` 不納入 Git。

```sh
cargo build --release --examples
runs/comparison-env/bin/python comparisons/test_native.py
runs/comparison-env/bin/python comparisons/expanded.py --output runs/new-multiple --baseline-root runs/core-transition/baseline-source --seeds 50
runs/comparison-env/bin/python comparisons/run.py --output runs/new-native --platforms mini backtrader backtrader_volume vnpy nautilus nautilus_tick --seeds 200 --steps 500
runs/comparison-env/bin/python comparisons/check_semantics.py runs/new-native
runs/comparison-env/bin/python comparisons/run.py --benchmark --output runs/new-performance.json --platforms mini backtrader backtrader_volume vnpy nautilus nautilus_tick --sizes 1000 5000 10000 --repeats 3
cargo run --release --example history_paths
```

baseline-root 必須是 `0580ad1` 的原始 source checkout，並已用相同 Rust toolchain 編譯 `compare_engine`。測試 harness 不會自行下載或安裝平台。
