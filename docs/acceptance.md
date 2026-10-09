# 驗收條件（exit conditions）

2026-10-09。這份文件回答一個問題：**要看到甚麼證據，某一部分才算「夠了」，可以轉去做下一部分？**
每一條都寫明條件、量度方法、現狀和證據。未有證據的條目標為「未量度」，不當作通過。

## 原則

- **數字只在指定機器上有效。** 下面的「現狀」是在雲端 Linux container（4 vCPU Intel Xeon 2.8GHz、共用硬件、沒有 CPU 隔離）量到的。正式判斷要在部署機器上重跑；Mac 和 Linux 的 fsync 成本可以相差幾十倍，不能互相代用。
- **CI 不放絕對時間門檻。** CI 只跑正確性；時間條件用 `examples/acceptance.rs` 手動重跑，結果寫回本文件。比值（例如歷史 10 萬對 0）比絕對值更能跨機器比較，但仍然不是 CI 斷言。
- **Backtest 和 live 分開驗收。** Backtest 的真相是輸入資料，engine 本身就是權威；live 的真相在交易所，engine 只是一個需要對帳的副本。兩邊共用的是 Core 的決策和風控邏輯，不是同一套延遲或 durability 要求。
- **正確性先於速度，但驗證要有終點。** 一個條目通過之後就凍結成 regression test，不再擴大，除非改動了它所覆蓋的語義。

## 總覽

| 編號 | 條件 | 狀態 |
|---|---|---|
| C1–C4 | 正確性與故障語義 | ✅ 已達成，凍結 |
| H1 | 有交易時，每個 event 的成本不隨歷史增長 | ✅ 已達成（本輪） |
| H2 | 風控、timer、撮合不掃描完整歷史 | ✅ 已達成（本輪） |
| H3 | `serve` 的每個 response 不隨歷史增長 | ❌ 未達成：仍回傳完整 state JSON |
| H4 | 長時間運行的記憶體有上限（retention） | ❌ 未設計 |
| B1 | 5 年 1min 單次 backtest ≤ 1 秒 | ✅ engine 路徑達成；⚠️ 沒有正式入口（B3） |
| B2 | 20×20 參數 heatmap ≤ 10 分鐘 | ⏳ 未量度 |
| B3 | 正式的 non-durable backtest 入口 | ❌ 未實作 |
| B4 | Backtest 成交模型的時序假設明確寫出 | ⏳ 部分（見跨平台 audit） |
| L1 | Live 進程內 tick-to-trade p99 ≤ 500µs（30 日歷史量之後） | ⚠️ Core 部分達成；durable 路徑未達成 |
| L2 | Durable 路徑分段 profiling（Mac 和 Linux） | ⏳ 未量度 |
| L3 | Journal fsync 政策（每事件／只 outbox／batch）及 crash 語義 | ⏳ 取決於 L2 |
| L4 | 「已落盤但不確定是否已送出」窗口的處理 | ❌ 未解決 |

建議次序：**H3 → B3 → B2 → L2 → L3 → L4 → H4**。B1、H1、H2 完成後，backtest 這一邊只差正式入口和 heatmap。

---

## C. 正確性與故障語義（已達成，凍結）

| 編號 | 條件 | 證據 |
|---|---|---|
| C1 | 價格、數量全用整數；對沖方向的 pending 不互相抵銷；cancel 確認前繼續預留風險 | `tests/reliability.rs`：`reservations_are_immediate_and_opposite_sides_do_not_net`、`cancel_fill_race_keeps_reservation_until_terminal_confirmation` |
| C2 | Lost ack、重複成交、亂序、sequence gap、未知訂單、不平衡 snapshot 一律 fail closed | `tests/reliability.rs` 18 個案例；`tests/targets.rs` 4 個 target guard 案例 |
| C3 | Durable recovery 不重送歷史 effects；checksum 損壞拒絕啟動；kill latch 跨重啟保持 | `durable_recovery_snapshot_tail_and_exclusive_writer`、`committed_corruption_and_wrong_snapshot_fail_closed`、`kill_remains_latched_through_reconciliation_but_allows_cancel` |
| C4 | 每次架構改動都與凍結的舊版逐事件一致 | `tests/transition_equivalence.rs`：對 `0580ad1` 的 full-clone reference 比較每個事件的 state、effects、Err，64 seeds × 256 個混合事件；本輪再加上 index 與 full scan 一致的斷言 |

跨平台 audit（Backtrader、vn.py、NautilusTrader，見 [backtest-comparison.md](backtest-comparison.md) 和 [post-refactor-audit.md](post-refactor-audit.md)）沒有找到 Mini Core 的 bug，找到的是比較工具的 bug 和其他平台的語義差異。**凍結規則：** CI 繼續跑 `comparisons/run.py --platforms mini` 對 oracle 的比較；外部平台比較只在改動成交模型時手動重跑，不再新增案例類型。

---

## H. 成本不隨歷史增長

### H1：有交易時，每個 event 的成本不隨歷史增長 ✅

**條件：** 在 0 張和 100,000 張已完成歷史訂單下，完整交易週期（submit → ack → quote → trade → fill）的 p99 比值 < 2。

**量度：** `cargo run --release --example acceptance`。歷史經真實的 submit／ack／fill 路徑建立，不計時。每批 200 次呼叫取平均，丟棄 10 批 warm-up，再取 101 批的 p50／p99。單次呼叫只有幾 ns 到幾 µs，低於 timer 解析度，所以 p99 是「批平均的 p99」，不是單一 event 的尾延遲。

**現狀（3 次運行）：**

| 操作 | 歷史 0：p50／p99 | 歷史 100,000：p50／p99 |
|---|---:|---:|
| `exposure_bounds` | 9–14 ns／35–101 ns | 9–12 ns／9–32 ns |
| `Tick` | 33–35 ns／66–205 ns | 34–62 ns／51–199 ns |
| `PaperExchange::trade`（沒有成交） | 13–15 ns／17–50 ns | 13–17 ns／13–44 ns |
| 完整交易週期（約 6 個 event） | 1.5–2.1 µs／2.3–2.6 µs | 1.6–1.7 µs／2.5–3.5 µs |

交易週期 p99 比值：1.21、1.05、1.44。**通過。**

改動前（`05182f8`，同一 example、同一機器）比值為 29.8，見下面〈改動前後〉。

### H2：風控、timer、撮合不掃描完整歷史 ✅

本輪改動（見〈本輪改動〉）：

| 原本的 O(history) 位置 | 現在 |
|---|---|
| `Core::exposure_bounds` 掃描全部 orders | 只走 open set（`!terminal \|\| uncertain`，即 `remaining()` 可能非零的訂單） |
| `Event::Tick` 掃描全部 orders 找 deadline | `(deadline, id)` 有序 index，只取已到期的 |
| `SubmitTargeted` 的 barrier 掃描全部 orders | open set 是否為空 |
| `Event::Disconnect` 掃描全部 orders | 只走 open set |
| `PaperExchange::trade` 掃描全部 venue orders | 只走 resting set，仍按 ID 次序撮合（成交分配不變） |

`examples/history_paths.rs` 在 0／100／1,000／5,000 張歷史下，exposure、Tick、venue trade 都是平的（約 9 ns、33 ns、13 ns）。

### H3：`serve` 的每個 response 不隨歷史增長 ❌

`src/main.rs` 每個 response 都序列化完整 `Core`。同一 example 量到：0 張歷史約 0.6 µs，1,000 張約 0.32 ms，5,000 張約 1.8–2.7 ms。Python strategy 每個 event 都要 decode 這份 JSON。

**條件：** 在 0 和 100,000 張歷史下，一個 Quote 的 response 大小比值 < 2，round-trip p99 比值 < 2。
**方向：** compact response／event delta，保留完整 snapshot 作為明確的查詢；需要 versioned protocol，並對 Python bridge、dashboard、recovery 做一致性測試。

### H4：長時間運行的記憶體有上限 ❌

`orders`／`fills` 為了去重和對帳保留全部歷史，沒有 retention 或 compaction。H1 只證明**時間**不隨歷史增長，記憶體仍然線性增長。

**條件：** 定義 retention 政策（例如對帳 watermark 之前的 terminal orders 移到冷存儲，只保留 ID 去重所需的資料），並證明模擬 30 日運行後記憶體穩定。在 live 跑長時間之前要完成。

---

## B. Backtest

### B1：5 年 1min 單次 backtest ≤ 1 秒 ✅（engine 路徑）

**條件：** 2,629,440 根 1min bar，固定參數，使用真實 Core 語義（`SetTarget`／`SubmitTargeted`、風控、venue 撮合、cancel、Tick timer）。目標 ≤ 1 秒；超過 10 秒要寫 report。

**量度：** `examples/acceptance.rs` 的 `sma_backtest`：確定性 random walk（整數 tick），SMA 20／60 crossover，目標倉位 ±1，限價距離 20 ticks，未成交的單在下一根 bar 取消。結束時核對 Core 和 venue 的 position 一致、health 為 Healthy。

**現狀：**

| 運行 | events | orders | fills | cancels | 秒 | ns/event |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 8,672,850 | 61,457 | 30,998 | 31,077 | 0.585 | 67 |
| 2 | 8,672,850 | 61,457 | 30,998 | 31,077 | 0.534 | 62 |
| 3 | 8,672,850 | 61,457 | 30,998 | 31,077 | 0.731 | 84 |

改動前同一量度需要 1,934.7 秒（約 32 分鐘），見〈改動前後〉。

**限制：** 合成價格、沒有 fee、沒有讀檔時間；strategy 寫在 Rust 裏並在同一進程內執行。這證明 Core + PaperExchange 夠快，**不代表 Python strategy 經 IPC 的 backtest 也能做到**（見 B3、H3）。

### B2：20×20 參數 heatmap ≤ 10 分鐘 ⏳

按 B1 的速度，400 次 run 單核約 4 分鐘；用多核應該更快。未有正式入口，所以未量度。
**條件：** 400 組 (fast, slow) 參數，同一份 5 年資料，多核，總 wall time ≤ 10 分鐘，每組結果可重現（同參數同輸出）。

### B3：正式的 non-durable backtest 入口 ❌

現在不經 journal 的路徑只有 `examples/compare_engine.rs`（比較工具）和 `examples/acceptance.rs`（驗收工具）。Python strategy 要 backtest 就要經過 `paper`／`serve`，每個 event 都做 fsync 並回傳完整 state。

**條件：**
- 同一個 `Core`，沒有 journal、沒有 fsync。
- 輸入：bar／trade 檔案；輸出：fills ledger、equity series、summary（gross、MDD、成交數）。
- 與 `paper` 路徑對同一輸入的 fills 和最終帳目完全一致（parity test）。
- Strategy 介面要先決定：Rust trait（最快）或批次 Python（每根 bar 一次 IPC 的成本要量度）。這是設計決定，未有共識。

### B4：成交模型的時序假設寫明 ⏳

跨平台 audit 顯示，同一 callback 內 submit 後立即 cancel，Mini／vn.py 不成交，Backtrader 下一步成交，Nautilus 當步成交。現時 Mini 的模型是「cancel 一定贏」，對 live 而言偏樂觀。
**條件：** 文件寫明 backtest 的送單延遲和 cancel／fill 競爭模型；如果加入延遲模型，用 fixture 證明 cancel 可以輸掉競爭。

---

## L. Live

### L1：進程內 tick-to-trade p99 ≤ 500µs（30 日歷史量之後） ⚠️

- **Core 部分：** H1 顯示 100,000 張歷史下，完整交易週期 p99 約 2.5–3.5 µs。**通過**，而且不再隨運行時間變慢。
- **Durable 路徑：** [core-state-transitions.md](core-state-transitions.md) 在 macOS 量到，沒有訂單時 1,000 個 observation 要 9.7 秒，即每個 event 約 9.7 ms（包括 fsync、完整 JSON IPC、Python）。**比預算慢約 20 倍，未通過。**

### L2：Durable 路徑分段 profiling ⏳

**條件：** 在 Mac 和 Linux 分別量度每個 event 的 fsync、journal 序列化、response 序列化、pipe IPC、Python decode 和 strategy，各自的 p50／p99，以及總和與端到端數字的差。

**假設（未驗證）：** Rust 的 `File::sync_all` 在 Apple 平台使用 `F_FULLFSYNC`，一次通常要幾 ms，很可能是 Mac 上的主要成本；之前在 Linux container 單次量到的 `fsync` p50 約 158 µs（量度程式未放入 repo）。L2 的目的就是用數據確認或推翻這個假設。

### L3：Journal fsync 政策 ⏳（取決於 L2）

選項：每事件 fsync（現狀）、只對會產生 `SendOrder`／`SendCancel` 的事件 fsync（outbox）、group commit／batch。
**條件：** 選定政策後，寫明 crash 後的保證（哪些事件可能遺失、遺失後如何經對帳恢復），並用故障注入測試證明；不可以把 batch 當成沒有語義影響的性能修補。

### L4：「已落盤但不確定是否已送出」窗口 ❌

現在 journal 記錄 input 後才 publish effects；如果 process 在 send 前後崩潰，重啟後不知道訂單有沒有到達交易所。
**條件：** 明確的 outbox 狀態（intent persisted → sent → acked），重啟後對未確認的 outbox 項目用 client order ID 向交易所查詢，而不是推斷為 reject 或重送；testnet 上用 kill -9 注入證明。

---

## 改動前後（本輪）

同一個 `examples/acceptance.rs`，在同一台機器上對 `05182f8`（改動前，已移除 full clone）和本輪改動後分別 build 和運行。舊版的長時間運行與其他量度部分重疊執行（4 vCPU），數字只作數量級比較。

| 量度 | `05182f8` | 本輪 |
|---|---:|---:|
| SMA backtest，200,000 bars | 3.36 s | 0.040 s |
| SMA backtest，500,000 bars | 39.9 s | 0.133 s |
| **SMA backtest，5 年 2,629,440 bars** | **1,934.7 s（約 32 分鐘）** | **0.53–0.73 s** |
| `exposure_bounds` p50，歷史 100,000 | 2.33 ms | 9–12 ns |
| `Tick` p50，歷史 100,000 | 1.77 ms | 34–62 ns |
| `PaperExchange::trade`（沒有成交）p50，歷史 100,000 | 1.38 ms | 13–17 ns |
| 交易週期 p50／p99，歷史 100,000 | 7.69 ms／9.91 ms | 1.6–1.7 µs／2.5–3.5 µs |
| 交易週期 p99 比值（100,000 對 0） | 29.8 | 1.05–1.44 |

舊版 200k → 500k bars 是 2.5 倍輸入、11.9 倍時間，即使已移除 clone，backtest 仍然超線性。這就是 5 年 run 要半小時的原因：成本來自每個 event 掃描完整歷史，不是物理限制。

注意：交易週期量度本身會在量度期間新增約 22,000 張訂單，所以「歷史 0」一欄在舊版其實包括增長中的歷史（p50 202 µs）；舊版的真實比值比 29.8 更差。

## 本輪改動

- `src/core.rs`：`Core` 新增不序列化的 `OrderIndex`（open set、`(deadline, id)` set）。所有交易中的訂單寫入都經 `put_order`，同時更新 index；full reconciliation 在 prepare 階段重建 index，commit 只是搬入。Deserialize 時自動重建 index（`#[serde(remote = "Self")]` 加手寫 trait impl），所以 journal snapshot 和 replay 不需要額外步驟。JSON 格式不變。
- 直接修改 `core.orders`（例如測試準備資料）之後要呼叫 `Core::reindex()`；`Core::index_consistent()` 是 full-scan oracle。
- `src/sim.rs`：`PaperExchange` 新增 resting set，撮合按 ID 次序走 resting set；用 cursor 遍歷，避免在成交時配置記憶體。
- `src/dashboard.rs`：open orders 數目改用 `Core::open_orders()`。
- 測試：`tests/transition_equivalence.rs` 每個事件後檢查 index 一致和 exposure 與 full scan 相同；`tests/order_index.rs` 對凍結的 full-scan 撮合做 64 seeds × 400 步的 differential test，並測試 deserialize 重建、`reindex` 和 5,000 張完成歷史後沒有 open work。
- `examples/acceptance.rs`：H1 和 B1 的量度工具，只用改動前已存在的 public API，可以對舊 checkout 編譯比較。

## 重跑

```sh
cargo test --locked
cargo run --release --example acceptance              # H1 + B1，完整 5 年
cargo run --release --example acceptance -- --bars 200000 --history 20000   # 快速版
cargo run --release --example history_paths           # H2 各操作
```

對舊版比較：

```sh
mkdir -p runs/acceptance-baseline
git archive 05182f8 | tar -x -C runs/acceptance-baseline
cp examples/acceptance.rs runs/acceptance-baseline/examples/
cargo build --release --manifest-path runs/acceptance-baseline/Cargo.toml --example acceptance
runs/acceptance-baseline/target/release/examples/acceptance
```
