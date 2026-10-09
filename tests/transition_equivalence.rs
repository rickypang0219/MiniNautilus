//! Differential contract against the frozen full-clone implementation at 0580ad1.
//! Keep the reference unchanged when editing production handlers.
pub use mininautilus::model;
use mininautilus::{core::Core, model::*};
#[path = "support/reference_core.rs"]
mod reference;

struct Pair {
    core: Core,
    reference: reference::Core,
}
impl Pair {
    fn from(core: Core) -> Self {
        let reference = serde_json::from_str(&serde_json::to_string(&core).unwrap()).unwrap();
        Self { core, reference }
    }
    fn envelope(&mut self, input: Envelope) {
        let actual = self.core.apply(&input);
        let expected = self.reference.apply(&input);
        assert_eq!(actual, expected, "effects/error: {input:?}");
        assert_eq!(
            serde_json::to_string(&self.core).unwrap(),
            serde_json::to_string(&self.reference).unwrap(),
            "state: {input:?}"
        );
        // Derived open/deadline indexes must equal a full scan after every event.
        assert!(self.core.index_consistent(), "index: {input:?}");
        let (mut lo, mut hi) = (self.core.position as i128, self.core.position as i128);
        for order in self.core.orders.values() {
            match order.intent.side {
                Side::Buy => hi += order.remaining() as i128,
                Side::Sell => lo -= order.remaining() as i128,
            }
        }
        assert_eq!(self.core.exposure_bounds(), (lo, hi), "exposure: {input:?}");
    }
    fn send(&mut self, at: Time, event: Event) {
        self.envelope(Envelope {
            seq: self.core.seq + 1,
            at,
            event,
        });
    }
    fn report(&mut self, report: Report) {
        self.send(
            self.core.now,
            Event::Execution {
                epoch: self.core.epoch,
                venue_seq: self.core.venue_seq + 1,
                report,
            },
        );
    }
}

fn intent(id: u64, seq: u64) -> Intent {
    Intent {
        id,
        side: Side::Buy,
        qty: 3,
        limit: 100,
        based_on_seq: seq,
        valid_until: u64::MAX,
    }
}
fn pending() -> Pair {
    let mut p = Pair::from(Core::new(Config::default()).unwrap());
    p.send(0, Event::Quote { bid: 99, ask: 101 });
    p.send(0, Event::Submit(intent(1, p.core.seq)));
    p
}

#[test]
fn arithmetic_and_envelope_failures_match_full_clone_rollback() {
    for (position, cash) in [(i64::MAX, 0), (0, i128::MIN)] {
        let mut p = pending();
        p.core.position = position;
        p.core.cash = cash;
        p = Pair::from(p.core);
        p.report(Report::Fill(Fill {
            execution_id: 1,
            order_id: 1,
            qty: 1,
            price: 100,
        }));
        assert_eq!(p.core.position, position);
        assert_eq!(p.core.cash, cash);
        assert!(p.core.fills.is_empty());
        assert_eq!(p.core.health, Health::Reconciling);
    }
    let mut p = pending();
    p.send(u64::MAX, Event::Cancel { id: 1 }); // deadline overflow after old handler's first write
    assert_eq!(p.core.orders[&1].pending, Some(PendingAction::Submit));
    p.envelope(Envelope {
        seq: p.core.seq + 2,
        at: u64::MAX,
        event: Event::Kill,
    });
    p.envelope(Envelope {
        seq: p.core.seq + 1,
        at: 0,
        event: Event::Kill,
    });
    p.core.epoch = u64::MAX;
    p = Pair::from(p.core);
    p.send(u64::MAX, Event::Reconnect);
    p.core.seq = u64::MAX;
    p = Pair::from(p.core);
    p.envelope(Envelope {
        seq: 0,
        at: u64::MAX,
        event: Event::Kill,
    });
}

#[test]
fn gaps_duplicates_and_reconciliation_match_full_clone() {
    let mut p = pending();
    p.report(Report::Fill(Fill {
        execution_id: 1,
        order_id: 1,
        qty: 1,
        price: 100,
    }));
    p.report(Report::Accepted { id: 1 });
    p.report(Report::Fill(Fill {
        execution_id: 1,
        order_id: 1,
        qty: 1,
        price: 100,
    }));
    p.report(Report::Canceled {
        id: 1,
        cumulative_filled: 2,
    });
    p.send(
        1,
        Event::Execution {
            epoch: 0,
            venue_seq: 100,
            report: Report::Fill(Fill {
                execution_id: 2,
                order_id: 1,
                qty: 99,
                price: 100,
            }),
        },
    );
    assert_ne!(p.core.venue_seq, 100); // discard staged gap watermark on invalid fill
    let snapshot = Reconciliation {
        epoch: 0,
        watermark: 100,
        position: 1,
        orders: vec![VenueOrder {
            intent: p.core.orders[&1].intent.clone(),
            filled: 1,
            lifecycle: Lifecycle::Canceled,
        }],
        fills: p.core.fills.values().cloned().collect(),
        absent: vec![],
    };
    let mut invalid = snapshot.clone();
    invalid.position = 2;
    p.send(2, Event::Reconcile(invalid));
    p.send(3, Event::Reconcile(snapshot));
    assert_eq!(p.core.health, Health::Healthy);
    p.report(Report::Accepted { id: 1 });
    p.send(4, Event::Disconnect);
    p.send(5, Event::Quote { bid: -1, ask: 100 });
    assert_eq!(p.core.health, Health::Disconnected);
}

fn random(state: &mut u64) -> u64 {
    *state ^= *state << 13;
    *state ^= *state >> 7;
    *state ^= *state << 17;
    *state
}

#[test]
fn seeded_mixed_events_preserve_every_state_and_effect() {
    for seed in 1..=64 {
        let mut rng = seed;
        let mut p = Pair::from(Core::new(Config::default()).unwrap());
        for index in 0..256 {
            let at = p.core.now + random(&mut rng) % 5;
            let id = random(&mut rng) % 24 + 1;
            let qty = (random(&mut rng) % 5) as i64;
            let mut order = intent(id, p.core.seq);
            order.qty = qty;
            order.side = if random(&mut rng).is_multiple_of(2) {
                Side::Buy
            } else {
                Side::Sell
            };
            let event = match random(&mut rng) % 18 {
                0 => Event::Quote { bid: 99, ask: 101 },
                1 => Event::QuoteObserved {
                    bid: 99,
                    ask: 101,
                    observed_at: at + random(&mut rng) % 2,
                },
                2 => Event::MarketUnavailable,
                3 => Event::Trade {
                    taker: Side::Buy,
                    price: 100,
                    qty,
                },
                4 | 5 => Event::Submit(order),
                6 => Event::Cancel { id },
                7 => Event::SetTarget(Target {
                    revision: index,
                    position: qty,
                    valid_until: at + 5,
                }),
                8 => Event::SubmitTargeted {
                    intent: order,
                    revision: index.saturating_sub(1),
                    expected_position: p.core.position,
                },
                9..=12 => {
                    let report = match random(&mut rng) % 5 {
                        0 => Report::Accepted { id },
                        1 => Report::Rejected { id },
                        2 => Report::CancelRejected { id },
                        3 => Report::Canceled {
                            id,
                            cumulative_filled: qty,
                        },
                        _ => Report::Fill(Fill {
                            execution_id: random(&mut rng) % 32,
                            order_id: id,
                            qty,
                            price: 100,
                        }),
                    };
                    Event::Execution {
                        epoch: p.core.epoch,
                        venue_seq: p.core.venue_seq + random(&mut rng) % 3,
                        report,
                    }
                }
                13 => Event::Tick,
                14 => Event::Disconnect,
                15 => Event::Reconnect,
                16 => Event::Heartbeat {
                    epoch: p.core.epoch,
                },
                _ => Event::Reconcile(Reconciliation {
                    epoch: p.core.epoch,
                    watermark: p.core.venue_seq,
                    position: p.core.position,
                    orders: p
                        .core
                        .orders
                        .values()
                        .map(|o| VenueOrder {
                            intent: o.intent.clone(),
                            filled: o.filled,
                            lifecycle: if o.lifecycle == Lifecycle::Pending {
                                Lifecycle::Accepted
                            } else {
                                o.lifecycle
                            },
                        })
                        .collect(),
                    fills: p.core.fills.values().cloned().collect(),
                    absent: vec![],
                }),
            };
            p.send(at, event);
        }
        p.send(p.core.now, Event::Kill);
    }
}
