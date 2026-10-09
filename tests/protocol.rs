//! Protocol 2: applying compact deltas reproduces the complete state JSON, and
//! response size does not depend on retained history.
use mininautilus::{core::Core, model::*, protocol::Response, sim::PaperExchange};
use serde_json::Value;

fn random(state: &mut u64) -> u64 {
    *state ^= *state << 13;
    *state ^= *state >> 7;
    *state ^= *state << 17;
    *state
}

/// Mirror of what the Python bridge does with a response.
fn receive(mirror: &mut Value, response: Value) {
    if let Some(state) = response.get("state") {
        *mirror = state.clone();
        return;
    }
    let delta = &response["delta"];
    for (key, value) in delta["header"].as_object().unwrap() {
        mirror[key] = value.clone();
    }
    for map in ["orders", "fills"] {
        for (key, value) in delta[map].as_object().unwrap() {
            mirror[map][key] = value.clone();
        }
    }
}

struct Session {
    core: Core,
    venue: PaperExchange,
    mirror: Value,
    full_responses: usize,
    /// Compare the mirror with a full serialization after every request.
    verify_each: bool,
}

impl Session {
    fn new() -> Self {
        let mut core = Core::new(Config {
            max_abs_position: 50,
            ..Config::default()
        })
        .unwrap();
        core.track_changes();
        let mirror = serde_json::to_value(Response::full(&core, vec![])).unwrap()["state"].clone();
        Self {
            core,
            venue: PaperExchange::new(),
            mirror,
            full_responses: 0,
            verify_each: true,
        }
    }

    fn apply(&mut self, at: Time, event: Event) -> Vec<Effect> {
        let seq = self.core.seq + 1;
        self.core.apply(&Envelope { seq, at, event }).unwrap()
    }

    /// One request: the event, plus venue reports, like `mininautilus paper`.
    fn request(&mut self, at: Time, event: Event) -> Value {
        let effects = self.apply(at, event.clone());
        let mut reports = Vec::new();
        for effect in &effects {
            reports.extend(self.venue.execute(self.core.epoch, effect).unwrap());
        }
        if let Event::Trade { taker, price, qty } = event {
            reports.extend(
                self.venue
                    .trade(self.core.epoch, taker, price, qty)
                    .unwrap(),
            );
        }
        for report in reports {
            self.apply(at, report);
        }
        let changes = self.core.take_changes();
        let response =
            serde_json::to_value(Response::compact(&self.core, effects, changes)).unwrap();
        if response.get("state").is_some() {
            self.full_responses += 1;
        }
        receive(&mut self.mirror, response.clone());
        if self.verify_each {
            self.verify();
        }
        response
    }

    fn verify(&self) {
        assert_eq!(self.mirror, serde_json::to_value(&self.core).unwrap());
    }
}

#[test]
fn deltas_reproduce_full_state_through_faults_and_reconciliation() {
    let mut replaced = 0;
    for seed in 1..=32u64 {
        let mut rng = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
        let mut s = Session::new();
        let mut next_id = 1;
        for step in 0..300u64 {
            let at = step * 10;
            let event = match random(&mut rng) % 12 {
                0..=2 => Event::Quote { bid: 99, ask: 101 },
                3..=5 => {
                    let side = if random(&mut rng).is_multiple_of(2) {
                        Side::Buy
                    } else {
                        Side::Sell
                    };
                    let intent = Intent {
                        id: next_id,
                        side,
                        qty: 1 + (random(&mut rng) % 3) as i64,
                        limit: 99 + (random(&mut rng) % 3) as i64,
                        based_on_seq: s.core.seq,
                        valid_until: at + 1_000,
                    };
                    next_id += 1;
                    s.venue.drop_next_ack = random(&mut rng).is_multiple_of(5);
                    Event::Submit(intent)
                }
                6 | 7 => Event::Trade {
                    taker: if random(&mut rng).is_multiple_of(2) {
                        Side::Buy
                    } else {
                        Side::Sell
                    },
                    price: 99 + (random(&mut rng) % 3) as i64,
                    qty: 1 + (random(&mut rng) % 4) as i64,
                },
                8 if next_id > 1 => Event::Cancel {
                    id: 1 + random(&mut rng) % (next_id - 1),
                },
                9 => Event::Tick,
                10 => Event::Heartbeat {
                    epoch: s.core.epoch,
                },
                _ => {
                    // Disconnect, then reconnect: the venue answers QueryState with a
                    // full snapshot, which replaces history (a full response).
                    s.request(at, Event::Disconnect);
                    Event::Reconnect
                }
            };
            s.request(at, event);
        }
        assert!(s.core.fills.len() > 5, "seed {seed} exercised fills");
        replaced += s.full_responses;
    }
    assert!(
        replaced > 0,
        "reconciliation must exercise full-state responses"
    );
}

#[test]
fn response_size_does_not_grow_with_history() {
    let size_after = |history: u64| {
        let mut s = Session::new();
        s.verify_each = false; // O(history) per request; checked once below
        s.request(0, Event::Quote { bid: 100, ask: 100 });
        for id in 1..=history {
            let side = if id % 2 == 1 { Side::Buy } else { Side::Sell };
            let intent = Intent {
                id,
                side,
                qty: 1,
                limit: 100,
                based_on_seq: s.core.seq,
                valid_until: u64::MAX,
            };
            s.request(0, Event::Submit(intent));
            let taker = if side == Side::Buy {
                Side::Sell
            } else {
                Side::Buy
            };
            s.request(
                0,
                Event::Trade {
                    taker,
                    price: 100,
                    qty: 1,
                },
            );
        }
        assert_eq!(s.core.orders.len() as u64, history);
        assert_eq!(s.full_responses, 0);
        let quote = s.request(0, Event::Quote { bid: 100, ask: 100 });
        s.verify();
        serde_json::to_vec(&quote).unwrap().len()
    };
    let (small, large) = (size_after(0), size_after(5_000));
    // Only header digits (seq) may grow; history must not appear in the response.
    assert!(large <= small + 16, "{small} vs {large} bytes");
}

#[test]
fn untracked_core_never_accumulates_changes() {
    let mut core = Core::new(Config::default()).unwrap();
    core.apply(&Envelope {
        seq: 1,
        at: 0,
        event: Event::Quote { bid: 99, ask: 101 },
    })
    .unwrap();
    assert_eq!(core.take_changes(), None);
}
