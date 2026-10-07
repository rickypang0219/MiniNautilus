use mininautilus::{core::Core, journal::DurableEngine, model::*, sim::PaperExchange};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
};

fn fresh() -> Core {
    let mut c = Core::new(Config::default()).unwrap();
    send(&mut c, 0, Event::Quote { bid: 99, ask: 101 });
    c
}
fn send(c: &mut Core, at: u64, event: Event) -> Vec<Effect> {
    c.apply(&Envelope {
        seq: c.seq + 1,
        at,
        event,
    })
    .unwrap()
}
fn submit(c: &mut Core, id: u64, side: Side, qty: i64) -> Vec<Effect> {
    let intent = Intent {
        id,
        side,
        qty,
        limit: 100,
        based_on_seq: c.seq,
        valid_until: c.now + 100,
    };
    send(c, c.now, Event::Submit(intent))
}
fn report(c: &mut Core, seq: u64, report: Report) {
    send(
        c,
        c.now,
        Event::Execution {
            epoch: c.epoch,
            venue_seq: seq,
            report,
        },
    );
}
fn fill(id: u64, execution_id: u64, qty: i64) -> Report {
    Report::Fill(Fill {
        execution_id,
        order_id: id,
        qty,
        price: 100,
    })
}

#[test]
fn lost_ack_disconnect_partial_fill_reconcile_duplicate() {
    let mut c = fresh();
    let mut venue = PaperExchange::new();
    venue.drop_next_ack = true;
    let effects = submit(&mut c, 1, Side::Buy, 5);
    for e in &effects {
        assert!(venue.execute(0, e).unwrap().is_empty());
    }
    send(&mut c, 100, Event::Tick);
    assert_eq!(c.health, Health::Reconciling);
    assert_eq!(c.exposure_bounds(), (0, 5));
    assert!(matches!(
        submit(&mut c, 2, Side::Buy, 1)[0],
        Effect::Refused { .. }
    ));
    send(&mut c, 101, Event::Disconnect);
    venue.connected = false;
    assert!(venue.trade(0, Side::Sell, 100, 2).unwrap().is_empty());
    send(&mut c, 102, Event::Reconnect);
    let epoch = c.epoch;
    send(&mut c, 102, Event::Reconcile(venue.snapshot(epoch)));
    report(&mut c, venue.venue_seq, fill(1, 1, 2));
    assert_eq!(c.position, 2);
    assert_eq!(c.cash, -200);
    assert_eq!(c.fills.len(), 1);
    assert_eq!(c.exposure_bounds(), (2, 5));
    assert_eq!(c.health, Health::Healthy);
    assert_eq!(venue.orders.len(), 1);
}

#[test]
fn reservations_are_immediate_and_opposite_sides_do_not_net() {
    let mut c = fresh();
    assert!(matches!(
        submit(&mut c, 1, Side::Buy, 8)[0],
        Effect::SendOrder(_)
    ));
    assert!(matches!(
        submit(&mut c, 2, Side::Buy, 3)[0],
        Effect::Refused { .. }
    ));
    submit(&mut c, 3, Side::Sell, 10);
    assert_eq!(c.exposure_bounds(), (-10, 8));
    assert!(matches!(
        submit(&mut c, 4, Side::Sell, 1)[0],
        Effect::Refused { .. }
    ));
}

#[test]
fn fills_before_ack_duplicate_and_delayed_ack_never_regress() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 3);
    report(&mut c, 1, fill(1, 1, 1));
    report(&mut c, 1, fill(1, 1, 1));
    report(&mut c, 2, fill(1, 2, 2));
    report(&mut c, 3, Report::Accepted { id: 1 });
    assert_eq!(c.position, 3);
    assert_eq!(c.orders[&1].lifecycle, Lifecycle::Filled);
    assert_eq!(c.exposure_bounds(), (3, 3));
}

#[test]
fn cancel_fill_race_keeps_reservation_until_terminal_confirmation() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 5);
    report(&mut c, 1, Report::Accepted { id: 1 });
    send(&mut c, 1, Event::Cancel { id: 1 });
    report(&mut c, 2, fill(1, 1, 2));
    assert_eq!(c.orders[&1].pending, Some(PendingAction::Cancel));
    assert_eq!(c.exposure_bounds(), (2, 5));
    report(
        &mut c,
        3,
        Report::Canceled {
            id: 1,
            cumulative_filled: 2,
        },
    );
    assert_eq!(c.exposure_bounds(), (2, 2));
}

#[test]
fn cancel_with_missing_fills_retains_risk_and_gates() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 5);
    report(
        &mut c,
        1,
        Report::Canceled {
            id: 1,
            cumulative_filled: 2,
        },
    );
    assert_eq!(c.health, Health::Reconciling);
    assert_eq!(c.position, 0);
    assert_eq!(c.orders[&1].remaining(), 5);
}

#[test]
fn resting_orders_do_not_timeout_but_pending_actions_do() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 5);
    report(&mut c, 1, Report::Accepted { id: 1 });
    send(&mut c, 1000, Event::Tick);
    assert_eq!(c.health, Health::Healthy);
    send(&mut c, 1001, Event::Cancel { id: 1 });
    send(&mut c, 1101, Event::Tick);
    assert_eq!(c.health, Health::Reconciling);
    assert_eq!(c.orders[&1].remaining(), 5);
}

#[test]
fn stale_market_private_stream_and_stale_python_intents_are_rejected() {
    let mut c = fresh();
    send(&mut c, 1001, Event::Tick);
    assert!(matches!(
        submit(&mut c, 1, Side::Buy, 1)[0],
        Effect::Refused { .. }
    ));
    send(&mut c, 5001, Event::Quote { bid: 99, ask: 101 });
    assert!(matches!(
        submit(&mut c, 1, Side::Buy, 1)[0],
        Effect::Refused { .. }
    ));
    let mut c = fresh();
    for _ in 0..101 {
        send(&mut c, 0, Event::Tick);
    }
    let intent = Intent {
        id: 1,
        side: Side::Buy,
        qty: 1,
        limit: 100,
        based_on_seq: 1,
        valid_until: 100,
    };
    assert!(matches!(
        send(&mut c, 0, Event::Submit(intent))[0],
        Effect::Refused { .. }
    ));
}

#[test]
fn invalid_overfill_rolls_back_accounting_and_gates() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 2);
    report(&mut c, 1, fill(1, 1, 3));
    assert_eq!((c.position, c.cash, c.fills.len()), (0, 0, 0));
    assert_eq!(c.orders[&1].filled, 0);
    assert_eq!(c.health, Health::Reconciling);
}

#[test]
fn execution_id_collision_is_not_silently_deduplicated() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 5);
    report(&mut c, 1, fill(1, 1, 1));
    report(&mut c, 2, fill(1, 1, 2));
    assert_eq!(c.position, 1);
    assert_eq!(c.health, Health::Reconciling);
}

#[test]
fn sequence_gap_and_old_session_reports() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 2);
    report(&mut c, 3, fill(1, 1, 1));
    assert_eq!(c.health, Health::Reconciling);
    send(&mut c, 1, Event::Disconnect);
    send(&mut c, 2, Event::Reconnect);
    send(
        &mut c,
        2,
        Event::Execution {
            epoch: 0,
            venue_seq: 4,
            report: fill(1, 2, 1),
        },
    );
    assert_eq!(c.position, 1);
}

#[test]
fn unknown_missing_and_unbalanced_snapshots_never_open_gate() {
    for case in 0..4 {
        let mut c = fresh();
        let mut venue = PaperExchange::new();
        let effects = submit(&mut c, 1, Side::Buy, 3);
        venue.execute(0, &effects[0]).unwrap();
        venue.trade(0, Side::Sell, 100, 1).unwrap();
        send(&mut c, 1, Event::Reconnect);
        let mut snapshot = venue.snapshot(c.epoch);
        match case {
            0 => snapshot.position = 99,
            1 => snapshot.orders.clear(),
            2 => {
                let mut unknown = snapshot.orders[0].clone();
                unknown.intent.id = 9;
                snapshot.orders.push(unknown);
            }
            _ => snapshot.fills.clear(),
        }
        send(&mut c, 2, Event::Reconcile(snapshot));
        assert_eq!(c.health, Health::Reconciling);
        assert_eq!(c.position, 0);
    }
}

#[test]
fn reconciliation_cannot_reopen_a_confirmed_canceled_order() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 3);
    report(
        &mut c,
        1,
        Report::Canceled {
            id: 1,
            cumulative_filled: 0,
        },
    );
    send(&mut c, 1, Event::Reconnect);
    let snapshot = Reconciliation {
        epoch: c.epoch,
        watermark: 1,
        position: 0,
        fills: vec![],
        orders: vec![VenueOrder {
            intent: c.orders[&1].intent.clone(),
            filled: 0,
            lifecycle: Lifecycle::Accepted,
        }],
    };
    send(&mut c, 2, Event::Reconcile(snapshot));
    assert_eq!(c.health, Health::Reconciling);
    assert_eq!(c.orders[&1].lifecycle, Lifecycle::Canceled);
}

#[test]
fn deterministic_replay_and_bad_envelope() {
    let mut a = fresh();
    let mut b = a.clone();
    let mut inputs = Vec::new();
    for id in 1..=30 {
        inputs.push(Envelope {
            seq: id + 1,
            at: id,
            event: Event::Submit(Intent {
                id,
                side: Side::Buy,
                qty: 1,
                limit: 100,
                based_on_seq: id,
                valid_until: 100,
            }),
        });
    }
    for input in &inputs {
        assert_eq!(a.apply(input), b.apply(input));
    }
    assert_eq!(a, b);
    assert!(a.apply(&inputs[0]).is_err());
    assert_eq!(a, b);
}

#[test]
fn kill_remains_latched_through_reconciliation_but_allows_cancel() {
    let mut c = fresh();
    submit(&mut c, 1, Side::Buy, 1);
    send(&mut c, 0, Event::Kill);
    assert!(matches!(
        submit(&mut c, 2, Side::Buy, 1)[0],
        Effect::Refused { .. }
    ));
    assert!(matches!(
        send(&mut c, 0, Event::Cancel { id: 1 })[0],
        Effect::SendCancel { .. }
    ));
}

static UNIQUE: AtomicUsize = AtomicUsize::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "mini-{}-{}",
            std::process::id(),
            UNIQUE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn durable_recovery_snapshot_tail_and_exclusive_writer() {
    let dir = Temp::new();
    let path = dir.0.join("events.jsonl");
    let snapshot = dir.0.join("snapshot.json");
    let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
    assert!(DurableEngine::recover(&path, None).is_err());
    engine
        .process(0, Event::Quote { bid: 99, ask: 101 })
        .unwrap();
    let effects = engine
        .process(
            1,
            Event::Submit(Intent {
                id: 1,
                side: Side::Buy,
                qty: 5,
                limit: 100,
                based_on_seq: 1,
                valid_until: 100,
            }),
        )
        .unwrap();
    assert!(matches!(effects[0], Effect::SendOrder(_)));
    engine.snapshot(&snapshot).unwrap();
    assert!(engine.snapshot(&path).is_err());
    engine
        .process(
            2,
            Event::Execution {
                epoch: 0,
                venue_seq: 1,
                report: fill(1, 1, 2),
            },
        )
        .unwrap();
    let expected = engine.core().clone();
    drop(engine);
    fs::OpenOptions::new()
        .append(true)
        .open(&path)
        .unwrap()
        .write_all(b"{torn")
        .unwrap();
    let recovered = DurableEngine::recover(&path, Some(&snapshot)).unwrap();
    assert_eq!(recovered.core().position, expected.position);
    assert_eq!(recovered.core().cash, expected.cash);
    assert_eq!(recovered.core().health, Health::Disconnected);
    assert_eq!(recovered.core().orders[&1].remaining(), 3);
    assert_eq!(recovered.core().seq, expected.seq + 1);
}

#[test]
fn committed_corruption_and_wrong_snapshot_fail_closed() {
    let dir = Temp::new();
    let path = dir.0.join("events.jsonl");
    let snapshot = dir.0.join("snapshot.json");
    let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
    engine
        .process(0, Event::Quote { bid: 99, ask: 101 })
        .unwrap();
    engine.snapshot(&snapshot).unwrap();
    drop(engine);
    let mut saved: serde_json::Value =
        serde_json::from_slice(&fs::read(&snapshot).unwrap()).unwrap();
    saved["core"]["position"] = 99.into();
    fs::write(&snapshot, serde_json::to_vec(&saved).unwrap()).unwrap();
    assert!(DurableEngine::recover(&path, Some(&snapshot)).is_err());
    let content = fs::read_to_string(&path).unwrap().replace("101", "102");
    fs::write(&path, content).unwrap();
    assert!(DurableEngine::recover(&path, None).is_err());
}

#[test]
fn all_permutations_of_ack_and_duplicate_fill_preserve_accounting() {
    // Delivery order can vary, but canonical fill IDs must count exactly once.
    for order in [
        [0, 1, 2],
        [0, 2, 1],
        [1, 0, 2],
        [1, 2, 0],
        [2, 0, 1],
        [2, 1, 0],
    ] {
        let mut c = fresh();
        submit(&mut c, 1, Side::Buy, 2);
        let reports = [Report::Accepted { id: 1 }, fill(1, 1, 2), fill(1, 1, 2)];
        for (i, n) in order.iter().enumerate() {
            report(&mut c, i as u64 + 1, reports[*n].clone());
        }
        assert_eq!((c.position, c.cash, c.fills.len()), (2, -200, 1));
        assert_eq!(c.orders[&1].lifecycle, Lifecycle::Filled);
    }
}

#[test]
fn rejected_order_cannot_later_be_accepted_or_canceled_without_reconciliation() {
    for contradictory in [
        Report::Accepted { id: 1 },
        Report::Canceled {
            id: 1,
            cumulative_filled: 0,
        },
    ] {
        let mut core = fresh();
        submit(&mut core, 1, Side::Buy, 3);
        report(&mut core, 1, Report::Rejected { id: 1 });
        assert_eq!(core.orders[&1].lifecycle, Lifecycle::Rejected);
        assert_eq!(core.health, Health::Healthy);
        report(&mut core, 2, contradictory);
        assert_eq!(core.health, Health::Reconciling);
        assert_eq!(core.orders[&1].lifecycle, Lifecycle::Rejected);
        assert_eq!(core.position, 0);
        assert_eq!(core.cash, 0);
        assert_eq!(core.exposure_bounds(), (0, 0));
        assert!(matches!(
            submit(&mut core, 2, Side::Buy, 1)[0],
            Effect::Refused { .. }
        ));
    }
}
