//! G3: live rotation keeps exact dedup evidence in a cold index (docs/adr-live-rotation.md).
use mininautilus::{
    cold::ColdIndex,
    journal::{self, DurableEngine},
    model::*,
};
use std::{
    fs,
    path::{Path, PathBuf},
};

fn dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("mini-live-rotate-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir_all(&path).unwrap();
    path
}

fn config() -> Config {
    Config {
        max_abs_position: 1_000,
        request_timeout_ms: 10_000,
        private_stale_ms: 1_000_000,
        market_stale_ms: 1_000_000,
        ..Config::default()
    }
}

fn report(engine: &mut DurableEngine, at: Time, report: Report) -> Vec<Effect> {
    let (epoch, venue_seq) = (engine.core().epoch, engine.core().venue_seq + 1);
    engine
        .process(
            at,
            Event::Execution {
                epoch,
                venue_seq,
                report,
            },
        )
        .unwrap()
}

/// `count` buy orders from `first`, each accepted and filled once at 100;
/// execution IDs are globally unique (`order id * 10`).
fn trade(engine: &mut DurableEngine, at: &mut Time, first: OrderId, count: u64) {
    for id in first..first + count {
        *at += 10;
        engine
            .process(*at, Event::Quote { bid: 99, ask: 100 })
            .unwrap();
        let seq = engine.core().seq;
        let effects = engine
            .process(
                *at,
                Event::Submit(Intent {
                    id,
                    side: Side::Buy,
                    qty: 1,
                    limit: 100,
                    based_on_seq: seq,
                    valid_until: *at + 1_000,
                }),
            )
            .unwrap();
        assert!(matches!(effects[..], [Effect::SendOrder(_)]), "{effects:?}");
        report(engine, *at, Report::Accepted { id });
        report(engine, *at, Report::Fill(fill(id)));
    }
}

fn fill(id: OrderId) -> Fill {
    Fill {
        execution_id: id * 10,
        order_id: id,
        qty: 1,
        price: 100,
    }
}

/// The adapter's barrier: Disconnect, Reconnect, Reconcile with the venue history.
fn reconcile(engine: &mut DurableEngine, at: Time) {
    engine.process(at, Event::Disconnect).unwrap();
    engine.process(at, Event::Reconnect).unwrap();
    let core = engine.core();
    let orders = core
        .orders
        .values()
        .map(|o| VenueOrder {
            intent: o.intent.clone(),
            filled: o.filled,
            lifecycle: o.lifecycle,
        })
        .collect();
    let snapshot = Reconciliation {
        epoch: core.epoch,
        watermark: core.venue_seq,
        orders,
        fills: core.fills.values().cloned().collect(),
        position: core.position - core.opening_position,
        absent: vec![],
    };
    engine.process(at, Event::Reconcile(snapshot)).unwrap();
    assert_eq!(engine.core().health, Health::Healthy);
}

fn rotated_session(name: &str) -> (PathBuf, PathBuf, DurableEngine, Time) {
    let directory = dir(name);
    let first = directory.join("events.jsonl");
    let second = directory.join("events-1.jsonl");
    let mut engine = DurableEngine::create(&first, config()).unwrap();
    let mut at = 0;
    trade(&mut engine, &mut at, 1, 5);
    reconcile(&mut engine, at);
    let mut next = engine.rotate_into_retaining(&second).unwrap();
    reconcile(&mut next, at);
    (first, second, next, at)
}

#[test]
fn rotation_frees_history_and_carries_position_floor_and_cold_reference() {
    let (first, second, engine, _) = rotated_session("carry");
    let core = engine.core();
    assert!(core.orders.is_empty() && core.fills.is_empty());
    assert_eq!((core.position, core.id_floor), (5, 5));
    let cold = core
        .cold
        .clone()
        .expect("live rotation records a cold index");
    assert_eq!(
        (cold.orders, cold.fills, cold.file.as_str()),
        (5, 5, "events-1.jsonl.cold")
    );
    let index = ColdIndex::open(second.parent().unwrap(), &cold).unwrap();
    assert_eq!(index.fill(30).unwrap(), Some(fill(3)));
    assert_eq!(
        index.order(4).unwrap().unwrap().lifecycle,
        Lifecycle::Filled
    );
    assert!(index.order(6).unwrap().is_none());
    // The archive is closed and still replays; it cannot be resumed.
    assert_eq!(journal::replay(&first).unwrap().position, 5);
    assert!(DurableEngine::recover(&first, None).is_err());
}

#[test]
fn late_duplicate_of_a_retired_fill_is_exact_dedup_not_a_gate() {
    let (_, second, mut engine, at) = rotated_session("dup");
    let before = engine.core().venue_seq;
    report(&mut engine, at, Report::Fill(fill(2)));
    report(
        &mut engine,
        at,
        Report::Canceled {
            id: 3,
            cumulative_filled: 1,
        },
    );
    report(&mut engine, at, Report::Accepted { id: 1 });
    let core = engine.core();
    assert_eq!(core.health, Health::Healthy);
    assert_eq!(core.position, 5);
    assert_eq!(
        core.venue_seq,
        before + 3,
        "duplicates consume their sequence"
    );
    assert!(
        core.orders.is_empty() && core.fills.is_empty(),
        "nothing re-enters RAM"
    );
    drop(engine);
    // The verdict is journaled: replay never needs the cold file.
    let text = fs::read_to_string(&second).unwrap();
    assert!(text.contains("Retired") && text.contains("Duplicate"));
    fs::remove_file(second.with_file_name("events-1.jsonl.cold")).unwrap();
    assert_eq!(journal::replay(&second).unwrap().position, 5);
}

#[test]
fn contradictions_and_unavailable_evidence_gate() {
    let (_, _, mut engine, at) = rotated_session("contra");
    let mut changed = fill(2);
    changed.qty = 2;
    report(&mut engine, at, Report::Fill(changed));
    assert_eq!(engine.core().health, Health::Reconciling);
    reconcile(&mut engine, at);
    report(
        &mut engine,
        at,
        Report::Canceled {
            id: 3,
            cumulative_filled: 0,
        },
    );
    assert_eq!(engine.core().health, Health::Reconciling);
    reconcile(&mut engine, at);
    // A brand-new fill on a retired order (unknown execution ID) is a contradiction.
    report(
        &mut engine,
        at,
        Report::Fill(Fill {
            execution_id: 999,
            order_id: 1,
            qty: 1,
            price: 100,
        }),
    );
    assert_eq!(engine.core().health, Health::Reconciling);
    assert_eq!(engine.core().position, 5);
}

#[test]
fn corrupt_cold_index_fails_closed() {
    let (_, second, engine, at) = rotated_session("corrupt");
    drop(engine);
    let cold = second.with_file_name("events-1.jsonl.cold");
    let mut bytes = fs::read(&cold).unwrap();
    bytes[30] ^= 1;
    fs::write(&cold, bytes).unwrap();
    let mut engine = DurableEngine::recover(&second, None).unwrap();
    reconcile(&mut engine, at);
    report(&mut engine, at, Report::Fill(fill(2)));
    assert_eq!(engine.core().health, Health::Reconciling);
    let text = fs::read_to_string(&second).unwrap();
    assert!(text.contains("Unavailable"));
}

#[test]
fn callers_cannot_inject_a_retired_verdict() {
    let (_, _, mut engine, at) = rotated_session("inject");
    let (epoch, venue_seq) = (engine.core().epoch, engine.core().venue_seq + 1);
    let forged = Event::Execution {
        epoch,
        venue_seq,
        report: Report::Retired {
            report: Box::new(Report::Fill(fill(2))),
            verdict: RetiredVerdict::Duplicate,
        },
    };
    assert!(engine.process(at, forged).is_err());
}

#[test]
fn second_rotation_extends_the_index_and_rerun_after_crash_is_idempotent() {
    let (_, second, mut engine, mut at) = rotated_session("extend");
    trade(&mut engine, &mut at, 6, 3);
    reconcile(&mut engine, at);
    let third = second.with_file_name("events-2.jsonl");
    let next = engine.rotate_into_retaining(&third).unwrap();
    let cold = next.core().cold.clone().unwrap();
    assert_eq!((cold.orders, cold.fills, next.core().id_floor), (8, 8, 8));
    let index = ColdIndex::open(third.parent().unwrap(), &cold).unwrap();
    assert_eq!(index.fill(10).unwrap(), Some(fill(1)), "first segment kept");
    assert_eq!(index.fill(80).unwrap(), Some(fill(8)));
    drop(next);
    // Crash after Closed but before the successor: rerunning completes it identically.
    fs::remove_file(&third).unwrap();
    journal::rotate_retaining(&second, &third).unwrap();
    assert_eq!(journal::replay(&third).unwrap().cold, Some(cold));
}

#[test]
fn stray_cold_file_without_close_is_ignored_by_recovery() {
    let directory = dir("stray");
    let file = directory.join("events.jsonl");
    let mut engine = DurableEngine::create(&file, config()).unwrap();
    let mut at = 0;
    trade(&mut engine, &mut at, 1, 2);
    drop(engine);
    // Crash after the cold index was written but before the predecessor closed.
    fs::write(directory.join("events-1.jsonl.cold"), b"partial").unwrap();
    let engine = DurableEngine::recover(&file, None).unwrap();
    assert_eq!(engine.core().position, 2);
    assert!(engine.core().cold.is_none());
    assert!(!Path::new(&directory.join("events-1.jsonl")).exists());
}

#[test]
fn streaming_merge_is_exact_for_interleaved_fill_keys() {
    use mininautilus::cold::{self, RetiredOrder};
    let d = dir("streaming-merge");
    let terminal = |id| RetiredOrder {
        id,
        filled: 1,
        lifecycle: Lifecycle::Filled,
    };
    let old_orders = [terminal(1), terminal(2)];
    let mut old_fills = [fill(1), fill(2)];
    old_fills[0].execution_id = 1;
    old_fills[1].execution_id = 10;
    let reference = cold::write(&d.join("old.cold"), &old_orders, &old_fills).unwrap();
    let index = ColdIndex::open(&d, &reference).unwrap();
    let orders = [terminal(3), terminal(4)];
    let mut fills = [fill(3), fill(4)];
    fills[0].execution_id = 5;
    fills[1].execution_id = 20;
    let merged = cold::extend(&d.join("merged.cold"), Some(&index), &orders, &fills).unwrap();
    let all_orders = [terminal(1), terminal(2), terminal(3), terminal(4)];
    let all_fills = [
        old_fills[0].clone(),
        fills[0].clone(),
        old_fills[1].clone(),
        fills[1].clone(),
    ];
    let expected = cold::write(&d.join("expected.cold"), &all_orders, &all_fills).unwrap();
    assert_eq!(merged.checksum, expected.checksum);
    assert_eq!(
        fs::read(d.join("merged.cold")).unwrap(),
        fs::read(d.join("expected.cold")).unwrap()
    );
    let reopened = ColdIndex::open(&d, &merged).unwrap();
    for f in all_fills {
        assert_eq!(reopened.fill(f.execution_id).unwrap(), Some(f));
    }
    // Reusing a retired execution ID is refused, never silently deduplicated during publication.
    let changed = Fill {
        execution_id: 10,
        order_id: 3,
        qty: 1,
        price: 100,
    };
    assert!(cold::extend(&d.join("bad.cold"), Some(&index), &orders[..1], &[changed]).is_err());
    assert!(!d.join("bad.cold").exists());
    assert!(!d.join(".bad.cold.tmp").exists());
}

#[test]
#[cfg(target_os = "linux")]
fn cold_enospc_and_sync_failure_do_not_close_predecessor() {
    use std::os::unix::fs::symlink;
    for device in ["/dev/full", "/dev/null"] {
        let d = dir(if device.ends_with("full") {
            "enospc"
        } else {
            "sync-failure"
        });
        let path = d.join("events.jsonl");
        let mut engine = DurableEngine::create(&path, config()).unwrap();
        let mut at = 0;
        trade(&mut engine, &mut at, 1, 2);
        reconcile(&mut engine, at);
        let before = fs::read(&path).unwrap();
        symlink(device, d.join(".next.cold.tmp")).unwrap();
        assert!(engine.rotate_into_retaining(&d.join("next")).is_err());
        assert_eq!(fs::read(&path).unwrap(), before);
        assert!(!d.join("next").exists());
        assert_eq!(
            DurableEngine::recover(&path, None).unwrap().core().position,
            2
        );
        assert!(!d.join(".next.cold.tmp").exists());
    }
}

#[test]
fn reclamation_never_deletes_current_or_pinned_evidence() {
    use mininautilus::cold::{self, RetiredOrder};
    let d = dir("gc-pinned");
    let order = RetiredOrder {
        id: 1,
        filled: 1,
        lifecycle: Lifecycle::Filled,
    };
    let old = cold::write(&d.join("old.cold"), &[order], &[fill(1)]).unwrap();
    let current = cold::write(&d.join("current.cold"), &[order], &[fill(1)]).unwrap();
    let pinned = ColdIndex::open(&d, &old).unwrap();
    assert!(!cold::remove_superseded(&d, &old, &current).unwrap());
    assert!(cold::remove_superseded(&d, &current, &current).is_err());
    assert_eq!(pinned.fill(10).unwrap(), Some(fill(1)));
    drop(pinned);
    assert!(cold::remove_superseded(&d, &old, &current).unwrap());
    assert!(d.join("current.cold").exists());
    assert!(!cold::remove_superseded(&d, &old, &current).unwrap());
}

#[test]
fn crash_rerun_keeps_the_exact_successor_genesis_anchor() {
    let (_, second, mut engine, at) = rotated_session("anchor-rerun");
    reconcile(&mut engine, at);
    let third = second.with_file_name("events-2.jsonl");
    let next = engine.rotate_into_retaining(&third).unwrap();
    drop(next);
    let original = fs::read(&third).unwrap();
    // recover() adds Disconnect, which is not part of the publication genesis.
    let original_genesis = original.split(|b| *b == b'\n').next().unwrap().to_vec();
    fs::remove_file(&third).unwrap();
    journal::rotate_retaining(&second, &third).unwrap();
    let rerun = fs::read(&third).unwrap();
    assert_eq!(
        rerun.split(|b| *b == b'\n').next().unwrap(),
        original_genesis
    );
}

#[test]
fn gc_is_opt_in_and_only_runs_after_successor_publication() {
    let (_, second, mut engine, at) = rotated_session("gc-published");
    let old_cold = engine.core().cold.clone().unwrap();
    reconcile(&mut engine, at);
    let third = second.with_file_name("events-2.jsonl");
    let next = engine.rotate_into_retaining_with_gc(&third).unwrap();
    assert!(!second.parent().unwrap().join(old_cold.file).exists());
    assert!(second.exists(), "old journal is retained for replay");
    assert_eq!(journal::replay(&second).unwrap().position, 5);
    let reference = next.core().cold.clone().unwrap();
    assert_eq!(
        ColdIndex::open(third.parent().unwrap(), &reference)
            .unwrap()
            .fill(10)
            .unwrap(),
        Some(fill(1))
    );
}

#[test]
#[cfg(feature = "fault-injection")]
fn child_rotation_fault_target() {
    let Ok(directory) = std::env::var("MINI_LC_TEST_DIR") else {
        return;
    };
    let d = PathBuf::from(directory);
    fs::create_dir_all(&d).unwrap();
    let mut engine = DurableEngine::create(&d.join("events"), config()).unwrap();
    let mut at = 0;
    trade(&mut engine, &mut at, 1, 5);
    reconcile(&mut engine, at);
    let mut engine = engine.rotate_into_retaining(&d.join("events-1")).unwrap();
    reconcile(&mut engine, at);
    trade(&mut engine, &mut at, 6, 2);
    reconcile(&mut engine, at);
    fs::write(d.join("armed"), b"armed").unwrap();
    engine
        .rotate_into_retaining_with_gc(&d.join("events-2"))
        .unwrap();
}

#[test]
#[cfg(feature = "fault-injection")]
fn publication_crashes_recover_or_fail_closed_without_losing_evidence() {
    use std::process::Command;
    for boundary in [
        "cold_write_before",
        "cold_write_mid",
        "cold_fsync_after",
        "cold_rename_after",
        "cold_dirsync_after",
        "closed_before",
        "closed_durable_after",
        "genesis_before",
        "genesis_fsync_after",
        "genesis_dirsync_after",
        "gc_before",
        "gc_after_unlink",
        "gc_after",
    ] {
        for repetition in 0..3 {
            let d = dir(&format!("crash-{boundary}-{repetition}"));
            let result = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "child_rotation_fault_target", "--nocapture"])
                .env("MINI_LC_TEST_DIR", &d)
                .env("MINI_LC_FAULT_ARM_FILE", d.join("armed"))
                .env("MINI_LC_CRASH_AT", boundary)
                .output()
                .unwrap();
            assert_eq!(
                result.status.code(),
                Some(99),
                "{boundary}: {}",
                String::from_utf8_lossy(&result.stderr)
            );
            fs::remove_file(d.join("armed")).unwrap();
            let old = d.join("events-1");
            let new = d.join("events-2");
            assert_eq!(journal::replay(&old).unwrap().position, 7);
            if !new.exists() {
                journal::rotate_retaining(&old, &new).unwrap();
            } else if journal::replay(&new).is_err() {
                // An empty successor exists after create-new but before its durable
                // genesis. Fail closed, then the operator removes only this empty file.
                assert!(fs::metadata(&new).unwrap().len() == 0);
                assert!(DurableEngine::recover(&new, None).is_err());
                fs::remove_file(&new).unwrap();
                journal::rotate_retaining(&old, &new).unwrap();
            }
            let mut engine = DurableEngine::recover(&new, None).unwrap();
            reconcile(&mut engine, 100);
            report(&mut engine, 100, Report::Fill(fill(1)));
            assert_eq!(engine.core().health, Health::Healthy, "{boundary}");
            assert_eq!(engine.core().position, 7, "{boundary}");
        }
    }
}
