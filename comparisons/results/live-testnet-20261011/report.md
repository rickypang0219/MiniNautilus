# Soak report: examples/sma_spot.py --mode testnet --interval 1s --fast 3 --slow 8 --max-orders 3000 --order-ttl 10 --sync outbox

Window: 5744 s. Quantiles are Prometheus bucket estimates.

## Checks

| Check | Condition | Value | Status |
|---|---|---:|---|
| L1-rust | Rust per-request p99 <= 0.5 ms | 3.598 | FAIL |
| ipc | Python-observed IPC p99 <= 2 ms | 4.114 | FAIL |
| tick-to-trade | Candle receipt -> order p99 <= 5 ms | 990.000 | FAIL |
| drift | Rust request p99 last/first slice < 2 | 1.142 | PASS |
| errors | No REST errors | 0 | PASS |
| gate | Engine healthy >= 99% of the run | 0.940 | FAIL |

## Rust (engine process)

| Metric | count | mean ms | p50 ms | p90 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| request | 22456 | 0.271 | 0.335 | 0.473 | 3.598 |
| response encode | 22456 | 0.054 | 0.063 | 0.094 | 0.155 |
| input stage: commit | 22456 | 0.001 | 0.001 | 0.001 | 0.018 |
| input stage: journal_encode | 22456 | 0.058 | 0.065 | 0.093 | 0.100 |
| input stage: journal_sync | 460 | 2.202 | 3.347 | 4.723 | 7.444 |
| input stage: journal_write | 22456 | 0.024 | 0.029 | 0.046 | 0.050 |
| input stage: prepare | 22456 | 0.008 | 0.009 | 0.017 | 0.020 |

## Python (strategy process, network)

| Metric | count | mean ms | p50 ms | p90 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| ipc | 22455 | 0.450 | 0.356 | 0.485 | 4.114 |
| strategy | 5396 | 0.009 | 0.005 | 0.009 | 0.018 |
| tick_to_trade | 458 | 454.857 | 500.000 | 900.000 | 990.000 |
| loop | 5396 | 560.359 | 515.638 | 968.619 | 1861.667 |
| ws_event_lag | 5396 | 44.772 | 15.979 | 52.329 | 764.655 |
| candle_close_lag | 5396 | 644.127 | 657.419 | 1666.357 | 1966.636 |
| rest_pacing | 11719 | 103.366 | 103.217 | 407.449 | 490.745 |
| REST /api/v3/account | 3545 | 30.001 | 35.085 | 47.153 | 49.869 |
| REST /api/v3/exchangeInfo | 0 | — | — | — | — |
| REST /api/v3/klines | 0 | — | — | — | — |
| REST /api/v3/myTrades | 927 | 28.240 | 35.065 | 47.117 | 49.829 |
| REST /api/v3/openOrders | 1 | 29.552 | 35.000 | 47.000 | 49.700 |
| REST /api/v3/order | 1845 | 28.030 | 35.041 | 47.073 | 49.781 |
| REST /api/v3/ticker/bookTicker | 5396 | 27.188 | 35.064 | 47.116 | 49.827 |
| REST /api/v3/time | 0 | — | — | — | — |

## Counts

```json
{
 "rust_events": {
  "QuoteObserved": 5396,
  "MarketUnavailable": 1,
  "SetTarget": 5396,
  "SubmitTargeted": 458,
  "Cancel": 2,
  "Execution": 928,
  "Tick": 6957,
  "Heartbeat": 3316,
  "Disconnect": 1,
  "Reconnect": 1
 },
 "rust_effects": {
  "SendOrder": 458,
  "SendCancel": 2,
  "QueryState": 1
 },
 "python_events": {
  "candle": 5396,
  "signal": 5396,
  "order": 457,
  "order_delivery": 457
 },
 "rest_errors": 0,
 "rest_errors_by_status": {},
 "journal_syncs": 460.04005572971096,
 "requests": 22456.286268199234
}
```

## Engine state

```json
{
 "healthy_fraction": 0.9399268674908585,
 "max_retained_orders": 458.0,
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
   3.441948051948053,
   3.5426315789473675,
   3.541538461538461,
   3.4552000000000067,
   3.7117241379310415,
   3.9322727272727382
  ],
  "slice_seconds": 957,
  "last_over_first": 1.1424555710674291
 },
 "python_ipc": {
  "p99_ms_per_slice": [
   3.9213483146067287,
   4.116704545454549,
   4.098064516129036,
   3.9648192771084285,
   4.158404255319156,
   4.524999999999991
  ],
  "slice_seconds": 957,
  "last_over_first": 1.1539398280802307
 }
}
```

## Rust CPU profile `perf-1.data` (self time)

- 9.9% __GI___memcpy_simd                                                                                                                                                                                                                                                                                         -      -
- 9.1% serde_json::ser::format_escaped_str_contents::<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>                                                                                                                                                                                                -      -
- 6.2% <alloc::vec::Vec<u8> as alloc::vec::spec_extend::SpecExtend<&u8, core::slice::iter::Iter<u8>>>::spec_extend                                                                                                                                                                                                -      -
- 6.0% <mininautilus::model::Event as serde_core::ser::Serialize>::serialize::<&mut serde_json::ser::Serializer<&mut alloc::vec::Vec<u8>>>                                                                                                                                                                        -      -
- 5.2% <mininautilus::journal::DurableEngine>::process_profiled                                                                                                                                                                                                                                                   -      -
- 4.3% alloc::io::buf_read::default_read_until::<alloc::io::buffered::bufreader::BufReader<std::io::stdio::StdinRaw>>                                                                                                                                                                                             -      -
- 4.2% <[_]>::split_at_unchecked::precondition_check                                                                                                                                                                                                                                                              -      -
- 3.4% <u64 as itoa::Unsigned>::fmt                                                                                                                                                                                                                                                                               -      -
- 3.2% core::slice::raw::from_raw_parts::precondition_check                                                                                                                                                                                                                                                       -      -
- 3.0% core::ptr::copy_nonoverlapping::precondition_check                                                                                                                                                                                                                                                         -      -
- 2.2% <&mut serde_json::ser::Serializer<&mut alloc::vec::Vec<u8>> as serde_core::ser::Serializer>::serialize_map                                                                                                                                                                                                 -      -
- 1.9% <*const ()>::is_aligned_to                                                                                                                                                                                                                                                                                 -      -
- 1.7% <*const ()>::is_aligned_to                                                                                                                                                                                                                                                                                 -      -
- 1.7% <<mininautilus::journal::EventTime as serde_core::de::Deserialize>::deserialize::__Visitor as serde_core::de::Visitor>::visit_map::<serde_json::de::MapAccess<serde_json::read::StrRead>>                                                                                                                  -      -
- 1.7% <[u8]>::split_first                                                                                                                                                                                                                                                                                        -      -

## Rust CPU profile `perf-2.data` (self time)

- 13.0% core::slice::raw::from_raw_parts::precondition_check                                                                                              -      -
- 11.5% __GI___memcpy_simd                                                                                                                                -      -
- 7.0% <&mut serde_json::ser::Serializer<&mut alloc::vec::Vec<u8>> as serde_core::ser::Serializer>::serialize_struct                                     -      -
- 7.0% <alloc::vec::into_iter::IntoIter<mininautilus::model::Event> as core::iter::traits::iterator::Iterator>::next                                     -      -
- 7.0% <core::option::Option<&mut mininautilus::journal::Stages>>::as_deref_mut                                                                          -      -
- 6.8% <alloc::vec::Vec<u8> as alloc::vec::spec_extend::SpecExtend<&u8, core::slice::iter::Iter<u8>>>::spec_extend                                       -      -
- 6.7% <alloc::string::String>::push_str                                                                                                                 -      -
- 6.5% <[u8]>::chunks_exact                                                                                                                              -      -
- 6.5% <alloc::raw_vec::RawVecInner>::with_capacity_in                                                                                                   -      -
- 6.5% <str as serde_core::ser::Serialize>::serialize::<serde_json::ser::MapKeySerializer<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>>  -      -
- 6.2% <serde_json::ser::State as core::cmp::PartialEq>::eq                                                                                              -      -
- 6.1% <[u8]>::split_first                                                                                                                               -      -
- 0.9% <*const _>::offset_from_unsigned::precondition_check                                                                                              -      -
- 0.9% <alloc::raw_vec::RawVec<u8> as core::ops::drop::Drop>::drop                                                                                       -      -
- 0.9% <alloc::sync::Arc<mininautilus::metrics::Metrics> as core::ops::deref::Deref>::deref                                                              -      -

## Rust CPU profile `perf-3.data` (self time)

- 11.9% serde_json::ser::format_escaped_str_contents::<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>                                                                                                                                                                        -      -
- 7.6% <alloc::collections::btree::map::IntoIter<u64, u64>>::dying_next                                                                                                                                                                                                                   -      -
- 7.6% <core::option::Option<mininautilus::model::Event>>::or_else::<<core::iter::adapters::chain::Chain<core::option::IntoIter<mininautilus::model::Event>, alloc::vec::into_iter::IntoIter<mininautilus::model::Event>> as core::iter::traits::iterator::Iterator>::next::{closure#0}>  -      -
- 7.6% core::slice::raw::from_raw_parts::precondition_check                                                                                                                                                                                                                               -      -
- 7.6% malloc                                                                                                                                                                                                                                                                             -      -
- 7.6% memcmp                                                                                                                                                                                                                                                                             -      -
- 7.2% <mininautilus::journal::DurableEngine>::process_staged                                                                                                                                                                                                                             -      -
- 6.0% <*const ()>::is_aligned_to                                                                                                                                                                                                                                                         -      -
- 5.1% <alloc::collections::btree::map::BTreeMap<u64, &mininautilus::model::Fill> as core::ops::drop::Drop>::drop                                                                                                                                                                         -      -
- 5.1% core::ptr::copy_nonoverlapping::precondition_check                                                                                                                                                                                                                                 -      -
- 4.7% <*const _>::offset_from_unsigned::precondition_check                                                                                                                                                                                                                               -      -
- 4.7% <<mininautilus::Request as serde_core::de::Deserialize>::deserialize::__Visitor as serde_core::de::Visitor>::visit_map::<serde_json::de::MapAccess<serde_json::read::StrRead>>                                                                                                     -      -
- 4.7% <core::ops::control_flow::ControlFlow<()> as core::cmp::PartialEq>::eq                                                                                                                                                                                                             -      -
- 3.5% core::fmt::write                                                                                                                                                                                                                                                                   -      -
- 2.7% <alloc::vec::Vec<u8>>::append_elements                                                                                                                                                                                                                                             -      -

## Rust CPU profile `perf-4.data` (self time)

- 16.2% serde_json::ser::format_escaped_str_contents::<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>                                                                                           -      -
- 9.1% <&mut serde_json::de::Deserializer<serde_json::read::StrRead> as serde_core::de::Deserializer>::deserialize_struct::<<mininautilus::Request as serde_core::de::Deserialize>::deserialize::__Visitor>  -      -
- 9.1% <itoa::Buffer>::format::<i64>                                                                                                                                                                         -      -
- 9.1% core::ptr::copy_nonoverlapping::precondition_check                                                                                                                                                    -      -
- 9.1% core::slice::raw::from_raw_parts::precondition_check                                                                                                                                                  -      -
- 8.4% malloc_consolidate                                                                                                                                                                                    -      -
- 7.1% <[u8]>::split_at                                                                                                                                                                                      -      -
- 7.1% core::ptr::drop_glue::<[mininautilus::model::Event]>                                                                                                                                                  -      -
- 7.1% core::ptr::drop_glue::<alloc::collections::btree::map::IntoIter<u64, &mininautilus::model::Fill>>                                                                                                     -      -
- 4.4% <serde_json::ser::Compound<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter> as serde_core::ser::SerializeMap>::serialize_value::<&core::option::Option<mininautilus::model::Target>>      -      -
- 3.5% core::slice::raw::from_raw_parts::precondition_check                                                                                                                                                  -      -
- 1.7% <alloc::vec::Vec<u8>>::append_elements                                                                                                                                                                -      -
- 1.7% <core::fmt::Formatter>::pad_integral                                                                                                                                                                  -      -
- 1.7% __GI___memcpy_simd                                                                                                                                                                                    -      -
- 1.7% core::fmt::write                                                                                                                                                                                      -      -

## Rust CPU profile `perf-5.data` (self time)

- 12.4% <alloc::vec::Vec<u8>>::append_elements                                                                                                                                                                                                                         -      -
- 7.8% <<mininautilus::model::Event as serde_core::de::Deserialize>::deserialize::__Visitor as serde_core::de::Visitor>::visit_enum::<serde_json::de::VariantAccess<serde_json::read::StrRead>>                                                                       -      -
- 7.8% <serde_json::de::MapAccess<serde_json::read::StrRead> as serde_core::de::MapAccess>::next_value::<core::option::Option<mininautilus::model::Event>>                                                                                                            -      -
- 7.8% <u64 as itoa::Unsigned>::fmt                                                                                                                                                                                                                                   -      -
- 7.7% <core::result::Result<(), serde_json::error::Error> as core::ops::try_trait::Try>::from_output                                                                                                                                                                 -      -
- 7.7% mininautilus::metrics::event_kind                                                                                                                                                                                                                              -      -
- 7.5% <&mut serde_json::de::Deserializer<serde_json::read::StrRead> as serde_core::de::Deserializer>::deserialize_struct::<<<mininautilus::model::Event as serde_core::de::Deserialize>::deserialize::__Visitor as serde_core::de::Visitor>::visit_enum::__Visitor>  -      -
- 7.5% <core::option::Option<mininautilus::model::Target> as serde_core::ser::Serialize>::serialize::<&mut serde_json::ser::Serializer<&mut alloc::vec::Vec<u8>>>                                                                                                     -      -
- 7.5% <serde_json::ser::Compound<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter> as serde_core::ser::SerializeMap>::serialize_value::<&core::option::Option<mininautilus::model::Target>>                                                               -      -
- 7.5% core::ptr::drop_glue::<alloc::vec::set_len_on_drop::SetLenOnDrop>                                                                                                                                                                                              -      -
- 7.5% core::ptr::drop_glue::<mininautilus::journal::RecordedInput>                                                                                                                                                                                                   -      -
- 7.5% core::ub_checks::maybe_is_nonoverlapping::runtime                                                                                                                                                                                                              -      -
- 1.0% <[u8]>::split_at_unchecked                                                                                                                                                                                                                                     -      -
- 0.9% <alloc::string::String>::push_str                                                                                                                                                                                                                              -      -
- 0.9% serde_json::ser::format_escaped_str_contents::<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>                                                                                                                                                    -      -

## Rust CPU profile `perf-6.data` (self time)

- 20.5% serde_json::ser::format_escaped_str_contents::<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>                                                                                                  -      -
- 9.2% <core::fmt::Formatter>::pad                                                                                                                                                                                  -      -
- 8.1% core::ptr::drop_glue::<serde_json::de::Deserializer<serde_json::read::StrRead>>                                                                                                                              -      -
- 7.6% <serde_json::read::StrRead as serde_json::read::Read>::parse_str                                                                                                                                             -      -
- 7.2% <&mininautilus::model::Envelope as serde_core::ser::Serialize>::serialize::<serde::private::ser::FlatMapSerializer<serde_json::ser::Compound<&mut alloc::vec::Vec<u8>, serde_json::ser::CompactFormatter>>>  -      -
- 7.2% <alloc::collections::btree::map::IntoIter<u64, alloc::collections::btree::set_val::SetValZST>>::dying_next                                                                                                   -      -
- 7.2% <mininautilus::protocol::Delta as serde_core::ser::Serialize>::serialize::<&mut serde_json::ser::Serializer<&mut alloc::vec::Vec<u8>>>                                                                       -      -
- 6.8% <*const ()>::is_aligned_to                                                                                                                                                                                   -      -
- 6.8% <core::result::Result<alloc::string::String, core::io::error::Error> as core::ops::try_trait::Try>::branch                                                                                                   -      -
- 6.8% core::ptr::copy_nonoverlapping::precondition_check                                                                                                                                                           -      -
- 2.0% <*const ()>::is_aligned_to                                                                                                                                                                                   -      -
- 1.9% __GI___memcpy_simd                                                                                                                                                                                           -      -
- 1.9% core::ub_checks::maybe_is_nonoverlapping::runtime                                                                                                                                                            -      -
- 1.0% <i32 as core::convert::TryInto<u64>>::try_into                                                                                                                                                               -      -
- 1.0% mininautilus::metrics::event_kind                                                                                                                                                                            -      -

## Rust CPU profile `perf-7.data` (self time)

- 34.9% <*const ()>::is_aligned_to                                                 -      -
- 18.8% core::ptr::copy_nonoverlapping::precondition_check                         -      -
- 13.4% <*const _>::offset_from_unsigned::precondition_check                       -      -
- 13.4% <alloc::string::String>::push_str                                          -      -
- 13.4% <alloc::vec::Vec<u8>>::append_elements                                     -      -
- 5.3% <alloc::raw_vec::RawVec<u8> as core::ops::drop::Drop>::drop                -      -

## Python profile `pyspy-1.txt` (194 samples)

Self:
- 39.2% load_default_certs (ssl.py:597)
- 8.8% do_handshake (ssl.py:1382)
- 5.2% close (socket.py:789)
- 4.6% getaddrinfo (socket.py:974)
- 4.1% _escape (prometheus_client/openmetrics/exposition.py:228)
- 3.1% _roundtrip (mininautilus/bridge.py:126)
- 3.1% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 3.1% read (ssl.py:1166)
- 2.1% _escape (prometheus_client/openmetrics/exposition.py:227)
- 1.5% add_handler (urllib/request.py:444)
- 1.5% decode (json/decoder.py:337)
- 1.5% _child_samples (prometheus_client/metrics.py:699)
- 1.5% _escape (prometheus_client/openmetrics/exposition.py:232)
- 1.0% _real_close (ssl.py:1372)
- 1.0% set_raw (email/message.py:518)

Inclusive:
- 74.2% <module> (sma_spot.py:369)
- 61.9% request (mininautilus/binance.py:96)
- 58.8% https_open (urllib/request.py:1391)
- 58.8% _call_chain (urllib/request.py:496)
- 58.2% _open (urllib/request.py:536)
- 58.2% open (urllib/request.py:519)
- 45.4% quote (mininautilus/binance.py:135)
- 45.4% run (sma_spot.py:302)
- 40.2% do_open (urllib/request.py:1317)
- 40.2% __init__ (http/client.py:1490)
- 39.2% load_default_certs (ssl.py:597)
- 38.7% create_default_context (ssl.py:775)
- 23.7% _bootstrap (threading.py:1002)
- 23.7% run (threading.py:982)
- 23.7% _bootstrap_inner (threading.py:1045)

## Python profile `pyspy-2.txt` (327 samples)

Self:
- 37.0% load_default_certs (ssl.py:597)
- 9.2% <genexpr> (sma_spot.py:61)
- 6.4% do_handshake (ssl.py:1382)
- 4.9% close (socket.py:789)
- 3.7% compress (gzip.py:590)
- 2.8% read (ssl.py:1166)
- 2.1% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 2.1% _escape (prometheus_client/openmetrics/exposition.py:228)
- 1.8% raw_decode (json/decoder.py:353)
- 1.8% get_next_frame (websockets/sync/messages.py:87)
- 1.5% create_connection (socket.py:848)
- 1.5% _roundtrip (mininautilus/bridge.py:126)
- 1.5% __new__ (ssl.py:500)
- 1.5% audit (sma_spot.py:61)
- 1.2% __iter__ (<frozen _collections_abc>:861)

Inclusive:
- 77.4% <module> (sma_spot.py:369)
- 53.2% request (mininautilus/binance.py:96)
- 52.0% https_open (urllib/request.py:1391)
- 51.4% _open (urllib/request.py:536)
- 51.4% open (urllib/request.py:519)
- 51.4% _call_chain (urllib/request.py:496)
- 38.8% do_open (urllib/request.py:1317)
- 38.8% __init__ (http/client.py:1490)
- 37.0% load_default_certs (ssl.py:597)
- 37.0% create_default_context (ssl.py:775)
- 31.5% quote (mininautilus/binance.py:135)
- 31.5% run (sma_spot.py:302)
- 21.1% _bootstrap (threading.py:1002)
- 21.1% run (threading.py:982)
- 21.1% _bootstrap_inner (threading.py:1045)

## Python profile `pyspy-3.txt` (325 samples)

Self:
- 35.1% load_default_certs (ssl.py:597)
- 26.5% <genexpr> (sma_spot.py:61)
- 5.5% do_handshake (ssl.py:1382)
- 2.8% close (socket.py:789)
- 1.8% _escape (prometheus_client/openmetrics/exposition.py:228)
- 1.8% read (ssl.py:1166)
- 1.8% __new__ (ssl.py:500)
- 1.5% compress (gzip.py:590)
- 1.5% decode (json/decoder.py:337)
- 1.5% generate_latest (prometheus_client/openmetrics/exposition.py:81)
- 1.2% _roundtrip (mininautilus/bridge.py:126)
- 1.2% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:271)
- 0.9% _escape (prometheus_client/openmetrics/exposition.py:229)
- 0.9% _child_samples (prometheus_client/metrics.py:698)
- 0.9% _escape (prometheus_client/openmetrics/exposition.py:224)

Inclusive:
- 80.6% <module> (sma_spot.py:369)
- 46.2% request (mininautilus/binance.py:96)
- 45.2% https_open (urllib/request.py:1391)
- 45.2% _open (urllib/request.py:536)
- 45.2% open (urllib/request.py:519)
- 45.2% _call_chain (urllib/request.py:496)
- 36.9% do_open (urllib/request.py:1317)
- 36.9% __init__ (http/client.py:1490)
- 35.1% load_default_certs (ssl.py:597)
- 35.1% create_default_context (ssl.py:775)
- 28.3% quote (mininautilus/binance.py:135)
- 28.3% run (sma_spot.py:302)
- 26.5% <genexpr> (sma_spot.py:61)
- 25.2% audit (sma_spot.py:61)
- 25.2% send (sma_spot.py:160)

## Python profile `pyspy-4.txt` (522 samples)

Self:
- 48.7% <genexpr> (sma_spot.py:61)
- 23.8% load_default_certs (ssl.py:597)
- 2.9% do_handshake (ssl.py:1382)
- 1.9% close (socket.py:789)
- 1.7% audit (sma_spot.py:61)
- 1.3% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 1.3% __new__ (ssl.py:500)
- 0.8% _escape (prometheus_client/openmetrics/exposition.py:228)
- 0.8% _escape (prometheus_client/openmetrics/exposition.py:229)
- 0.8% generate_latest (prometheus_client/openmetrics/exposition.py:81)
- 0.8% _close_conn (http/client.py:437)
- 0.8% read (ssl.py:1166)
- 0.6% decode (json/decoder.py:337)
- 0.6% _escape (prometheus_client/openmetrics/exposition.py:232)
- 0.6% _roundtrip (mininautilus/bridge.py:126)

Inclusive:
- 87.4% <module> (sma_spot.py:369)
- 48.9% send (sma_spot.py:160)
- 48.7% <genexpr> (sma_spot.py:61)
- 47.9% audit (sma_spot.py:61)
- 31.6% request (mininautilus/binance.py:96)
- 30.7% https_open (urllib/request.py:1391)
- 30.7% _call_chain (urllib/request.py:496)
- 30.3% _open (urllib/request.py:536)
- 30.3% open (urllib/request.py:519)
- 25.5% do_open (urllib/request.py:1317)
- 25.5% __init__ (http/client.py:1490)
- 23.8% load_default_certs (ssl.py:597)
- 23.8% create_default_context (ssl.py:775)
- 18.2% run (sma_spot.py:302)
- 18.0% quote (mininautilus/binance.py:135)

## Python profile `pyspy-5.txt` (691 samples)

Self:
- 64.1% <genexpr> (sma_spot.py:61)
- 14.0% load_default_certs (ssl.py:597)
- 2.7% do_handshake (ssl.py:1382)
- 2.3% close (socket.py:789)
- 1.4% audit (sma_spot.py:61)
- 0.9% _escape (prometheus_client/openmetrics/exposition.py:228)
- 0.7% _escape (prometheus_client/openmetrics/exposition.py:232)
- 0.7% read (ssl.py:1166)
- 0.7% __new__ (ssl.py:500)
- 0.6% _escape (prometheus_client/openmetrics/exposition.py:224)
- 0.6% generate_latest (prometheus_client/openmetrics/exposition.py:81)
- 0.6% decode (json/decoder.py:337)
- 0.4% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:271)
- 0.4% _is_valid_legacy_labelname (prometheus_client/validation.py:100)
- 0.4% _escape (prometheus_client/openmetrics/exposition.py:229)

Inclusive:
- 89.4% <module> (sma_spot.py:369)
- 64.1% send (sma_spot.py:160)
- 64.1% <genexpr> (sma_spot.py:61)
- 62.7% audit (sma_spot.py:61)
- 19.4% request (mininautilus/binance.py:96)
- 19.4% run (sma_spot.py:246)
- 19.2% https_open (urllib/request.py:1391)
- 19.1% _open (urllib/request.py:536)
- 19.1% open (urllib/request.py:519)
- 19.1% _call_chain (urllib/request.py:496)
- 18.1% run (sma_spot.py:303)
- 15.1% do_open (urllib/request.py:1317)
- 14.9% __init__ (http/client.py:1490)
- 14.0% load_default_certs (ssl.py:597)
- 14.0% create_default_context (ssl.py:775)

## Python profile `pyspy-6.txt` (933 samples)

Self:
- 76.1% <genexpr> (sma_spot.py:61)
- 8.8% load_default_certs (ssl.py:597)
- 1.5% audit (sma_spot.py:61)
- 1.3% do_handshake (ssl.py:1382)
- 1.1% close (socket.py:789)
- 0.8% __new__ (ssl.py:500)
- 0.6% _roundtrip (mininautilus/bridge.py:126)
- 0.5% _escape (prometheus_client/openmetrics/exposition.py:228)
- 0.5% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 0.4% <listcomp> (prometheus_client/openmetrics/exposition.py:82)
- 0.4% generate_latest (prometheus_client/openmetrics/exposition.py:81)
- 0.4% getaddrinfo (socket.py:974)
- 0.3% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:271)
- 0.3% create_connection (socket.py:848)
- 0.3% _escape (prometheus_client/openmetrics/exposition.py:229)

Inclusive:
- 91.2% <module> (sma_spot.py:369)
- 76.1% <genexpr> (sma_spot.py:61)
- 75.2% send (sma_spot.py:160)
- 74.6% audit (sma_spot.py:61)
- 22.6% run (sma_spot.py:246)
- 21.0% run (sma_spot.py:303)
- 19.1% run (sma_spot.py:299)
- 12.6% request (mininautilus/binance.py:96)
- 12.4% https_open (urllib/request.py:1391)
- 12.4% _open (urllib/request.py:536)
- 12.4% open (urllib/request.py:519)
- 12.4% _call_chain (urllib/request.py:496)
- 11.0% run (sma_spot.py:252)
- 9.5% do_open (urllib/request.py:1317)
- 9.5% __init__ (http/client.py:1490)

## Python profile `pyspy-7.txt` (383 samples)

Self:
- 50.9% load_default_certs (ssl.py:597)
- 8.4% do_handshake (ssl.py:1382)
- 7.6% close (socket.py:789)
- 3.4% decode (json/decoder.py:337)
- 3.1% read (ssl.py:1166)
- 2.1% __new__ (ssl.py:500)
- 1.6% _is_valid_legacy_labelname (prometheus_client/validation.py:100)
- 1.6% _escape (prometheus_client/openmetrics/exposition.py:228)
- 1.6% getaddrinfo (socket.py:974)
- 1.3% compress (gzip.py:590)
- 1.0% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:267)
- 1.0% create_connection (socket.py:848)
- 0.8% add_handler (urllib/request.py:444)
- 0.8% _is_legacy_labelname_rune (prometheus_client/openmetrics/exposition.py:274)
- 0.5% _escape (prometheus_client/openmetrics/exposition.py:229)

Inclusive:
- 85.6% reconcile (mininautilus/binance.py:292)
- 85.6% reconcile (sma_spot.py:171)
- 85.6% <module> (sma_spot.py:369)
- 85.6% run (sma_spot.py:329)
- 72.1% request (mininautilus/binance.py:96)
- 70.5% https_open (urllib/request.py:1391)
- 70.5% _open (urllib/request.py:536)
- 70.5% _call_chain (urllib/request.py:496)
- 70.5% open (urllib/request.py:519)
- 53.8% do_open (urllib/request.py:1317)
- 53.5% __init__ (http/client.py:1490)
- 50.9% load_default_certs (ssl.py:597)
- 50.9% create_default_context (ssl.py:775)
- 44.6% trades (mininautilus/binance.py:199)
- 44.6% collect (mininautilus/binance.py:210)
