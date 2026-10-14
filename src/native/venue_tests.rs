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
    worker: Option<thread::JoinHandle<()>>,
}
impl Server {
    fn start(order_response: Value) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let actions = Arc::new(AtomicUsize::new(0));
        let stopped = stop.clone();
        let count = actions.clone();
        let worker = thread::spawn(move || {
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
                if exchange {
                    count.fetch_add(1, Ordering::AcqRel);
                    continue;
                } // Accepted remotely, response deliberately lost.
                let response = match request["type"].as_str() {
                    Some("userRole") => {
                        json!({"role":"agent","data":{"user":"0x1111111111111111111111111111111111111111"}})
                    }
                    Some("orderStatus") => order_response.clone(),
                    Some("openOrders") => json!([]),
                    other => panic!("unexpected request {other:?}"),
                };
                let body = response.to_string();
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
            }
        });
        Self {
            base,
            stop,
            actions,
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
