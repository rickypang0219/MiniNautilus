# 驗收條件（exit conditions）

2026-10-09（第二輪更新同日）。這份文件回答一個問題：**要看到甚麼證據，某一部分才算「夠了」，可以轉去做下一部分？**
每一條都寫明條件、量度方法、現狀和證據。未有證據的條目標為「未量度」，不當作通過。

## 原則

- **數字只在指定機器上有效。** 下面的「現狀」是在雲端 Linux container（4 vCPU Intel Xeon 2.8GHz、共用硬件、沒有 CPU 隔離）量到的。正式判斷要在部署機器上重跑；Mac 和 Linux 的 fsync 成本可以相差幾十倍，不能互相代用。
- **CI 不放絕對時間門檻。** CI 只跑正確性；時間條件用〈重跑〉列出的工具手動重跑，結果寫回本文件。比值（例如歷史 10 萬對 0）比絕對值更能跨機器比較，但仍然不是 CI 斷言。
- **Backtest 和 live 分開驗收。** Backtest 的真相是輸入資料，engine 本身就是權威；live 的真相在交易所，engine 只是一個需要對帳的副本。兩邊共用的是 Core 的決策和風控邏輯，不是同一套延遲或 durability 要求。
- **正確性先於速度，但驗證要有終點。** 一個條目通過之後就凍結成 regression test，不再擴大，除非改動了它所覆蓋的語義。

## 總覽

| 編號 | 條件 | 狀態 |
|---|---|---|
| C1–C4 | 正確性與故障語義 | ✅ 已達成，凍結 |
| H1 | 有交易時，每個 event 的成本不隨歷史增長 | ✅ |
| H2 | 風控、timer、撮合不掃描完整歷史 | ✅ |
| H3 | `serve` 的每個 response 不隨歷史增長 | ✅ protocol 2（compact delta） |
| H4 | 長時間運行的記憶體有上限 | ✅ journal 輪換；⚠️ 何時輪換是運維決定 |
| B1 | 5 年 1min 單次 backtest ≤ 1 秒 | ✅ engine 0.53–0.73 s；⚠️ 正式入口端到端 2.4–3.5 s（MISS，< 10 s） |
| B2 | 20×20 參數 heatmap ≤ 10 分鐘 | ✅ 228 秒（4 核） |
| B3 | 正式的 non-durable backtest 入口（Python strategy） | ✅ `sim` + `backtest`，三條路徑逐筆一致 |
| B4 | Backtest 成交模型的時序假設明確寫出 | ✅ 寫明並用 fixture 固定 |
| L1 | Live 進程內 tick-to-trade p99 ≤ 500µs（30 日歷史量之後） | ⚠️ Linux + outbox 達成（durable 部分 p99 328 µs）；Mac 未量度 |
| L2 | Durable 路徑分段 profiling（Mac 和 Linux） | ⚠️ Linux 已量；工具已備，Mac 要在你的機器上跑 |
| L3 | Journal fsync 政策及 crash 語義 | ✅ `--sync outbox` 實作並以故障注入證明；⚠️ 預設仍是 `every`，切換要你拍板 |
| L4 | 「已落盤但不確定是否已送出」窗口 | ✅ Core + Binance adapter 的「證明不存在」；⚠️ testnet kill -9 未實測 |

剩下的都不是寫 code 可以單方面完成的：Mac 量度（L1、L2）、預設 sync 政策（L3）、testnet 實測（L4），以及輪換時機（H4）。

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

### H3：`serve` 的每個 response 不隨歷史增長 ✅

**條件：** 在 0 和 100,000 張歷史下，一個 Quote 的 response 大小比值 < 2，round-trip p99 比值 < 2。

**做法（protocol 2，`src/protocol.rs`）：** 啟動時和 full reconciliation 取代歷史後送一次完整 state；其他 response 只送固定大小的 header（seq、now、health、position、cash、target 等），加上這個 request 寫過的 orders 和 fills。Core 的 change log 是 opt-in（`track_changes`），in-process backtest 不會累積。Python bridge 把 delta 套到一份 mirror 上，mirror 的內容與 Rust 完整 state JSON 完全相同，所以現有讀 `engine.state["orders"]` 的程式不用改；另外維護 open orders 和最大 ID 的 index，strategy 不再每個 event 掃描歷史（`bridge.open_orders`、`bridge.next_order_id`）。`MINI_RESPONSE=full`（或 `Engine(full_state=True)`）可以切回舊行為作除錯。

**量度：** `python3 examples/ipc_probe.py`（`sim` 模式，沒有 journal，只量序列化和 IPC），每組 2,000 次 Quote round trip。

| 模式 | 歷史 | response | round trip p50／p99 |
|---|---:|---:|---:|
| compact | 0 | 260 B | 66 µs／151 µs |
| compact | 10,000 | 265 B | 67 µs／204 µs |
| compact | 100,000 | 267 B | 63 µs／126 µs |
| full（舊） | 1,000 | 258 KB | 4.3 ms／7.4 ms |
| full（舊） | 3,000 | 786 KB | 14.2 ms／27.2 ms |

大小比值 1.03，p99 比值 0.83。**通過。** Round trip 約 65 µs 主要是 Python 的 JSON 和 pipe，與歷史無關。

**證據：** `tests/protocol.rs`（32 seeds × 300 個 request，包括 lost ack、timeout、disconnect、reconnect 和 full reconciliation，每個 request 後 mirror 與完整 state 相同；5,000 張歷史下 response 不變大）；`tests/test_protocol.py`（compact、full、`sim` 三種模式逐步 state 相同，journal 的 `inspect` 結果與 mirror 相同）。

### H4：長時間運行的記憶體有上限 ✅（機制）／⚠️（時機）

`orders`／`fills` 為了去重和對帳保留整個 session 的歷史。H1 只證明時間不隨歷史增長；記憶體上限靠 **journal 輪換**：

- `mininautilus rotate OLD NEW`（`journal::rotate`）：只在 Healthy 且沒有任何 open／uncertain 訂單時允許。舊 journal 末尾寫入 `Closed { successor }`，之後不能再 `--recover`（仍可 `inspect`）；新 journal 的 Genesis 帶 `carry`：倉位、現金、kill latch、epoch、target revision，以及 `id_floor`（舊 session 用過的最大 client order ID）。
- 新 session 的 Core 不帶任何歷史訂單；`id_floor` 以下的 ID 一律拒絕，Python 的 `next_order_id` 也從它之後開始，避免 Binance client ID 重用。
- Reconciliation 的 `position` 是相對 session 開頭的變化（Binance adapter 本來就這樣計），Core 用 `opening_position` 換算。沒有輪換過的 journal 這幾個欄位都是 0，JSON 不變。

**證據：** `tests/rotation.rs`：5 個 session、每個 120 張訂單（含部分成交和取消），每次輪換後記憶體中的訂單數不超過一個 session，倉位、現金延續，kill latch 和 ID 下限跨輪換保持；有 open order 時拒絕輪換且不改動 journal。

**仍要決定：** 甚麼時候輪換（例如每日收市後、每 N 張訂單、或 flat 時）。輪換要求 book 完全 resolved，所以最自然的時機是 shutdown reconciliation 之後。

---

## B. Backtest

### B1：5 年 1min 單次 backtest ≤ 1 秒

**條件：** 2,629,440 根 1min bar，固定參數，使用真實 Core 語義（`SetTarget`／`SubmitTargeted`、風控、venue 撮合、cancel、Tick timer）。目標 ≤ 1 秒；超過 10 秒要寫 report。

**Engine 路徑 ✅：** `examples/acceptance.rs` 的 `sma_backtest`（strategy 寫在 Rust、同一進程），3 次：0.534–0.731 秒，62–84 ns/event。改動前同一量度需要 1,934.7 秒（約 32 分鐘），見〈改動前後〉。

**正式入口端到端 ⚠️ MISS（< 10 秒，不用寫 report）：** `python3 examples/heatmap.py --single 20 60`：Python 計 SMA target、寫 target 檔、Rust 讀 bar CSV 並模擬。

| 部分 | 秒 |
|---|---:|
| Python 計 SMA target（純 Python，prefix sum） | 0.88–1.05 |
| Rust 讀 2.63M 行 bar CSV | 0.64–0.94 |
| Rust 模擬（11,309,286 events、197,185 orders、50,097 fills） | 0.93–1.49 |
| 合計（不計產生合成資料） | 2.41–3.51 |

（3 次運行的範圍；共用機器，波動明顯。三次的 events、fills 和 equity 完全相同。）

模擬比 engine 路徑多：每根 bar 多了 Heartbeat、Tick，以及「每根 bar 取消未成交單再重發」的執行規則（148k 次取消）。要壓到 1 秒以內，下一步是 binary bar 格式（省 CSV 解析）和把 SMA 計算移到 numpy；兩者都不改語義。

### B2：20×20 參數 heatmap ≤ 10 分鐘 ✅

**條件：** 400 組 (fast, slow) 參數，同一份 5 年資料，多核，總 wall time ≤ 10 分鐘，每組結果可重現。

**量度：** `python3 examples/heatmap.py`（fast 5–50、slow 60–250，各 20 格，4 個 worker process）：**228.3 秒**（另加 8.8 秒產生合成資料）。每組 Python 計 target 後呼叫 `mininautilus backtest`，完全確定性（同參數同輸入同輸出；`tests/test_backtest.py` 驗證兩條路徑逐筆相同）。

注意：在 random walk 上「最好」的參數沒有意義，這只是速度量度。

### B3：正式的 non-durable backtest 入口（Python strategy） ✅

兩條等價路徑（`python/mininautilus/backtest.py`）：

| 路徑 | 用途 | 每根 bar 的成本 |
|---|---|---|
| `run_interactive(Engine(sim=True), bars, strategy)` | 決策依賴成交、倉位或訂單狀態的 strategy | 一次 IPC round trip：上一根 bar 的決策和這根 bar 的市場事件在同一個 request（`batch: [[at, event], ...]`） |
| `run_targets(bars.csv, changes)` → `mininautilus backtest` | 只依賴市場資料的 target（指標類 strategy），參數搜索 | 沒有 IPC；Rust 在進程內跑完 |

- `mininautilus sim [CONFIG]`：與 `paper` 相同的 JSON-lines 介面，但 Core 只在記憶體，沒有 journal、沒有 fsync。
- `mininautilus backtest BARS.csv TARGETS.csv [CONFIG] [--ledger FILLS.csv] [--limit-offset T] [--order-ttl-ms MS]`：輸出 summary JSON（events、orders、fills、cancels、refusals、position、cash、gross equity、MDD、讀檔及模擬時間），可選 fills ledger。
- 執行規則（`backtest::plan`，Python 有逐行對應的 `plan`）：目標改變就 `SetTarget`；取消所有 open order（一根 bar 的有效期）；把剩下的差額以 `price ± limit_offset` 送出 `SubmitTargeted`。所有事件仍經 Core 驗證，風控照常生效。

**證據：** `tests/test_backtest.py`：(1) 增量 SMA strategy 與稀疏 target 序列相同；(2) 同一份 1,500 根 bar，Python 逐 bar 互動（`sim`）與 Rust target runner 的 events 數、orders、position、cash 和逐筆 fills ledger 完全相同（兩組參數，含限價偏移）；(3) durable `paper`（journal + fsync）與 `sim` 的最終 state 相同，journal replay 也相同。

### B4：成交模型的時序假設 ✅

Backtest runner 的時序（`tests/backtest.rs` 固定）：

1. 第 i 根 bar：Quote → Trade（resting order 可在此成交）→ Heartbeat → Tick。
2. 然後才是第 i 根 bar 收市的決策：SetTarget、Cancel、SubmitTargeted。
3. 所以第 i 根 bar 送出的單，最早在第 i+1 根 bar 的 trade 成交（**沒有 look-ahead**）；而第 i+1 根 bar 的 cancel 在該 bar 的 trade 之後，**cancel 可以輸給成交**。
4. 成交價是 trade 價，可以優於限價；同價位按 order ID 次序分配 bar volume；taker 方向與 resting order 同方向時不成交。

Fixture：下一根 bar 成交而不被取消；同一根 bar 不會成交自己的決策；部分成交 → 取消餘額 → 重發差額。OHLC 資料沒有 aggressor 方向，轉換成 `taker` 時要自己定規則（例如收市價高於上一根為 Buy），這是模型假設，不是事實。

跨平台 audit 裏「同一 callback 內 submit 後立即 cancel」的競爭，這個 runner 的執行規則不會產生（取消只發生在下一根 bar）。

---

## L. Live

### L1：進程內 tick-to-trade p99 ≤ 500µs（30 日歷史量之後） ⚠️

- **Core：** H1 顯示 100,000 張歷史下完整交易週期 p99 約 2.5–3.5 µs，不隨運行時間變慢。
- **Durable 路徑（Linux container）：** L2 量到每個 input p99：`every` 569 µs（超出），`outbox` 328 µs（通過）。Journal 編碼、compact response 都與歷史無關（H1、H3）。
- **未量度：** Mac 上的數字；包括 Python strategy 的完整 tick-to-trade（quote 到 SendOrder）。`ipc_probe.py --durable-dir` 量到 `paper` 一次 Quote round trip：`every` p50 321 µs／p99 550 µs，`outbox` p50 73 µs／p99 145 µs（沒有下單）。

### L2：Durable 路徑分段 profiling ⚠️（Linux 已量，Mac 待量）

**工具：** `cargo run --release --example durable_profile -- --dir DIR`（Rust 各階段：prepare、journal encode、write、sync、commit、compact／full response encode），`python3 examples/ipc_probe.py --durable-dir DIR`（加上 pipe 和 Python）。`DIR` 要放在部署用的磁碟上。

**Linux container（2,000 bars、6,599 個 input，每 10 根 bar 下一張單）：**

| 階段（每個 input） | `every` p50／p99 | `outbox` p50／p99 |
|---|---:|---:|
| Core prepare | 0.18／1.6 µs | 0.05／0.45 µs |
| Journal encode | 1.5／3.7 µs | 0.6／2.5 µs |
| write(2) | 2.7／14.7 µs | 0.4／5.4 µs |
| **sync_all** | **192／556 µs** | **0.03／325 µs**（只有 200 個 input 需要 sync） |
| Core commit | 0.1／2.5 µs | 0.03／0.9 µs |
| 合計 | 197／569 µs | 1.2／328 µs |
| Compact response encode（每 request） | 2.4／7.5 µs，565 B | 0.7／4.4 µs |
| 舊 full response encode（約 200 張歷史單） | 68／137 µs，47.6 KB | 36／61 µs |

**結論（Linux）：** fsync 佔 durable 路徑 97%；其餘 Rust 部分合共約 5 µs。上一輪的假設「Mac 的成本主要是 `F_FULLFSYNC`」仍未在 Mac 驗證：請在 Mac 跑上面兩個命令，把結果貼回本節。

### L3：Journal fsync 政策 ✅（實作及證明）／⚠️（預設值）

`serve`／`paper` 接受 `--sync every|outbox`（`journal::SyncPolicy`）：

- `every`（預設，原來的行為）：每個 input 都 `sync_all`。
- `outbox`：每個 input 仍然在 publish 前寫入檔案，但只在 effects 含 `SendOrder`／`SendCancel` 的 input 前 `sync_all`。這一次 sync 同時覆蓋之前所有未 sync 的 input，所以**任何離開進程的動作，其因果歷史都已經落盤**。
- **Crash 保證：** process crash 不會遺失任何 input（已寫入 OS page cache）。OS 或斷電可能遺失最後一段未 sync 的 input；這段之中沒有任何對外動作，遺失的成交回報由 recovery 的強制 Disconnect 和 venue reconciliation 補回；遺失的行情本來就會過期。如果斷電令中段 frame 損壞，checksum chain 照舊 fail closed，要人手處理。
- **證據：** `tests/uncertain_send.rs::outbox_policy_keeps_every_external_action_durable`：每個 `SendOrder` 都在 sync 之後；模擬 process crash（不 sync 就 drop）後完整 replay；模擬 OS crash（把檔案截到最後一次 sync 的長度）後，所有送出過的訂單都還在，對帳後倉位和 fills 與 crash 前相同。

**建議：** live 用 `outbox`（Linux 上 durable 路徑 p99 由 569 µs 降至 328 µs，fsync 次數少 33 倍）。預設暫時保留 `every`，因為改預設會放寬現有腳本的保證，要你決定。

### L4：「已落盤但不確定是否已送出」窗口 ✅（Core + adapter）／⚠️（testnet 實測）

不需要額外的「sent」狀態：寫一個 sent 記錄要多一次 fsync，而且「已送出」也不代表交易所已收到。決定性的資訊只能來自交易所，所以改為**證明不存在**：

- `Reconciliation.absent`（新欄位，舊 journal 相容）：交易所證明從未收到的本地訂單。Core 只接受從未被 ack、沒有成交的 Pending 訂單；它們變成 Rejected（terminal），ID 永遠不再使用。對已 ack 或已成交的訂單聲稱 absent 會 fail closed。
- Binance adapter：簽名 request 在 `timestamp + recvWindow` 之後到達會被拒絕。對未 ack 的訂單，`reconcile` 會等到「最後一次送出（或本進程啟動）+ recvWindow + 2 秒」之後，再用兩個 client ID 查詢；兩者都不存在才列入 `absent`。已 ack 的訂單找不到就拒絕恢復，要人手調查。永遠不會自動重送。
- 沒有 `absent` 的完整 venue snapshot 仍然不能套用（訂單可能還在路上），gate 保持關閉。

**證據：** `tests/uncertain_send.rs`（persist 後、送出前 crash → recover → 沒有證明時 gate 保持關閉 → 有證明後 Healthy、風險釋放、ID 不可重用；對已成交訂單的錯誤 absent 被拒絕，正確 snapshot 仍可恢復）；`tests/test_python.py`（adapter 在 recvWindow 前會等待；已 ack 的訂單不見時拒絕恢復）。

**未做：** 在 testnet 上用 kill -9 實測（這個環境沒有 testnet key）。建議腳本：送出 Submit 後立即 kill -9 `serve`，重啟 `--recover`，用 `examples/binance_spot.py --resume` 對帳，確認訂單被列為 absent 或正常對帳。

---

## 改動前後（第一輪：open-order index）

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

## 第一輪改動

- `src/core.rs`：`Core` 新增不序列化的 `OrderIndex`（open set、`(deadline, id)` set）。所有交易中的訂單寫入都經 `put_order`，同時更新 index；full reconciliation 在 prepare 階段重建 index，commit 只是搬入。Deserialize 時自動重建 index（`#[serde(remote = "Self")]` 加手寫 trait impl），所以 journal snapshot 和 replay 不需要額外步驟。JSON 格式不變。
- 直接修改 `core.orders`（例如測試準備資料）之後要呼叫 `Core::reindex()`；`Core::index_consistent()` 是 full-scan oracle。
- `src/sim.rs`：`PaperExchange` 新增 resting set，撮合按 ID 次序走 resting set；用 cursor 遍歷，避免在成交時配置記憶體。
- `src/dashboard.rs`：open orders 數目改用 `Core::open_orders()`。
- 測試：`tests/transition_equivalence.rs` 每個事件後檢查 index 一致和 exposure 與 full scan 相同；`tests/order_index.rs` 對凍結的 full-scan 撮合做 64 seeds × 400 步的 differential test，並測試 deserialize 重建、`reindex` 和 5,000 張完成歷史後沒有 open work。
- `examples/acceptance.rs`：H1 和 B1 的量度工具，只用改動前已存在的 public API，可以對舊 checkout 編譯比較。

## 第二輪改動

- `src/protocol.rs`、`src/main.rs`、`python/mininautilus/bridge.py`：protocol 2（H3），批次 request（`events`、`batch`），`sim` 模式。
- `src/backtest.rs`、`python/mininautilus/backtest.py`、`mininautilus backtest`：B3 的兩條路徑和共用執行規則；`examples/heatmap.py`：B1／B2 量度。
- `src/journal.rs`：`SyncPolicy`（L3）、`process_profiled` 分段計時（L2）、`rotate` 和 `Closed`／`carry`（H4）。
- `src/core.rs`：change log（opt-in）、`Reconciliation.absent` 的處理（L4）、`opening_position`／`opening_cash`／`id_floor` 和 `Carry`（H4）。
- `python/mininautilus/binance.py`：recvWindow 之後的「證明不存在」（L4）。
- `python/mininautilus/{strategy,sma,targets}.py`：改用 `open_orders`／`next_order_id`，不再每個 event 掃描歷史。
- 測試：`tests/protocol.rs`、`tests/backtest.rs`、`tests/uncertain_send.rs`、`tests/rotation.rs`、`tests/test_protocol.py`、`tests/test_backtest.py`，以及 `tests/test_python.py` 新增 2 個 adapter 案例。

## 重跑

```sh
cargo test --locked && python3 -m unittest discover -s tests -p 'test_*.py'
cargo build --release
cargo run --release --example acceptance                  # H1 + B1（engine）
cargo run --release --example history_paths               # H2
python3 examples/ipc_probe.py --durable-dir runs          # H3 + L1/L2（IPC 部分）
python3 examples/heatmap.py --single 20 60                # B1（正式入口）
python3 examples/heatmap.py                               # B2
cargo run --release --example durable_profile -- --dir runs   # L2（Rust 各階段）
```

對舊版比較：

```sh
mkdir -p runs/acceptance-baseline
git archive 05182f8 | tar -x -C runs/acceptance-baseline
cp examples/acceptance.rs runs/acceptance-baseline/examples/
cargo build --release --manifest-path runs/acceptance-baseline/Cargo.toml --example acceptance
runs/acceptance-baseline/target/release/examples/acceptance
```
