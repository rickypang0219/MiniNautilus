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
    order_queries: Arc<std::sync::Mutex<Vec<Value>>>,
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
        Self::with_open_rows(order_responses, mutation_response, fill_pages, json!([]))
    }
    fn with_open_rows(
        order_responses: Vec<Value>,
        mutation_response: Option<Value>,
        fill_pages: Vec<Value>,
        open_rows: Value,
    ) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let actions = Arc::new(AtomicUsize::new(0));
        let fill_reads = Arc::new(AtomicUsize::new(0));
        let reads = fill_reads.clone();
        let order_queries = Arc::new(std::sync::Mutex::new(Vec::new()));
        let seen_queries = order_queries.clone();
        let stopped = stop.clone();
        let count = actions.clone();
        let worker = thread::spawn(move || {
            let mut queries = 0usize;
            'connections: while !stopped.load(Ordering::Acquire) {
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
                if reader.read_line(&mut line).unwrap() == 0 {
                    continue 'connections;
                }
                let exchange = line.starts_with("POST /exchange ");
                let mut length = 0;
                loop {
                    line.clear();
                    if reader.read_line(&mut line).unwrap() == 0 {
                        continue 'connections;
                    }
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
                            seen_queries.lock().unwrap().push(request["oid"].clone());
                            let mut response =
                                order_responses[queries.min(order_responses.len() - 1)].clone();
                            if response == "echoOpen" {
                                let cloid = request["oid"].as_str().unwrap();
                                let id =
                                    u64::from_str_radix(&cloid[cloid.len() - 16..], 16).unwrap();
                                response = json!({"status":"order","order":{"status":"open","order":{"oid":id,"cloid":cloid,"coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}}});
                            }
                            queries += 1;
                            response
                        }
                        Some("openOrders") => open_rows.clone(),
                        Some("userFillsByTime") => {
                            let n = reads.fetch_add(1, Ordering::AcqRel);
                            fill_pages[n.min(fill_pages.len() - 1)].clone()
                        }
                        other => panic!("unexpected request {other:?}"),
                    }
                };
                let status = response["__http_status"].as_u64().unwrap_or(200);
                let body = response.to_string();
                // A real deadline client can close while this mock is replying.
                // Its timeout/absence assertions still run; only peer disconnects
                // are expected here, not an arbitrary mock-server I/O failure.
                if let Err(error) = write!(
                    stream,
                    "HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                ) {
                    assert!(
                        matches!(
                            error.kind(),
                            std::io::ErrorKind::BrokenPipe | std::io::ErrorKind::ConnectionReset
                        ),
                        "mock response: {error}"
                    );
                }
            }
        });
        Self {
            base,
            stop,
            actions,
            fill_reads,
            order_queries,
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
            terminal_facts: BTreeMap::new(),
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
fn delayed_cloid_index_can_use_verified_original_fill_oid_without_resend() {
    let at = wall_ms();
    let visible = json!({"status":"order","order":{"status":"filled","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0","limitPx":"80000"}}});
    let fill = json!({"time":at,"tid":1,"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B"});
    let server = Server::with_fills(
        vec![json!({"status":"unknownOid"}), visible],
        None,
        vec![json!([fill])],
    );
    let dir = dir("oid-fallback");
    let mut venue = server.venue(&dir);
    venue.session.started_ms = at - 1;
    assert_eq!(venue.query(&intent()).unwrap()["status"], "filled");
    assert_eq!(
        *server.order_queries.lock().unwrap(),
        vec![json!("0x0123456789abcdef0000000000000001"), json!(123)]
    );
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
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
fn same_millisecond_fills_follow_unique_position_chain_not_opaque_trade_ids() {
    let fill = |tid, time, side, sz, px, start, pnl| json!({"tid":tid,"time":time,"oid":tid,"coin":"BTC","side":side,"sz":sz,"px":px,"startPosition":start,"closedPnl":pnl,"fee":"0.001","feeToken":"USDC"});
    let mut rows = vec![
        fill(1, 1, "A", "0.0002", "81312", "0", "0"),
        fill(2, 2, "B", "0.00006", "81298", "-0.00006", "0.00084"),
        fill(99, 2, "B", "0.00014", "81295", "-0.0002", "0.00238"),
    ];
    let a = account_ledger("BTC", &rows, &[]).unwrap();
    assert_eq!(a.position, Decimal::ZERO);
    assert_eq!(a.realized, Decimal::new(322, 5));
    rows.reverse();
    assert_eq!(
        account_ledger("BTC", &rows, &[]).unwrap().cash_change,
        a.cash_change
    );
    rows[1]["startPosition"] = "-0.0002".into();
    assert!(account_ledger("BTC", &rows, &[]).is_err());
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

#[test]
fn cancel_rejection_race_requires_original_terminal_facts_without_resending() {
    let open = json!({"status":"order","order":{"status":"open","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}}});
    let mut filled = open.clone();
    filled["order"]["status"] = "filled".into();
    filled["order"]["order"]["sz"] = "0".into();
    for changed_identity in [false, true] {
        let mut terminal = filled.clone();
        if changed_identity {
            terminal["order"]["order"]["cloid"] = "0x0123456789abcdef0000000000000002".into();
        }
        let server = Server::script(
            vec![open.clone(), open.clone(), terminal],
            Some(
                json!({"status":"ok","response":{"data":{"statuses":[{"error":"Order was never placed, already canceled, or filled. asset=3"}]}}}),
            ),
        );
        let dir = dir("cancel-rejected-race");
        let mut venue = server.venue(&dir);
        let result = venue.cancel(&intent());
        assert_eq!(result.is_err(), changed_identity);
        assert_eq!(server.actions.load(Ordering::Acquire), 1);
        assert_eq!(venue.attempts.entries.len(), 1);
        assert!(server.order_queries.lock().unwrap().len() >= 3);
        drop(venue);
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn unrelated_cancel_rejection_remains_fatal() {
    let open = json!({"status":"order","order":{"status":"open","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}}});
    for error in [
        "agent not authorized",
        "Order was never placed, already canceled, or filled. asset=4",
    ] {
        let server = Server::script(
            vec![open.clone()],
            Some(json!({"status":"ok","response":{"data":{"statuses":[{"error":error}]}}})),
        );
        let dir = dir("cancel-rejected-unrelated");
        let mut venue = server.venue(&dir);
        assert!(venue.cancel(&intent()).is_err());
        assert_eq!(server.actions.load(Ordering::Acquire), 1);
        assert_eq!(server.order_queries.lock().unwrap().len(), 1);
        drop(venue);
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn cancel_race_response_cannot_prove_an_open_order_terminal() {
    let open = json!({"status":"order","order":{"status":"open","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}}});
    let server = Server::script(
        vec![open],
        Some(
            json!({"status":"ok","response":{"data":{"statuses":[{"error":"Order was never placed, already canceled, or filled. asset=3"}]}}}),
        ),
    );
    let dir = dir("cancel-race-still-open");
    let mut venue = server.venue(&dir);
    assert!(
        venue
            .cancel(&intent())
            .unwrap_err()
            .to_string()
            .contains("cancel not terminal")
    );
    assert_eq!(server.actions.load(Ordering::Acquire), 1);
    assert_eq!(venue.attempts.entries.len(), 1);
    drop(venue);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn repeated_poll_reuses_only_core_verified_terminal_order_status() {
    let response = json!({"status":"order","order":{"status":"canceled","order":{"oid":123,"cloid":"0x0123456789abcdef0000000000000001","coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}}});
    let server = Server::start(response);
    let directory = dir("terminal-cache");
    let mut venue = server.venue(&directory);
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    core.orders.insert(
        1,
        Order {
            intent: intent(),
            filled: 0,
            lifecycle: Lifecycle::Canceled,
            pending: None,
            deadline: None,
            uncertain: false,
        },
    );
    venue.poll(&core).unwrap();
    venue.poll(&core).unwrap();
    assert_eq!(
        server.order_queries.lock().unwrap().len(),
        1,
        "a verified immutable terminal fact needs no second regular status RPC"
    );
    assert_eq!(
        server.fill_reads.load(Ordering::Acquire),
        2,
        "complete fill history must still be checked on every poll"
    );
}

#[test]
fn eight_hundred_verified_terminal_facts_plus_four_active_query_only_four_statuses() {
    let server = Server::start(json!("echoOpen"));
    let directory = dir("800-terminal-4-active");
    let mut venue = server.venue(&directory);
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    for id in 1..=804 {
        let mut i = intent();
        i.id = id;
        let terminal = id <= 800;
        let order = Order {
            intent: i.clone(),
            filled: 0,
            lifecycle: if terminal {
                Lifecycle::Canceled
            } else {
                Lifecycle::Accepted
            },
            pending: None,
            deadline: None,
            uncertain: false,
        };
        if terminal {
            let remote = json!({"status":"canceled","order":{"oid":id,"cloid":venue.session.cloid(id),"coin":"BTC","side":"B","origSz":"0.0002","sz":"0.0002","limitPx":"80000"}});
            // The same complete, terms-verified fact used to populate reconciliation's cache.
            let (fact, _, _) = collect_order(&venue.session.instrument, &i, &remote, &[]).unwrap();
            venue.remember_terminal(fact, remote, &[]).unwrap();
        }
        core.orders.insert(id, order);
    }
    let started = Instant::now();
    venue.poll(&core).unwrap();
    let queries = server.order_queries.lock().unwrap().clone();
    assert_eq!(queries.len(), 4);
    assert_eq!(server.fill_reads.load(Ordering::Acquire), 1);
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    println!(
        "terminal_cache_fixture={{\"terminal\":800,\"active\":4,\"status_queries\":{},\"elapsed_seconds\":{}}}",
        queries.len(),
        started.elapsed().as_secs_f64()
    );
    // A corrupt cache or a contradictory local order must stop, not silently fall back.
    venue.terminal_facts.get_mut(&1).unwrap().proof_sha256 = "corrupt".into();
    assert!(
        venue
            .poll(&core)
            .unwrap_err()
            .to_string()
            .contains("proof mismatch")
    );
}

#[test]
fn terminal_cache_rejects_cross_poll_fee_changes_missing_fills_and_foreign_fills() {
    let server = Server::start(json!("echoOpen"));
    let directory = dir("cached-fill-integrity");
    let mut venue = server.venue(&directory);
    let mut i = intent();
    i.id = 1;
    let raw = json!({"status":"filled","order":{"oid":1,"cloid":venue.session.cloid(1),"coin":"BTC","side":"B","origSz":"0.0002","sz":"0","limitPx":"80000"}});
    let fill = json!({"tid":1,"oid":1,"time":wall_ms(),"coin":"BTC","side":"B","sz":"0.0002","px":"80000","fee":"0.01","feeToken":"USDC"});
    let (fact, fills, _) = collect_order(
        &venue.session.instrument,
        &i,
        &raw,
        std::slice::from_ref(&fill),
    )
    .unwrap();
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    core.orders.insert(
        1,
        Order {
            intent: i,
            filled: 20,
            lifecycle: Lifecycle::Filled,
            pending: None,
            deadline: None,
            uncertain: false,
        },
    );
    core.fills.insert(fills[0].execution_id, fills[0].clone());
    venue
        .remember_terminal(fact, raw, std::slice::from_ref(&fill))
        .unwrap();
    assert!(
        venue
            .poll_orders(&core, std::slice::from_ref(&fill))
            .unwrap()
            .is_empty()
    );
    let mut changed = fill.clone();
    changed["fee"] = "0.02".into();
    assert!(venue.poll_orders(&core, &[changed]).is_err());
    assert!(venue.poll_orders(&core, &[]).is_err());
    let mut foreign = fill.clone();
    foreign["oid"] = 999.into();
    foreign["tid"] = 999.into();
    assert!(
        venue
            .poll_orders(&core, &[fill, foreign])
            .unwrap_err()
            .to_string()
            .contains("unowned")
    );
    core.orders.get_mut(&1).unwrap().uncertain = true;
    assert!(venue.poll_orders(&core, &[]).is_err());
}

#[test]
fn http_failure_records_numeric_status_without_reusable_signature_or_response_body() {
    let server = Server::script(
        vec![json!({})],
        Some(json!({"__http_status":429,"echo":"NEVER_RECORD_BODY"})),
    );
    let directory = dir("http-error-status");
    let mut venue = server.venue(&directory);
    let path = directory.join("observations.jsonl");
    venue.http.recorder = Some(super::super::record::Recorder::create(&path).unwrap());
    let error=venue.http.post("/exchange",&json!({"action":{"type":"order"},"signature":{"r":"NEVER_RECORD_SIGNATURE"},"nonce":7})).unwrap_err();
    assert!(error.to_string().contains("HTTP 429"), "{error}");
    let raw = std::fs::read_to_string(path).unwrap();
    assert!(!raw.contains("NEVER_RECORD"));
    let row: Value = serde_json::from_str(raw.trim()).unwrap();
    assert_eq!(row["kind"], "http_error");
    assert_eq!(row["status"], 429);
    assert_eq!(row["operation"], "order");
    assert_eq!(
        server.actions.load(Ordering::Acquire),
        1,
        "no mutation retry on HTTP failure"
    );
}

#[test]
fn btc_display_pnl_is_not_an_exact_wallet_cash_posting() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/native/entry-display.json"
    ))
    .unwrap();
    let fills = rows(&fixture["fills"]).unwrap();
    let ledger = account_ledger("BTC", fills, &[]).unwrap();
    assert_eq!(ledger.realized, dec(&fixture["exact_realized"]).unwrap());
    assert_eq!(
        ledger.cash_change,
        dec(&fixture["flat_cash"]).unwrap() - dec(&fixture["baseline_cash"]).unwrap()
    );
    assert_eq!(
        ledger.reported_realized,
        dec(&fixture["reported_realized"]).unwrap()
    );
    assert_eq!(
        ledger.display_pnl_difference,
        dec(&fixture["display_pnl_difference"]).unwrap()
    );
    assert_ne!(
        ledger.cash_change,
        ledger.reported_realized - ledger.fees + ledger.funding
    );
    let mut wrong = fills.clone();
    wrong[2]["closedPnl"] = "-0.00172".into();
    assert!(account_ledger("BTC", &wrong, &[]).is_err());
}

#[test]
fn opening_minimum_never_blocks_dust_reduction_or_allows_cross_zero_opening() {
    let instrument = Instrument {
        symbol: "BTC".into(),
        asset: 3,
        sz_decimals: 5,
    };
    let mut i = intent();
    i.qty = 2;
    assert!(below_opening_minimum(&instrument, &i, 18));
    i.side = Side::Sell;
    assert!(!below_opening_minimum(&instrument, &i, 2));
    i.qty = 3;
    assert!(below_opening_minimum(&instrument, &i, 2));
    i.qty = 20;
    assert!(!below_opening_minimum(&instrument, &i, 0));
}

#[test]
fn poll_preserves_reconciled_no_attempt_proof_but_rejects_contradictory_fills() {
    let server = Server::start(json!({"status":"unknownOid"}));
    let dir = dir("reconciled-unsent-poll");
    let mut venue = server.venue(&dir);
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    for event in [
        Event::Heartbeat { epoch: 0 },
        Event::Quote {
            bid: 800000,
            ask: 800000,
        },
        Event::Submit(intent()),
        Event::Reconnect,
        Event::Reconcile(Reconciliation {
            epoch: 1,
            watermark: 0,
            position: 0,
            orders: vec![],
            fills: vec![],
            absent: vec![1],
        }),
    ] {
        core.apply(&Envelope {
            seq: core.seq + 1,
            at: wall_ms(),
            event,
        })
        .unwrap();
    }
    assert_eq!(core.orders[&1].lifecycle, Lifecycle::Rejected);
    assert!(!venue.locally_unsent(&core, 1));
    venue.enable_local_unsent_proof();
    assert!(venue.poll(&core).unwrap().is_empty());
    assert!(
        server.order_queries.lock().unwrap().is_empty(),
        "a proven unsent order has no venue status"
    );
    assert!(
        venue.poll_orders(&core, &[json!({"tid":99})]).is_err(),
        "no-attempt proof must not hide any unowned execution"
    );
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
}

#[test]
fn current_open_snapshot_and_full_fills_resolve_stale_open_status_without_resend() {
    let mut captured: Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/native/partial-open-snapshot.json"
    ))
    .unwrap();
    let at = wall_ms();
    captured["fills"][0]["time"] = at.into();
    let server = Server::with_open_rows(
        vec![captured["order_status"].clone()],
        None,
        vec![captured["fills"].clone()],
        captured["open_orders"].clone(),
    );
    let dir = dir("current-open-partial");
    let mut venue = server.venue(&dir);
    venue.session.started_ms = at - 1;
    let mut core = Core::new(super::super::runtime::config()).unwrap();
    for event in [
        Event::Heartbeat { epoch: 0 },
        Event::Quote {
            bid: 800000,
            ask: 800000,
        },
        Event::Submit(intent()),
    ] {
        core.apply(&Envelope {
            seq: core.seq + 1,
            at,
            event,
        })
        .unwrap();
    }
    let reports = venue.poll(&core).unwrap();
    assert!(
        reports
            .iter()
            .any(|r| matches!(r,Report::Fill(f) if f.qty==5))
    );
    assert!(
        core.fills.is_empty(),
        "only the durable writer may apply reports"
    );
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
    assert_eq!(server.fill_reads.load(Ordering::Acquire), 1);
    for (execution_id, qty) in [(99, 5), (2, 10)] {
        let mut known = core.clone();
        known
            .apply(&Envelope {
                seq: known.seq + 1,
                at,
                event: Event::Execution {
                    epoch: 0,
                    venue_seq: 1,
                    report: Report::Fill(Fill {
                        execution_id,
                        order_id: 1,
                        qty,
                        price: 800000,
                    }),
                },
            })
            .unwrap();
        let before = known.clone();
        assert!(
            venue.poll(&known).is_err(),
            "current snapshot must retain every already-known immutable fill"
        );
        assert_eq!(known, before);
    }
    assert_eq!(server.actions.load(Ordering::Acquire), 0);
}

#[test]
fn current_open_snapshot_cannot_hide_missing_fills_or_override_identity_or_terminal_facts() {
    let captured: Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/native/partial-open-snapshot.json"
    ))
    .unwrap();
    let instrument = Instrument {
        symbol: "BTC".into(),
        asset: 3,
        sz_decimals: 5,
    };
    let i = intent();
    let remote = &captured["order_status"]["order"];
    let open = captured["open_orders"].as_array().unwrap();
    let normalized = current_open_status(&instrument, &i, remote, open).unwrap();
    assert_eq!(
        collect_order(
            &instrument,
            &i,
            &normalized,
            captured["fills"].as_array().unwrap()
        )
        .unwrap()
        .0
        .filled,
        5
    );
    assert!(collect_order(&instrument, &i, &normalized, &[]).is_err());
    assert!(current_open_status(&instrument, &i, remote, &[]).is_err());
    assert!(
        current_open_status(&instrument, &i, remote, &[open[0].clone(), open[0].clone()]).is_err()
    );
    for (field, value) in [
        ("oid", json!(999)),
        ("cloid", json!("foreign")),
        ("coin", json!("ETH")),
        ("side", json!("A")),
        ("origSz", json!("0.0003")),
        ("limitPx", json!("80001")),
        ("timestamp", json!(2)),
        ("sz", json!("0")),
        ("sz", json!("0.00021")),
    ] {
        let mut wrong = open.clone();
        wrong[0][field] = value;
        assert!(
            current_open_status(&instrument, &i, remote, &wrong).is_err(),
            "accepted wrong {field}"
        );
    }
    let mut ahead = remote.clone();
    ahead["order"]["sz"] = "0.0001".into();
    assert!(
        current_open_status(&instrument, &i, &ahead, open).is_err(),
        "must not regress a more advanced status"
    );
    for status in ["filled", "canceled", "rejected"] {
        let mut terminal = remote.clone();
        terminal["status"] = status.into();
        assert!(current_open_status(&instrument, &i, &terminal, open).is_err());
    }
}

#[test]
fn mock_server_survives_peer_eof_before_or_during_headers() {
    let server = Server::start(json!({"status":"unknownOid"}));
    for prefix in ["", "POST /info HTTP/1.1\r\nHost: local\r\n"] {
        let mut stream =
            std::net::TcpStream::connect(server.base.strip_prefix("http://").unwrap()).unwrap();
        stream.write_all(prefix.as_bytes()).unwrap();
        stream.shutdown(std::net::Shutdown::Write).unwrap();
    }
    let dir = dir("peer-eof");
    let venue = server.venue(&dir);
    assert_eq!(
        venue.http.info("userRole", json!({})).unwrap()["role"],
        "agent"
    );
}
