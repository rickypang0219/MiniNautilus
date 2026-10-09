//! Acceptance probes from docs/acceptance.md. In-process Core + PaperExchange only:
//! no journal, fsync, IPC or Python. Uses only public APIs that predate the open-order
//! index so the same file can be built against an older checkout for comparison.
//!
//! cargo run --release --example acceptance -- [--bars N] [--history N] [--skip-backtest]
use mininautilus::{core::Core, model::*, sim::PaperExchange};
use serde_json::json;
use std::{hint::black_box, time::Instant};

const MINUTES_5Y: u64 = 5 * 365 * 24 * 60 + 24 * 60; // 2,629,440 incl. one leap day

struct Driver {
    core: Core,
    venue: PaperExchange,
    fills: u64,
}

impl Driver {
    fn new() -> Self {
        let core = Core::new(Config {
            max_abs_position: 1_000_000,
            max_order_qty: 1_000_000,
            max_order_notional: i64::MAX,
            private_stale_ms: u64::MAX,
            ..Config::default()
        })
        .unwrap();
        Self {
            core,
            venue: PaperExchange::new(),
            fills: 0,
        }
    }

    fn apply(&mut self, at: Time, event: Event) -> Vec<Effect> {
        let seq = self.core.seq + 1;
        self.core
            .apply(&Envelope { seq, at, event })
            .expect("contiguous envelope")
    }

    /// Apply a strategy event, route effects to the venue and its reports back.
    fn command(&mut self, at: Time, event: Event) -> Vec<Effect> {
        let effects = self.apply(at, event);
        for effect in &effects {
            for report in self.venue.execute(self.core.epoch, effect).unwrap() {
                self.apply(at, report);
            }
        }
        effects
    }

    fn market(&mut self, at: Time, taker: Side, price: i64, qty: i64) {
        self.apply(
            at,
            Event::Quote {
                bid: price,
                ask: price,
            },
        );
        self.apply(at, Event::Trade { taker, price, qty });
        for report in self
            .venue
            .trade(self.core.epoch, taker, price, qty)
            .unwrap()
        {
            self.fills += 1;
            self.apply(at, report);
        }
    }
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    sorted[((sorted.len() - 1) as f64 * p).round() as usize]
}

/// Build `count` completed orders through the real submit/ack/fill path.
fn completed_history(count: u64) -> Driver {
    let mut d = Driver::new();
    d.market(0, Side::Buy, 100, 1);
    for id in 1..=count {
        let side = if id % 2 == 1 { Side::Buy } else { Side::Sell };
        let intent = Intent {
            id,
            side,
            qty: 1,
            limit: 100,
            based_on_seq: d.core.seq,
            valid_until: u64::MAX,
        };
        d.command(0, Event::Submit(intent));
        let taker = if side == Side::Buy {
            Side::Sell
        } else {
            Side::Buy
        };
        d.market(0, taker, 100, 1);
    }
    assert_eq!(d.core.orders.len() as u64, count);
    assert_eq!(d.core.position, 0);
    assert_eq!(d.core.health, Health::Healthy);
    d
}

/// Per-event cost with a fixed completed history. Batches of `BATCH` calls are timed
/// (ns-scale calls are below timer resolution); p50/p99 are over batch means.
fn history_probe(count: u64) -> serde_json::Value {
    const BATCH: usize = 200;
    const BATCHES: usize = 101;
    const WARMUP: usize = 10;
    let mut d = completed_history(count);
    let mut next_id = count + 1;
    let mut rows = serde_json::Map::new();
    for operation in [
        "exposure_bounds",
        "tick",
        "venue_trade_no_match",
        "trade_cycle",
    ] {
        let mut samples = Vec::with_capacity(BATCHES);
        for batch in 0..BATCHES + WARMUP {
            let start = Instant::now();
            for _ in 0..BATCH {
                match operation {
                    "exposure_bounds" => {
                        black_box(black_box(&d.core).exposure_bounds());
                    }
                    "tick" => {
                        assert!(d.apply(0, Event::Tick).is_empty());
                    }
                    "venue_trade_no_match" => {
                        assert!(d.venue.trade(0, Side::Sell, 1, 1).unwrap().is_empty());
                    }
                    _ => {
                        // Submit -> ack -> market trade -> fill: the per-signal path.
                        let side = if next_id % 2 == 1 {
                            Side::Buy
                        } else {
                            Side::Sell
                        };
                        let intent = Intent {
                            id: next_id,
                            side,
                            qty: 1,
                            limit: 100,
                            based_on_seq: d.core.seq,
                            valid_until: u64::MAX,
                        };
                        next_id += 1;
                        d.command(0, Event::Submit(intent));
                        let taker = if side == Side::Buy {
                            Side::Sell
                        } else {
                            Side::Buy
                        };
                        d.market(0, taker, 100, 1);
                    }
                }
            }
            if batch >= WARMUP {
                samples.push(start.elapsed().as_nanos() as f64 / BATCH as f64);
            }
        }
        assert_eq!(d.core.health, Health::Healthy);
        samples.sort_by(f64::total_cmp);
        rows.insert(
            operation.into(),
            json!({"p50_ns": percentile(&samples, 0.5), "p99_ns": percentile(&samples, 0.99)}),
        );
    }
    json!({"history_orders": count, "operations": rows})
}

/// Deterministic xorshift random walk in integer ticks; no external data.
struct Walk(u64);
impl Walk {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
}

/// SMA crossover on 1-minute bars through SetTarget/SubmitTargeted, venue fills,
/// cancels of unfilled orders and Tick timers. Gross PnL in tick-lots, no fees.
fn sma_backtest(bars: u64, fast: usize, slow: usize, size: i64) -> serde_json::Value {
    let mut walk = Walk(0x2545_F491_4F6C_DD1D);
    let mut d = Driver::new();
    let mut closes = vec![0i64; slow];
    let (mut fast_sum, mut slow_sum) = (0i64, 0i64);
    let mut price: i64 = 1_000_000;
    let mut revision = 0;
    let mut desired = 0i64;
    let mut working: Option<OrderId> = None;
    let mut next_id = 1;
    let (mut peak, mut max_drawdown) = (i128::MIN, 0i128);
    let mut cancels = 0u64;
    let start = Instant::now();
    for i in 0..bars {
        let at = i * 60_000;
        let r = walk.next();
        price = (price + (r % 41) as i64 - 20).max(1);
        let taker = if r & (1 << 40) == 0 {
            Side::Buy
        } else {
            Side::Sell
        };
        d.market(at, taker, price, 1 + (r >> 48) as i64 % 50);
        d.apply(at, Event::Tick);

        let slot = i as usize % slow;
        slow_sum += price - closes[slot];
        fast_sum += price - closes[(i as usize + slow - fast) % slow];
        closes[slot] = price;
        let equity = d.core.cash + d.core.position as i128 * price as i128;
        peak = peak.max(equity);
        max_drawdown = max_drawdown.max(peak - equity);
        if i + 1 < slow as u64 {
            continue;
        }

        // A resting order that did not fill on this bar is canceled first.
        if let Some(id) = working {
            if d.core.orders[&id].lifecycle.terminal() {
                working = None;
            } else {
                d.command(at, Event::Cancel { id });
                cancels += 1;
                working = None;
            }
        }
        let want = if fast_sum * (slow as i64) > slow_sum * (fast as i64) {
            size
        } else {
            -size
        };
        if want != desired {
            desired = want;
            revision += 1;
            d.command(
                at,
                Event::SetTarget(Target {
                    revision,
                    position: want,
                    valid_until: at + 60_000,
                }),
            );
        }
        let delta = desired - d.core.position;
        if delta != 0 {
            let side = if delta > 0 { Side::Buy } else { Side::Sell };
            // Marketable within 20 ticks of the next bar's random step.
            let limit = price + side.sign() * 20;
            let intent = Intent {
                id: next_id,
                side,
                qty: delta.abs(),
                limit: limit.max(1),
                based_on_seq: d.core.seq,
                valid_until: at + 60_000,
            };
            let effects = d.command(
                at,
                Event::SubmitTargeted {
                    intent,
                    revision,
                    expected_position: d.core.position,
                },
            );
            if effects.iter().any(|e| matches!(e, Effect::SendOrder(_))) {
                working = Some(next_id);
            }
            next_id += 1;
        }
    }
    let elapsed = start.elapsed().as_secs_f64();
    assert_eq!(d.core.health, Health::Healthy);
    assert_eq!(d.core.position, d.venue.position);
    json!({"bars": bars, "fast": fast, "slow": slow, "events": d.core.seq,
        "orders": d.core.orders.len(), "fills": d.fills, "cancels": cancels,
        "final_position": d.core.position,
        "gross_equity_tick_lots": (d.core.cash + d.core.position as i128 * price as i128).to_string(),
        "max_drawdown_tick_lots": max_drawdown.to_string(),
        "elapsed_seconds": elapsed, "ns_per_event": elapsed * 1e9 / d.core.seq as f64})
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let value = |name: &str, default: u64| {
        args.iter()
            .position(|a| a == name)
            .map_or(default, |i| args[i + 1].parse().expect("integer argument"))
    };
    let bars = value("--bars", MINUTES_5Y);
    let history = value("--history", 100_000);

    let small = history_probe(0);
    let large = history_probe(history);
    let ratio = |op: &str| {
        large["operations"][op]["p99_ns"].as_f64().unwrap()
            / small["operations"][op]["p99_ns"].as_f64().unwrap().max(1.0)
    };
    let cycle_ratio = ratio("trade_cycle");
    let mut report = json!({"history": [small, large],
        "trade_cycle_p99_ratio": cycle_ratio,
        "tick_p99_ratio": ratio("tick")});
    eprintln!(
        "A1 history independence: trade_cycle p99 ratio {cycle_ratio:.2} (target < 2) -> {}",
        if cycle_ratio < 2.0 { "PASS" } else { "FAIL" }
    );
    if !args.iter().any(|a| a == "--skip-backtest") {
        let run = sma_backtest(bars, 20, 60, 1);
        let seconds = run["elapsed_seconds"].as_f64().unwrap();
        eprintln!(
            "B1 single backtest: {bars} bars in {seconds:.3}s (target <= 1s, report > 10s) -> {}",
            if seconds <= 1.0 {
                "PASS"
            } else if seconds <= 10.0 {
                "MISS"
            } else {
                "FAIL"
            }
        );
        report["sma_backtest"] = run;
    }
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}
