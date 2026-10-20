//! Open-order indexes: equal to full scans, rebuilt on load, and history-free.
use mininautilus::{core::Core, model::*, sim::PaperExchange};
use std::collections::BTreeMap;

fn apply(core: &mut Core, at: Time, event: Event) -> Vec<Effect> {
    let seq = core.seq + 1;
    core.apply(&Envelope { seq, at, event }).unwrap()
}

fn random(state: &mut u64) -> u64 {
    *state ^= *state << 13;
    *state ^= *state >> 7;
    *state ^= *state << 17;
    *state
}

/// Frozen copy of the pre-index `PaperExchange::trade` loop (full history scan).
struct ScanVenue {
    orders: BTreeMap<OrderId, VenueOrder>,
    position: i64,
    fills: Vec<Fill>,
}
impl ScanVenue {
    fn trade(&mut self, taker: Side, price: i64, mut qty: i64) -> Vec<Fill> {
        let mut out = Vec::new();
        for order in self.orders.values_mut() {
            if qty == 0 {
                break;
            }
            if order.lifecycle.terminal() || order.intent.side == taker {
                continue;
            }
            let crosses = match order.intent.side {
                Side::Buy => price <= order.intent.limit,
                Side::Sell => price >= order.intent.limit,
            };
            if !crosses {
                continue;
            }
            let filled = qty.min(order.intent.qty - order.filled);
            qty -= filled;
            order.filled += filled;
            order.lifecycle = if order.filled == order.intent.qty {
                Lifecycle::Filled
            } else {
                Lifecycle::Partial
            };
            self.position += order.intent.side.sign() * filled;
            let fill = Fill {
                execution_id: self.fills.len() as u64 + 1,
                order_id: order.intent.id,
                qty: filled,
                price,
            };
            self.fills.push(fill.clone());
            out.push(fill);
        }
        out
    }
}

fn fills_of(events: Vec<Event>) -> Vec<Fill> {
    events
        .into_iter()
        .filter_map(|event| match event {
            Event::Execution {
                report: Report::Fill(fill),
                ..
            } => Some(fill),
            _ => None,
        })
        .collect()
}

#[test]
fn indexed_matching_equals_full_history_scan() {
    for seed in 1..=64u64 {
        let mut rng = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
        let mut venue = PaperExchange::new();
        let mut scan = ScanVenue {
            orders: BTreeMap::new(),
            position: 0,
            fills: Vec::new(),
        };
        let mut next_id = 1;
        for _ in 0..400 {
            match random(&mut rng) % 4 {
                0 | 1 => {
                    let intent = Intent {
                        id: next_id,
                        side: if random(&mut rng).is_multiple_of(2) {
                            Side::Buy
                        } else {
                            Side::Sell
                        },
                        qty: 1 + (random(&mut rng) % 4) as i64,
                        limit: 98 + (random(&mut rng) % 5) as i64,
                        based_on_seq: 0,
                        valid_until: 0,
                    };
                    next_id += 1;
                    venue
                        .execute(0, &Effect::SendOrder(intent.clone()))
                        .unwrap();
                    scan.orders.insert(
                        intent.id,
                        VenueOrder {
                            intent,
                            filled: 0,
                            lifecycle: Lifecycle::Accepted,
                        },
                    );
                }
                2 if next_id > 1 => {
                    let id = 1 + random(&mut rng) % (next_id - 1);
                    venue.execute(0, &Effect::SendCancel { id }).unwrap();
                    if let Some(order) = scan.orders.get_mut(&id)
                        && !order.lifecycle.terminal()
                    {
                        order.lifecycle = Lifecycle::Canceled;
                    }
                }
                _ => {
                    let taker = if random(&mut rng).is_multiple_of(2) {
                        Side::Buy
                    } else {
                        Side::Sell
                    };
                    let price = 98 + (random(&mut rng) % 5) as i64;
                    let qty = 1 + (random(&mut rng) % 6) as i64;
                    let actual = fills_of(venue.trade(0, taker, price, qty).unwrap());
                    assert_eq!(actual, scan.trade(taker, price, qty), "seed {seed}");
                }
            }
            assert_eq!(venue.position, scan.position);
            assert_eq!(venue.orders, scan.orders);
        }
        assert_eq!(venue.fills, scan.fills);
    }
}

#[test]
fn deserialized_core_rebuilds_indexes() {
    let mut core = Core::new(Config::default()).unwrap();
    apply(&mut core, 0, Event::Quote { bid: 99, ask: 101 });
    for id in 1..=3 {
        let side = if id == 2 { Side::Sell } else { Side::Buy };
        let intent = Intent {
            id,
            side,
            qty: 2,
            limit: 100,
            based_on_seq: core.seq,
            valid_until: 10,
        };
        apply(&mut core, 0, Event::Submit(intent));
    }
    assert_eq!(core.open_orders().count(), 3);
    let loaded: Core = serde_json::from_str(&serde_json::to_string(&core).unwrap()).unwrap();
    assert!(loaded.index_consistent());
    assert_eq!(loaded, core);
    assert_eq!(loaded.exposure_bounds(), (-2, 4));
    // The deadline index survives the round trip: the timeout still fires.
    let mut loaded = loaded;
    apply(&mut loaded, 200, Event::Tick);
    assert!(loaded.orders.values().all(|o| o.uncertain));
    assert_eq!(loaded.health, Health::Reconciling);
}

#[test]
fn direct_history_edits_are_repaired_by_reindex() {
    let mut core = Core::new(Config::default()).unwrap();
    core.orders.insert(
        7,
        Order {
            intent: Intent {
                id: 7,
                side: Side::Buy,
                qty: 3,
                limit: 100,
                based_on_seq: 0,
                valid_until: 0,
            },
            filled: 0,
            lifecycle: Lifecycle::Accepted,
            pending: None,
            deadline: None,
            uncertain: false,
        },
    );
    assert!(!core.index_consistent());
    core.reindex();
    assert!(core.index_consistent());
    assert_eq!(core.exposure_bounds(), (0, 3));
}

/// Completed history must leave nothing for risk, timers or matching to walk.
#[test]
fn completed_history_leaves_no_open_work() {
    let mut core = Core::new(Config::default()).unwrap();
    let mut venue = PaperExchange::new();
    apply(&mut core, 0, Event::Quote { bid: 100, ask: 100 });
    for id in 1..=5_000 {
        let side = if id % 2 == 1 { Side::Buy } else { Side::Sell };
        let intent = Intent {
            id,
            side,
            qty: 1,
            limit: 100,
            based_on_seq: core.seq,
            valid_until: 1_000,
        };
        for effect in apply(&mut core, 0, Event::Submit(intent)) {
            for report in venue.execute(core.epoch, &effect).unwrap() {
                apply(&mut core, 0, report);
            }
        }
        let taker = if side == Side::Buy {
            Side::Sell
        } else {
            Side::Buy
        };
        for report in venue.trade(core.epoch, taker, 100, 1).unwrap() {
            apply(&mut core, 0, report);
        }
    }
    assert_eq!(core.orders.len(), 5_000);
    assert_eq!(core.open_orders().count(), 0);
    assert!(core.index_consistent());
    assert!(apply(&mut core, 0, Event::Tick).is_empty());
    assert!(venue.trade(0, Side::Sell, 1, 1).unwrap().is_empty());
}
