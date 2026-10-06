use mininautilus::{core::Core, model::*};
fn send(c: &mut Core, event: Event) -> Vec<Effect> {
    c.apply(&Envelope {
        seq: c.seq + 1,
        at: 0,
        event,
    })
    .unwrap()
}
fn target(c: &mut Core, revision: u64, position: i64) {
    send(
        c,
        Event::SetTarget(Target {
            revision,
            position,
            valid_until: 1000,
        }),
    );
}
fn attempt(
    c: &mut Core,
    id: u64,
    revision: u64,
    expected_position: i64,
    side: Side,
    qty: i64,
) -> Vec<Effect> {
    let intent = Intent {
        id,
        side,
        qty,
        limit: 100,
        based_on_seq: c.seq,
        valid_until: 1000,
    };
    send(
        c,
        Event::SubmitTargeted {
            intent,
            revision,
            expected_position,
        },
    )
}
fn report(c: &mut Core, r: Report) {
    let event = Event::Execution {
        epoch: c.epoch,
        venue_seq: c.venue_seq + 1,
        report: r,
    };
    send(c, event);
}
fn fresh() -> Core {
    let mut c = Core::new(Config::default()).unwrap();
    send(&mut c, Event::Quote { bid: 99, ask: 101 });
    c
}
fn refused(e: Vec<Effect>) -> bool {
    e.iter().any(|x| matches!(x, Effect::Refused { .. }))
}

#[test]
fn old_position_intent_is_rejected_even_inside_global_risk_cap() {
    let mut c = fresh();
    target(&mut c, 1, 5);
    assert!(!refused(attempt(&mut c, 1, 1, 0, Side::Buy, 5)));
    report(
        &mut c,
        Report::Fill(Fill {
            execution_id: 1,
            order_id: 1,
            qty: 2,
            price: 100,
        }),
    );
    report(
        &mut c,
        Report::Canceled {
            id: 1,
            cumulative_filled: 2,
        },
    );
    // Old delta would produce 7 lots, still below the global cap 10 but above target 5.
    let mut unguarded = c.clone();
    let old_plan = Intent {
        id: 2,
        side: Side::Buy,
        qty: 5,
        limit: 100,
        based_on_seq: 2,
        valid_until: 1000,
    };
    assert!(
        send(&mut unguarded, Event::Submit(old_plan))
            .iter()
            .any(|e| matches!(e, Effect::SendOrder(_)))
    );
    assert_eq!(unguarded.exposure_bounds(), (2, 7));
    assert!(refused(attempt(&mut c, 2, 1, 0, Side::Buy, 5)));
    assert!(!refused(attempt(&mut c, 2, 1, 2, Side::Buy, 3)));
}
#[test]
fn target_change_and_cancel_fill_race_cannot_double_buy_or_sell() {
    let mut c = fresh();
    target(&mut c, 1, 5);
    attempt(&mut c, 1, 1, 0, Side::Buy, 5);
    target(&mut c, 2, 0);
    send(&mut c, Event::Cancel { id: 1 });
    // Venue fills during cancel flight. Replacement must wait for terminal evidence.
    report(
        &mut c,
        Report::Fill(Fill {
            execution_id: 1,
            order_id: 1,
            qty: 2,
            price: 100,
        }),
    );
    assert!(refused(attempt(&mut c, 2, 2, 2, Side::Sell, 2)));
    report(
        &mut c,
        Report::Canceled {
            id: 1,
            cumulative_filled: 2,
        },
    );
    assert!(refused(attempt(&mut c, 2, 1, 2, Side::Buy, 3)));
    assert!(!refused(attempt(&mut c, 2, 2, 2, Side::Sell, 2)));
    assert!(refused(attempt(&mut c, 3, 2, 2, Side::Sell, 2)));
    report(
        &mut c,
        Report::Fill(Fill {
            execution_id: 2,
            order_id: 2,
            qty: 2,
            price: 100,
        }),
    );
    report(
        &mut c,
        Report::Fill(Fill {
            execution_id: 2,
            order_id: 2,
            qty: 2,
            price: 100,
        }),
    );
    assert_eq!(c.position, 0);
    assert_eq!(c.fills.len(), 2);
}
#[test]
fn delayed_signal_cannot_restore_old_target_after_disconnect() {
    let mut c = fresh();
    target(&mut c, 4, 5);
    target(&mut c, 3, 0);
    assert_eq!(c.target.as_ref().unwrap().position, 5);
    send(&mut c, Event::Disconnect);
    assert!(c.target.is_none());
    target(&mut c, 4, 5);
    assert!(c.target.is_none());
}
#[test]
fn targeted_quantity_must_match_delta_and_target_must_not_expire() {
    let mut c = fresh();
    target(&mut c, 1, 3);
    assert!(refused(attempt(&mut c, 1, 1, 0, Side::Buy, 4)));
    assert!(refused(attempt(&mut c, 1, 1, 0, Side::Sell, 3)));
    let intent = Intent {
        id: 1,
        side: Side::Buy,
        qty: 3,
        limit: 100,
        based_on_seq: c.seq,
        valid_until: 2000,
    };
    let e = c
        .apply(&Envelope {
            seq: c.seq + 1,
            at: 1001,
            event: Event::SubmitTargeted {
                intent,
                revision: 1,
                expected_position: 0,
            },
        })
        .unwrap();
    assert!(refused(e));
}
