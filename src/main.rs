use mininautilus::telemetry::Telemetry;
use mininautilus::{core::Core, journal::DurableEngine, model::*, sim::PaperExchange};
use serde::{Deserialize, Serialize};
use std::{
    io::{self, BufRead, Write},
    path::Path,
};

#[derive(Deserialize)]
struct Request {
    at: Time,
    event: Event,
}
#[derive(Serialize)]
struct Response<'a> {
    effects: Vec<Effect>,
    state: &'a Core,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("mininautilus: {error}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("demo") => demo(),
        Some("inspect") => {
            let path = args.get(2).ok_or("inspect JOURNAL")?;
            let state = mininautilus::journal::replay(Path::new(path))?;
            println!("{}", serde_json::to_string(&state)?);
            Ok(())
        }
        Some("serve" | "paper") => {
            let paper = args[1] == "paper";
            let path = args
                .get(2)
                .ok_or("usage: mininautilus serve JOURNAL [--recover] [CONFIG.json]")?;
            let mut engine = if args.get(3).is_some_and(|s| s == "--recover") {
                if paper {
                    return Err(
                        "paper restart requires a fresh journal and replayed market inputs".into(),
                    );
                }
                DurableEngine::recover(Path::new(path), args.get(4).map(Path::new))?
            } else {
                let config = match args.get(3) {
                    Some(path) => serde_json::from_slice(&std::fs::read(path)?)?,
                    None => Config::default(),
                };
                DurableEngine::create(Path::new(path), config)?
            };
            let mut venue = PaperExchange::new();
            let mut telemetry = std::env::var_os("MINI_TELEMETRY")
                .map(|path| Telemetry::start(Path::new(&path), 256))
                .transpose()?;
            let mut stdout = io::stdout().lock();
            // A startup response lets the Python bridge resume monotonic time and IDs.
            serde_json::to_writer(
                &mut stdout,
                &Response {
                    effects: vec![],
                    state: engine.core(),
                },
            )?;
            writeln!(stdout)?;
            stdout.flush()?;
            for line in io::stdin().lock().lines() {
                let input: Request = serde_json::from_str(&line?)?;
                let effects = engine.process(input.at, input.event.clone())?;
                if paper {
                    let mut events = Vec::new();
                    for effect in &effects {
                        events.extend(venue.execute(engine.core().epoch, effect)?);
                    }
                    if let Event::Trade { taker, price, qty } = input.event {
                        events.extend(venue.trade(engine.core().epoch, taker, price, qty)?);
                    }
                    for event in events {
                        engine.process(input.at, event)?;
                    }
                }
                if let Some(logger) = telemetry.as_mut() {
                    logger.emit(format!(
                        "seq={} position={} health={:?}",
                        engine.core().seq,
                        engine.core().position,
                        engine.core().health
                    ));
                }
                serde_json::to_writer(
                    &mut stdout,
                    &Response {
                        effects,
                        state: engine.core(),
                    },
                )?;
                writeln!(stdout)?;
                stdout.flush()?;
            }
            if let Some(logger) = telemetry {
                eprintln!("telemetry_dropped={}", logger.finish()?);
            }
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
                "MiniNautilus\n  demo\n  serve JOURNAL [--recover|CONFIG.json]\n  paper JOURNAL [CONFIG.json]\n  inspect JOURNAL\n  snapshot JOURNAL OUTPUT"
            );
            Ok(())
        }
    }
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
