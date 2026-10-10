# MiniNautilus 驗證結果

檢查 commit：`34dde68a6917c64c6fadeb005a46e7253de0229a`，branch：`codex/cross-platform-backtest-audit`。
報告整理：2026-10-11（香港）。測試在共用 Linux container 執行；不是目標 EC2/EBS 的部署驗收。原始長測發生於 2026-10-10；完整時間戳保存在 JSON。

## 結論

已完成要求的離線測試、90 分鐘合成 soak、本地 crash/recovery、真實 WebSocket 行情錄製與離線 replay，以及跨平台 backtest 比較。正確性檢查通過；**性能驗收仍有兩項失敗：tick-to-trade p99 與 drift**。沒有修改 repository 的 tracked files，也沒有向 Binance 提交訂單。

## 已完成的檢查

| 工作 | 結果 |
|---|---|
| Formatting、Clippy、debug/release build | 通過；Rust 1.96.0，Cargo locked |
| Rust tests | 64 passed，0 failed/ignored |
| Python tests | 54 passed，0 failed/skipped；包含實際 Rust subprocess、metrics 和 dashboard |
| Node tests | 4 passed，0 failed/skipped |
| Native Backtrader adapter tests | 2 passed |
| Mini / durable parity | 35 個案例的 fills、checkpoints、position、cash 全部一致；journal replay 核對 |
| Mini 獨立 oracle fuzz | 1,000 seeds × 200 observations = 200,000 筆；逐筆成交及帳目一致 |
| 六種平台配置 | 15 fixtures + 20 random cases；native runner 和 pinned semantic checks 通過。模型敏感案例不要求所有平台相等 |
| 原生 SMA parity | 20 seeds、20,000 observations 一致 |
| Nautilus 診斷 | 重現 pinned 1.230.0 的 PnL cycle aggregation 和 L1 liquidity-refresh 問題；沒有 patch 外部套件 |
| 真實 SIGKILL recovery | `every` / `outbox` 都通過；持久化 Submit 後不 dispatch，kill 子程序，恢復後 gate 保持關閉直到提供 mock absence proof；ID 不可重用 |
| 既有故障注入 | Rust suite 包含 append/sync failure、rollback、checksum、uncertain send、OS-loss 模擬與輪換測試；不等同實際斷電或交易所驗證 |

跨平台的 all-field exact parity（35 cases）：Mini 35、Backtrader 28、Backtrader volume filler 31、vn.py 28、Nautilus bar 7、Nautilus tick 10。這些不是正確性排名；`check_semantics.py` 驗證了預期模型差異。

## 90 分鐘合成 soak

負載：seed 7、50 market updates/s 目標、每 10 updates 一根 synthetic candle、`--sync outbox`。執行 5,400 秒；Prometheus 分析窗口排除開始 5 秒，為 5,395 秒。沒有 REST 或 WebSocket 請求。

- 實際處理 **264,300 updates**（約 48.94/s）、**1,330,144 journal inputs**、**1,604 orders**、**3,490 fills**。
- 最後 position 2、Healthy；journal replay 與 runtime 的 orders/fills/position/health 摘要一致。
- 未記錄 Alert、QueryState 或 gate_closed；metrics 採樣 Healthy 比例 100%。這不等同對所有未知故障的證明。
- 約 1,912 loop overruns：它表示 loop 未能按 20ms 節奏完成；不是 gate 或帳目錯誤，亦解釋了 updates 少於理想 270,000。
- Rust RSS：2,244 → 3,216 KiB；Python RSS：33,908 → 40,016 KiB。每 30 秒採樣，180 個樣本。此 run 不做 rotation，history 與 SMA prices 保留會增長；不能由這個樣本宣稱記憶體永久有上限或已證明 leak。
- 完成 6 組 Rust perf、6 組 py-spy profiles。低 CPU 使用率令樣本有限，只用於候選熱點，不當作精確耗時歸因。

| 門檻 | 實測 | 結果 |
|---|---:|---|
| Rust request p99 ≤ 0.5 ms | 0.321 ms | PASS |
| Python IPC p99 ≤ 2 ms | 1.039 ms | PASS |
| Candle → SendOrder p99 ≤ 5 ms | 14.98 ms | **FAIL** |
| 最後／首段 Rust p99 < 2 | 2.1005 | **FAIL** |
| Healthy ≥ 99% | 100% | PASS |
| REST errors | 未執行 REST | **不適用**；原生報告的零錯誤 PASS 不代表網絡通過 |

分位數是 Prometheus histogram 插值估計。Counts 的 Prometheus increase 也有 extrapolation；上面的精確 inputs/orders/fills 取自 runtime 與 journal。

### 延遲失敗分析

六段 Rust p99（ms）：0.184、0.190、0.493、0.206、0.316、0.387。最慢在第三段；之後回落，再上升。同期第三段 Python strategy、write、sync、IPC 也變慢，所以不能只憑時間相關性判定是 O(history) 問題。原始 drift FAIL 保留，不改門檻、不挑選時間段將它變成 PASS。

長測的實際 sync 操作 p99 約 11.64 ms，tick-to-trade p99 14.98 ms。這支持進一步檢查訂單觸發的 sync，但兩個獨立 histogram 不能證明同一訂單的因果關係。每次 SubmitTargeted/Cancel 之間平均約累積 136 KB journal bytes，最多約 1.20 MB；這是 write-volume proxy，並非 sync latency 的直接量度。

五分鐘獨立對照（相同 seed/rate/duration；兩次 run 無本 task 的重型並行工作）：

| 指標 | every | outbox |
|---|---:|---:|
| Rust request p99 | 4.216 ms | 0.345 ms |
| Python IPC p99 | 4.416 ms | 2.851 ms |
| Tick-to-trade p99 | 4.908 ms | 26.300 ms |
| 實際 sync 操作 p99 | 4.116 ms | 11.700 ms |

**outbox 改善整體 request，不保證改善下單尾延遲。** 兩輪實際 updates 因 overrun 分別為 14,715 與 13,972，所以它是相同 offered-load/time 的觀察比較，不是完全相同事件前綴的配對實驗。shared-host 差異與累積 flush 成本仍待隔離。

後續針對性檢查：

- IPC history probe：0 / 10,000 / 100,000 歷史 orders，compact response 260 / 265 / 267 bytes；p99 725.892 / 1203.359 / 90.350 µs。H3 檢查通過，但樣本顯示環境波動，不能據此否定長測 drift。
- Durable profile（2,000 bars、6,599 inputs）：every total p99 1.384 ms、outbox 0.471 ms。這是不同的同步短 benchmark，不替代 paced soak；outbox 的 per-input sync 分布包含未 sync 的 inputs，不能與 soak 的「實際 sync 次數」分布直接相比。
- Prepare/commit、open-order indexes 的現有測試通過；history benchmark 的 trade-cycle p99 ratio 0.978。

## 真實行情錄製與 replay

從 Spot Testnet WebSocket 錄到 **599 根連續 1s closed candles**，0 gaps、0 duplicates、0 conflicting duplicates、0 out-of-order。錄製目標 600 秒；結束時 recv timeout 加 close timeout 使總耗時約 603 秒。保存的 TimeoutError 位於截止時刻，不是途中斷線；錄製期間沒有 reconnect。

另做 clean-close → reconnect，兩次都收到 candle。這只證明重新連線可讀，不證明有 REST backfill 或無損 reconnect。

原始檔 SHA256：`b712f05d7236d308b5fd7f95eea207ce6cb4711c9e0962a75790522eb71d09b2`。

**發現的輸入限制：** 351 根 candle 成交量為零，正式 batch parser 拒絕 volume=0。沒有把零量改成假成交量，也沒有悄悄丟棄後稱為完整 replay。

1. **完整 599 根**：保留 quote/strategy observation；零量 candle 不 emit Trade。兩次 in-memory replay、durable paper state、journal inspect 完全一致；238 orders、22 fills、final position 0、Healthy。
2. **248 根非零量子集**：另外比較 batch repeat 與 interactive ledger，逐筆一致；93 orders、26 fills。這是不同的 observation sequence，不宣稱與完整資料的策略結果相等。
3. 初次 full replay 使用預設 risk cap 時沒有成交，原因是 BTC 價格映射到整數 ticks 後超過預設 `max_order_notional=1,000,000`；改用明示的本地 replay config 後驗證成功。沒有修改引擎風控邏輯。

這是 Testnet 真實 candles + 模擬執行，不是交易所成交。價格單位 0.01 USDT、數量單位 0.00000001 BTC 僅為 replay normalization，未經 REST exchangeInfo 核對；aggressor 由 close/open 推定，不是真實 trade tape；沒有 fees/slippage/queue 模型。未同步交易所時鐘，所以沒有報告網絡延遲或投資績效。

## Backtest 性能

- 正式 5 年 1min、2,629,440-bar Python-target → Rust backtest：0.9948 s（不含合成資料準備）；11,309,286 events、197,185 orders、50,097 fills，與文件 fixture 帳目一致。
- 400 組參數、4 workers：80.52 s，另有約 9.99 s 資料準備。全部 400 個結果已保存。
- Rust-only acceptance backtest：0.385 s。該 harness 的事件及策略實作不同，不與正式 Python 路徑直接比較速度。
- 這些 benchmark 與最初的 pipeline-smoke 並行；不是 CPU 隔離的 latency SLA。後來的 controlled runs 和 90 分鐘 run 沒有本 task 的重型測試並行。

## 下一步

1. 為每筆 SendOrder/SendCancel 關聯 Python elapsed、Rust stages、sync bytes 和 sequence，直接定位訂單尾延遲；再做配對負載驗證。不要由整體 request p99 推論下單尾延遲。
2. 在目標 EC2/EBS 上重跑相同負載，採集 scheduler/I/O 資料來區分 host noise 與歷史效應。保留目前兩個 FAIL。
3. 若要 batch runner 原生支援真實 candle 檔，另開實作任務定義 volume=0 的語義（保留策略 observation、不生成成交）。本輪僅使用 helper，不修改源碼。
4. 由使用者決定 outbox 預設及 rotation 時機；目前結果不支持自動改預設。若需要 group commit，先定義 crash/durability 語義。
5. 真正的 Binance 簽名驗證、REST live runner、Testnet 下單及 exchange crash/reconciliation 仍受先前 HTTP 451 阻擋；本輪沒有提交訂單或再次嘗試憑證。

## 證據與重現

原始工作目錄：`/workspace/MiniNautilus/runs/validation-20261010/`。
完整 `evidence.tar.gz` 位於執行環境的 `/workspace/reports/mininautilus-validation-20261011/`，未提交 Git。它保存測試 logs、helpers、comparison inputs/results、WebSocket 原始檔、Prometheus storage、profiles 與完整 soak journals；排除可再生的兩個大型 `.bars.bin` 檔。該目錄亦有 SHA256 manifest。這些本地 artifacts 不保證可從新 checkout 取得。

可攜的精簡結果已提交於 [cloud-validation-20261011](../comparisons/results/cloud-validation-20261011/)：summary、對照結果、各時間段分析、IPC history probe、durable profile、journal write windows 和工具版本。

工具：Rust 1.96.0、Python 3.12.14、numpy 2.5.3、websockets 17.1、prometheus_client 0.26.0、py-spy 0.4.2、Prometheus 3.5.0、Debian linux-perf 6.12.107-1。Prometheus 下載已核對官方 SHA256，perf 來自已驗證簽名的 Debian index。程式 source、依賴宣告和 lockfiles 未變更。

`helpers/run_soak.py` 只為這個環境指定 perf 路徑並加 RSS 取樣；原始 ops runner/report 仍保留。`helpers/finalize_soak.py` 補回本機 perf 報告、保存 synthetic 限制，並逐一驗證 journal。`helpers/replay_ws_complete.py` 包含零量處理、明示 risk config 與三種 replay 比較。
