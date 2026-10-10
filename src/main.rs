use mininautilus::telemetry::Telemetry;
use mininautilus::{
    core::{Changes, Core},
    journal::{DurableEngine, EventTime, Stages, SyncPolicy},
    metrics::Metrics,
    model::*,
    protocol::Response,
    sim::PaperExchange,
};
use serde::Deserialize;
use std::{
    io::{self, BufRead, Write},
    path::Path,
    sync::{Arc, atomic::Ordering::Relaxed},
};

/// One request may carry a batch of events processed in order (for example a
/// bar's Quote, Trade and Tick) so a backtest needs one round trip per bar.
#[derive(Deserialize)]
struct Request {
    at: Time,
    #[serde(default)]
    event: Option<Event>,
    #[serde(default)]
    events: Vec<Event>,
    /// `[at, event]` pairs processed after `event`/`events`, each at its own time:
    /// lets a backtest send the previous bar's decisions with the next bar.
    #[serde(default)]
    batch: Vec<(Time, Event)>,
    #[serde(default)]
    time: Option<EventTime>,
}

/// `serve`/`paper` journal every input; `sim` keeps the Core in memory only.
enum Backend {
    Durable(DurableEngine),
    Memory(Core),
}
impl Backend {
    fn core(&self) -> &Core {
        match self {
            Self::Durable(engine) => engine.core(),
            Self::Memory(core) => core,
        }
    }
    fn process(
        &mut self,
        at: Time,
        event: Event,
        time: Option<EventTime>,
    ) -> Result<Vec<Effect>, Box<dyn std::error::Error>> {
        Ok(match self {
            Self::Durable(engine) => engine.process_timed(at, event, time)?,
            Self::Memory(core) => {
                let seq = core.seq + 1;
                core.apply(&Envelope { seq, at, event })?
            }
        })
    }
    /// `process` plus per-stage timing for the metrics endpoint.
    fn process_observed(
        &mut self,
        at: Time,
        event: Event,
        time: Option<EventTime>,
    ) -> Result<(Vec<Effect>, Stages), Box<dyn std::error::Error>> {
        Ok(match self {
            Self::Durable(engine) => engine.process_profiled(at, event, time)?,
            Self::Memory(core) => {
                let seq = core.seq + 1;
                let start = std::time::Instant::now();
                let effects = core.apply(&Envelope { seq, at, event })?;
                let stages = Stages {
                    prepare_ns: start.elapsed().as_nanos() as u64,
                    ..Stages::default()
                };
                (effects, stages)
            }
        })
    }
    fn track_changes(&mut self) {
        match self {
            Self::Durable(engine) => engine.track_changes(),
            Self::Memory(core) => core.track_changes(),
        }
    }
    fn take_changes(&mut self) -> Option<Changes> {
        match self {
            Self::Durable(engine) => engine.take_changes(),
            Self::Memory(core) => core.take_changes(),
        }
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("mininautilus: {error}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut args: Vec<_> = std::env::args().collect();
    // `--sync every|outbox` applies to serve/paper journals (docs/acceptance.md L3).
    let mut sync = SyncPolicy::EveryInput;
    if let Some(i) = args.iter().position(|a| a == "--sync") {
        sync = args.get(i + 1).ok_or("--sync every|outbox")?.parse()?;
        args.drain(i..i + 2);
    }
    match args.get(1).map(String::as_str) {
        Some("demo") => demo(),
        Some("dashboard") => {
            let root = args
                .get(2)
                .ok_or("dashboard JOURNAL_OR_DIRECTORY [--port PORT]")?;
            let port = if args.get(3).is_some_and(|s| s == "--port") {
                args.get(4).ok_or("missing port")?.parse::<u16>()?
            } else if args.len() > 3 {
                return Err("dashboard JOURNAL_OR_DIRECTORY [--port PORT]".into());
            } else {
                8765
            };
            mininautilus::dashboard::serve(Path::new(root), port)?;
            Ok(())
        }
        Some("inspect") => {
            let path = args.get(2).ok_or("inspect JOURNAL")?;
            let state = mininautilus::journal::replay(Path::new(path))?;
            println!("{}", serde_json::to_string(&state)?);
            Ok(())
        }
        Some("serve" | "paper" | "sim") => {
            let mode = args[1].as_str();
            let config_at = |i: usize| -> Result<Config, Box<dyn std::error::Error>> {
                Ok(match args.get(i) {
                    Some(path) => serde_json::from_slice(&std::fs::read(path)?)?,
                    None => Config::default(),
                })
            };
            let mut backend = if mode == "sim" {
                // Backtest: no journal and no fsync; the input file is the truth.
                Backend::Memory(Core::new(config_at(2)?)?)
            } else {
                let path = args
                    .get(2)
                    .ok_or("usage: mininautilus serve JOURNAL [--recover] [CONFIG.json]")?;
                Backend::Durable(if args.get(3).is_some_and(|s| s == "--recover") {
                    if mode == "paper" {
                        return Err(
                            "paper restart requires a fresh journal and replayed market inputs"
                                .into(),
                        );
                    }
                    DurableEngine::recover(Path::new(path), args.get(4).map(Path::new))?
                } else {
                    DurableEngine::create(Path::new(path), config_at(3)?)?
                })
            };
            if let Backend::Durable(engine) = &mut backend {
                engine.set_sync_policy(sync);
            }
            serve(backend, mode != "serve")
        }
        Some("backtest") => {
            let usage = "backtest BARS.csv TARGETS.csv [CONFIG.json] [--ledger FILLS.csv] \
                         [--limit-offset TICKS] [--order-ttl-ms MS]";
            let mut positional = Vec::new();
            let mut ledger_path = None;
            let mut policy = mininautilus::backtest::Policy::default();
            let mut rest = args[2..].iter();
            while let Some(arg) = rest.next() {
                match arg.as_str() {
                    "--ledger" => ledger_path = Some(rest.next().ok_or(usage)?),
                    "--limit-offset" => policy.limit_offset = rest.next().ok_or(usage)?.parse()?,
                    "--order-ttl-ms" => policy.order_ttl_ms = rest.next().ok_or(usage)?.parse()?,
                    _ => positional.push(arg),
                }
            }
            let [bars, targets, config @ ..] = positional.as_slice() else {
                return Err(usage.into());
            };
            let config = match config {
                [] => Config::default(),
                [path] => serde_json::from_slice(&std::fs::read(path)?)?,
                _ => return Err(usage.into()),
            };
            backtest(bars, targets, config, policy, ledger_path)
        }
        Some("rotate") => {
            let usage = "rotate OLD_JOURNAL NEW_JOURNAL";
            let (old, new) = (args.get(2).ok_or(usage)?, args.get(3).ok_or(usage)?);
            let core = mininautilus::journal::rotate(Path::new(old), Path::new(new))?;
            println!(
                "rotated: position={} id_floor={} killed={}",
                core.position, core.id_floor, core.killed
            );
            Ok(())
        }
        Some("convert-bars") => {
            let usage = "convert-bars BARS.csv BARS.bin";
            let (input, output) = (args.get(2).ok_or(usage)?, args.get(3).ok_or(usage)?);
            let bars = mininautilus::backtest::load_bars(Path::new(input))?;
            std::fs::write(output, mininautilus::backtest::encode_bars(&bars))?;
            println!("{} bars", bars.len());
            Ok(())
        }
        Some("snapshot") => {
            let journal = args.get(2).ok_or("snapshot JOURNAL OUTPUT")?;
            let output = args.get(3).ok_or("snapshot JOURNAL OUTPUT")?;
            let seq = mininautilus::journal::checkpoint(Path::new(journal), Path::new(output))?;
            println!("snapshot saved at seq {seq}");
            Ok(())
        }
        _ => {
            println!(
                "MiniNautilus\n  dashboard JOURNAL_OR_DIRECTORY [--port PORT]\n  demo\n  serve JOURNAL [--recover|CONFIG.json] [--sync every|outbox]\n  paper JOURNAL [CONFIG.json] [--sync every|outbox]\n  sim [CONFIG.json]  (in-memory paper venue, no journal)\n  backtest BARS.csv|BARS.bin TARGETS.csv [CONFIG.json] [--ledger FILLS.csv]\n  convert-bars BARS.csv BARS.bin\n  inspect JOURNAL\n  snapshot JOURNAL OUTPUT\n  rotate OLD_JOURNAL NEW_JOURNAL  (archive a flat, healthy session)"
            );
            Ok(())
        }
    }
}

/// In-process backtest of a precomputed target series; prints a JSON summary.
fn backtest(
    bars: &str,
    targets: &str,
    config: Config,
    policy: mininautilus::backtest::Policy,
    ledger_path: Option<&String>,
) -> Result<(), Box<dyn std::error::Error>> {
    use mininautilus::backtest;
    let started = std::time::Instant::now();
    let bars = backtest::load_bars(Path::new(bars))?;
    let targets = backtest::read_targets(
        io::BufReader::new(std::fs::File::open(targets)?),
        bars.len(),
    )?;
    let loaded = started.elapsed().as_secs_f64();
    let (run, summary) = backtest::run(&bars, &targets, config, policy, ledger_path.is_some())?;
    let simulated = started.elapsed().as_secs_f64() - loaded;
    if let (Some(path), Some(ledger)) = (ledger_path, &run.ledger) {
        let mut out = io::BufWriter::new(std::fs::File::create(path)?);
        writeln!(out, "bar,at,order_id,side,qty,price")?;
        for f in ledger {
            writeln!(
                out,
                "{},{},{},{:?},{},{}",
                f.bar, f.at, f.order_id, f.side, f.qty, f.price
            )?;
        }
        out.flush()?;
    }
    let mut value = serde_json::to_value(&summary)?;
    value["load_seconds"] = loaded.into();
    value["simulate_seconds"] = simulated.into();
    println!("{value}");
    Ok(())
}

/// JSON-lines loop shared by serve/paper/sim. `MINI_RESPONSE=full` restores the
/// protocol-1 behavior of returning the complete state after every request.
fn serve(mut engine: Backend, simulate: bool) -> Result<(), Box<dyn std::error::Error>> {
    let full = std::env::var("MINI_RESPONSE").is_ok_and(|v| v == "full");
    engine.track_changes();
    let mut venue = PaperExchange::new();
    let mut telemetry = std::env::var_os("MINI_TELEMETRY")
        .map(|path| Telemetry::start(Path::new(&path), 256))
        .transpose()?;
    // `MINI_METRICS_ADDR=127.0.0.1:9464` exposes Prometheus metrics (docs/observability.md).
    let metrics = match std::env::var("MINI_METRICS_ADDR") {
        Ok(address) => {
            let (metrics, bound) = mininautilus::metrics::serve(&address)?;
            eprintln!("metrics listening on http://{bound}/metrics");
            Some(metrics)
        }
        Err(_) => None,
    };
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    // A startup response lets the Python bridge resume monotonic time and IDs.
    engine.take_changes();
    serde_json::to_writer(&mut stdout, &Response::full(engine.core(), vec![]))?;
    writeln!(stdout)?;
    stdout.flush()?;
    for line in io::stdin().lock().lines() {
        let line = line?;
        let started = std::time::Instant::now();
        let request: Request = serde_json::from_str(&line)?;
        let mut effects = Vec::new();
        let at = request.at;
        let inputs = request
            .event
            .into_iter()
            .chain(request.events)
            .map(|e| (at, e));
        for (at, event) in inputs.chain(request.batch) {
            let produced = process(
                &mut engine,
                &metrics,
                at,
                event.clone(),
                request.time.clone(),
            )?;
            if simulate {
                let mut reports = Vec::new();
                for effect in &produced {
                    reports.extend(venue.execute(engine.core().epoch, effect)?);
                }
                if let Event::Trade { taker, price, qty } = event {
                    reports.extend(venue.trade(engine.core().epoch, taker, price, qty)?);
                }
                for report in reports {
                    let mut time = request.time.clone();
                    if let Some(t) = &mut time {
                        t.source = Some("simulation".into());
                        t.fill_event_times.clear();
                    }
                    process(&mut engine, &metrics, at, report, time)?;
                }
            }
            effects.extend(produced);
        }
        if let Some(logger) = telemetry.as_mut() {
            let core = engine.core();
            logger.emit(format!(
                "seq={} position={} health={:?}",
                core.seq, core.position, core.health
            ));
        }
        let changes = engine.take_changes();
        let response = if full {
            Response::full(engine.core(), effects)
        } else {
            Response::compact(engine.core(), effects, changes)
        };
        match &metrics {
            None => serde_json::to_writer(&mut stdout, &response)?,
            Some(metrics) => {
                let encode = std::time::Instant::now();
                let bytes = serde_json::to_vec(&response)?;
                metrics
                    .response_encode
                    .observe_ns(encode.elapsed().as_nanos() as u64);
                metrics
                    .response_bytes
                    .fetch_add(bytes.len() as u64 + 1, Relaxed);
                stdout.write_all(&bytes)?;
            }
        }
        writeln!(stdout)?;
        stdout.flush()?;
        if let Some(metrics) = &metrics {
            metrics.state(engine.core());
            metrics.requests.fetch_add(1, Relaxed);
            metrics
                .request
                .observe_ns(started.elapsed().as_nanos() as u64);
        }
    }
    if let Backend::Durable(engine) = &mut engine {
        engine.sync()?;
    }
    if let Some(logger) = telemetry {
        eprintln!("telemetry_dropped={}", logger.finish()?);
    }
    Ok(())
}

/// Apply one input, recording its stages and effects when metrics are enabled.
fn process(
    engine: &mut Backend,
    metrics: &Option<Arc<Metrics>>,
    at: Time,
    event: Event,
    time: Option<EventTime>,
) -> Result<Vec<Effect>, Box<dyn std::error::Error>> {
    let Some(metrics) = metrics else {
        return engine.process(at, event, time);
    };
    let kind = mininautilus::metrics::event_kind(&event);
    let (effects, stages) = engine.process_observed(at, event, time)?;
    metrics.input(kind, &effects, &stages);
    Ok(effects)
}

fn step(core: &mut Core, at: Time, event: Event) -> Result<Vec<Effect>, String> {
    core.apply(&Envelope {
        seq: core.seq + 1,
        at,
        event,
    })
}
fn demo() -> Result<(), Box<dyn std::error::Error>> {
    let mut core = Core::new(Config::default())?;
    let mut venue = PaperExchange::new();
    venue.drop_next_ack = true;
    step(&mut core, 0, Event::Quote { bid: 99, ask: 101 })?;
    let effects = step(
        &mut core,
        1,
        Event::Submit(Intent {
            id: 1,
            side: Side::Buy,
            qty: 5,
            limit: 100,
            based_on_seq: 1,
            valid_until: 100,
        }),
    )?;
    for effect in &effects {
        venue.execute(core.epoch, effect)?;
    }
    step(&mut core, 101, Event::Tick)?;
    step(&mut core, 102, Event::Disconnect)?;
    venue.connected = false;
    venue.trade(core.epoch, Side::Sell, 100, 2)?;
    venue.connected = true;
    let effects = step(&mut core, 200, Event::Reconnect)?;
    for effect in &effects {
        for event in venue.execute(core.epoch, effect)? {
            step(&mut core, 200, event)?;
        }
    }
    let duplicate = venue.fills[0].clone();
    let epoch = core.epoch;
    step(
        &mut core,
        201,
        Event::Execution {
            epoch,
            venue_seq: venue.venue_seq,
            report: Report::Fill(duplicate),
        },
    )?;
    assert_eq!(core.position, 2);
    assert_eq!(core.fills.len(), 1);
    assert_eq!(core.exposure_bounds(), (2, 5));
    println!("lost ack → timeout → disconnect → partial fill → reconciliation → duplicate fill");
    println!(
        "position={}, unique_fills={}, reserved_buy={}, venue_orders={}, health={:?}",
        core.position,
        core.fills.len(),
        core.orders[&1].remaining(),
        venue.orders.len(),
        core.health
    );
    Ok(())
}
