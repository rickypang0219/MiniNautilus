# Live Testnet soak — 2026-10-11, Tokyo EC2

Host: stack `MiniNautilusHost`, t4g.micro (Graviton, 1 GiB), gp3, ap-northeast-1. Source `bb41137`; `scripts/verify.sh` → VERIFY OK on the host.
Command: `ops/soak.py --minutes 90 -- examples/sma_spot.py --mode testnet --interval 1s --fast 3 --slow 8 --max-orders 3000 --order-ttl 10 --sync outbox`.
All candles, quotes, orders, fills and order states came from Binance Spot Testnet (REST polling for private state). Single run: numbers are observations, not repeated evidence.

## Outcome

5,747 s; 5,400 closed 1 s candles; 458 orders, all acknowledged; 456 fills; 0 REST errors; 0 reconnects; 2 backfilled bars; shutdown reconciliation succeeded, final position 0. Raw report: `comparisons/results/live-testnet-20261011/`; full artifacts in the stack bucket `runs/live-testnet-90m.tgz`.

| Metric | p50 | p99 |
|---|---:|---:|
| Binance REST round trip (all endpoints) | ~35 ms | ~50 ms |
| Client-side REST pacing wait | 103 ms | 491 ms |
| `venue.submit` incl. pacing (per order trace) | 233 ms | 254 ms |
| Strategy decision → SendOrder effect | 256 ms | 499 ms |
| Candle receipt → order handed to venue | 500 ms | 990 ms |
| Candle close → receipt | 657 ms | 1,967 ms |
| WebSocket event lag | 16 ms | 765 ms |
| Rust request (all) | 0.34 ms | 3.6 ms |
| Order-path fsync | 2.1 ms | 5.8 ms |
| Python IPC | 0.36 ms | 4.1 ms |

Drift (Rust p99 last/first of six slices): 1.14, PASS. RSS: Rust 1.2 → 4.5 MB, Python 31 → 36 MB over 458 orders (no rotation).

## Findings (by impact)

1. **REST pacing dominates tick-to-trade.** `BinanceSpot.request` waits a fixed 0.25 s between calls and blocks the coordinator. Each candle runs order polling, account check, bookTicker and submit in series, so latency is ~0.25 s × calls. Binance itself answers in ~35 ms. Rust and fsync are <1% of the path.
2. **Shutdown/recovery reconciliation took 344 s.** It queries every session order (cancel lookup, order, myTrades: ~3 calls × 458 orders × 0.25 s). Recovery time grows with session history while trading is gated; this is the 6% "unhealthy" in the gate check.
3. **`audit()` in sma_spot.py is O(orders × fills) per request.** py-spy: ~75% of Python CPU samples. It grows quadratically with session size.
4. Candle receipt lags close by ~0.66 s median because the loop is busy in REST when the kline arrives; the WebSocket itself is ~16 ms.
5. Order-path fsync (p50 2.1 ms, pending 8 KB median) is real but small next to items 1–4 on this host.
