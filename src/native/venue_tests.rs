use super::*;
use std::{
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize},
    },
    thread,
};
struct Server {
    base: String,
    stop: Arc<AtomicBool>,
    actions: Arc<AtomicUsize>,
    fill_reads: Arc<AtomicUsize>,
    worker: Option<thread::JoinHandle<()>>,
}
impl Server {
    fn start(order_response: Value) -> Self {
        Self::script(vec![order_response], None)
    }
    fn script(order_responses: Vec<Value>, mutation_response: Option<Value>) -> Self {
        Self::with_fills(order_responses, mutation_response, vec![json!([])])
    }
    fn with_fills(
        order_responses: Vec<Value>,
        mutation_response: Option<Value>,
        fill_pages: Vec<Value>,
    ) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let actions = Arc::new(AtomicUsize::new(0));
        let fill_reads = Arc::new(AtomicUsize::new(0));
        let reads = fill_reads.clone();
        let stopped = stop.clone();
        let count = actions.clone();
        let worker = thread::spawn(move || {
            let mut queries = 0usize;
            while !stopped.load(Ordering::Acquire) {
                let (mut stream, _) = match listener.accept() {
                    Ok(s) => s,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(e) => panic!("{e}"),
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(1)))
                    .unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let exchange = line.starts_with("POST /exchange ");
                let mut length = 0;
                loop {
                    line.clear();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        length = value.trim().parse::<usize>().unwrap();
                    }
                }
                let mut body = vec![0; length];
                reader.read_exact(&mut body).unwrap();
                let request: Value = serde_json::from_slice(&body).unwrap();
                let response = if exchange {
                    count.fetch_add(1, Ordering::AcqRel);
                    let Some(reply) = &mutation_response else {
                        continue;
                    }; // lost acknowledgement
                    reply.clone()
                } else {
                    match request["type"].as_str() {
                        Some("userRole") => {
                            json!({"role":"agent","data":{"user":"0x1111111111111111111111111111111111111111"}})
                        }
                        Some("orderStatus") => {
                            let response =
                                order_responses[queries.min(order_responses.len() - 1)].clone();
                            queries += 1;
                            response
                        }
                        Some("openOrders") => json!([]),
                        Some("userFillsByTime") => {
                            let n = reads.fetch_add(1, Ordering::AcqRel);
                            fill_pages[n.min(fill_pages.len() - 1)].clone()
                        }
                        other => panic!("unexpected request {other:?}"),
                    }
                };
                let body = response.to_string();
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
            }
        });
        Self {
            base,
            stop,
            actions,
            fill_reads,
            worker: Some(worker),
        }
    }
    fn venue(&self, dir: &Path) -> Venue {
        let fixture: Value =
            serde_json::from_str(include_str!("../../tests/fixtures/native/signing.json")).unwrap();
        Venue {
            http: Http {
                client: Client::builder()
                    .no_proxy()
                    .timeout(Duration::from_secs(2))
                    .build()
                    .unwrap(),
                base: self.base.clone(),
                recorder: None,
                test_transport: true,
            },
            session: Session {
                session: "0123456789abcdef".into(),
                account: "0x1111111111111111111111111111111111111111".into(),
                agent: fixture["agent"].as_str().unwrap().into(),
                started_ms: wall_ms(),
                baseline_cash: "1000".into(),
                instrument: Instrument {
                    symbol: "BTC".into(),
                    asset: 3,
                    sz_decimals: 5,
                },
                max_position_lots: 20,
                max_notional: "40".into(),
                max_loss: "10".into(),
                nonce_floor: 0,
                risk_cash_floor: None,
            },
            signer: Signer::new(
                fixture["public_test_key"].as_str().unwrap(),
                fixture["agent"].as_str().unwrap(),
            )
            .unwrap(),
            attempts: Attempts::open(&dir.join("actions.jsonl")).unwrap(),
            owned_oids: BTreeSet::new(),
            last_ledger: None,
            last_request: Instant::now() - Duration::from_secs(1),
            resolve_unattempted: false,
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.worker.take().unwrap().join().unwrap();
    }
}
fn dir(name: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!("native-rpc-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&p);
    std::fs::create_dir(&p).unwrap();
    p
}
fn intent() -> Intent {
    Intent {
        id: 1,
        side: Side::Buy,
        qty: 20,
        limit: 800000,
        based_on_seq: 1,
        valid_until: wall_ms() + 3000,
    }
}

#[test]
fn dust_is_allowed_only_when_fresh_position_makes_it_strictly_reduce_only() {
    let server = Server::start(json!({"status":"unknownOid"}));
    let dir = dir("dust-reduce");
    let v = server.venue(&dir);
    let mut i = intent();
    i.qty = 1;
    assert!(v.validate(&i).is_err());
    assert!(v.validate_for_position(&i, -1).unwrap());
    assert!(v.validate_for_position(&i, 0).is_err());
    assert!(v.validate_for_position(&i, 1).is_err());
    i.qty = 2;
    assert!(v.validate_for_position(&i, -1).is_err());
}

#[test]
fn fills_ahead_of_order_status_are_rechecked_without_applying_or_resending() {
    let at = wall_ms();
    let old = json!({"status":"order","order":{"status":"open","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}}});
    let mut current = old.clone();
    current["order"]["order"]["sz"] = "0.0001".into();
    let fill =
        json!({"time":at,"oid":123,"coin":"BTC","side":"B","tid":1,"sz":"0.0001","px":"80000"});
    let server = Server::with_fills(vec![old, current], None, vec![json!([fill])]);
    let dir = dir("fill-ahead");
    let mut venue = server.venue(&dir);
    venue.session.started_ms = at - 1;
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    for (n, event) in [
        Event::Heartbeat { epoch: 0 },
        Event::Quote {
            bid: 800000,
            ask: 800000,
        },
        Event::Submit(intent()),
    ]
    .into_iter()
    .enumerate()
    {
        core.apply(&Envelope {
            seq: n as u64 + 1,
            at,
            event,
        })
        .unwrap();
    }
    let reports = venue.poll(&core).unwrap();
    assert!(
        reports
            .iter()
            .any(|r| matches!(r,Report::Fill(f) if f.qty==10))
    );
    assert_eq!(server.fill_reads.load(Ordering::Acquire), 2);
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    assert!(core.fills.is_empty());
}

#[test]
fn no_attempt_proof_cannot_cover_acknowledged_or_durably_attempted_orders() {
    let server = Server::start(json!({"status":"unknownOid"}));
    let dir = dir("no-attempt");
    let mut v = server.venue(&dir);
    v.enable_local_unsent_proof();
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    for (n, event) in [
        Event::Heartbeat { epoch: 0 },
        Event::Quote {
            bid: 800000,
            ask: 800000,
        },
        Event::Submit(intent()),
    ]
    .into_iter()
    .enumerate()
    {
        core.apply(&Envelope {
            seq: n as u64 + 1,
            at: wall_ms(),
            event,
        })
        .unwrap();
    }
    assert!(v.locally_unsent(&core, 1));
    v.attempts
        .prepare("order-1", &Action::cancel(3, v.session.cloid(1)), wall_ms())
        .unwrap();
    assert!(!v.locally_unsent(&core, 1));
    let accepted = core.orders.get_mut(&1).unwrap();
    accepted.lifecycle = Lifecycle::Accepted;
    v.attempts.entries.clear();
    assert!(!v.locally_unsent(&core, 1));
}

#[test]
fn polling_rechecks_fill_visibility_without_losing_immutable_facts() {
    for case in ["late", "missing", "changed"] {
        let at = wall_ms();
        let filled = json!({"status":"order","order":{"status":"filled","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0","limitPx":"80000"}}});
        let complete =
            json!({"time":at,"oid":123,"coin":"BTC","side":"B","tid":1,"sz":"0.0002","px":"80000"});
        let mut partial = complete.clone();
        partial["sz"] = "0.0001".into();
        let pages = match case {
            "late" => vec![json!([]), json!([complete])],
            "missing" => vec![json!([])],
            _ => vec![json!([partial]), json!([complete])],
        };
        let server = Server::with_fills(vec![filled], None, pages);
        let dir = dir(&format!("poll-{case}"));
        let mut v = server.venue(&dir);
        v.session.started_ms = at - 1;
        let mut core = Core::new(super::super::runtime::config()).unwrap();
        for (n, event) in [
            Event::Heartbeat { epoch: 0 },
            Event::Quote {
                bid: 800000,
                ask: 800000,
            },
            Event::Submit(intent()),
        ]
        .into_iter()
        .enumerate()
        {
            let event = match event {
                Event::Submit(mut i) => {
                    i.based_on_seq = n as u64;
                    Event::Submit(i)
                }
                event => event,
            };
            core.apply(&Envelope {
                seq: n as u64 + 1,
                at,
                event,
            })
            .unwrap();
        }
        let result = v.poll(&core);
        match case {
            "late" => {
                let reports = result.unwrap();
                assert!(matches!(
                    &reports[..],
                    [Report::Fill(Fill {
                        qty: 20,
                        order_id: 1,
                        ..
                    })]
                ));
                assert_eq!(server.fill_reads.load(Ordering::Acquire), 2);
            }
            "missing" => {
                assert!(result.is_err());
                assert_eq!(server.fill_reads.load(Ordering::Acquire), 3);
            }
            _ => assert!(
                result
                    .unwrap_err()
                    .to_string()
                    .contains("changed duplicate")
            ),
        }
        assert_eq!(core.orders[&1].lifecycle, Lifecycle::Pending);
        assert!(core.fills.is_empty());
        assert_eq!(server.actions.load(Ordering::Acquire), 0);
        assert!(v.attempts.entries.is_empty());
        drop(v);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
#[test]
fn lost_response_is_not_retried_even_after_action_journal_reopen() {
    let server = Server::start(json!({"status":"unknownOid"}));
    let dir = dir("lost-response");
    let action = Action::cancel(3, "0x0123456789abcdef0000000000000001".into());
    {
        let mut v = server.venue(&dir);
        assert!(v.mutate(&action, "cancel-1", None, None).is_err());
        assert_eq!(server.actions.load(Ordering::Acquire), 1);
    }
    {
        let mut v = server.venue(&dir);
        assert!(v.mutate(&action, "cancel-1", None, None).is_err());
        assert_eq!(server.actions.load(Ordering::Acquire), 1);
    }
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn expired_admission_and_closed_gate_do_not_reach_transport() {
    let server = Server::start(json!({"status":"unknownOid"}));
    let dir = dir("admission");
    let mut v = server.venue(&dir);
    let action = Action::cancel(3, v.session.cloid(1));
    assert!(
        v.mutate(&action, "order-1", Some((wall_ms(), wall_ms() - 1)), None)
            .is_err()
    );
    let gate = AtomicU8::new(2);
    assert!(v.mutate(&action, "order-2", None, Some(&gate)).is_err());
    assert!(v.attempts.entries.is_empty());
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    drop(v);
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn unknown_order_cannot_become_a_reconciliation_absence() {
    let server = Server::start(json!({"status":"unknownOid"}));
    let dir = dir("unknown");
    let mut v = server.venue(&dir);
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    let at = wall_ms();
    core.apply(&Envelope {
        seq: 1,
        at,
        event: Event::Heartbeat { epoch: 0 },
    })
    .unwrap();
    core.apply(&Envelope {
        seq: 2,
        at,
        event: Event::Quote {
            bid: 800000,
            ask: 800000,
        },
    })
    .unwrap();
    let mut i = intent();
    i.based_on_seq = 2;
    assert!(
        core.apply(&Envelope {
            seq: 3,
            at,
            event: Event::Submit(i)
        })
        .unwrap()
        .iter()
        .any(|e| matches!(e, Effect::SendOrder(_)))
    );
    assert!(v.reconcile(&core).is_err());
    assert_eq!(core.orders[&1].lifecycle, Lifecycle::Pending);
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    drop(v);
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn delayed_order_visibility_is_read_again_without_mutation() {
    let visible = json!({"status":"order","order":{"status":"filled","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0","limitPx":"80000"}}});
    let server = Server::script(
        vec![
            json!({"status":"unknownOid"}),
            json!({"status":"unknownOid"}),
            visible,
        ],
        None,
    );
    let dir = dir("delayed-visibility");
    let mut v = server.venue(&dir);
    assert_eq!(v.query(&intent()).unwrap()["status"], "filled");
    assert!(v.owned_oids.contains(&123));
    assert!(v.attempts.entries.is_empty());
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    drop(v);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn unknown_visibility_does_not_hide_a_later_identity_mismatch() {
    let wrong = json!({"status":"order","order":{"status":"filled","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000002","coin":"BTC","side":"B","origSz":"0.0002","sz":"0","limitPx":"80000"}}});
    let server = Server::script(vec![json!({"status":"unknownOid"}), wrong], None);
    let dir = dir("delayed-wrong-identity");
    let mut v = server.venue(&dir);
    assert!(
        v.query(&intent())
            .unwrap_err()
            .to_string()
            .contains("identity/terms mismatch")
    );
    assert!(v.owned_oids.is_empty());
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    drop(v);
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn cancel_after_fill_does_not_send_another_mutation() {
    let server = Server::start(
        json!({"status":"order","order":{"status":"filled","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0","limitPx":"80000"}}}),
    );
    let dir = dir("cancel-race");
    let mut v = server.venue(&dir);
    v.cancel(&intent()).unwrap();
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    assert!(v.attempts.entries.is_empty());
    drop(v);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn cancel_ack_waits_for_terminal_visibility_without_resending() {
    let open = json!({"status":"order","order":{"status":"open","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}}});
    let mut canceled = open.clone();
    canceled["order"]["status"] = "canceled".into();
    let server = Server::script(
        vec![open.clone(), open, canceled],
        Some(json!({"status":"ok","response":{"data":{"statuses":["success"]}}})),
    );
    let dir = dir("cancel-visibility");
    let mut v = server.venue(&dir);
    let started = Instant::now();
    v.cancel(&intent()).unwrap();
    assert!(started.elapsed() >= Duration::from_millis(100));
    assert_eq!(server.actions.load(Ordering::Acquire), 1);
    assert_eq!(v.attempts.entries.len(), 1);
    drop(v);
    std::fs::remove_dir_all(dir).unwrap();
}
