use mininautilus::{
    core::Core,
    dashboard::Projection,
    journal::{DurableEngine, JournalFollower},
    model::*,
};
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "mn-dashboard-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        )))
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}
fn observe(c: &mut Core, p: &mut Projection, at: u64, event: Event) {
    let input = Envelope {
        seq: c.seq + 1,
        at,
        event,
    };
    let effects = c.apply(&input).unwrap();
    p.observe(c, Some(&input), &effects);
}
fn fill(c: &mut Core, p: &mut Projection, id: u64, side: Side, qty: i64, price: i64) {
    let intent = Intent {
        id,
        side,
        qty,
        limit: price,
        based_on_seq: c.seq,
        valid_until: 1000,
    };
    observe(c, p, 0, Event::Submit(intent));
    let event = Event::Execution {
        epoch: c.epoch,
        venue_seq: c.venue_seq + 1,
        report: Report::Fill(Fill {
            execution_id: id,
            order_id: id,
            qty,
            price,
        }),
    };
    observe(c, p, 0, event.clone());
    // An identical exchange report must not create another trade or move PnL.
    observe(c, p, 0, event);
}
#[test]
fn follows_locked_live_writer_without_dispatch_or_mutation() {
    let path = Temp::new();
    let mut engine = DurableEngine::create(&path.0, Config::default()).unwrap();
    let mut follower = JournalFollower::open(&path.0).unwrap();
    let mut projection = Projection::default();
    assert_eq!(
        follower
            .poll(100, |c, i, e| projection.observe(c, i, e))
            .unwrap(),
        1
    );
    engine
        .process(0, Event::Quote { bid: 99, ask: 101 })
        .unwrap();
    engine
        .process(
            1,
            Event::Submit(Intent {
                id: 1,
                side: Side::Buy,
                qty: 2,
                limit: 100,
                based_on_seq: 1,
                valid_until: 99,
            }),
        )
        .unwrap();
    let before = fs::read(&path.0).unwrap();
    assert_eq!(
        follower
            .poll(100, |c, i, e| projection.observe(c, i, e))
            .unwrap(),
        2
    );
    assert_eq!(follower.core().unwrap().seq, 2);
    assert_eq!(fs::read(&path.0).unwrap(), before);
    let view = projection.view(&Value::Null, &Value::Null);
    assert_eq!(view["open_orders"], 1);
    assert_eq!(view["position"], 0);
    assert_eq!(
        follower.poll(100, |_, _, _| panic!("duplicate")).unwrap(),
        0
    );
}
#[test]
fn partial_frame_waits_and_corruption_keeps_last_validated_state() {
    let source = Temp::new();
    let copy = Temp::new();
    let mut engine = DurableEngine::create(&source.0, Config::default()).unwrap();
    engine
        .process(0, Event::Quote { bid: 99, ask: 101 })
        .unwrap();
    drop(engine);
    let bytes = fs::read(&source.0).unwrap();
    let split = bytes.len() - 10;
    fs::write(&copy.0, &bytes[..split]).unwrap();
    let mut follower = JournalFollower::open(&copy.0).unwrap();
    assert_eq!(follower.poll(100, |_, _, _| {}).unwrap(), 1);
    assert!(follower.pending_tail());
    assert_eq!(follower.core().unwrap().seq, 0);
    let mut append = fs::OpenOptions::new().append(true).open(&copy.0).unwrap();
    append.write_all(&bytes[split..]).unwrap();
    assert_eq!(follower.poll(100, |_, _, _| {}).unwrap(), 1);
    assert!(!follower.pending_tail());
    append
        .write_all(b"{\"previous\":0,\"checksum\":0,\"payload\":\"bad\"}\n")
        .unwrap();
    assert!(
        follower
            .poll(100, |_, _, _| panic!("corrupt callback"))
            .is_err()
    );
    assert_eq!(follower.core().unwrap().seq, 1);
}
#[test]
fn truncation_is_detected_without_repairing_file() {
    let path = Temp::new();
    let engine = DurableEngine::create(&path.0, Config::default()).unwrap();
    drop(engine);
    let mut follower = JournalFollower::open(&path.0).unwrap();
    follower.poll(100, |_, _, _| {}).unwrap();
    fs::write(&path.0, []).unwrap();
    assert!(follower.poll(100, |_, _, _| {}).is_err());
    assert_eq!(fs::metadata(&path.0).unwrap().len(), 0);
}
#[test]
fn long_short_average_cost_and_last_known_marks_reconcile() {
    let mut c = Core::new(Config::default()).unwrap();
    let mut p = Projection::default();
    observe(&mut c, &mut p, 0, Event::Quote { bid: 109, ask: 111 });
    fill(&mut c, &mut p, 1, Side::Buy, 4, 100);
    fill(&mut c, &mut p, 2, Side::Sell, 6, 110);
    assert_eq!(c.position, -2);
    assert_eq!(c.cash, 260);
    let v = p.view(&Value::Null, &Value::Null);
    assert_eq!(v["pnl"]["gross"], 38.);
    assert_eq!(v["pnl"]["realized"], 40.);
    assert_eq!(v["pnl"]["unrealized"], -2.);
    assert_eq!(v["fill_count"], 2);
    assert!(v["pnl"]["net"].is_null());
    observe(&mut c, &mut p, 1, Event::MarketUnavailable);
    let v = p.view(&Value::Null, &Value::Null);
    assert_eq!(v["mark_stale"], true);
    assert_eq!(v["pnl"]["exact_gross_tick_lots"], "38");
}
#[test]
fn units_and_complete_fees_are_required_for_net_pnl() {
    let mut c = Core::new(Config::default()).unwrap();
    let mut p = Projection::default();
    observe(&mut c, &mut p, 0, Event::Quote { bid: 110, ask: 111 });
    fill(&mut c, &mut p, 1, Side::Buy, 2, 100);
    let meta = json!({"parameters":["BTCUSDT","paper","0.01","0.001"]});
    let mut audit = json!({"raw_trades":[{"id":0,"symbol":"BTCUSDT","isBuyer":true,"qty":"0.002","price":"1.00","commission":"0.000002","commissionAsset":"BTC"}]});
    let v = p.view(&meta, &audit);
    assert_eq!(v["mode"], "paper");
    assert_eq!(v["unit"], "USDT");
    assert!((v["pnl"]["gross"].as_f64().unwrap() - 0.0002).abs() < 1e-12);
    assert!((v["pnl"]["net"].as_f64().unwrap() - 0.000198).abs() < 1e-12);
    audit["raw_trades"][0]["qty"] = json!("0.003");
    assert!(p.view(&meta, &audit)["pnl"]["net"].is_null());
    assert!(p.view(&Value::Null, &audit)["pnl"]["net"].is_null());
}
#[test]
fn full_ledger_and_chart_time_span_survive_large_runs() {
    let mut c = Core::new(Config::default()).unwrap();
    let mut p = Projection::default();
    for i in 0..2600 {
        observe(
            &mut c,
            &mut p,
            i * 250,
            Event::SetTarget(Target {
                revision: i + 1,
                position: 0,
                valid_until: i * 250 + 100,
            }),
        );
    }
    let v = p.view(&Value::Null, &Value::Null);
    assert_eq!(v["signals"].as_array().unwrap().len(), 2600);
    assert_eq!(v["actions"].as_array().unwrap().len(), 2600);
    let chart = v["history"].as_array().unwrap();
    assert!(chart.len() <= 1200);
    assert_eq!(chart[0]["at"], 0);
    assert_eq!(chart.last().unwrap()["at"], 2599 * 250);
}

#[test]
fn chart_endpoint_tracks_quotes_inside_sampling_interval() {
    let mut c = Core::new(Config::default()).unwrap();
    let mut p = Projection::default();
    observe(&mut c, &mut p, 0, Event::Quote { bid: 99, ask: 101 });
    fill(&mut c, &mut p, 1, Side::Buy, 2, 100);
    observe(&mut c, &mut p, 10, Event::Quote { bid: 110, ask: 111 });
    let v = p.view(&Value::Null, &Value::Null);
    let last = v["history"].as_array().unwrap().last().unwrap();
    assert_eq!(last["at"], 10);
    assert_eq!(last["pnl"], v["pnl"]["gross"]);
    assert_eq!(last["pnl"], 20.);
}
