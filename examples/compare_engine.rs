//! Offline comparison driver: the production Core + PaperExchange, without IPC per
//! event or durable journal. Deliberately separate from the production `paper` CLI.
use mininautilus::{core::Core, model::*, sim::PaperExchange};
use serde::Deserialize;
use serde_json::json;
use std::{
    io::{self, Read},
    time::Instant,
};

#[derive(Deserialize)]
struct Workload {
    steps: Vec<Step>,
    #[serde(default)]
    probe_quotes: u64,
}
#[derive(Deserialize)]
struct Step {
    price: i64,
    volume: i64,
    taker: Side,
    #[serde(default)]
    actions: Vec<Action>,
}
#[derive(Deserialize)]
#[serde(tag = "kind")]
enum Action {
    Submit {
        id: u64,
        side: Side,
        qty: i64,
        limit: i64,
    },
    Cancel {
        id: u64,
    },
}
fn apply(core: &mut Core, at: u64, event: Event) -> Vec<Effect> {
    core.apply(&Envelope {
        seq: core.seq + 1,
        at,
        event,
    })
    .unwrap()
}
fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    let workload: Workload = serde_json::from_str(&input).unwrap();
    let mut core = Core::new(Config {
        max_abs_position: 1_000_000,
        max_order_qty: 1_000_000,
        max_order_notional: i64::MAX,
        private_stale_ms: u64::MAX,
        ..Config::default()
    })
    .unwrap();
    let mut venue = PaperExchange::new();
    let mut fills = Vec::new();
    let mut checkpoints = Vec::new();
    let start = Instant::now();
    for (i, step) in workload.steps.iter().enumerate() {
        let at = i as u64 * 60_000;
        apply(
            &mut core,
            at,
            Event::Quote {
                bid: step.price,
                ask: step.price,
            },
        );
        if step.volume > 0 {
            apply(
                &mut core,
                at,
                Event::Trade {
                    taker: step.taker,
                    price: step.price,
                    qty: step.volume,
                },
            );
            for event in venue
                .trade(core.epoch, step.taker, step.price, step.volume)
                .unwrap()
            {
                if let Event::Execution {
                    report: Report::Fill(fill),
                    ..
                } = &event
                {
                    fills.push(json!({"step":i,"id":fill.order_id,"side":core.orders[&fill.order_id].intent.side,"qty":fill.qty,"price":fill.price}));
                }
                apply(&mut core, at, event);
            }
        }
        for action in &step.actions {
            let event = match *action {
                Action::Submit {
                    id,
                    side,
                    qty,
                    limit,
                } => Event::Submit(Intent {
                    id,
                    side,
                    qty,
                    limit,
                    based_on_seq: core.seq,
                    valid_until: at + 1000,
                }),
                Action::Cancel { id } => Event::Cancel { id },
            };
            for effect in apply(&mut core, at, event) {
                assert!(
                    !matches!(effect, Effect::Refused { .. } | Effect::Alert(_)),
                    "{effect:?}"
                );
                for report in venue.execute(core.epoch, &effect).unwrap() {
                    apply(&mut core, at, report);
                }
            }
        }
        assert_eq!(core.health, Health::Healthy);
        assert_eq!(core.position, venue.position);
        checkpoints.push(json!({"step":i,"position":core.position,"cash":core.cash,"equity":core.cash + core.position as i128 * step.price as i128}));
    }
    let elapsed = start.elapsed().as_secs_f64();
    // A quote does not traverse matching/risk order loops. Repeating it with a
    // fixed completed history isolates Core::apply's transactional clone cost.
    let probe_start = Instant::now();
    for i in 0..workload.probe_quotes {
        let effects = apply(
            &mut core,
            workload.steps.len() as u64 * 60_000 + i,
            Event::Quote { bid: 100, ask: 100 },
        );
        assert!(effects.is_empty());
    }
    let probe_elapsed = probe_start.elapsed().as_secs_f64();
    println!(
        "{}",
        json!({"fills":fills,"checkpoints":checkpoints,"position":core.position,"cash":core.cash,"elapsed_seconds":elapsed,"events":core.seq,
            "probe_quotes":workload.probe_quotes,"probe_elapsed_seconds":probe_elapsed,"retained_orders":core.orders.len(),"retained_fills":core.fills.len()})
    );
}
