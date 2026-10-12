# Soak report: examples/sma_spot.py --mode testnet --runtime actor --receipt /home/trader/repo/target/runtime-v2-build.json --binary /home/trader/repo/target/release/mininautilus --interval 1s --fast 3 --slow 8 --max-orders 100000 --order-ttl 10 --sync outbox --rotate-orders 100 --recovery-drill 1800 --recovery-drill 3600

Window: 5704 s. Quantiles are Prometheus bucket estimates.

## Checks

| Check | Condition | Value | Status |
|---|---|---:|---|
| L1-rust | Rust per-request p99 <= 0.5 ms | 3.841 | FAIL |
| ipc | Python-observed IPC p99 <= 2 ms | 4.047 | FAIL |
| tick-to-trade | Candle receipt -> order p99 <= 5 ms | 483.579 | FAIL |
| drift | Rust request p99 last/first slice < 2 | 1.020 | PASS |
| errors | No REST errors | 0 | PASS |
| gate | Engine healthy >= 99% of the run | 0.921 | FAIL |

## Rust (engine process)

| Metric | count | mean ms | p50 ms | p90 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| request | 18854 | 0.115 | 0.063 | 0.095 | 3.841 |
| response encode | 18854 | 0.005 | 0.005 | 0.009 | 0.013 |
| input stage: commit | 18854 | 0.000 | 0.001 | 0.001 | 0.004 |
| input stage: journal_encode | 18854 | 0.006 | 0.007 | 0.009 | 0.012 |
| input stage: journal_sync | 516 | 2.162 | 3.173 | 4.640 | 4.970 |
| input stage: journal_write | 18854 | 0.017 | 0.017 | 0.043 | 0.049 |
| input stage: prepare | 18854 | 0.001 | 0.001 | 0.003 | 0.005 |

## Python (strategy process, network)

| Metric | count | mean ms | p50 ms | p90 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| ipc | 18852 | 0.236 | 0.148 | 0.362 | 4.047 |
| strategy | 0 | — | — | — | — |
| tick_to_trade | 504 | 56.927 | 37.104 | 86.250 | 483.579 |
| loop | 0 | — | — | — | — |
| ws_event_lag | 5251 | 17.737 | 15.796 | 28.340 | 48.447 |
| candle_close_lag | 0 | — | — | — | — |
| rest_pacing | 9716 | 88.551 | 0.009 | 416.499 | 491.650 |
| REST /api/v3/account | 1007 | 29.715 | 35.090 | 47.162 | 49.878 |
| REST /api/v3/exchangeInfo | 0 | — | — | — | — |
| REST /api/v3/klines | 18 | 28.241 | 35.000 | 47.000 | 49.700 |
| REST /api/v3/myTrades | 1160 | 28.017 | 35.078 | 47.140 | 49.854 |
| REST /api/v3/openOrders | 25 | 27.492 | 35.000 | 47.000 | 49.700 |
| REST /api/v3/order | 2257 | 27.888 | 35.067 | 47.120 | 49.832 |
| REST /api/v3/ticker/bookTicker | 5247 | 27.114 | 35.107 | 47.192 | 49.911 |
| REST /api/v3/time | 0 | — | — | — | — |

## Counts

```json
{
 "rust_events": {
  "QuoteObserved": 5244,
  "MarketUnavailable": 1,
  "SetTarget": 5244,
  "SubmitTargeted": 505,
  "Cancel": 11,
  "Execution": 1095,
  "Tick": 5242,
  "Heartbeat": 1463,
  "Disconnect": 20,
  "Reconnect": 18,
  "Reconcile": 12
 },
 "rust_effects": {
  "SendOrder": 505,
  "SendCancel": 11,
  "QueryState": 18
 },
 "python_events": {
  "candle": 5319,
  "warmup": 7,
  "signal": 5243,
  "order": 504,
  "rotation_pending": 4,
  "coordinator_gated": 6,
  "reconciled": 10,
  "rotated": 4
 },
 "rest_errors": 0,
 "rest_errors_by_status": {},
 "journal_syncs": 516.0675089442301,
 "requests": 18854.46643142757
}
```

## Engine state

```json
{
 "healthy_fraction": 0.9207434683499921,
 "max_retained_orders": 100.0,
 "max_open_orders": 1.0,
 "final_position": null,
 "killed": null
}
```

## Drift (p99 per slice, ms)

```json
{
 "rust_request": {
  "p99_ms_per_slice": [
   3.75708860759493,
   3.7090909090909023,
   3.841538461538461,
   3.840263157894743,
   4.00869565217391,
   3.833076923076924
  ],
  "slice_seconds": 950,
  "last_over_first": 1.0202253189686248
 },
 "python_ipc": {
  "p99_ms_per_slice": [
   4.114361702127662,
   3.984814814814817,
   3.984186046511626,
   4.033647058823531,
   4.135294117647058,
   4.009999999999993
  ],
  "slice_seconds": 950,
  "last_over_first": 0.9746347769877157
 }
}
```

## Rust CPU profile `perf-1.data` (self time)

- 25.2% core::fmt::write                                                                                    -      -
- 22.3% malloc                                                                                              -      -
- 13.5% std::sys::backtrace::__rust_begin_short_backtrace::<mininautilus::metrics::serve::{closure#0}, ()>  -      -
- 12.2% malloc_consolidate                                                                                  -      -
- 12.0% serde_json::ser::format_escaped_str::<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>  -      -
- 12.0% mininautilus::journal::encode_frame                                                                 -      -
- 0.8% <core::fmt::Formatter>::write_formatted_parts                                                       -      -
- 0.8% _int_malloc                                                                                         -      -
- 0.8% <u64 as itoa::Unsigned>::fmt                                                                        -      -

## Rust CPU profile `perf-2.data` (self time)

- 24.8% <serde_json::ser::Compound<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter> as serde_core::ser::SerializeStruct>::serialize_field::<i64>  -      -
- 24.8% <mininautilus::core::transition::Prepared>::commit                                                                                                    -      -
- 23.2% <alloc::string::String as core::fmt::Write>::write_str                                                                                                -      -
- 20.4% core::fmt::float::float_to_decimal_common_shortest::<f64>                                                                                             -      -
- 1.7% _int_free                                                                                                                                             -      -
- 1.7% <mininautilus::journal::DurableEngine>::process_staged                                                                                                -      -
- 1.6% __GI___memcpy_simd                                                                                                                                    -      -
- 1.4% <core::fmt::Formatter>::pad                                                                                                                           -      -

## Rust CPU profile `perf-3.data` (self time)

- 26.7% alloc::io::read::append_to_string::<<alloc::io::buffered::bufreader::BufReader<&std::net::tcp::TcpStream> as alloc::io::buf_read::BufRead>::read_line::{closure#0}>  -      -
- 25.6% malloc@plt                                                                                                                                                           -      -
- 22.1% <mininautilus::journal::Payload as serde_core::ser::Serialize>::serialize::<&mut serde_json::ser::Serializer<&mut alloc::vec::Vec<u8>>>                              -      -
- 18.8% _int_malloc                                                                                                                                                          -      -
- 2.8% __GI___memcpy_simd                                                                                                                                                   -      -
- 1.8% <core::fmt::Formatter>::pad                                                                                                                                          -      -
- 1.8% core::fmt::write                                                                                                                                                     -      -

## Rust CPU profile `perf-4.data` (self time)

- 46.3% mininautilus::process                                                                                                                                                                     -      -
- 27.7% serde_json::ser::format_escaped_str::<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>                                                                                        -      -
- 9.2% <alloc::string::String as core::fmt::Write>::write_str                                                                                                                                    -      -
- 6.5% realloc                                                                                                                                                                                   -      -
- 4.9% <<mininautilus::model::Event as serde_core::de::Deserialize>::deserialize::__Visitor as serde_core::de::Visitor>::visit_enum::<serde_json::de::VariantAccess<serde_json::read::StrRead>>  -      -
- 3.9% __GI___memcpy_simd                                                                                                                                                                        -      -
- 0.6% core::fmt::write                                                                                                                                                                          -      -

## Rust CPU profile `perf-5.data` (self time)

- 34.3% _int_malloc                                                -      -
- 20.6% core::fmt::float::float_to_decimal_common_shortest::<f64>  -      -
- 20.5% malloc                                                     -      -
- 19.1% <mininautilus::core::Core>::prepare                        -      -
- 1.4% __GI___memcpy_simd                                         -      -
- 1.4% <alloc::string::String as core::fmt::Write>::write_str     -      -
- 1.3% <std::fs::File as core::io::write::Write>::write_all       -      -
- 0.9% <mininautilus::journal::DurableEngine>::process_staged     -      -

## Rust CPU profile `perf-6.data` (self time)

- 25.5% <core::fmt::Formatter>::write_formatted_parts              -      -
- 25.2% <mininautilus::core::Core>::prepare                        -      -
- 24.9% <core::fmt::Formatter>::pad_integral::write_prefix         -      -
- 17.6% <mininautilus::core::transition::Transition>::report       -      -
- 1.7% <alloc::string::String as core::fmt::Write>::write_str     -      -
- 1.7% _int_free                                                  -      -
- 1.7% core::fmt::float::float_to_decimal_common_shortest::<f64>  -      -
- 1.2% <mininautilus::journal::DurableEngine>::process_staged     -      -

## Rust CPU profile `perf-7.data` (self time)

- 32.4% <core::fmt::Formatter>::write_formatted_parts                                             -      -
- 30.1% malloc                                                                                    -      -
- 17.8% __GI___memcpy_simd                                                                        -      -
- 16.2% core::num::imp::flt2dec::digits_to_dec_str                                                -      -
- 1.1% core::fmt::write                                                                          -      -
- 1.1% _int_free                                                                                 -      -
- 1.0% __GI___libc_write                                                                         -      -

## Python profile `pyspy-1.txt` (183 samples)

Self:
- 40.4% load_default_certs (ssl.py:597)
- 5.5% close (socket.py:789)
- 5.5% _escape (prometheus_client/openmetrics/exposition.py:228)
- 3.8% do_handshake (ssl.py:1382)
- 2.7% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 2.7% compress (gzip.py:590)
- 2.7% get_next_frame (websockets/sync/messages.py:87)
- 2.2% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:271)
- 2.2% __new__ (ssl.py:500)
- 1.6% read (ssl.py:1166)
- 1.6% _multi_samples (prometheus_client/metrics.py:278)
- 1.6% generate_latest (prometheus_client/openmetrics/exposition.py:152)
- 1.6% _worker (concurrent/futures/thread.py:81)
- 1.1% <lambda> (<string>:1)
- 1.1% _escape (prometheus_client/openmetrics/exposition.py:232)

Inclusive:
- 97.8% run (threading.py:982)
- 97.8% _bootstrap_inner (threading.py:1045)
- 97.8% _bootstrap (threading.py:1002)
- 62.8% request (mininautilus/binance.py:72)
- 53.6% _request (mininautilus/binance.py:103)
- 50.8% _call_chain (urllib/request.py:496)
- 50.8% open (urllib/request.py:519)
- 50.8% _open (urllib/request.py:536)
- 50.8% https_open (urllib/request.py:1391)
- 50.3% quote (mininautilus/binance.py:142)
- 50.3% _market_worker (mininautilus/live_actor.py:343)
- 50.3% quote (mininautilus/binance_live.py:81)
- 43.2% do_open (urllib/request.py:1317)
- 42.6% __init__ (http/client.py:1490)
- 40.4% create_default_context (ssl.py:775)

## Python profile `pyspy-2.txt` (175 samples)

Self:
- 46.3% load_default_certs (ssl.py:597)
- 5.7% _escape (prometheus_client/openmetrics/exposition.py:228)
- 5.7% close (socket.py:789)
- 5.7% do_handshake (ssl.py:1382)
- 4.0% _escape (prometheus_client/openmetrics/exposition.py:232)
- 2.9% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 2.9% _is_valid_legacy_labelname (prometheus_client/validation.py:100)
- 2.3% __new__ (ssl.py:500)
- 2.3% write (socketserver.py:834)
- 2.3% _worker (concurrent/futures/thread.py:81)
- 1.7% read (ssl.py:1166)
- 1.7% read (ssl.py:1168)
- 1.1% decode (json/decoder.py:337)
- 1.1% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:271)
- 1.1% compress (gzip.py:590)

Inclusive:
- 99.4% run (threading.py:982)
- 99.4% _bootstrap_inner (threading.py:1045)
- 99.4% _bootstrap (threading.py:1002)
- 68.0% request (mininautilus/binance.py:72)
- 60.0% _request (mininautilus/binance.py:103)
- 57.7% open (urllib/request.py:519)
- 57.7% _open (urllib/request.py:536)
- 57.7% _call_chain (urllib/request.py:496)
- 57.7% https_open (urllib/request.py:1391)
- 48.6% do_open (urllib/request.py:1317)
- 48.6% __init__ (http/client.py:1490)
- 46.3% create_default_context (ssl.py:775)
- 46.3% load_default_certs (ssl.py:597)
- 37.7% quote (mininautilus/binance.py:142)
- 37.7% _market_worker (mininautilus/live_actor.py:343)

## Python profile `pyspy-3.txt` (220 samples)

Self:
- 44.1% load_default_certs (ssl.py:597)
- 5.5% close (socket.py:789)
- 4.5% _escape (prometheus_client/openmetrics/exposition.py:228)
- 4.5% do_handshake (ssl.py:1382)
- 3.2% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 2.7% read (ssl.py:1166)
- 1.8% getaddrinfo (socket.py:974)
- 1.8% <lambda> (<string>:1)
- 1.8% __new__ (ssl.py:500)
- 1.8% compress (gzip.py:590)
- 1.8% generate_latest (prometheus_client/openmetrics/exposition.py:152)
- 1.4% decode (json/decoder.py:337)
- 1.4% generate_latest (prometheus_client/openmetrics/exposition.py:81)
- 1.4% collect (prometheus_client/metrics.py:91)
- 1.4% _escape (prometheus_client/openmetrics/exposition.py:232)

Inclusive:
- 97.7% _bootstrap (threading.py:1002)
- 96.4% run (threading.py:982)
- 96.4% _bootstrap_inner (threading.py:1045)
- 66.8% request (mininautilus/binance.py:72)
- 58.6% _request (mininautilus/binance.py:103)
- 56.4% open (urllib/request.py:519)
- 56.4% _open (urllib/request.py:536)
- 56.4% _call_chain (urllib/request.py:496)
- 56.4% https_open (urllib/request.py:1391)
- 46.4% do_open (urllib/request.py:1317)
- 45.9% __init__ (http/client.py:1490)
- 44.1% quote (mininautilus/binance.py:142)
- 44.1% _market_worker (mininautilus/live_actor.py:343)
- 44.1% quote (mininautilus/binance_live.py:81)
- 44.1% create_default_context (ssl.py:775)

## Python profile `pyspy-4.txt` (212 samples)

Self:
- 35.4% load_default_certs (ssl.py:597)
- 9.0% do_handshake (ssl.py:1382)
- 4.7% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 4.2% compress (gzip.py:590)
- 3.3% close (socket.py:789)
- 2.8% __new__ (ssl.py:500)
- 2.4% _escape (prometheus_client/openmetrics/exposition.py:228)
- 1.9% <listcomp> (prometheus_client/openmetrics/exposition.py:82)
- 1.9% read (ssl.py:1166)
- 1.9% _escape (prometheus_client/openmetrics/exposition.py:232)
- 1.4% _multi_samples (prometheus_client/metrics.py:278)
- 1.4% getaddrinfo (socket.py:974)
- 1.4% _escape (prometheus_client/openmetrics/exposition.py:224)
- 1.4% generate_latest (prometheus_client/openmetrics/exposition.py:81)
- 0.9% write (socketserver.py:834)

Inclusive:
- 98.1% _bootstrap (threading.py:1002)
- 97.2% run (threading.py:982)
- 97.2% _bootstrap_inner (threading.py:1045)
- 60.4% request (mininautilus/binance.py:72)
- 53.3% _request (mininautilus/binance.py:103)
- 52.8% _call_chain (urllib/request.py:496)
- 52.8% _open (urllib/request.py:536)
- 52.8% https_open (urllib/request.py:1391)
- 52.4% open (urllib/request.py:519)
- 38.7% do_open (urllib/request.py:1317)
- 38.7% __init__ (http/client.py:1490)
- 37.3% quote (mininautilus/binance.py:142)
- 37.3% _market_worker (mininautilus/live_actor.py:343)
- 37.3% quote (mininautilus/binance_live.py:81)
- 35.4% create_default_context (ssl.py:775)

## Python profile `pyspy-5.txt` (193 samples)

Self:
- 43.0% load_default_certs (ssl.py:597)
- 4.7% do_handshake (ssl.py:1382)
- 3.6% _escape (prometheus_client/openmetrics/exposition.py:232)
- 3.6% _escape (prometheus_client/openmetrics/exposition.py:228)
- 3.1% close (socket.py:789)
- 2.6% getaddrinfo (socket.py:974)
- 2.6% __new__ (ssl.py:500)
- 2.6% <listcomp> (prometheus_client/openmetrics/exposition.py:82)
- 2.6% _worker (concurrent/futures/thread.py:81)
- 2.1% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 2.1% create_connection (socket.py:848)
- 1.6% _escape (prometheus_client/openmetrics/exposition.py:229)
- 1.6% compress (gzip.py:590)
- 1.6% decode (json/decoder.py:337)
- 1.0% <lambda> (<string>:1)

Inclusive:
- 97.9% _bootstrap_inner (threading.py:1045)
- 97.9% _bootstrap (threading.py:1002)
- 97.9% run (threading.py:982)
- 62.2% request (mininautilus/binance.py:72)
- 57.5% _call_chain (urllib/request.py:496)
- 57.5% open (urllib/request.py:519)
- 57.5% _request (mininautilus/binance.py:103)
- 57.5% _open (urllib/request.py:536)
- 57.5% https_open (urllib/request.py:1391)
- 48.7% quote (mininautilus/binance.py:142)
- 48.7% quote (mininautilus/binance_live.py:81)
- 48.7% _market_worker (mininautilus/live_actor.py:343)
- 45.6% do_open (urllib/request.py:1317)
- 45.6% __init__ (http/client.py:1490)
- 43.0% create_default_context (ssl.py:775)

## Python profile `pyspy-6.txt` (202 samples)

Self:
- 39.1% load_default_certs (ssl.py:597)
- 5.0% do_handshake (ssl.py:1382)
- 4.5% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 4.0% close (socket.py:789)
- 2.5% _worker (concurrent/futures/thread.py:81)
- 2.5% _escape (prometheus_client/openmetrics/exposition.py:228)
- 2.0% compress (gzip.py:590)
- 2.0% generate_latest (prometheus_client/openmetrics/exposition.py:152)
- 2.0% read (ssl.py:1166)
- 2.0% _multi_samples (prometheus_client/metrics.py:278)
- 2.0% generate_latest (prometheus_client/openmetrics/exposition.py:81)
- 2.0% _write_to_self (asyncio/selector_events.py:139)
- 1.5% <lambda> (<string>:1)
- 1.5% _child_samples (prometheus_client/metrics.py:699)
- 1.5% __new__ (ssl.py:500)

Inclusive:
- 97.5% run (threading.py:982)
- 97.5% _bootstrap_inner (threading.py:1045)
- 97.5% _bootstrap (threading.py:1002)
- 59.4% request (mininautilus/binance.py:72)
- 53.0% _request (mininautilus/binance.py:103)
- 51.0% https_open (urllib/request.py:1391)
- 50.5% _call_chain (urllib/request.py:496)
- 50.5% open (urllib/request.py:519)
- 50.5% _open (urllib/request.py:536)
- 41.1% do_open (urllib/request.py:1317)
- 40.6% __init__ (http/client.py:1490)
- 40.1% quote (mininautilus/binance.py:142)
- 40.1% _market_worker (mininautilus/live_actor.py:343)
- 40.1% quote (mininautilus/binance_live.py:81)
- 39.1% create_default_context (ssl.py:775)

## Python profile `pyspy-7.txt` (201 samples)

Self:
- 41.8% load_default_certs (ssl.py:597)
- 9.5% do_handshake (ssl.py:1382)
- 6.0% close (socket.py:789)
- 3.5% _escape (prometheus_client/openmetrics/exposition.py:228)
- 3.5% compress (gzip.py:590)
- 3.0% __new__ (ssl.py:500)
- 2.5% read (ssl.py:1166)
- 2.0% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:267)
- 2.0% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 1.5% _is_valid_legacy_labelname (prometheus_client/validation.py:100)
- 1.5% _worker (concurrent/futures/thread.py:81)
- 1.0% create_connection (socket.py:848)
- 1.0% loads (json/__init__.py:341)
- 1.0% <lambda> (<string>:1)
- 1.0% generate_latest (prometheus_client/openmetrics/exposition.py:152)

Inclusive:
- 96.0% _bootstrap_inner (threading.py:1045)
- 96.0% _bootstrap (threading.py:1002)
- 96.0% run (threading.py:982)
- 71.6% request (mininautilus/binance.py:72)
- 61.7% _request (mininautilus/binance.py:103)
- 59.7% _call_chain (urllib/request.py:496)
- 59.7% open (urllib/request.py:519)
- 59.7% _open (urllib/request.py:536)
- 59.7% https_open (urllib/request.py:1391)
- 44.8% do_open (urllib/request.py:1317)
- 44.8% __init__ (http/client.py:1490)
- 43.3% quote (mininautilus/binance_live.py:81)
- 43.3% _market_worker (mininautilus/live_actor.py:343)
- 42.3% quote (mininautilus/binance.py:142)
- 41.8% create_default_context (ssl.py:775)
