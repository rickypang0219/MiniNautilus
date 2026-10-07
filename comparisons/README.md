# Cross-platform backtest audit

This directory runs the real Backtrader, NautilusTrader and vn.py CTA engines against
MiniNautilus. It does not replace their matchers with a common simulator. All runs
are offline, synthetic, single instrument, integer ticks/lots, zero fees and zero
slippage. No exchange keys or live orders are involved.

The Chinese findings and measured results are in [the audit report](../docs/backtest-comparison.md).

## Reproduce

From the repository root, use Python 3.12 and a separate environment. The dependency
versions are pinned in `requirements.txt`; the runtime project's dependencies are
unchanged. vn.py brings Qt dependencies even though this harness does not start a GUI.

```sh
uv venv --python python3.12 runs/comparison-env
uv pip install --python runs/comparison-env/bin/python --only-binary=:all: -r comparisons/requirements.txt
cargo build --locked --release --example compare_engine
cargo build --locked --release
runs/comparison-env/bin/python comparisons/run.py --output runs/platform-audit-new --seeds 20
runs/comparison-env/bin/python comparisons/run.py --output runs/platform-audit-all-new --platforms mini backtrader backtrader_volume vnpy nautilus nautilus_tick --seeds 50
runs/comparison-env/bin/python comparisons/check_semantics.py runs/platform-audit-all-new
runs/comparison-env/bin/python comparisons/run.py --output runs/durable-audit-new --platforms mini mini_durable --seeds 3 --steps 50
runs/comparison-env/bin/python comparisons/signals.py runs/sma-parity-new.json
python3 comparisons/fuzz.py runs/liquidity-fuzz-new.json --seeds 1000
python3 comparisons/history_scaling.py runs/history-scaling-new.json
runs/comparison-env/bin/python comparisons/reproduce_nautilus_pnl.py runs/nautilus-pnl-new.json
runs/comparison-env/bin/python comparisons/remedies.py runs/model-remedies-new.json
runs/comparison-env/bin/python comparisons/run.py --benchmark --output runs/platform-performance-new.json --sizes 1000 5000 10000 --repeats 3
runs/comparison-env/bin/python comparisons/run.py --benchmark --output runs/durable-performance-new.json --platforms mini_durable --sizes 200 1000 --repeats 3
```

For the volume-filler variant, include `backtrader_volume` in `--platforms`.
`mini` alone needs only Python's standard library and the release Rust example.
Each correctness run requires a fresh output directory, saves the exact workloads,
full fill ledgers, checkpoints, versions and input digest, and fails on internal
accounting errors or differences in the designated common-contract fixtures.
Model-sensitive cases are observations; the version-pinned semantic checks run
separately. Missing third-party dependencies are errors, not successful skips.

## What is being compared

- `cases.py`: 15 hand-auditable fixtures, seeded passive-order streams, and seeded
  multiple-order/partial-fill/cancel stress streams. Actions follow the current
  observation; no fixture generator uses a future price to choose an order.
- `adapters.py`: native platform drivers. Prices are mapped to single-price OHLC
  minute bars for the three external engines; Mini receives an equal bid/ask quote plus
  an explicit trade. The same numbers do **not** convey the same market information:
  OHLC bars omit aggressor side and queue position. Dedicated fixtures expose this.
  `nautilus_tick` provides a second Nautilus control using actual `TradeTick`
  objects and aggressor side. Zero-volume observations do not create a trade or
  strategy callback; this adapter rejects zero-volume observations with actions.
- `oracle.py`: a separate Python specification of Mini's **documented** ID-order,
  shared-liquidity, aggressor-sensitive matching contract. It checks implementation
  correctness, not realism, and is never called an external platform.
- `examples/compare_engine.rs`: the actual Rust `Core` and `PaperExchange`, driven
  in process without the journal. No production matching code is copied here.
- `mini_durable`: the existing release `paper` CLI through `Engine`, with fsync,
  JSON IPC and full state output. Every run also verifies its persisted journal
  replays to exactly the final runtime state.
- `signals.py`: independent SMA implementations (Mini, Backtrader, Nautilus and
  TA-Lib, which vn.py uses), with identical full-window warm-up and hold-on-tie
  rules. This tests indicator arithmetic separately from fill-dependent strategy
  feedback; it is not a comparison of the libraries' default crossover helpers.

Cash means signed trade cash flow from an initially flat position, not margin
account balance. Common equity is `cash + net_position * last_price`. Backtrader's
broker cash/value are checked directly; vn.py and Nautilus margin accounting are
reconciled to their native position and account PnL. Their per-step canonical cash
series are reconstructed from native fills. vn.py uses net inventory and OPEN
offsets; futures hedge books and close-today offsets are outside this contract.

Nautilus margin balances round realized fill increments to USD cents. We retain
its exact fill-based PnL, account PnL, rational average-cost rounding reference and
observed difference. The bound is derived from at most half a cent per closing
fill plus half a cent for the final unrealized mark; this is **not exact PnL
equality**. Native float ties can choose a different cent from the rational
reference. Separately, the pinned version's `portfolio.total_pnl` undercounts two
equal-profit NETTING cycles. Both default and explicit-account calls are retained
in diagnostics; the account-balance check is independent of that API.

`nautilus_cycles.py` demonstrates cycle-identity aggregation on trusted snapshots
created by the local engine. Five closed-cycle cases reconcile to native account
PnL, including equal profits/losses, and duplicate snapshots count once. This is a
diagnostic correction, not an installed patch to the external library.

`remedies.py` keeps alternative profiles separate from the default audit: a custom
Backtrader shared-per-bar volume filler, and Nautilus trade ticks with
`liquidity_consumption=True`. The latter fixes the shared-volume fixture but exposes
another pinned-version issue: distinct L1 trade ticks at unchanged price **and**
size fail to refresh consumed liquidity. Four unique-timestamp/TradeId sequences
retain that observation. Passing this script means the documented observations
were reproduced; it does not mean all alternative profiles equal Mini.

## Timing boundaries

All input generation, imports and instrument/engine construction are outside the
in-process replay timers. A warm-up run is discarded, then three raw durations and
the median are saved. Each timed run must still pass fill and ledger assertions.
No absolute timing threshold belongs in CI.

`mini` times the Rust replay loop, including its checkpoint/fill collection, but
excludes process launch, JSON input parsing and final output encoding. Its result
also includes subprocess wall time. Backtrader times `Cerebro.run` with streaming
feed iteration (`runonce=False`, `preload=False`); vn.py times `run_backtesting`;
Nautilus times `run` with logging and end-of-run analysis disabled. These are
useful declared workloads, **not identical engine feature sets**. The durable Mini
timer includes startup, process shutdown, fsync and IPC; post-run journal inspection
is excluded. There is no claim about live exchange latency, HFT capacity, queue
latency, multi-instrument throughput or memory peaks.

`history_scaling.py` holds the completed order/fill history fixed, then times
5,000 `Core::apply(Quote)` calls. It isolates history-dependent clone cost without
matching, JSON encoding or journaling. The ordinary CI job runs Mini against the
independent oracle; third-party comparisons are an explicit optional environment.

## Interpretation boundaries

Equal fills and balances in these cases cannot prove the absence of unknown bugs.
Fee models, funding, borrow, FX, corporate actions, tick/lot conversion, order book
queue position, exchange calendars, live latency, market impact and real datasets
need separate tests. Different defaults are not automatically programming errors.
Keep explicit semantic fixtures when changing any fill model; never tune parameters
just to force equal final PnL.
