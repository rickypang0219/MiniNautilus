//! L3/L4: journal sync policy crash semantics and the persisted-but-unsent window.
use mininautilus::{
    journal::{DurableEngine, SyncPolicy},
    model::*,
    sim::PaperExchange,
};
use std::{
    fs,
    path::{Path, PathBuf},
};

fn journal(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("mini-{name}-{}.jsonl", std::process::id()));
    let _ = fs::remove_file(&path);
    path
}

fn intent(id: u64, seq: u64) -> Intent {
    Intent {
        id,
        side: Side::Buy,
        qty: 2,
        limit: 100,
        based_on_seq: seq,
        valid_until: 10_000,
    }
}

/// Recover (which forces Disconnect), reconnect, and return the venue's snapshot.
fn recover_and_query(path: &Path, venue: &mut PaperExchange) -> (DurableEngine, Reconciliation) {
    let mut engine = DurableEngine::recover(path, None).unwrap();
    assert_eq!(engine.core().health, Health::Disconnected);
    let at = engine.core().now;
    let effects = engine.process(at, Event::Reconnect).unwrap();
    let query = effects
        .iter()
        .find(|e| matches!(e, Effect::QueryState { .. }))
        .unwrap();
    let Event::Reconcile(snapshot) = venue.execute(engine.core().epoch, query).unwrap().remove(0)
    else {
        panic!("paper venue answers QueryState with a snapshot");
    };
    (engine, snapshot)
}

#[test]
fn persisted_but_never_sent_order_is_resolved_only_by_proof_of_absence() {
    let path = journal("unsent");
    let mut venue = PaperExchange::new();
    {
        let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
        engine
            .process(0, Event::Quote { bid: 99, ask: 101 })
            .unwrap();
        let effects = engine.process(1, Event::Submit(intent(1, 1))).unwrap();
        assert!(matches!(effects[..], [Effect::SendOrder(_)]));
        // Crash here: the input is durable, the SendOrder never reached the venue.
    }
    let (mut engine, snapshot) = recover_and_query(&path, &mut venue);
    assert_eq!(
        engine.core().exposure_bounds(),
        (0, 2),
        "unknown outcome keeps risk"
    );

    // A complete venue history without the order cannot be applied: it might
    // still arrive. The gate stays closed rather than guessing.
    let at = engine.core().now;
    engine
        .process(at, Event::Reconcile(snapshot.clone()))
        .unwrap();
    assert_ne!(engine.core().health, Health::Healthy);

    // The adapter proves absence (queried by client ID after the request expired).
    let at = engine.core().now;
    let effects = engine.process(at, Event::Reconnect).unwrap();
    assert!(
        effects
            .iter()
            .any(|e| matches!(e, Effect::QueryState { .. }))
    );
    let proven = Reconciliation {
        epoch: engine.core().epoch,
        absent: vec![1],
        ..venue.snapshot(engine.core().epoch)
    };
    engine.process(at, Event::Reconcile(proven)).unwrap();
    let core = engine.core();
    assert_eq!(core.health, Health::Healthy);
    assert_eq!(core.orders[&1].lifecycle, Lifecycle::Rejected);
    assert_eq!(core.exposure_bounds(), (0, 0));
    assert_eq!(core.position, 0);
    // The ID stays used: a late duplicate can never be confused with a new order.
    let at = core.now;
    engine
        .process(at, Event::Quote { bid: 99, ask: 101 })
        .unwrap();
    let seq = engine.core().seq;
    let effects = engine.process(at, Event::Submit(intent(1, seq))).unwrap();
    assert!(
        matches!(&effects[..], [Effect::Refused { reason, .. }] if reason.contains("already used"))
    );
    drop(engine);
    fs::remove_file(path).unwrap();
}

#[test]
fn absence_claim_for_an_acknowledged_or_filled_order_fails_closed() {
    let path = journal("absent-acked");
    let mut venue = PaperExchange::new();
    {
        let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
        engine
            .process(0, Event::Quote { bid: 99, ask: 101 })
            .unwrap();
        for effect in engine.process(1, Event::Submit(intent(1, 1))).unwrap() {
            for report in venue.execute(engine.core().epoch, &effect).unwrap() {
                engine.process(1, report).unwrap();
            }
        }
        for report in venue
            .trade(engine.core().epoch, Side::Sell, 100, 1)
            .unwrap()
        {
            engine.process(2, report).unwrap();
        }
        assert_eq!(engine.core().position, 1);
    }
    let (mut engine, snapshot) = recover_and_query(&path, &mut venue);
    let mut wrong = snapshot.clone();
    wrong.orders.clear();
    wrong.fills.clear();
    wrong.position = 0;
    wrong.absent = vec![1];
    let at = engine.core().now;
    engine.process(at, Event::Reconcile(wrong)).unwrap();
    assert_ne!(engine.core().health, Health::Healthy);
    assert_eq!(engine.core().position, 1, "known fill is never discarded");

    // The truthful snapshot (order received, partially filled) still recovers.
    let effects = engine.process(at, Event::Reconnect).unwrap();
    assert!(!effects.is_empty());
    let truthful = venue.snapshot(engine.core().epoch);
    engine.process(at, Event::Reconcile(truthful)).unwrap();
    assert_eq!(engine.core().health, Health::Healthy);
    assert_eq!(engine.core().position, 1);
    assert_eq!(engine.core().orders[&1].lifecycle, Lifecycle::Partial);
    drop(engine);
    fs::remove_file(path).unwrap();
}

/// Outbox: a process crash loses nothing; an OS crash may lose only the unsynced
/// suffix, which never contains an input whose effects left the process.
#[test]
fn outbox_policy_keeps_every_external_action_durable() {
    let path = journal("outbox");
    let mut venue = PaperExchange::new();
    let mut synced_len = 0;
    let mut sent = Vec::new();
    let final_core;
    {
        let config = Config {
            max_abs_position: 1_000,
            ..Config::default()
        };
        let mut engine = DurableEngine::create(&path, config).unwrap();
        engine.set_sync_policy(SyncPolicy::Outbox);
        let mut next_id = 1;
        for step in 0..60u64 {
            let mut inputs = vec![
                Event::Quote { bid: 99, ask: 101 },
                Event::Heartbeat { epoch: 0 },
            ];
            if step % 7 == 3 {
                inputs.push(Event::Submit(intent(next_id, engine.core().seq + 2)));
                next_id += 1;
            }
            if step % 7 == 5 {
                inputs.push(Event::Trade {
                    taker: Side::Sell,
                    price: 100,
                    qty: 1,
                });
            }
            for event in inputs {
                let (effects, stages) = engine.process_profiled(step, event.clone(), None).unwrap();
                if stages.synced {
                    synced_len = fs::metadata(&path).unwrap().len();
                }
                let mut reports = Vec::new();
                for effect in &effects {
                    if let Effect::SendOrder(intent) = effect {
                        assert!(stages.synced, "no external action before its sync");
                        sent.push(intent.id);
                    }
                    reports.extend(venue.execute(engine.core().epoch, effect).unwrap());
                }
                if let Event::Trade { taker, price, qty } = event {
                    reports.extend(venue.trade(engine.core().epoch, taker, price, qty).unwrap());
                }
                for report in reports {
                    engine.process(step, report).unwrap();
                }
            }
        }
        assert!(sent.len() >= 8);
        final_core = engine.core().clone();
        // Process crash: the engine is dropped without a final sync.
    }
    let written = fs::metadata(&path).unwrap().len();
    assert!(
        written > synced_len,
        "workload must leave an unsynced suffix"
    );

    // Process crash: written pages survive; replay reaches the same state.
    let recovered = DurableEngine::recover(&path, None).unwrap();
    assert_eq!(recovered.core().seq, final_core.seq + 1); // + forced Disconnect
    assert_eq!(recovered.core().orders.len(), final_core.orders.len());
    assert_eq!(recovered.core().fills, final_core.fills);
    drop(recovered);

    // OS crash: only the synced prefix survives (minus the Disconnect just added).
    let bytes = fs::read(&path).unwrap();
    fs::write(&path, &bytes[..synced_len as usize]).unwrap();
    let (mut engine, snapshot) = recover_and_query(&path, &mut venue);
    for id in &sent {
        assert!(
            engine.core().orders.contains_key(id),
            "sent order {id} lost"
        );
    }
    // Venue reports lost with the suffix are re-established by reconciliation.
    let at = engine.core().now;
    engine.process(at, Event::Reconcile(snapshot)).unwrap();
    assert_eq!(engine.core().health, Health::Healthy);
    assert_eq!(engine.core().position, final_core.position);
    assert_eq!(engine.core().fills, final_core.fills);
    drop(engine);
    fs::remove_file(path).unwrap();
}
