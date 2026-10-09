# MiniNautilus

一個用 Rust 學 trading systems 嘅實驗室：**先驗證狀態正確同可恢復，再量度低延遲設計**。

已實作 deterministic core、故障 simulator、durable journal/recovery、Python strategy、Binance Spot Testnet adapter，以及獨立效能實驗。範圍係 **一個 account、一個 instrument、limit order + cancel**。

目前係可執行嘅 reference implementation，**未係 production/HFT engine**：Core 已改為 validate/prepare/commit，避免每個事件 clone 全份 state；journal 仍每個事件做 `sync_all`，Python bridge 用完整 state JSON IPC，Binance 用 REST polling。風控、timer 和 paper 撮合只走 open-order index，不再掃描完整歷史；完整 state 的 IPC 成本仍隨歷史增長。各部分「做到甚麼才算夠」見 [驗收條件](docs/acceptance.md)。

## 先跑第一個 milestone

需要 Rust 1.96+、Python 3.10+。Python 只用 standard library。喺 project root 執行：

```sh
cargo build --locked
cargo run --locked -- demo
```

預期：

```text
lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill
position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy
```

呢個 demo 證明：ack 遺失唔會自動重送；斷線期間成交喺 reconciliation 補回；重複 fill 唔會重複計數；剩餘 3 lots 繼續 reserve。

## Python strategy，Rust backtest

```sh
mkdir -p runs
python3 examples/replay.py --journal runs/my-backtest.jsonl
python3 examples/analyze.py runs/my-backtest.jsonl --db runs/my-backtest.sqlite
```

預期兩張單、三個獨立 fills、最後 position 0、gross realized PnL **12 tick-lots**。呢個係合成測試數據，唔係策略績效。Journal/database 必須用新檔名；唔會覆蓋已有 run。

Strategy 寫喺 [`python/mininautilus/strategy.py`](python/mininautilus/strategy.py)。Python 只讀 Rust snapshot 同產生 intent；OMS、risk、position、paper matching 全部由 Rust 處理。Signal 帶 `based_on_seq` 同 `valid_until`，Rust 會再驗證。

Cold path 可以另外開 bounded telemetry worker：

```sh
MINI_TELEMETRY=runs/telemetry.log python3 examples/replay.py --journal runs/with-telemetry.jsonl
```

Telemetry queue 滿會計 dropped count；佢唔係 recovery journal。SQLite analysis 由已停止、經 Rust 驗證嘅 journal 重建，唔喺落單路徑。

## Real-time dashboard（backtest / live 共用）

```sh
cargo run --locked -- dashboard runs --port 8765
```

開 [http://127.0.0.1:8765](http://127.0.0.1:8765) 即時睇 trades、orders、signals、position、PnL 同 recovery 狀態。獨立 Rust observer 讀 journal，唔阻塞 engine；無 Docker／Node dependency。支援分頁、搜尋、side filter、pause view 同 CSV export。完整費用未齊時只顯示 gross，唔假設 fee 為零。

詳見 [dashboard 使用與設計](docs/dashboard.md)。

## Live market data + paper execution

```sh
python3 examples/binance_spot.py --run-dir runs/binance-paper --iterations 20
```

用 **Binance Spot Testnet 公開報價**，唔需要 key，唔會向交易所落單。Paper 模式以 quote touch 做簡化成交，每次最多一 lot；冇模擬 queue position、market impact 或真實成交概率。Live example 另開一個 Python strategy process，main loop 繼續處理 gateway 同 Rust messages。

## Binance Spot Testnet execution

Adapter 固定使用 `https://testnet.binance.vision`；無 production URL override。

1. 喺本機環境設定 `BINANCE_TESTNET_API_KEY` 同 `BINANCE_TESTNET_API_SECRET`；唔需要將 key 放入 code、run directory 或 chat。
2. 使用專用 Testnet account，同一個交易對唔好有其他 bot/manual orders/transfers。
3. 先查看該 symbol 嘅 `exchangeInfo`。`--lots` 係 `LOT_SIZE.stepSize` 嘅倍數，`--max-notional-units` 單位係 `tickSize × stepSize`；預設三 lots 可能低於 minimum notional。
4. 選定符合 filters 嘅數值後執行下面命令。只有明確選擇 `--mode testnet` 先會送出 Testnet orders。

```sh
python3 examples/binance_spot.py --mode testnet --run-dir runs/testnet-session --lots YOUR_LOT_COUNT --max-notional-units YOUR_LIMIT
```

停止時會取消本 session 嘅未完成單並 reconciliation；**唔會平掉已成交持倉**。原 session 恢復：

```sh
python3 examples/binance_spot.py --mode testnet --run-dir runs/testnet-session --resume --lots YOUR_LOT_COUNT
```

恢復沿用已記錄嘅 risk config、symbol、tick/lot 同 client-ID namespace。`--lots` 控制 strategy target，唔會放寬已保存嘅 risk limit。

REST 沒有原子 snapshot/stream watermark；所以 adapter 採用 **cancel-and-reconcile**：凍結新單、確認所有本 session 訂單 terminal、分頁讀取全部 fills，再核對 base balance（包括 base-asset fees）。Missing orders、unknown open orders、history lag、balance mismatch 都會保持停止。詳細限制見 [architecture](docs/architecture.md)。

## Recovery / inspection

```sh
target/debug/mininautilus inspect runs/my-backtest.jsonl
target/debug/mininautilus snapshot runs/my-backtest.jsonl runs/my-backtest.snapshot.json
target/debug/mininautilus serve runs/my-backtest.jsonl --recover runs/my-backtest.snapshot.json
```

`inspect` / `snapshot` 唔修改 journal，亦唔 dispatch effects。`serve --recover` 會 replay、驗證 snapshot、修復最後未完成一行，然後持久化 `Disconnect`；必須 reconciliation 先再交易。Replay 唔會重發歷史 orders。

`serve` 係 JSON-lines protocol：每行 `{"at":0,"event":{"Quote":{"bid":99,"ask":101}}}`，回覆 `effects` 同 `state`。`at` 係 monotonic engine milliseconds。直接使用 `serve` 嘅 caller 負責 transport、timer 同 execution reports；Python example 已接好呢啲邊界。

Paper server 嘅 venue 係記憶體內模型，唔支援原地 restart；要恢復分析可 `inspect`，要重新模擬則用新 journal 重播同一份 market inputs。

## Tests / experiments

```sh
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked
python3 -m unittest discover -s tests -p 'test_*.py' -v
cargo bench --locked --bench latency
```

Rust 測試涵蓋 OMS/risk、loss/duplicate/reorder、recovery、journal corruption、exclusive writer、I/O failure、queue full/drop/wraparound 同跨 thread payload visibility。Python 測試涵蓋真實 Rust subprocess、deterministic replay、PnL projection 同 mocked Binance transport contracts。

效能實驗包含 padded/unpadded SPSC、spin/yield/hybrid wait、contiguous/boxed storage 同 bitmap。Linux 可以額外比較綁核：

```sh
MINI_PIN=2,3 MINI_SAMPLES=100000 cargo bench --locked --bench latency
```

macOS 唔會假裝提供 Linux strict affinity。Linux affinity 已 cross-compile check；要喺 Linux 實機先可以量度。Queue benchmark 唔包含 journal、Python、REST 或 trading core，唔代表整個 engine latency。見 [experiment notes](docs/experiments.md)。

## Code map

| File | 責任 |
|---|---|
| `src/model.rs` | Typed event、effect、order lifecycle、pending action |
| `src/core.rs` | Single writer、risk reservation、OMS、fills、reconciliation |
| `src/sim.rs` | Rust paper exchange、lost ack、duplicate fill、disconnect |
| `src/journal.rs` | Input-before-effect journal、checksum chain、snapshot、replay |
| `src/telemetry.rs` | Bounded cold-path worker |
| `src/queue.rs` | Experimental SPSC、memory ordering、bitmap、Linux pinning |
| `src/main.rs` | CLI / JSON-lines runtime |
| `python/mininautilus/` | Python bridge、strategy、Binance Testnet gateway |
| `examples/` | Offline replay、live/paper runtime、SQLite PnL analysis |
| `tests/` | Failure scenarios 同跨語言測試 |

五個階段嘅 runnable baseline 已接通。下一步可以揀一個受測試保護嘅瓶頸，逐個替換 clone、allocation、同步 journal 或 transport，再用相同事件檢查行為有冇改變。

## Real-time Python SMA / Spot Testnet

See [the SMA experiment guide](docs/realtime-sma.md) for WebSocket candles, long/flat
execution, bounded Testnet runs, recovery and deterministic replay.

See [order churn and latency experiments](docs/order-stress.md) for cancel/fill races,
versioned target guards, delayed reports and independent venue/journal verification.

## 跨平台 correctness / performance audit

已實際比較 Backtrader、NautilusTrader、vn.py 原生回測引擎，包括逐筆成交、
部分成交／撤單、SMA 訊號、持倉與 PnL，以及不同歷史長度下的效能。
見 [發現、成因、修正方法與實測數字](docs/backtest-comparison.md)，以及
[可重跑的比較工具](comparisons/README.md)。所有案例均為離線合成數據。

Core 與 Journal 的每事件 full clone 已改為 prepare／commit；見
[逐段 code 導讀、全局影響與前後測量](docs/core-state-transitions.md)。

最新驗證：[架構改動後的跨平台結果、adapter 修正及剩餘性能瓶頸](docs/post-refactor-audit.md)。

驗收條件、現狀及量度工具：[docs/acceptance.md](docs/acceptance.md)（`cargo run --release --example acceptance`）。
