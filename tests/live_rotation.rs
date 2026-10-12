//! G3: live rotation keeps exact dedup evidence in a cold index (docs/adr-live-rotation.md).
use mininautilus::{
    cold::ColdIndex,
    journal::{self, DurableEngine},
    model::*,
};
use std::{
    fs,
    path::{Path, PathBuf},
};

fn dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("mini-live-rotate-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir_all(&path).unwrap();
    path
}

fn config() -> Config {
    Config {
        max_abs_position: 1_000,
        request_timeout_ms: 10_000,
        private_stale_ms: 1_000_000,
        market_stale_ms: 1_000_000,
        ..Config::default()
    }
}

fn report(engine: &mut DurableEngine, at: Time, report: Report) -> Vec<Effect> {
    let (epoch, venue_seq) = (engine.core().epoch, engine.core().venue_seq + 1);
    engine
        .process(
            at,
            Event::Execution {
                epoch,
                venue_seq,
                report,
            },
        )
        .unwrap()
}

/// `count` buy orders from `first`, each accepted and filled once at 100;
/// execution IDs are globally unique (`order id * 10`).
fn trade(engine: &mut DurableEngine, at: &mut Time, first: OrderId, count: u64) {
    for id in first..first + count {
        *at += 10;
        engine
            .process(*at, Event::Quote { bid: 99, ask: 100 })
            .unwrap();
        let seq = engine.core().seq;
        let effects = engine
            .process(
                *at,
                Event::Submit(Intent {
                    id,
                    side: Side::Buy,
                    qty: 1,
                    limit: 100,
                    based_on_seq: seq,
                    valid_until: *at + 1_000,
                }),
            )
            .unwrap();
        assert!(matches!(effects[..], [Effect::SendOrder(_)]), "{effects:?}");
        report(engine, *at, Report::Accepted { id });
        report(engine, *at, Report::Fill(fill(id)));
    }
}

fn fill(id: OrderId) -> Fill {
    Fill {
        execution_id: id * 10,
        order_id: id,
        qty: 1,
        price: 100,
    }
}

/// The adapter's barrier: Disconnect, Reconnect, Reconcile with the venue history.
fn reconcile(engine: &mut DurableEngine, at: Time) {
    engine.process(at, Event::Disconnect).unwrap();
    engine.process(at, Event::Reconnect).unwrap();
    let core = engine.core();
    let orders = core
        .orders
        .values()
        .map(|o| VenueOrder {
            intent: o.intent.clone(),
            filled: o.filled,
            lifecycle: o.lifecycle,
        })
        .collect();
    let snapshot = Reconciliation {
        epoch: core.epoch,
        watermark: core.venue_seq,
        orders,
        fills: core.fills.values().cloned().collect(),
        position: core.position - core.opening_position,
        absent: vec![],
    };
    engine.process(at, Event::Reconcile(snapshot)).unwrap();
    assert_eq!(engine.core().health, Health::Healthy);
}

fn rotated_session(name: &str) -> (PathBuf, PathBuf, DurableEngine, Time) {
    let directory = dir(name);
    let first = directory.join("events.jsonl");
    let second = directory.join("events-1.jsonl");
    let mut engine = DurableEngine::create(&first, config()).unwrap();
    let mut at = 0;
    trade(&mut engine, &mut at, 1, 5);
    reconcile(&mut engine, at);
    let mut next = engine.rotate_into_retaining(&second).unwrap();
    reconcile(&mut next, at);
    (first, second, next, at)
}

#[test]
fn rotation_frees_history_and_carries_position_floor_and_cold_reference() {
    let (first, second, engine, _) = rotated_session("carry");
    let core = engine.core();
    assert!(core.orders.is_empty() && core.fills.is_empty());
    assert_eq!((core.position, core.id_floor), (5, 5));
    let cold = core
        .cold
        .clone()
        .expect("live rotation records a cold index");
    assert_eq!(
        (cold.orders, cold.fills, cold.file.as_str()),
        (5, 5, "events-1.jsonl.cold")
    );
    let index = ColdIndex::open(second.parent().unwrap(), &cold).unwrap();
    assert_eq!(index.fill(30).unwrap(), Some(fill(3)));
    assert_eq!(
        index.order(4).unwrap().unwrap().lifecycle,
        Lifecycle::Filled
    );
    assert!(index.order(6).unwrap().is_none());
    // The archive is closed and still replays; it cannot be resumed.
    assert_eq!(journal::replay(&first).unwrap().position, 5);
    assert!(DurableEngine::recover(&first, None).is_err());
}

#[test]
fn late_duplicate_of_a_retired_fill_is_exact_dedup_not_a_gate() {
    let (_, second, mut engine, at) = rotated_session("dup");
    let before = engine.core().venue_seq;
    report(&mut engine, at, Report::Fill(fill(2)));
    report(
        &mut engine,
        at,
        Report::Canceled {
            id: 3,
            cumulative_filled: 1,
        },
    );
    report(&mut engine, at, Report::Accepted { id: 1 });
    let core = engine.core();
    assert_eq!(core.health, Health::Healthy);
    assert_eq!(core.position, 5);
    assert_eq!(
        core.venue_seq,
        before + 3,
        "duplicates consume their sequence"
    );
    assert!(
        core.orders.is_empty() && core.fills.is_empty(),
        "nothing re-enters RAM"
    );
    drop(engine);
    // The verdict is journaled: replay never needs the cold file.
    let text = fs::read_to_string(&second).unwrap();
    assert!(text.contains("Retired") && text.contains("Duplicate"));
    fs::remove_file(second.with_file_name("events-1.jsonl.cold")).unwrap();
    assert_eq!(journal::replay(&second).unwrap().position, 5);
}

#[test]
fn contradictions_and_unavailable_evidence_gate() {
    let (_, _, mut engine, at) = rotated_session("contra");
    let mut changed = fill(2);
    changed.qty = 2;
    report(&mut engine, at, Report::Fill(changed));
    assert_eq!(engine.core().health, Health::Reconciling);
    reconcile(&mut engine, at);
    report(
        &mut engine,
        at,
        Report::Canceled {
            id: 3,
            cumulative_filled: 0,
        },
    );
    assert_eq!(engine.core().health, Health::Reconciling);
    reconcile(&mut engine, at);
    // A brand-new fill on a retired order (unknown execution ID) is a contradiction.
    report(
        &mut engine,
        at,
        Report::Fill(Fill {
            execution_id: 999,
            order_id: 1,
            qty: 1,
            price: 100,
        }),
    );
    assert_eq!(engine.core().health, Health::Reconciling);
    assert_eq!(engine.core().position, 5);
}

#[test]
fn corrupt_cold_index_fails_closed() {
    let (_, second, engine, at) = rotated_session("corrupt");
    drop(engine);
    let cold = second.with_file_name("events-1.jsonl.cold");
    let mut bytes = fs::read(&cold).unwrap();
    bytes[30] ^= 1;
    fs::write(&cold, bytes).unwrap();
    let mut engine = DurableEngine::recover(&second, None).unwrap();
    reconcile(&mut engine, at);
    report(&mut engine, at, Report::Fill(fill(2)));
    assert_eq!(engine.core().health, Health::Reconciling);
    let text = fs::read_to_string(&second).unwrap();
    assert!(text.contains("Unavailable"));
}

#[test]
fn callers_cannot_inject_a_retired_verdict() {
    let (_, _, mut engine, at) = rotated_session("inject");
    let (epoch, venue_seq) = (engine.core().epoch, engine.core().venue_seq + 1);
    let forged = Event::Execution {
        epoch,
        venue_seq,
        report: Report::Retired {
            report: Box::new(Report::Fill(fill(2))),
            verdict: RetiredVerdict::Duplicate,
        },
    };
    assert!(engine.process(at, forged).is_err());
}

#[test]
fn second_rotation_extends_the_index_and_rerun_after_crash_is_idempotent() {
    let (_, second, mut engine, mut at) = rotated_session("extend");
    trade(&mut engine, &mut at, 6, 3);
    reconcile(&mut engine, at);
    let third = second.with_file_name("events-2.jsonl");
    let next = engine.rotate_into_retaining(&third).unwrap();
    let cold = next.core().cold.clone().unwrap();
    assert_eq!((cold.orders, cold.fills, next.core().id_floor), (8, 8, 8));
    let index = ColdIndex::open(third.parent().unwrap(), &cold).unwrap();
    assert_eq!(index.fill(10).unwrap(), Some(fill(1)), "first segment kept");
    assert_eq!(index.fill(80).unwrap(), Some(fill(8)));
    drop(next);
    // Crash after Closed but before the successor: rerunning completes it identically.
    fs::remove_file(&third).unwrap();
    journal::rotate_retaining(&second, &third).unwrap();
    assert_eq!(journal::replay(&third).unwrap().cold, Some(cold));
}

#[test]
fn stray_cold_file_without_close_is_ignored_by_recovery() {
    let directory = dir("stray");
    let file = directory.join("events.jsonl");
    let mut engine = DurableEngine::create(&file, config()).unwrap();
    let mut at = 0;
    trade(&mut engine, &mut at, 1, 2);
    drop(engine);
    // Crash after the cold index was written but before the predecessor closed.
    fs::write(directory.join("events-1.jsonl.cold"), b"partial").unwrap();
    let engine = DurableEngine::recover(&file, None).unwrap();
    assert_eq!(engine.core().position, 2);
    assert!(engine.core().cold.is_none());
    assert!(!Path::new(&directory.join("events-1.jsonl")).exists());
}
