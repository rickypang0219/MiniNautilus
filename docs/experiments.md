# Performance lab

The queue is an independent experimental implementation, not the financial-state
transport. It uses one non-cloneable producer and consumer, bounded power-of-two
storage, monotonic wrapping indices and no allocation per push/pop. Endpoints are
Send when payloads are Send, and intentionally not Sync.

Producer: acquire consumed head → initialize slot → release new tail.
Consumer: acquire published tail → move payload out → release new head.
Acquire/release is needed in both directions: publication and safe slot reuse.
The source includes the unsafe invariants. Tests exercise wraparound, capacity,
closure, destructor counts, and concurrent payload visibility. Stress tests do not
prove correctness; Miri/Loom/model checking remain future validation work. Miri
was not installed in the development environment.

Head/tail can occupy adjacent words or be separated by 16 machine words inside an
aligned allocation. This is a controlled false-sharing experiment, not a universal
assumption about cache-line size or a promise that padding will always be faster.

Run:

```sh
cargo bench --locked --bench latency
MINI_SAMPLES=100000 cargo bench --locked --bench latency
# Linux only; select CPUs available to your process/cpuset:
MINI_PIN=2,3 cargo bench --locked --bench latency
```

The harness warms up 5,000 messages, then samples 50,000 by default. It reports p50,
p99, p99.9 offer-to-receive latency, throughput, full retries, and sampled peak queue
occupancy. Latency includes producer backpressure from the first attempt to push.
Throughput covers the measured consumer interval; retry/peak counters include
warmup. It is a saturated producer benchmark, not an open-loop arrival model, an
idle-wakeup test, or an end-to-end trading benchmark. It does not report CPU usage.
Record machine load, CPU model, topology, power mode, OS and repetition count when
making comparisons. Avoid extrapolating these short single-run results to HFT.

On the development macOS/arm64 machine, an unpinned 50,000-sample run completed for
all six padded/wait combinations. Full raw output is in `runs/latency.csv` (ignored
run artifact). For example, padded spin measured p99 8,041 ns and p99.9 12,208 ns in
that run. These are queue-only observations; background compilation was running,
so this is a harness smoke test rather than a controlled comparison. No ranking of
wait modes or padding is asserted from it.

The cache experiment compares contiguous values and separately allocated boxed
values. The bitmap experiment visits only every 16th entry; it deliberately does
less work and is not an apples-to-apples scan speedup. Neither experiment has been
substituted into the trading core yet.

Linux `sched_setaffinity` was compile-checked for aarch64-unknown-linux-gnu. Binding
was not executed on Linux hardware. macOS returns Unsupported for strict pinning
rather than silently claiming success. Busy spinning is confined to this lab; the
REST runtime waits/polls and has no CPU-latency guarantee.

Suggested next measured changes, one at a time:

1. Replace transaction-wide clones with a validated mutation plan while preserving
   all fault/replay tests; profile allocations and event latency first.
2. Introduce a durable journal worker with explicit commit acknowledgements. Batch
   only after specifying the maximum acceptable durability/latency window.
3. Route typed adapter/core messages through a proven bounded queue. Specify full
   behavior separately for executions, market data, and telemetry.
4. Add WebSocket private/market streams with explicit gap recovery, then compare
   park/spin/hybrid under controlled offered load and real idle periods.
5. Add Linux pinning and shard experiments only after the reference trace remains
   equivalent. Account-wide limits need coordinated budgets across shards.
