//! L2: per-stage cost of the durable path, per sync policy. Run it on the machine
//! you deploy to; fsync cost differs by orders of magnitude between machines.
//!
//! cargo run --release --example durable_profile -- [--dir DIR] [--bars N]
//!
//! Each bar is Quote + Trade + Heartbeat; every 10th bar also submits an order
//! that the paper venue acknowledges and fills (SendOrder, so Outbox syncs).
//! Stages: Core prepare, journal frame encode, write(2), sync_all, Core commit,
//! then protocol-2 response encode (compact) versus the protocol-1 full state.
//! Python decode and pipe IPC are measured by examples/ipc_probe.py.
use mininautilus::{
    journal::{DurableEngine, Stages, SyncPolicy},
    model::*,
    protocol::Response,
    sim::PaperExchange,
};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    time::Instant,
};

fn percentiles(mut values: Vec<u64>) -> Value {
    values.sort_unstable();
    let at = |p: f64| values[((values.len() - 1) as f64 * p).round() as usize];
    json!({"p50_us": at(0.5) as f64 / 1e3, "p99_us": at(0.99) as f64 / 1e3,
           "max_us": at(1.0) as f64 / 1e3, "mean_us": values.iter().sum::<u64>() as f64 / values.len() as f64 / 1e3})
}

fn run(dir: &Path, policy: SyncPolicy, bars: u64) -> Value {
    let path = dir.join(format!("profile-{policy:?}-{}.jsonl", std::process::id()));
    let _ = std::fs::remove_file(&path);
    let config = Config {
        max_abs_position: 1_000_000,
        max_order_qty: 1_000_000,
        max_order_notional: i64::MAX,
        ..Config::default()
    };
    let mut engine = DurableEngine::create(&path, config).unwrap();
    engine.set_sync_policy(policy);
    engine.track_changes();
    let mut venue = PaperExchange::new();
    let mut stages: Vec<Stages> = Vec::new();
    let (mut compact_ns, mut full_ns, mut compact_bytes, mut full_bytes) =
        (Vec::new(), Vec::new(), 0usize, 0usize);
    let mut next_id = 1;
    let started = Instant::now();
    for bar in 0..bars {
        let at = bar * 100;
        let mut inputs = vec![
            Event::Quote { bid: 100, ask: 100 },
            Event::Trade {
                taker: Side::Sell,
                price: 100,
                qty: 1,
            },
            Event::Heartbeat {
                epoch: engine.core().epoch,
            },
        ];
        if bar % 10 == 9 {
            inputs.push(Event::Submit(Intent {
                id: next_id,
                side: Side::Buy,
                qty: 1,
                limit: 100,
                based_on_seq: engine.core().seq + 3,
                valid_until: at + 1_000,
            }));
            next_id += 1;
        }
        let mut effects = Vec::new();
        for event in inputs {
            let (produced, stage) = engine.process_profiled(at, event.clone(), None).unwrap();
            stages.push(stage);
            let mut reports = Vec::new();
            for effect in &produced {
                reports.extend(venue.execute(engine.core().epoch, effect).unwrap());
            }
            if let Event::Trade { taker, price, qty } = event {
                reports.extend(venue.trade(engine.core().epoch, taker, price, qty).unwrap());
            }
            for report in reports {
                let (_, stage) = engine.process_profiled(at, report, None).unwrap();
                stages.push(stage);
            }
            effects.extend(produced);
        }
        assert_eq!(engine.core().health, Health::Healthy);
        let changes = engine.take_changes();
        let start = Instant::now();
        let compact =
            serde_json::to_vec(&Response::compact(engine.core(), effects.clone(), changes))
                .unwrap();
        compact_ns.push(start.elapsed().as_nanos() as u64);
        compact_bytes = compact.len();
        if bar % 50 == 0 {
            let start = Instant::now();
            let full = serde_json::to_vec(&Response::full(engine.core(), effects)).unwrap();
            full_ns.push(start.elapsed().as_nanos() as u64);
            full_bytes = full.len();
        }
    }
    engine.sync().unwrap();
    let elapsed = started.elapsed().as_secs_f64();
    let pick = |f: fn(&Stages) -> u64| percentiles(stages.iter().map(f).collect());
    let total: Vec<u64> = stages
        .iter()
        .map(|s| s.prepare_ns + s.encode_ns + s.write_ns + s.sync_ns + s.commit_ns)
        .collect();
    let synced = stages.iter().filter(|s| s.synced).count();
    let journal_bytes = std::fs::metadata(&path).unwrap().len();
    drop(engine);
    std::fs::remove_file(&path).unwrap();
    json!({"policy": format!("{policy:?}"), "bars": bars, "inputs": stages.len(), "synced_inputs": synced,
        "orders": next_id - 1, "elapsed_seconds": elapsed, "journal_bytes": journal_bytes,
        "per_input": {"prepare": pick(|s| s.prepare_ns), "encode": pick(|s| s.encode_ns),
            "write": pick(|s| s.write_ns), "sync": pick(|s| s.sync_ns), "commit": pick(|s| s.commit_ns),
            "total": percentiles(total)},
        "per_request": {"compact_response_encode": percentiles(compact_ns), "compact_bytes_last": compact_bytes,
            "full_response_encode": percentiles(full_ns), "full_bytes_last": full_bytes}})
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let value = |name: &str| {
        args.iter()
            .position(|a| a == name)
            .map(|i| args[i + 1].clone())
    };
    let dir =
        PathBuf::from(value("--dir").unwrap_or_else(|| std::env::temp_dir().display().to_string()));
    let bars = value("--bars").map_or(2_000, |v| v.parse().unwrap());
    let report = json!({
        "platform": format!("{} {}", std::env::consts::OS, std::env::consts::ARCH),
        "dir": dir,
        "runs": [run(&dir, SyncPolicy::EveryInput, bars), run(&dir, SyncPolicy::Outbox, bars)],
    });
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}
