//! H4: journal rotation bounds retained history while carrying account state.
use mininautilus::{
    journal::{self, DurableEngine},
    model::*,
    sim::PaperExchange,
};
use std::{
    fs,
    path::{Path, PathBuf},
};

fn path(name: &str, session: usize) -> PathBuf {
    std::env::temp_dir().join(format!(
        "mini-rotate-{name}-{}-{session}.jsonl",
        std::process::id()
    ))
}

fn config() -> Config {
    Config {
        max_abs_position: 1_000,
        ..Config::default()
    }
}

/// Route effects and reports through the session's paper venue.
fn step(
    engine: &mut DurableEngine,
    venue: &mut PaperExchange,
    at: Time,
    event: Event,
) -> Vec<Effect> {
    let effects = engine.process(at, event.clone()).unwrap();
    let mut reports = Vec::new();
    for effect in &effects {
        reports.extend(venue.execute(engine.core().epoch, effect).unwrap());
    }
    if let Event::Trade { taker, price, qty } = event {
        reports.extend(venue.trade(engine.core().epoch, taker, price, qty).unwrap());
    }
    for report in reports {
        engine.process(at, report).unwrap();
    }
    effects
}

/// After `recover` (forced Disconnect), reconcile against the session's venue.
fn reopen(file: &Path, venue: &mut PaperExchange) -> DurableEngine {
    let mut engine = DurableEngine::recover(file, None).unwrap();
    let at = engine.core().now;
    step(&mut engine, venue, at, Event::Reconnect);
    assert_eq!(engine.core().health, Health::Healthy);
    engine
}

#[test]
fn rotation_bounds_history_and_carries_balances_ids_and_kill() {
    const SESSIONS: usize = 5;
    const ORDERS: u64 = 120;
    let mut expected_position = 0i64;
    let mut expected_cash = 0i128;
    let mut largest_book = 0;
    let mut file = path("bound", 0);
    let _ = fs::remove_file(&file);
    DurableEngine::create(&file, config()).unwrap();
    let mut at = 0;
    for session in 0..SESSIONS {
        // Each session's venue is fresh: its positions are session-relative.
        let mut venue = PaperExchange::new();
        let mut engine = reopen(&file, &mut venue);
        let floor = engine.core().id_floor;
        assert_eq!(engine.core().position, expected_position);
        assert_eq!(engine.core().cash, expected_cash);
        assert!(
            engine.core().orders.is_empty(),
            "history does not carry over"
        );
        for i in 0..ORDERS {
            at += 10;
            step(
                &mut engine,
                &mut venue,
                at,
                Event::Quote { bid: 99, ask: 101 },
            );
            let side = if i % 3 == 0 { Side::Sell } else { Side::Buy };
            let id = floor + i + 1;
            let seq = engine.core().seq;
            let effects = step(
                &mut engine,
                &mut venue,
                at,
                Event::Submit(Intent {
                    id,
                    side,
                    qty: 2,
                    limit: 100,
                    based_on_seq: seq,
                    valid_until: at + 1_000,
                }),
            );
            assert!(matches!(effects[..], [Effect::SendOrder(_)]), "{effects:?}");
            let taker = if side == Side::Buy {
                Side::Sell
            } else {
                Side::Buy
            };
            // Partial fills leave orders open; they are canceled before rotation.
            let qty = 1 + (i % 2) as i64;
            step(
                &mut engine,
                &mut venue,
                at,
                Event::Trade {
                    taker,
                    price: 100,
                    qty,
                },
            );
            let epoch = engine.core().epoch;
            step(&mut engine, &mut venue, at, Event::Heartbeat { epoch });
        }
        let open: Vec<_> = engine.core().open_orders().map(|(id, _)| *id).collect();
        for id in open {
            step(&mut engine, &mut venue, at, Event::Cancel { id });
        }
        largest_book = largest_book.max(engine.core().orders.len());
        if session == SESSIONS - 1 {
            engine.process(at, Event::Kill).unwrap();
        }
        (expected_position, expected_cash) = (engine.core().position, engine.core().cash);
        drop(engine);

        let next = path("bound", session + 1);
        let _ = fs::remove_file(&next);
        let carried = journal::rotate(&file, &next).unwrap();
        assert_eq!(carried.position, expected_position);
        assert_eq!(carried.id_floor, floor + ORDERS);
        // The archive still replays; it can no longer be resumed for writing.
        assert_eq!(journal::replay(&file).unwrap().position, expected_position);
        let error = DurableEngine::recover(&file, None).err().unwrap();
        assert!(error.to_string().contains("rotated"), "{error}");
        fs::remove_file(&file).unwrap();
        file = next;
    }
    assert!(
        largest_book as u64 <= ORDERS,
        "retained orders bounded by one session"
    );
    assert_ne!(expected_position, 0, "workload carried a non-flat position");

    let mut venue = PaperExchange::new();
    let mut engine = reopen(&file, &mut venue);
    assert!(engine.core().killed, "kill latch survives rotation");
    assert_eq!(engine.core().position, expected_position);
    let seq = engine.core().seq;
    let effects = engine
        .process(
            at,
            Event::Submit(Intent {
                id: 1,
                side: Side::Buy,
                qty: 1,
                limit: 100,
                based_on_seq: seq,
                valid_until: at + 1_000,
            }),
        )
        .unwrap();
    assert!(
        matches!(&effects[..], [Effect::Refused { reason, .. }] if reason.contains("already used")),
        "archived client IDs are never reused: {effects:?}"
    );
    drop(engine);
    fs::remove_file(&file).unwrap();
}

#[test]
fn rotation_refuses_open_orders_and_unhealthy_books() {
    let file = path("refuse", 0);
    let next = path("refuse", 1);
    let _ = fs::remove_file(&file);
    let _ = fs::remove_file(&next);
    {
        let mut engine = DurableEngine::create(&file, config()).unwrap();
        engine
            .process(0, Event::Quote { bid: 99, ask: 101 })
            .unwrap();
        engine
            .process(
                0,
                Event::Submit(Intent {
                    id: 1,
                    side: Side::Buy,
                    qty: 1,
                    limit: 100,
                    based_on_seq: 1,
                    valid_until: 100,
                }),
            )
            .unwrap();
    }
    let error = journal::rotate(&file, &next).err().unwrap();
    assert!(error.to_string().contains("terminal"), "{error}");
    assert!(!next.exists());
    // Nothing was appended: the journal still recovers normally.
    let engine = DurableEngine::recover(&file, None).unwrap();
    assert_eq!(engine.core().orders.len(), 1);
    drop(engine);
    fs::remove_file(&file).unwrap();
}
