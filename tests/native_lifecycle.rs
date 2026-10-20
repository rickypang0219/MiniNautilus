#![cfg(feature = "native-runtime")]
use mininautilus::{
    journal::DurableEngine,
    model::*,
    native::{
        lifecycle, runtime,
        signing::{Action, Attempts},
        venue::Session,
    },
};
use serde_json::json;
use std::{fs, path::PathBuf};
fn dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("native-lifecycle-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir(&path).unwrap();
    path
}
#[test]
fn flat_terminal_is_not_enough_without_a_reconciliation_barrier() {
    let root = dir("barrier");
    let old = lifecycle::journal_path(&root, 0);
    let mut engine = DurableEngine::create(&old, runtime::config()).unwrap();
    engine.process(1, Event::Heartbeat { epoch: 0 }).unwrap();
    engine
        .process(
            1,
            Event::Quote {
                bid: 800000,
                ask: 800000,
            },
        )
        .unwrap();
    for (id, side) in [(1, Side::Buy), (2, Side::Sell)] {
        let effects = engine
            .process(
                1,
                Event::Submit(Intent {
                    id,
                    side,
                    qty: 20,
                    limit: 800000,
                    based_on_seq: engine.core().seq,
                    valid_until: 100,
                }),
            )
            .unwrap();
        assert!(effects.iter().any(|e| matches!(e, Effect::SendOrder(_))));
        engine
            .process(
                1,
                Event::Execution {
                    epoch: 0,
                    venue_seq: id,
                    report: Report::Fill(Fill {
                        execution_id: id,
                        order_id: id,
                        qty: 20,
                        price: 800000,
                    }),
                },
            )
            .unwrap();
    }
    assert_eq!(engine.core().position, 0);
    drop(engine);
    let before = fs::read(&old).unwrap();
    assert!(lifecycle::rotate(&root, 0).is_err());
    assert_eq!(before, fs::read(&old).unwrap());
    assert!(!lifecycle::journal_path(&root, 1).exists());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn explicit_rotation_cleanup_can_cancel_resting_orders_but_not_race_inflight_jobs() {
    let mut core = mininautilus::core::Core::new(runtime::config()).unwrap();
    core.apply(&Envelope {
        seq: 1,
        at: 1,
        event: Event::Heartbeat { epoch: 0 },
    })
    .unwrap();
    core.apply(&Envelope {
        seq: 2,
        at: 1,
        event: Event::Quote {
            bid: 800000,
            ask: 800000,
        },
    })
    .unwrap();
    core.apply(&Envelope {
        seq: 3,
        at: 1,
        event: Event::Submit(Intent {
            id: 1,
            side: Side::Buy,
            qty: 20,
            limit: 800000,
            based_on_seq: 2,
            valid_until: 100,
        }),
    })
    .unwrap();
    core.apply(&Envelope {
        seq: 4,
        at: 1,
        event: Event::Execution {
            epoch: 0,
            venue_seq: 1,
            report: Report::Accepted { id: 1 },
        },
    })
    .unwrap();
    let mut segment = runtime::Segment {
        journal: PathBuf::new(),
        orders: 4,
        bytes: 1000,
        age_seconds: 180,
        flatten_on_rotation: false,
        nonce_floor: 0,
        risk_cash_floor: None,
        expected_cash: None,
    };
    assert!(!segment.can_drain(&core, false, false));
    segment.flatten_on_rotation = true;
    assert!(segment.can_drain(&core, false, false));
    assert!(!segment.can_drain(&core, true, false));
    assert!(!segment.can_drain(&core, false, true));
    assert!(
        core.rotation_ready().is_err(),
        "starting cleanup does not grant rotation permission"
    );
}
#[test]
fn repeated_real_core_rotation_preserves_economic_state_and_bounds_active_orders() {
    let parent = dir("fixture");
    let root = parent.join("run");
    let report = lifecycle::fixture(&root, 104, 8).unwrap();
    assert_eq!(report["rotations"], 12);
    assert_eq!(report["position"], 0);
    assert_eq!(report["cash_tick_lots"], "0");
    assert!(report["retained_orders"].as_u64().unwrap() <= 8);
    fs::remove_dir_all(parent).unwrap();
}
#[test]
fn carried_nonce_survives_equal_clock_and_loss_budget_does_not_reset() {
    let root = dir("nonce");
    let action = Action::cancel(3, "0x0123456789abcdef0000000000000001".into());
    let mut attempts = Attempts::open(&root.join("actions")).unwrap();
    attempts.carry_nonce(10000);
    assert_eq!(attempts.prepare("next", &action, 10000).unwrap().0, 10001);
    drop(attempts);
    let loaded = Attempts::open(&root.join("actions")).unwrap();
    assert_eq!(loaded.nonce_floor(), 10001);
    drop(loaded);
    let mut session:Session=serde_json::from_value(json!({"session":"0123456789abcdef","account":"0x1111111111111111111111111111111111111111","agent":"0x2222222222222222222222222222222222222222","started_ms":1,"baseline_cash":"995","instrument":{"symbol":"BTC","asset":3,"sz_decimals":5},"max_position_lots":20,"max_notional":"40","max_loss":"10","risk_cash_floor":"990","nonce_floor":10001})).unwrap();
    session.validate().unwrap();
    assert_eq!(session.risk_floor().unwrap().to_string(), "990");
    session.baseline_cash = "1020".into();
    assert_eq!(session.risk_floor().unwrap().to_string(), "1010");
    session.baseline_cash = "989".into();
    assert!(session.validate().is_err());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn interrupted_rotation_is_completed_only_from_unchanged_flat_facts() {
    use mininautilus::journal;
    for closed in [false, true] {
        let parent = dir(if closed {
            "repair-closed"
        } else {
            "repair-preclose"
        });
        let root = parent.join("run");
        lifecycle::fixture(&root, 8, 8).unwrap();
        let old = lifecycle::journal_path(&root, 0);
        let mut engine = DurableEngine::recover(&old, None).unwrap();
        let at = engine.core().now;
        engine.process(at, Event::Reconnect).unwrap();
        let c = engine.core();
        let snapshot = Reconciliation {
            epoch: c.epoch,
            watermark: c.venue_seq,
            position: 0,
            absent: vec![],
            orders: c
                .orders
                .values()
                .map(|o| VenueOrder {
                    intent: o.intent.clone(),
                    filled: o.filled,
                    lifecycle: o.lifecycle,
                })
                .collect(),
            fills: c.fills.values().cloned().collect(),
        };
        engine.process(at, Event::Reconcile(snapshot)).unwrap();
        drop(engine);
        let segment = lifecycle::directory(&root, 0);
        fs::create_dir(&segment).unwrap();
        let account = "0x1111111111111111111111111111111111111111";
        let agent = "0x2222222222222222222222222222222222222222";
        let session = json!({"session":"0123456789abcdef","account":account,"agent":agent,"started_ms":1,"baseline_cash":"1000","instrument":{"symbol":"BTC","asset":3,"sz_decimals":5},"max_position_lots":20,"max_notional":"40","max_loss":"10"});
        runtime::save_json(&segment.join("session.json"), &session).unwrap();
        fs::write(segment.join("actions.jsonl"), b"fixture").unwrap();
        runtime::save_json(&segment.join("run-report.json"), &json!({"position":0})).unwrap();
        let prepared = json!({"session_sha256":lifecycle::digest(&segment.join("session.json")).unwrap(),"actions_sha256":lifecycle::digest(&segment.join("actions.jsonl")).unwrap(),"report_sha256":lifecycle::digest(&segment.join("run-report.json")).unwrap(),"nonce_floor":100,"ledger":{"cash_change":"0"},"account":account,"agent":agent});
        runtime::save_json(&segment.join("prepared.json"), &prepared).unwrap();
        let pointer = lifecycle::Pointer {
            schema: 1,
            account: account.into(),
            agent: agent.into(),
            index: 0,
            phase: "rotating".into(),
            nonce_floor: 0,
            risk_cash_floor: "990".into(),
            expected_cash: "1000".into(),
            previous_seal: None,
        };
        lifecycle::publish(&root, &pointer).unwrap();
        if closed {
            journal::rotate_retaining(&old, &lifecycle::journal_path(&root, 1)).unwrap();
        }
        let next = lifecycle::repair_rotation(&root, pointer).unwrap();
        assert_eq!(next.index, 1);
        assert_eq!(next.nonce_floor, 100);
        assert_eq!(next.expected_cash, "1000");
        assert_eq!(lifecycle::load(&root).unwrap().index, 1);
        fs::write(segment.join("actions.jsonl"), b"changed").unwrap();
        assert!(lifecycle::load(&root).is_err());
        fs::remove_dir_all(parent).unwrap();
    }
}
