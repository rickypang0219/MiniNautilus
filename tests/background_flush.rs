use mininautilus::{
    journal::{DurableEngine, SyncPolicy, replay},
    model::*,
};
use std::{fs, time::Duration};

#[test]
fn background_flush_keeps_order_input_replayable_before_effect() {
    let path = std::env::temp_dir().join(format!("mini-bg-flush-{}", std::process::id()));
    let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
    engine.set_sync_policy(SyncPolicy::Outbox);
    engine
        .enable_background_flush(Duration::from_millis(2), 1)
        .unwrap();
    for at in 0..100 {
        engine
            .process(at, Event::Quote { bid: 99, ask: 101 })
            .unwrap();
    }
    let (effects, stages) = engine
        .process_profiled(
            100,
            Event::Submit(Intent {
                id: 1,
                side: Side::Buy,
                qty: 1,
                limit: 100,
                based_on_seq: 100,
                valid_until: 200,
            }),
            None,
        )
        .unwrap();
    assert!(effects.iter().any(|e| matches!(e, Effect::SendOrder(_))));
    assert!(stages.synced); // Background progress never substitutes for causal sync.
    let published = engine.core().clone();
    drop(engine);
    assert_eq!(replay(&path).unwrap(), published);
    let recovered = DurableEngine::recover(&path, None).unwrap();
    assert_eq!(
        recovered.core().orders[&1].intent,
        published.orders[&1].intent
    );
    assert!(recovered.core().orders[&1].uncertain);
    assert_ne!(recovered.core().health, Health::Healthy);
    drop(recovered);
    fs::remove_file(path).unwrap();
}
