use mininautilus::{
    core::Core,
    dashboard::{
        Projection,
        ledger::{Query, page, summary},
    },
    journal::{DurableEngine, EventTime, JournalFollower, replay},
    model::*,
};
use serde_json::{Value, json};
use std::{
    fs,
    sync::atomic::{AtomicU64, Ordering},
};
static ID: AtomicU64 = AtomicU64::new(0);
#[test]
fn timestamp_metadata_is_durable_and_does_not_drive_ordering() {
    let path = std::env::temp_dir().join(format!(
        "mn-time-{}-{}",
        std::process::id(),
        ID.fetch_add(1, Ordering::Relaxed)
    ));
    let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
    let times = [
        EventTime {
            event_time_ms: Some(1_800_000_000_000),
            received_time_ms: Some(1_800_000_000_050),
            source: Some("exchange".into()),
            ..Default::default()
        },
        EventTime {
            event_time_ms: Some(1_799_999_999_000),
            received_time_ms: Some(1_800_036_000_000),
            source: Some("late exchange event".into()),
            ..Default::default()
        },
    ];
    for (i, t) in times.iter().enumerate() {
        engine
            .process_timed(
                i as u64,
                Event::Quote { bid: 99, ask: 101 },
                Some(t.clone()),
            )
            .unwrap();
    }
    let expected = engine.core().clone();
    drop(engine);
    assert_eq!(replay(&path).unwrap(), expected);
    let mut follower = JournalFollower::open(&path).unwrap();
    let mut observed = Vec::new();
    follower
        .poll_timed(100, |_, input, _, time| {
            if input.is_some() {
                observed.push(time.unwrap().clone());
            }
        })
        .unwrap();
    assert_eq!(observed, times);
    // Metadata is within the checksum, not an unprotected annotation.
    let contents = fs::read_to_string(&path)
        .unwrap()
        .replace("late exchange event", "fake exchange event");
    fs::write(&path, contents).unwrap();
    assert!(replay(&path).is_err());
    fs::remove_file(path).unwrap();
}
fn records() -> Value {
    json!({"seq":"5","events":[
        {"seq":"1","at":1,"event_time_ms":1000,"received_time_ms":1005,"event":"Tick"},
        {"seq":"2","at":2,"event_time_ms":900,"received_time_ms":1006,"event":"Disconnect"},
        {"seq":"3","at":3,"event_time_ms":1000,"received_time_ms":1007,"event":"Reconnect"},
        {"seq":"4","at":4,"event_time_ms":null,"received_time_ms":1008,"event":"Tick"},
        {"seq":"5","at":5,"event_time_ms":null,"received_time_ms":null,"event":"Tick"}]})
}
#[test]
fn stable_cursor_covers_late_same_time_and_appended_records_without_duplicates() {
    let mut view = records();
    let q = Query::parse("kind=events&limit=2").unwrap();
    let first = page(&view, &q);
    assert_eq!(first["rows"][0]["seq"], "5");
    assert_eq!(first["rows"][1]["seq"], "4");
    view["seq"] = json!("6");
    view["events"]
        .as_array_mut()
        .unwrap()
        .push(json!({"seq":"6","at":6,"event_time_ms":800}));
    let q = Query::parse(&format!(
        "kind=events&limit=2&until=5&before={}",
        first["next_cursor"].as_str().unwrap()
    ))
    .unwrap();
    let second = page(&view, &q);
    assert_eq!(second["newer"], 1);
    assert_eq!(second["rows"][0]["seq"], "3");
    assert_eq!(second["rows"][1]["seq"], "2");
    let q = Query::parse(&format!(
        "kind=events&limit=2&until=5&before={}",
        second["next_cursor"].as_str().unwrap()
    ))
    .unwrap();
    let third = page(&view, &q);
    assert_eq!(third["rows"].as_array().unwrap().len(), 1);
    assert_eq!(third["rows"][0]["seq"], "1");
    assert!(third["next_cursor"].is_null());
    let q = Query::parse("kind=events&after=5").unwrap();
    assert_eq!(page(&view, &q)["rows"][0]["seq"], "6");
}
#[test]
fn time_range_uses_requested_basis_and_marks_missing_datetimes() {
    let v = records();
    let event = page(&v, &Query::parse("kind=events&from=950&to=1008").unwrap());
    assert_eq!(event["total"], 3);
    assert_eq!(event["missing_time"], 1);
    let received = page(
        &v,
        &Query::parse("kind=events&from=1005&to=1008&basis=received").unwrap(),
    );
    assert_eq!(received["total"], 4);
    let engine = page(
        &v,
        &Query::parse("kind=events&from=2&to=3&basis=engine").unwrap(),
    );
    assert_eq!(engine["total"], 2);
    assert!(Query::parse("limit=201").is_err());
    assert!(Query::parse("kind=secrets").is_err());
    assert!(Query::parse("q=%xx").is_err());
    let s = summary(&v, &Query::parse("kind=events&limit=1").unwrap());
    assert!(s.get("events").is_none());
    assert_eq!(s["ledger"]["rows"].as_array().unwrap().len(), 1);
}
#[test]
fn reconciliation_preserves_fill_event_time_and_separate_discovery_time() {
    let mut core = Core::new(Config::default()).unwrap();
    let mut p = Projection::default();
    let intent = Intent {
        id: 1,
        side: Side::Buy,
        qty: 2,
        limit: 100,
        based_on_seq: 1,
        valid_until: 100,
    };
    for (at, event) in [
        Event::Quote { bid: 99, ask: 101 },
        Event::Submit(intent.clone()),
        Event::Disconnect,
        Event::Reconnect,
    ]
    .into_iter()
    .enumerate()
    {
        let input = Envelope {
            seq: core.seq + 1,
            at: at as u64,
            event,
        };
        let effects = core.apply(&input).unwrap();
        p.observe(&core, Some(&input), &effects);
    }
    let input = Envelope {
        seq: 5,
        at: 4,
        event: Event::Reconcile(Reconciliation {
            epoch: core.epoch,
            watermark: 1,
            orders: vec![VenueOrder {
                intent,
                filled: 2,
                lifecycle: Lifecycle::Filled,
            }],
            fills: vec![Fill {
                execution_id: 5,
                order_id: 1,
                qty: 2,
                price: 100,
            }],
            position: 2,
        }),
    };
    let effects = core.apply(&input).unwrap();
    assert_eq!(core.position, 2);
    let time = EventTime {
        received_time_ms: Some(40_000_000),
        fill_event_times: [(5, 4_000_000)].into(),
        ..Default::default()
    };
    p.observe_timed(&core, Some(&input), &effects, Some(&time));
    let v = p.view(&Value::Null, &Value::Null);
    assert_eq!(v["trades"][0]["event_time_ms"], 4_000_000);
    assert_eq!(v["trades"][0]["received_time_ms"], 40_000_000);
    assert_eq!(v["trades"][0]["seq"], "5");
    assert_eq!(v["trades"][0]["time_source"], "exchange trade");
}

#[test]
fn sqlite_index_matches_reference_pages_and_upserts_without_duplicates() {
    use mininautilus::dashboard::index::Index;
    let mut index = Index::new().unwrap();
    let mut v = records();
    let batch = vec![(
        "events".to_string(),
        v["events"].as_array().unwrap().clone(),
    )];
    index.put("run", &batch).unwrap();
    index.put("run", &batch).unwrap();
    for query in [
        "kind=events&limit=2",
        "kind=events&from=950&to=1008",
        "kind=events&basis=received&from=1005&to=1006",
        "kind=events&basis=engine&from=2&to=4",
        "kind=events&after=2&until=4",
        "kind=events&q=disconnect",
        "kind=events&side=Buy",
        "kind=events&before=00000000000000000003:00000000000000000000",
    ] {
        let q = Query::parse(query).unwrap();
        assert_eq!(index.page("run", 5, &q).unwrap(), page(&v, &q), "{query}");
    }
    v["events"][1]["event_time_ms"] = json!(1050);
    index
        .put(
            "run",
            &[("events".to_string(), vec![v["events"][1].clone()])],
        )
        .unwrap();
    let q = Query::parse("kind=events&from=1040").unwrap();
    assert_eq!(index.page("run", 5, &q).unwrap(), page(&v, &q));
    assert_eq!(index.page("another run", 5, &q).unwrap()["total"], 0);
}
