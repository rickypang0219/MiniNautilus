//! Residual history-scaling probes. Setup is excluded; no disk or Python IPC.
use mininautilus::{core::Core, model::*, sim::PaperExchange};
use serde_json::json;
use std::{hint::black_box, time::Instant};

fn apply(core: &mut Core, event: Event) -> Vec<Effect> {
    core.apply(&Envelope {
        seq: core.seq + 1,
        at: 0,
        event,
    })
    .unwrap()
}

fn main() {
    let mut rows = Vec::new();
    for count in [0, 100, 1000, 5000] {
        let mut core = Core::new(Config::default()).unwrap();
        let mut venue = PaperExchange::new();
        apply(&mut core, Event::Quote { bid: 100, ask: 100 });
        for id in 1..=count {
            let side = if id % 2 == 1 { Side::Buy } else { Side::Sell };
            let intent = Intent {
                id,
                side,
                qty: 1,
                limit: 100,
                based_on_seq: core.seq,
                valid_until: 1000,
            };
            for effect in apply(&mut core, Event::Submit(intent)) {
                assert!(!matches!(effect, Effect::Refused { .. } | Effect::Alert(_)));
                for report in venue.execute(core.epoch, &effect).unwrap() {
                    apply(&mut core, report);
                }
            }
            let taker = if side == Side::Buy {
                Side::Sell
            } else {
                Side::Buy
            };
            for report in venue.trade(core.epoch, taker, 100, 1).unwrap() {
                apply(&mut core, report);
            }
        }
        assert_eq!(core.orders.len(), count as usize);
        assert_eq!(core.fills.len(), count as usize);
        assert_eq!((core.position, core.cash), (0, 0));
        assert!(core.orders.values().all(|o| o.lifecycle.terminal()));
        for operation in ["exposure_bounds", "tick", "venue_trade", "serialize_core"] {
            let iterations = if operation == "serialize_core" {
                100
            } else {
                2000
            };
            let mut samples = Vec::new();
            for sample in 0..4 {
                let start = Instant::now();
                for _ in 0..iterations {
                    match operation {
                        "exposure_bounds" => {
                            black_box(black_box(&core).exposure_bounds());
                        }
                        "tick" => {
                            assert!(apply(black_box(&mut core), Event::Tick).is_empty());
                        }
                        "venue_trade" => {
                            assert!(
                                black_box(&mut venue)
                                    .trade(0, Side::Sell, 100, 1)
                                    .unwrap()
                                    .is_empty()
                            );
                        }
                        _ => {
                            black_box(serde_json::to_vec(black_box(&core)).unwrap());
                        }
                    }
                }
                let ns = start.elapsed().as_nanos() as f64 / iterations as f64;
                if sample > 0 {
                    samples.push(ns);
                }
            }
            rows.push(json!({"history_orders": count, "operation": operation, "iterations": iterations,
                "ns_per_call": samples, "state_json_bytes": serde_json::to_vec(&core).unwrap().len()}));
        }
    }
    println!("{}", serde_json::to_string_pretty(&rows).unwrap());
}
