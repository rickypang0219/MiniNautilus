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
        recorder: None,
        initial_ready_recorded: false,
        instrument: Some(Instrument {
            symbol: "BTC".into(),
            asset: 3,
            sz_decimals: 5,
        }),
        recovery_attempts: 0,
        recovery_not_before: None,
        execution_recoveries: 0,
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
        last_market: None,
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
fn older_or_duplicate_books_do_not_overwrite_or_refresh_the_core_quote() {
    let (mut writer, jobs, _trace, _dir) = writer("book-order");
    let at = wall_ms();
    for (exchange_time, observed_at, bid) in [
        (at - 10, at - 10, 800000),
        (at - 20, at, 700000),
        (at - 10, at, 800000),
    ] {
        writer
            .market(Market {
                generation: 0,
                bid,
                ask: bid + 1,
                observed_at,
                exchange_time,
                received: Instant::now(),
            })
            .unwrap();
        assert_eq!(writer.engine.core().quote, Some((800000, 800001, at - 10)));
    }
    assert_eq!(writer.engine.core().quote, Some((800000, 800001, at - 10)));
    assert!(jobs.try_recv().is_err());
}

#[test]
fn source_pause_fences_market_before_the_expired_control_is_dequeued() {
    let (mut writer, jobs, _trace, _dir) = writer("book-pause");
    writer.circuit.pause_source();
    let before = writer.engine.core().quote;
    writer
        .market(Market {
            generation: 0,
            bid: 700000,
            ask: 700001,
            observed_at: wall_ms(),
            exchange_time: wall_ms(),
            received: Instant::now(),
        })
        .unwrap();
    assert_eq!(writer.engine.core().quote, before);
    assert!(jobs.try_recv().is_err());
}

#[test]
fn subscription_ack_without_a_current_book_cannot_open_admission() {
    let (mut writer, _jobs, _trace, _dir) = writer("no-book");
    writer
        .process(Event::MarketUnavailable, Instant::now())
        .unwrap();
    writer.circuit.close();
    writer.dispatch_target().unwrap();
    assert_ne!(writer.circuit.permit.load(Ordering::Acquire), 1);
}

#[test]
fn a_recent_receive_does_not_make_an_old_exchange_snapshot_fresh() {
    let (mut writer, jobs, _trace, _dir) = writer("book-age");
    writer
        .market(Market {
            generation: 0,
            bid: 700000,
            ask: 700001,
            observed_at: wall_ms(),
            exchange_time: wall_ms() - 6000,
            received: Instant::now(),
        })
        .unwrap();
    assert_ne!(writer.engine.core().quote.map(|q| q.0), Some(700000));
    assert!(jobs.try_recv().is_err());
}

#[test]
fn queued_market_backlog_does_not_acquire_a_new_receive_time() {
    let (mut writer, jobs, _trace, _dir) = writer("book-backlog");
    writer
        .market(Market {
            generation: 0,
            bid: 700000,
            ask: 700001,
            observed_at: wall_ms(),
            exchange_time: wall_ms(),
            received: Instant::now() - Duration::from_secs(6),
        })
        .unwrap();
    assert_ne!(writer.engine.core().quote.map(|q| q.0), Some(700000));
    assert!(jobs.try_recv().is_err());
}

#[test]
fn equal_millisecond_books_can_change_without_claiming_a_sequence() {
    let (mut writer, _jobs, _trace, _dir) = writer("equal-book-time");
    let at = wall_ms();
    for bid in [800000, 800001] {
        writer
            .market(Market {
                generation: 0,
                bid,
                ask: bid + 1,
                observed_at: at,
                exchange_time: at,
                received: Instant::now(),
            })
            .unwrap();
    }
    assert_eq!(writer.engine.core().quote, Some((800001, 800002, at)));
}

#[test]
fn queued_order_cannot_outlive_its_quote_even_with_a_fresh_intent() {
    for source_expired in [false, true] {
        let (mut writer, jobs, _trace, dir) = writer(if source_expired {
            "source-ttl"
        } else {
            "receive-ttl"
        });
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
        let mut job = jobs.recv().unwrap();
        if let Job::Submit {
            market_received,
            market_valid_until,
            ..
        } = &mut job
        {
            if source_expired {
                *market_valid_until = wall_ms() - 1;
            } else {
                *market_received = Instant::now() - Duration::from_secs(6);
            }
        }
        let (queued, receiver) = bounded(1);
        queued.send(job).unwrap();
        drop(queued);
        let (critical, reports) = bounded(1);
        execute(
            None,
            receiver,
            critical,
            writer.circuit.clone(),
            writer.telemetry.clone(),
        );
        assert!(writer.circuit.failed.load(Ordering::Acquire));
        assert!(
            reports.try_recv().is_err(),
            "expired pricing cannot reach even simulated execution"
        );
        assert_eq!(
            writer.engine.core().orders[&1].lifecycle,
            Lifecycle::Pending
        );
        assert!(writer.engine.core().fills.is_empty());
        let expected = writer.engine.core().clone();
        writer.engine.sync().unwrap();
        drop(writer);
        assert_eq!(
            crate::journal::replay(&dir.join("journal.jsonl")).unwrap(),
            expected
        );
    }
}

#[test]
fn stale_book_closes_admission_and_clears_target_until_fresh_data() {
    let (mut writer, jobs, _trace, dir) = writer("book-invalid");
    writer
        .process(
            Event::QuoteObserved {
                bid: 800000,
                ask: 800001,
                observed_at: wall_ms() - 6000,
            },
            Instant::now(),
        )
        .unwrap();
    writer.target = Some((20, wall_ms() + 3000, Instant::now()));
    writer.dispatch_target().unwrap();
    assert!(writer.engine.core().quote.is_none());
    assert!(writer.target.is_none());
    assert_ne!(writer.circuit.permit.load(Ordering::Acquire), 1);
    assert!(jobs.try_recv().is_err());
    let at = wall_ms();
    writer
        .market(Market {
            generation: 0,
            bid: 800000,
            ask: 800001,
            observed_at: at,
            exchange_time: at,
            received: Instant::now(),
        })
        .unwrap();
    writer.dispatch_target().unwrap();
    assert_eq!(writer.circuit.permit.load(Ordering::Acquire), 1);
    assert!(jobs.try_recv().is_err(), "old target must not revive");
    let expected = writer.engine.core().clone();
    writer.engine.sync().unwrap();
    drop(writer);
    assert_eq!(
        crate::journal::replay(&dir.join("journal.jsonl")).unwrap(),
        expected
    );
}

#[test]
fn candle_response_crossing_reconnect_keeps_its_original_epoch() {
    use std::io::{BufRead, BufReader};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let http = Http::local_test(format!("http://{}", listener.local_addr().unwrap()));
    let epoch = Arc::new(AtomicU64::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    let changed_epoch = epoch.clone();
    let stopped = stop.clone();
    let server = thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut reader = BufReader::new(socket.try_clone().unwrap());
        let mut line = String::new();
        let mut length = 0;
        loop {
            line.clear();
            assert_ne!(reader.read_line(&mut line).unwrap(), 0);
            if line == "\r\n" {
                break;
            }
            if let Some(n) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                length = n.trim().parse::<usize>().unwrap();
            }
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        let request: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(request["type"], "candleSnapshot");
        let start = request["req"]["startTime"].as_u64().unwrap();
        let rows: Vec<_> = (0..9)
            .map(|n| {
                let open = start + n * 60000;
                json!({"s":"BTC","i":"1m","t":open,"T":open+59999,"c":"80000"})
            })
            .collect();
        // Change the Core epoch while the old REST request is in flight.
        changed_epoch.store(1, Ordering::Release);
        stopped.store(true, Ordering::Release);
        let response = serde_json::to_string(&rows).unwrap();
        write!(
            socket,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            response.len(),
            response
        )
        .unwrap();
    });
    let (critical, output) = bounded(32);
    candle_worker(
        http,
        Instrument {
            symbol: "BTC".into(),
            asset: 3,
            sz_decimals: 5,
        },
        "test".into(),
        0,
        Strategy::Native(strategy::Sma::new(3, 8).unwrap()),
        SourceControl {
            epoch: epoch.clone(),
            stop,
            critical,
            circuit: Arc::new(Circuit::default()),
        },
    )
    .unwrap();
    server.join().unwrap();
    let message = output.recv_timeout(Duration::from_secs(2)).unwrap();
    let Control::Target { request, .. } = &message else {
        panic!("expected a strategy result");
    };
    assert_eq!(epoch.load(Ordering::Acquire), 1);
    assert_eq!(
        request.epoch, 0,
        "old REST response must not acquire the new epoch"
    );
    let (mut writer, jobs, _trace, _dir) = writer("candle-epoch");
    writer.process(Event::Disconnect, Instant::now()).unwrap();
    writer.process(Event::Reconnect, Instant::now()).unwrap();
    writer.control(message).unwrap();
    assert!(writer.target.is_none());
    assert!(jobs.try_recv().is_err());
}

#[test]
fn lost_response_closes_gate_reconciles_original_order_and_waits_for_fresh_target() {
    let (mut writer, jobs, _trace, _dir) = writer("rest-recovery");
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
    writer
        .control(Control::ExecutionUncertain {
            epoch: 0,
            reason: "lost HTTP response".into(),
        })
        .unwrap();
    writer.advance_reconnect().unwrap();
    writer.dispatch_target().unwrap();
    assert_eq!(writer.engine.core().health, Health::Reconciling);
    assert_eq!(
        writer.engine.core().orders[&1].lifecycle,
        Lifecycle::Pending
    );
    assert!(writer.engine.core().orders[&1].uncertain);
    assert!(jobs.try_recv().is_err());
    let mut snapshot = snapshot(writer.engine.core());
    snapshot.position = 20;
    snapshot.orders[0].lifecycle = Lifecycle::Filled;
    snapshot.orders[0].filled = 20;
    snapshot.fills = vec![Fill {
        execution_id: 77,
        order_id: 1,
        qty: 20,
        price: 800000,
    }];
    writer.control(Control::Reconciled(snapshot)).unwrap();
    assert_eq!(writer.engine.core().position, 20);
    assert_eq!(writer.execution_recoveries, 1);
    assert_eq!(writer.recovery_attempts, 0);
    assert!(writer.target.is_none());
    writer.dispatch_target().unwrap();
    assert!(jobs.try_recv().is_err());
}

#[test]
fn unresolved_execution_recovery_has_a_finite_budget_without_resending() {
    let (mut writer, jobs, _trace, _dir) = writer("rest-budget");
    for _ in 0..3 {
        writer
            .control(Control::ExecutionUncertain {
                epoch: writer.engine.core().epoch,
                reason: "unresolved".into(),
            })
            .unwrap();
        writer.advance_reconnect().unwrap();
        writer.dispatch_target().unwrap();
        assert!(jobs.try_recv().is_err());
        assert_ne!(writer.circuit.permit.load(Ordering::Acquire), 1);
    }
    assert!(
        writer
            .control(Control::ExecutionUncertain {
                epoch: writer.engine.core().epoch,
                reason: "unresolved".into()
            })
            .is_err()
    );
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
    assert_ne!(
        writer.circuit.permit.load(Ordering::Acquire),
        1,
        "subscription ACK is not a fresh book"
    );
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

#[test]
fn partial_fill_dust_topup_is_not_submitted_but_reduction_remains_available() {
    let (mut writer, jobs, _trace, dir) = writer("minimum-topup");
    let now = Instant::now();
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
            now,
        )
        .unwrap();
    jobs.recv().unwrap();
    writer
        .control(Control::Done {
            epoch: 0,
            reports: vec![
                Report::Fill(Fill {
                    execution_id: 1,
                    order_id: 1,
                    qty: 18,
                    price: 800000,
                }),
                Report::Canceled {
                    id: 1,
                    cumulative_filled: 18,
                },
            ],
            heartbeat: true,
        })
        .unwrap();
    writer
        .process(
            Event::SetTarget(Target {
                revision: 1,
                position: 20,
                valid_until: wall_ms() + 3000,
            }),
            now,
        )
        .unwrap();
    writer.target = Some((20, wall_ms() + 3000, now));
    writer.dispatch_target().unwrap();
    assert!(
        jobs.try_recv().is_err(),
        "USD1.60 topup must not create a venue intent"
    );
    assert_eq!(writer.engine.core().orders.len(), 1);
    assert_eq!(writer.engine.core().position, 18);
    assert_eq!(writer.engine.core().health, Health::Healthy);
    assert!(
        writer.target.is_none(),
        "refusal must not loop until the order cap"
    );
    writer
        .process(
            Event::SetTarget(Target {
                revision: 2,
                position: 0,
                valid_until: wall_ms() + 3000,
            }),
            now,
        )
        .unwrap();
    writer.target = Some((0, wall_ms() + 3000, now));
    writer.dispatch_target().unwrap();
    assert!(matches!(
        jobs.recv().unwrap(),
        Job::Submit {
            intent: Intent {
                qty: 18,
                side: Side::Sell,
                ..
            },
            ..
        }
    ));
    drop(writer);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn initial_readiness_is_recorded_once_without_any_strategy_target_or_order() {
    let (mut writer, jobs, _trace, dir) = writer("initial-ready-record");
    let path = dir.join("initial-ready.jsonl");
    let recorder = super::super::record::Recorder::create(&path).unwrap();
    writer.recorder = Some(recorder.clone());
    writer.initial_ready_recorded = false;
    writer.stream_ready = false;
    writer.dispatch_target().unwrap();
    assert!(std::fs::read_to_string(&path).unwrap().is_empty());
    writer.stream_ready = true;
    writer.dispatch_target().unwrap();
    writer.dispatch_target().unwrap();
    recorder.sync().unwrap();
    let raw = std::fs::read_to_string(&path).unwrap();
    let rows: Vec<Value> = raw
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["kind"], "initial_admission_ready");
    assert_eq!(rows[0]["empty"], true);
    assert!(writer.target.is_none());
    assert!(writer.engine.core().orders.is_empty());
    assert!(jobs.try_recv().is_err());
    drop(writer);
    drop(recorder);
    std::fs::remove_dir_all(dir).unwrap();
}
