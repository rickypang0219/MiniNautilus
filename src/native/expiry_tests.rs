//! Expiry never changes the epoch until old execution facts have been consumed.
use super::*;

fn writer(name: &str) -> (Writer, Receiver<Job>, Receiver<Value>, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!("native-expiry-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir(&dir).unwrap();
    let engine = DurableEngine::create(&dir.join("journal.jsonl"), config()).unwrap();
    let (jobs, receiver) = bounded(32);
    let (queue, trace) = bounded(1024);
    let mut writer = Writer {
        engine,
        jobs,
        circuit: Arc::new(Circuit::default()),
        telemetry: Telemetry {
            queue,
            dropped: Arc::new(AtomicU64::new(0)),
        },
        busy: false,
        reconcile: false,
        stream_ready: true,
        source_generation: 0,
        reconciled_generation: 0,
        reconnect_pending: false,
        stale_market_discarded: 0,
        poll_requested: false,
        target: None,
        processed: 0,
        market_count: 0,
        critical_count: 0,
    };
    writer
        .process(Event::Heartbeat { epoch: 0 }, Instant::now())
        .unwrap();
    writer
        .process(
            Event::Quote {
                bid: 800000,
                ask: 800000,
            },
            Instant::now(),
        )
        .unwrap();
    writer.dispatch_target().unwrap();
    (writer, receiver, trace, dir)
}
fn snapshot(core: &Core) -> Reconciliation {
    Reconciliation {
        epoch: core.epoch,
        watermark: core.venue_seq,
        position: core.position,
        absent: vec![],
        orders: core
            .orders
            .values()
            .map(|o| VenueOrder {
                intent: o.intent.clone(),
                filled: o.filled,
                lifecycle: o.lifecycle,
            })
            .collect(),
        fills: core.fills.values().cloned().collect(),
    }
}
#[test]
fn expiry_drains_old_fill_then_requires_reconciliation_and_new_subscription() {
    let (mut writer, jobs, _trace, dir) = writer("fill");
    writer
        .process(
            Event::Submit(Intent {
                id: 1,
                side: Side::Buy,
                qty: 20,
                limit: 800000,
                based_on_seq: writer.engine.core().seq,
                valid_until: wall_ms() + 3000,
            }),
            Instant::now(),
        )
        .unwrap();
    assert!(matches!(jobs.recv().unwrap(), Job::Submit { .. }));
    let generation = writer.circuit.pause_source();
    // Even before the disconnect control message is dequeued, admission is closed.
    writer.dispatch_target().unwrap();
    assert_ne!(writer.circuit.permit.load(Ordering::Acquire), 1);
    writer.control(Control::Expired { generation }).unwrap();
    writer.advance_reconnect().unwrap();
    assert_eq!(
        writer.engine.core().epoch,
        0,
        "old execution still in flight"
    );
    writer
        .control(Control::Done {
            epoch: 0,
            reports: vec![Report::Fill(Fill {
                execution_id: 1,
                order_id: 1,
                qty: 20,
                price: 800000,
            })],
            heartbeat: true,
        })
        .unwrap();
    writer.advance_reconnect().unwrap();
    assert_eq!(writer.engine.core().epoch, 1);
    assert_eq!(writer.engine.core().position, 20);
    assert_eq!(writer.engine.core().health, Health::Reconciling);
    writer
        .market(Market {
            generation: 0,
            bid: 1,
            ask: 2,
            observed_at: wall_ms(),
            exchange_time: wall_ms(),
            received: Instant::now(),
        })
        .unwrap();
    assert_eq!(writer.stale_market_discarded, 1);
    assert!(writer.engine.core().quote.is_none());
    writer
        .control(Control::Reconciled(snapshot(writer.engine.core())))
        .unwrap();
    writer.dispatch_target().unwrap();
    assert_ne!(
        writer.circuit.permit.load(Ordering::Acquire),
        1,
        "subscription not re-established"
    );
    writer.control(Control::Ready { generation: 0 }).unwrap();
    writer.dispatch_target().unwrap();
    assert_ne!(
        writer.circuit.permit.load(Ordering::Acquire),
        1,
        "old readiness cannot reopen"
    );
    writer.control(Control::Ready { generation }).unwrap();
    writer.dispatch_target().unwrap();
    assert_eq!(writer.circuit.permit.load(Ordering::Acquire), 1);
    assert!(jobs.try_recv().is_err(), "no replayed order or old target");
    let request = Request {
        session: "test".into(),
        epoch: 0,
        revision: 1,
        deadline_ms: wall_ms() + 1000,
        bar: strategy::Bar {
            open_ms: 0,
            close_ms: 59999,
            price: 800000,
        },
    };
    let answer: Answer =
        serde_json::from_value(json!({"session":"test","epoch":0,"revision":1,"deadline_ms":request.deadline_ms,"target":-20}))
            .unwrap();
    writer
        .control(Control::Target {
            request,
            answer,
            received: Instant::now(),
        })
        .unwrap();
    assert!(
        writer.target.is_none(),
        "late old-epoch strategy must be discarded"
    );
    assert!(!writer.circuit.failed.load(Ordering::Acquire));
    writer
        .market(Market {
            generation,
            bid: 800000,
            ask: 800000,
            observed_at: wall_ms(),
            exchange_time: wall_ms(),
            received: Instant::now(),
        })
        .unwrap();
    writer
        .process(
            Event::SetTarget(Target {
                revision: 2,
                position: 0,
                valid_until: wall_ms() + 3000,
            }),
            Instant::now(),
        )
        .unwrap();
    writer.target = Some((0, wall_ms() + 3000, Instant::now()));
    writer.dispatch_target().unwrap();
    assert!(matches!(
        jobs.recv().unwrap(),
        Job::Submit {
            intent: Intent {
                id: 2,
                side: Side::Sell,
                qty: 20,
                ..
            },
            epoch: 1,
            ..
        }
    ));
    let expected = writer.engine.core().clone();
    writer.engine.sync().unwrap();
    drop(writer);
    assert_eq!(
        crate::journal::replay(&dir.join("journal.jsonl")).unwrap(),
        expected
    );
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn second_expiry_and_bad_snapshot_cannot_reopen_admission() {
    let (mut writer, jobs, _trace, dir) = writer("snapshot");
    let first = writer.circuit.pause_source();
    writer
        .control(Control::Expired { generation: first })
        .unwrap();
    writer.advance_reconnect().unwrap();
    writer
        .control(Control::Ready { generation: first })
        .unwrap();
    let second = writer.circuit.pause_source();
    writer
        .control(Control::Reconciled(snapshot(writer.engine.core())))
        .unwrap();
    writer.dispatch_target().unwrap();
    assert_ne!(writer.circuit.permit.load(Ordering::Acquire), 1);
    writer
        .control(Control::Expired { generation: second })
        .unwrap();
    writer.advance_reconnect().unwrap();
    writer
        .control(Control::Ready { generation: second })
        .unwrap();
    let mut bad = snapshot(writer.engine.core());
    bad.position = 20;
    writer.control(Control::Reconciled(bad)).unwrap();
    assert!(writer.circuit.failed.load(Ordering::Acquire));
    writer
        .control(Control::Reconciled(snapshot(writer.engine.core())))
        .unwrap();
    writer.dispatch_target().unwrap();
    assert_ne!(writer.circuit.permit.load(Ordering::Acquire), 1);
    assert!(jobs.try_recv().is_err());
    drop(writer);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn expiry_blocks_queued_send_without_inventing_a_rejection() {
    let (mut writer, jobs, _trace, dir) = writer("unsent");
    writer
        .process(
            Event::Submit(Intent {
                id: 1,
                side: Side::Buy,
                qty: 20,
                limit: 800000,
                based_on_seq: writer.engine.core().seq,
                valid_until: wall_ms() + 3000,
            }),
            Instant::now(),
        )
        .unwrap();
    writer.circuit.pause_source();
    let (closed, unused) = bounded(0);
    drop(unused);
    drop(std::mem::replace(&mut writer.jobs, closed));
    let (critical, reports) = bounded(1);
    execute(
        None,
        jobs,
        critical,
        writer.circuit.clone(),
        writer.telemetry.clone(),
    );
    assert!(writer.circuit.failed.load(Ordering::Acquire));
    assert!(reports.try_recv().is_err());
    assert_eq!(
        writer.engine.core().orders[&1].lifecycle,
        Lifecycle::Pending
    );
    assert_eq!(writer.engine.core().position, 0);
    assert_eq!(writer.engine.core().fills.len(), 0);
    drop(writer);
    std::fs::remove_dir_all(dir).unwrap();
}
