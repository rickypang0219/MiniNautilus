#![cfg(feature = "native-runtime")]
use mininautilus::{
    model::*,
    native::{signing::*, venue::*},
};
use serde_json::{Value, json};
use std::{fs, path::PathBuf};
fn temp(name: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("native-test-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&p);
    fs::create_dir_all(&p).unwrap();
    p
}
#[test]
fn official_sdk_messagepack_hash_and_signature_match() {
    let f: Value = serde_json::from_str(include_str!("fixtures/native/signing.json")).unwrap();
    let signer = Signer::new(
        f["public_test_key"].as_str().unwrap(),
        f["agent"].as_str().unwrap(),
    )
    .unwrap();
    for row in f["fixtures"].as_array().unwrap() {
        let action: Action = serde_json::from_value(row["action"].clone()).unwrap();
        let nonce = row["nonce"].as_u64().unwrap();
        let expiry = row["expiry"].as_u64().unwrap();
        assert_eq!(
            hex::encode(rmp_serde::to_vec_named(&action).unwrap()),
            row["msgpack"]
        );
        assert_eq!(
            hex::encode(action_hash(&action, nonce, expiry).unwrap()),
            row["hash"]
        );
        let signature = signer.sign(&action, nonce, expiry).unwrap();
        for field in ["r", "s"] {
            assert_eq!(
                signature[field]
                    .as_str()
                    .unwrap()
                    .trim_start_matches("0x")
                    .trim_start_matches('0'),
                row["signature"][field]
                    .as_str()
                    .unwrap()
                    .trim_start_matches("0x")
                    .trim_start_matches('0')
            );
        }
        assert_eq!(signature["v"], row["signature"]["v"]);
        assert_ne!(
            action_hash(&action, nonce + 1, expiry).unwrap(),
            action_hash(&action, nonce, expiry).unwrap()
        );
        assert_ne!(
            action_hash(&action, nonce, expiry + 1).unwrap(),
            action_hash(&action, nonce, expiry).unwrap()
        );
    }
    assert!(
        Signer::new(
            f["public_test_key"].as_str().unwrap(),
            "0x0000000000000000000000000000000000000000"
        )
        .is_err()
    );
}
#[test]
fn attempts_survive_restart_reject_resend_torn_and_corrupt_records() {
    let dir = temp("attempts");
    let path = dir.join("actions.jsonl");
    let action = Action::cancel(3, "0x0123456789abcdef0000000000000001".into());
    {
        let mut a = Attempts::open(&path).unwrap();
        assert_eq!(
            a.prepare("cancel-1", &action, 10000).unwrap(),
            (10000, 15000)
        );
        assert!(a.prepare("cancel-1", &action, 10000).is_err());
        assert!(Attempts::open(&path).is_err());
    }
    {
        let mut a = Attempts::open(&path).unwrap();
        assert!(a.prepare("cancel-1", &action, 10001).is_err());
        assert_eq!(a.prepare("cancel-2", &action, 10000).unwrap().0, 10001);
        assert!(a.prepare("cancel-3", &action, 1).is_err());
    }
    let good = fs::read(&path).unwrap();
    fs::write(&path, &good[..good.len() - 1]).unwrap();
    assert!(Attempts::open(&path).is_err());
    let corrupt = String::from_utf8(good)
        .unwrap()
        .replace("cancel-1", "cancel-9");
    fs::write(&path, corrupt).unwrap();
    assert!(Attempts::open(&path).is_err());
    let _ = fs::remove_dir_all(dir);
}
fn instrument() -> Instrument {
    Instrument {
        symbol: "BTC".into(),
        asset: 3,
        sz_decimals: 5,
    }
}
#[test]
fn exact_units_and_book_freshness_fail_closed() {
    let i = instrument();
    assert_eq!(i.price(&json!("65000")).unwrap(), 650000);
    assert_eq!(i.size(&json!("0.0002")).unwrap(), 20);
    for p in [
        json!("0"),
        json!("-1"),
        json!("12345.6"),
        json!("NaN"),
        json!(65000),
    ] {
        assert!(i.price(&p).is_err());
    }
    for s in ["0", "-0.1", "0.000001"] {
        assert!(i.size(&json!(s)).is_err());
    }
    let book = json!({"coin":"BTC","time":10000,"levels":[[{"px":"65000"}],[{"px":"65001"}]]});
    assert!(parse_quote(&i, &book, 10000).is_ok());
    assert!(parse_quote(&i, &book, 16000).is_err());
    assert!(parse_quote(&i, &book, 9999).is_err());
}
#[test]
fn fill_identity_duplicate_and_cumulative_checks() {
    let i = Intent {
        id: 1,
        side: Side::Buy,
        qty: 20,
        limit: 650000,
        based_on_seq: 0,
        valid_until: 100,
    };
    let r = json!({"status":"filled","order":{"oid":123,"sz":"0"}});
    let f = json!({"oid":123,"tid":12,"coin":"BTC","side":"B","sz":"0.0002","px":"65000"});
    assert_eq!(
        collect_order(&instrument(), &i, &r, &[f.clone(), f.clone()])
            .unwrap()
            .1
            .len(),
        1
    );
    let mut changed = f.clone();
    changed["sz"] = json!("0.0001");
    assert!(collect_order(&instrument(), &i, &r, &[f.clone(), changed.clone()]).is_err());
    assert!(collect_order(&instrument(), &i, &r, &[changed]).is_err());
    let mut wrong = f;
    wrong["coin"] = json!("ETH");
    assert!(collect_order(&instrument(), &i, &r, &[wrong]).is_err());
    assert!(lifecycle("newUnknownStatus", 0).is_err());
}
#[test]
fn ledger_tracks_long_short_fees_and_funding_and_rejects_missing_fills() {
    let fills = vec![
        json!({"time":1,"tid":1,"oid":1,"coin":"BTC","feeToken":"USDC","startPosition":"0","side":"B","sz":"0.0002","px":"65000","closedPnl":"0","fee":"0.01"}),
        json!({"time":2,"tid":2,"oid":2,"coin":"BTC","feeToken":"USDC","startPosition":"0.0002","side":"A","sz":"0.0004","px":"65100","closedPnl":"0.02","fee":"0.02"}),
        json!({"time":3,"tid":3,"oid":3,"coin":"BTC","feeToken":"USDC","startPosition":"-0.0002","side":"B","sz":"0.0002","px":"65000","closedPnl":"0.02","fee":"0.01"}),
    ];
    let fund =
        vec![json!({"time":2,"hash":"a","delta":{"coin":"BTC","type":"funding","usdc":"0.003"}})];
    let l = account_ledger("BTC", &fills, &fund).unwrap();
    assert_eq!(wire(l.position), "0");
    assert_eq!(wire(l.realized), "0.04");
    assert_eq!(wire(l.cash_change), "0.003");
    assert!(account_ledger("BTC", &fills[1..], &fund).is_err());
}
#[test]
fn fail_closed_latch_cannot_be_reopened_by_writer_or_queue_race() {
    use mininautilus::native::runtime::Circuit;
    use std::sync::{Arc, atomic::Ordering};
    let circuit = Arc::new(Circuit::default());
    let (tx, _rx) = crossbeam_channel::bounded(1);
    circuit.allow();
    assert_eq!(circuit.permit.load(Ordering::Acquire), 1);
    circuit.publish(&tx, 1).unwrap();
    assert!(circuit.publish(&tx, 2).is_err());
    assert!(circuit.failed.load(Ordering::Acquire));
    let workers: Vec<_> = (0..4)
        .map(|_| {
            let c = circuit.clone();
            std::thread::spawn(move || {
                for _ in 0..10000 {
                    c.close();
                    c.allow();
                }
            })
        })
        .collect();
    for t in workers {
        t.join().unwrap();
    }
    assert_eq!(circuit.permit.load(Ordering::Acquire), 2);
}
#[test]
fn strategy_window_and_tag_checks() {
    use mininautilus::native::strategy::*;
    let mut sma = Sma::new(3, 8).unwrap();
    let mut values = std::collections::VecDeque::new();
    let mut expected = 0;
    for n in 0..10000 {
        let p = 600000 + (n * 17 % 31);
        let b = Bar {
            open_ms: n as u64 * 60000,
            close_ms: n as u64 * 60000 + 59999,
            price: p,
        };
        let result = sma.add(b.clone()).unwrap();
        values.push_back(p);
        if values.len() > 8 {
            values.pop_front();
        }
        if n >= 7 {
            let slow: i64 = values.iter().sum();
            let fast: i64 = values.iter().rev().take(3).sum();
            let d = fast * 8 - slow * 3;
            if d != 0 {
                expected = if d > 0 { 20 } else { -20 };
            }
            assert_eq!(result, Some(expected));
        } else {
            assert_eq!(result, None);
        }
        assert_eq!(sma.add(b).unwrap(), None);
    }
    let mut changed = sma.last.clone().unwrap();
    changed.price += 1;
    assert!(sma.add(changed).is_err());
    let request = Request {
        session: "s".into(),
        epoch: 2,
        revision: 3,
        deadline_ms: 100,
        bar: Bar {
            open_ms: 0,
            close_ms: 59999,
            price: 1,
        },
    };
    let answer = Answer {
        session: "s".into(),
        epoch: 2,
        revision: 3,
        deadline_ms: 100,
        target: Some(20),
    };
    assert!(validate_answer(&request, &answer, 2, 100).is_ok());
    assert!(validate_answer(&request, &answer, 3, 100).is_err());
    assert!(validate_answer(&request, &answer, 2, 101).is_err());
    let mut a = answer;
    a.target = Some(100);
    assert!(validate_answer(&request, &a, 2, 100).is_err());
}
#[test]
fn native_and_isolated_python_strategy_parity() {
    use mininautilus::native::strategy::*;
    let dir = temp("python-strategy");
    let mut python = Strategy::open(true, &dir).unwrap();
    let mut native = Strategy::open(false, &dir).unwrap();
    for n in 0..100 {
        let request = Request {
            session: "fixture".into(),
            epoch: 1,
            revision: n + 1,
            deadline_ms: u64::MAX,
            bar: Bar {
                open_ms: n * 60000,
                close_ms: n * 60000 + 59999,
                price: 600000 + (n as i64 * 17 % 31),
            },
        };
        assert_eq!(
            python.calculate(&request).unwrap().target,
            native.calculate(&request).unwrap().target
        );
    }
    drop(python);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn action_expiry_cannot_outlive_intent_or_move_after_restart() {
    let dir = temp("expiry");
    let path = dir.join("actions.jsonl");
    let a = Action::cancel(3, "0x0123456789abcdef0000000000000001".into());
    {
        let mut j = Attempts::open(&path).unwrap();
        assert!(j.prepare_until("a", &a, 100, 100).is_err());
        assert!(j.prepare_until("a", &a, 100, 5101).is_err());
        assert_eq!(j.prepare_until("a", &a, 100, 500).unwrap(), (100, 500));
    }
    let mut j = Attempts::open(&path).unwrap();
    assert_eq!(j.entries["a"]["expires_after"], 500);
    assert!(j.prepare_until("a", &a, 200, 500).is_err());
    drop(j);
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn python_and_rust_action_journal_and_account_locks_interoperate() {
    let dir = temp("interop");
    let path = dir.join("actions.jsonl");
    let output=std::process::Command::new("python3").env("PYTHONPATH","python").args(["-c","from mininautilus.hyperliquid import ActionJournal; import sys; j=ActionJournal(sys.argv[1]); j.prepare('cancel-1', {'type':'cancelByCloid','cancels':[{'asset':3,'cloid':'0x0123456789abcdef0000000000000001'}]},10000); j.close()"]).arg(&path).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    {
        let mut j = Attempts::open(&path).unwrap();
        assert_eq!(j.entries.len(), 1);
        j.prepare(
            "cancel-2",
            &Action::cancel(3, "0x0123456789abcdef0000000000000002".into()),
            10001,
        )
        .unwrap();
    }
    let output=std::process::Command::new("python3").env("PYTHONPATH","python").args(["-c","from mininautilus.hyperliquid import ActionJournal; import sys; j=ActionJournal(sys.argv[1]); assert len(j.attempts)==2; j.close()"]).arg(&path).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let account = "0x1111111111111111111111111111111111111122";
    let lock = account_lock(account).unwrap();
    let result=std::process::Command::new("python3").env("PYTHONPATH","python").args(["-c","from mininautilus.hyperliquid import account_writer_lock; import sys\ntry:\n with account_writer_lock(sys.argv[1]): sys.exit(2)\nexcept BlockingIOError: pass",account]).status().unwrap();
    assert!(result.success());
    drop(lock);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn strategy_partial_output_cannot_extend_absolute_deadline() {
    use mininautilus::native::strategy::*;
    use std::{
        io::{BufReader, Write},
        os::unix::net::UnixStream,
        time::{Duration, Instant},
    };
    let (reader, mut sender) = UnixStream::pair().unwrap();
    reader
        .set_read_timeout(Some(Duration::from_millis(1200)))
        .unwrap();
    let child = std::process::Command::new("python3")
        .args(["-c", "import time; time.sleep(30)"])
        .spawn()
        .unwrap();
    let mut strategy = Strategy::Python {
        child,
        reader: BufReader::new(reader),
    };
    let worker = std::thread::spawn(move || {
        for _ in 0..15 {
            if sender.write_all(b" ").is_err() {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    });
    let request = Request {
        session: "timeout".into(),
        epoch: 0,
        revision: 1,
        deadline_ms: mininautilus::native::wall_ms() + 300,
        bar: Bar {
            open_ms: 0,
            close_ms: 59999,
            price: 800000,
        },
    };
    let started = Instant::now();
    assert!(strategy.calculate(&request).is_err());
    let elapsed = started.elapsed();
    drop(strategy);
    worker.join().unwrap();
    assert!(
        elapsed < Duration::from_secs(1),
        "partial reads renewed the request deadline: {elapsed:?}"
    );
}

#[test]
fn diagnostic_collar_matches_existing_btc_closeout_and_is_bounded() {
    let i = instrument();
    assert_eq!(closeout_limit(&i, 814210, true).unwrap(), 815030);
    assert_eq!(closeout_limit(&i, 814210, false).unwrap(), 813390);
    assert!(closeout_limit(&i, 0, true).is_err());
}
