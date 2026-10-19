# Rust native runtime: operation and evidence

Created: 2026-10-15T08:20:00+09:00. Scope: the [frozen migration proposal](rust-native-runtime-proposal.md).

## Ownership

`native-live` owns Hyperliquid REST/WebSocket transport, exact tick/lot normalization, source queues, account admission, signing, durable action attempts, the account writer, timers, execution and reconciliation in Rust. It calls the existing `DurableEngine` directly; market events and order effects do not cross the Python engine bridge. Core, journal formats, Binance and historical backtest implementations are unchanged.

The optional Python subprocess receives one closed candle plus session/epoch/revision/deadline. It has a bounded SMA ring and returns a target. Its request and output are limited to 8 KiB and an absolute 1.2-second deadline, including partial reads. It receives no Core mirror or credentials through the strategy protocol. The launcher clears the subprocess environment and passes only PATH, LANG and PYTHONNOUSERSITE; venue/AWS credentials are not inherited. Same-user process separation is still a fault boundary, **not a security sandbox**; use only trusted strategy code.

Rust uses a dedicated account writer, a single blocking REST execution worker, a Tokio WebSocket worker, a bounded candle/strategy worker and a telemetry worker. No thread-per-event or generic broadcast bus. Native queues have capacities market 128, critical 32, effects 32 and telemetry 1024. This first implementation is Linux-only and one dedicated Testnet BTC account; it is not a multi-instrument or HA runtime.

`l2Book` supplies best bid/ask. `orderUpdates` and `userFills` subscriptions request an immediate verified private refresh; they do not fabricate a native account sequence. Actual accounting facts still come from bounded REST history and quiescent reconciliation. Closed one-minute candles come from a bounded REST window: warm-up at most nine bars, then incremental updates; after the newest closed bar is cached, wait for the next boundary. No full-history strategy fetch.

## Build and commands

Admission requires a fresh current-generation quote; subscription ACKs alone
cannot enable it. Older/duplicate books cannot renew freshness, queued submits
cannot outlive pricing input, and REST observations cannot drop or rewrite known
fills or regress terminal status. Candle batches retain the pre-fetch epoch.
See [feed consistency and offline fault tests](native-feed-consistency.md).

```sh
scripts/verify.sh
cargo build --locked --release --features native-runtime --bin native-live
# Read-only, including private subscription acknowledgements when HL_MAIN_ADDRESS exists.
target/release/native-live probe --seconds 15
# Public Testnet data with simulated fills; Python is strategy only.
target/release/native-live paper --python-strategy --seconds 900 --flatten --run-dir runs/native-paper-UNIQUE
# Small real Testnet exposure; requires the existing HL_* bindings.
target/release/native-live testnet --python-strategy --seconds 900 --flatten --run-dir runs/native-live-UNIQUE
# Explicit original-session recovery and cleanup. Does not restart the strategy.
target/release/native-live recover --flatten --run-dir runs/native-live-UNIQUE
```

Signed commands refuse debug builds. Preserve the build receipt and executable hash; a release directory name alone is insufficient evidence. Set `HL_NETWORK=testnet`, `HL_MAIN_ADDRESS`, `HL_API_ADDRESS`, `HL_API_PRIVATE_KEY` through the environment's secure mechanism. Never put keys into commands, artifacts, Git, ordinary S3 objects or SSM command text.

Before signing, inspect orphaned processes/sessions and query actual positions/open orders. Only one writer across all hosts may use this dedicated account; the shared Python/Rust local lock is not a distributed lease. New sessions require flat/no open orders, cross leverage 1 and sufficient collateral. Caps remain 20 BTC lots (0.0002 BTC), USD40/order including reversal, USD100 minimum NAV and USD10 session loss.

Diagnostic commands deliberately hold long and short Testnet exposure:

```sh
target/release/native-live diagnostic --run-dir runs/native-diagnostic-UNIQUE
# Genuine process exit after the venue reports a fill but before the local fill is persisted.
target/release/native-live diagnostic --crash-after-fill --run-dir runs/native-crash-UNIQUE
target/release/native-live recover --flatten --run-dir runs/native-crash-UNIQUE
```

The injected exit is 86. Keep the interrupted run incomplete after successful recovery. No automatic mutation retry and no inference that `unknownOid` proves absence. A persisted intent that was never acknowledged may remain unresolved; preserve it rather than starting a replacement session to hide it.

## Failures and shutdown

A circuit latch closes new dispatch on overflow, disconnect, malformed data, stale strategy results or unknown mutation outcomes. A failed latch cannot be reopened by a racing writer. Check intent expiry at dispatch and after account admission; signed `expiresAfter` cannot exceed the original intent deadline. An already handed-off request remains uncertain even if a stop arrives immediately afterwards.

SIGTERM stops admission; shutdown joins the one execution owner, consumes returned facts, reconnects/reconciles original identities and optionally flattens proven exposure. It independently queries position/open orders afterwards. This is best effort: host loss/SIGKILL can leave exposure. Failures are retained even if cleanup succeeds. Exact WebSocket `1000/Expired` supports at most three renewals: pause admission, drain old execution facts, advance the Core epoch, verify the original account history, and confirm the new subscriptions before admitting another fresh target. Other source failures still stop this session; use explicit original-session recovery. See the [expiry recovery addendum](rust-native-runtime-expiry-recovery.md) for limits and tests.

Telemetry is isolated and may drop with a counter. The recovery journal remains synchronous on the writer's durable path. No durability contract is weakened. Session history remains capped at 1,000 orders; live Hyperliquid rotation is not enabled by this migration.

## Verification and invariants

| Invariant | Native evidence/test |
|---|---|
| Existing prepare → journal → sync → commit → effects; replay parity | Existing journal/fault/transition suites; `slow_strategy_and_full_telemetry_do_not_block_private_writer` replays the same resulting Core |
| Correct exchange signing | `official_sdk_messagepack_hash_and_signature_match` compares order/cancel bytes/hash/signature against pinned official SDK 0.24.0 using public test key 1 |
| No blind retry after ambiguous delivery | `lost_response_is_not_retried_even_after_action_journal_reopen`; durable action attempt restart/corruption tests |
| Unknown is not absence | `unknown_order_cannot_become_a_reconciliation_absence` |
| Finality/cancel race and duplicate economics | `cancel_after_fill_does_not_send_another_mutation`; cumulative-fill, changed-duplicate, independent PnL/fees/funding tests |
| Expiring authorization | `expired_queued_order_is_never_dispatched` (RED before fix), `expired_admission_and_closed_gate_do_not_reach_transport`, action-expiry persistence test |
| Bounded isolated strategy | Native/Python signal parity over the same bars; bounded-window tests; `strategy_partial_output_cannot_extend_absolute_deadline` (RED before fix) |
| Cross-language session exclusivity | Python/Rust journal and `flock` interoperability test |
| Critical progress under slow optional work | One-second blocked strategy and full telemetry queue test; stale epoch/result tests |

## Controlled performance experiment

`comparisons/native-runtime/manifest-v1.json` is fixed: 1,000 raw book frames/s for 120 seconds, first five seconds excluded, one deterministic target each second, unchanged Core config and `outbox`. It reuses the existing Python `Coordinator`/`Engine` and the native production writer/effect executor. Targets follow long/flat/short/flat; simulated fills use identical prices and quantities. This is an infrastructure workload, not a trading strategy evaluation.

Timestamp boundaries: application JSON frame available → JSON normalization → admission/queue → durable Core effect → simulated executor handoff. Excludes socket/TLS receive, signing, account RPC and exchange wait. Live `local_receive_to_ack_ns` includes account admission and remote response; **it is not an actual socket-handoff measurement**. These series cannot be pooled. No p99.9 without 100,000 observations in that series; order p99.9 is therefore NOT_MEASURED in this workload.

```sh
python3 ops/native_bench.py --mode python --run-dir runs/native-A-UNIQUE
python3 ops/native_bench.py --mode native --run-dir runs/native-B-UNIQUE
```

Freeze the source/harness, verify receipts, run A/A (five runs per side), then interleaved A/B (five per side) on the same quiet EC2. Compare run-level ranges, require >=10% improvement and no overlap, retain failures. A/A range >20% of the median means performance INCONCLUSIVE, not permission to discard runs. Compare all action records/input hashes, counts, CPU/event, RSS and market p99.9 as well as order p99. `--allow-dirty` is smoke-only.

## Temporary AWS lifecycle

Launch only after the complete runnable package is ready. Preserve IDs immediately, use a public subnet with no ingress and S3 gateway endpoint, encrypted delete-on-termination EBS, IMDSv2 and no NAT/ALB. A controller must terminate in `finally` and verify instance plus volume deletion. Independently schedule guest termination to bound loss of the controller. Never count a scheduled shutdown as completed cleanup.

Incident on 2026-10-15: the first c7i.large was created before implementation/experiments were ready and remained idle. It was explicitly terminated after the owner reported it; its EBS was verified deleted. It produced bootstrap evidence only, no AWS performance result. The report must retain that cost/operational failure.
