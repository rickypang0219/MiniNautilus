//! Frozen LC history-sweep harness. Fixture generation is streamed, and is not
//! included in lifecycle measurements. All measured operations use production APIs.
use mininautilus::{
    cold::{ColdIndex, ColdRef},
    core::Carry,
    journal::{self, DurableEngine},
    model::*,
};
use serde_json::json;
use std::os::unix::fs::MetadataExt;
use std::{
    fs::{self, File},
    io::{self, BufWriter, Write},
    path::Path,
    time::Instant,
};

fn fnv(previous: u64, bytes: &[u8]) -> u64 {
    bytes
        .iter()
        .fold(previous, |h, b| (h ^ *b as u64).wrapping_mul(0x100000001b3))
}
fn fixture(path: &Path, n: u64) -> io::Result<ColdRef> {
    let mut out = BufWriter::with_capacity(65536, File::create_new(path)?);
    let mut hash = 0xcbf29ce484222325;
    let mut emit = |bytes: &[u8]| -> io::Result<()> {
        hash = fnv(hash, bytes);
        out.write_all(bytes)
    };
    emit(b"MNCOLD01")?;
    emit(&n.to_le_bytes())?;
    emit(&n.to_le_bytes())?;
    for id in 1..=n {
        emit(&id.to_le_bytes())?;
        emit(&1_i64.to_le_bytes())?;
        emit(&[1])?;
    }
    for id in 1..=n {
        for v in [id, id, 1, 100] {
            emit(&v.to_le_bytes())?;
        }
    }
    out.write_all(&hash.to_le_bytes())?;
    out.flush()?;
    out.get_ref().sync_all()?;
    Ok(ColdRef {
        file: path.file_name().unwrap().to_str().unwrap().into(),
        checksum: hash,
        orders: n,
        fills: n,
    })
}
fn frame(path: &Path, config: &Config, carry: &Carry) -> io::Result<()> {
    let payload = serde_json::to_string(
        &json!({"Genesis":{"schema":1,"config":config,"carry":{"carry":carry,"predecessor_checksum":0}}}),
    )?;
    let sum = fnv(
        fnv(0xcbf29ce484222325, &0_u64.to_le_bytes()),
        payload.as_bytes(),
    );
    let mut f = File::create_new(path)?;
    writeln!(
        f,
        "{}",
        json!({"previous":0,"checksum":sum,"payload":payload})
    )?;
    f.sync_all()
}
fn reconcile(e: &mut DurableEngine, at: u64) -> io::Result<()> {
    e.process(at, Event::Disconnect)?;
    e.process(at, Event::Reconnect)?;
    let c = e.core();
    let snapshot = Reconciliation {
        epoch: c.epoch,
        watermark: c.venue_seq,
        orders: c
            .orders
            .values()
            .map(|o| VenueOrder {
                intent: o.intent.clone(),
                filled: o.filled,
                lifecycle: o.lifecycle,
            })
            .collect(),
        fills: c.fills.values().cloned().collect(),
        position: c.position - c.opening_position,
        absent: vec![],
    };
    e.process(at, Event::Reconcile(snapshot))?;
    Ok(())
}
fn status() -> serde_json::Value {
    let txt = fs::read_to_string("/proc/self/status").unwrap_or_default();
    let value = |name: &str| {
        txt.lines()
            .find(|l| l.starts_with(name))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|n| n.parse::<u64>().ok())
    };
    json!({"rss_kib":value("VmRSS:"),"hwm_kib":value("VmHWM:")})
}
fn app_writes() -> u64 {
    fs::read_to_string("/proc/self/io")
        .unwrap()
        .lines()
        .find_map(|l| l.strip_prefix("wchar: ").and_then(|n| n.parse().ok()))
        .unwrap()
}
fn app_reads() -> u64 {
    fs::read_to_string("/proc/self/io")
        .unwrap()
        .lines()
        .find_map(|l| l.strip_prefix("rchar: ").and_then(|n| n.parse().ok()))
        .unwrap()
}
fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let dir = Path::new(args.get(1).expect("directory"));
    let n: u64 = args.get(2).expect("history").parse().unwrap();
    fs::create_dir(dir)?;
    let paged = args.get(6).is_some_and(|s| s == "pages");
    let verification = if args.get(7).is_some_and(|s| s == "path") {
        mininautilus::cold_pages::Verification::PathVerified
    } else {
        mininautilus::cold_pages::Verification::Full
    };
    let reference = if paged {
        let mut reference = None;
        for start in (1..=n).step_by(10000) {
            let count = (n + 1 - start).min(10000);
            let orders: Vec<_> = (start..start + count)
                .map(|id| mininautilus::cold::RetiredOrder {
                    id,
                    filled: 1,
                    lifecycle: Lifecycle::Filled,
                })
                .collect();
            let fills: Vec<_> = (start..start + count)
                .map(|id| Fill {
                    execution_id: id,
                    order_id: id,
                    qty: 1,
                    price: 100,
                })
                .collect();
            let old = reference
                .as_ref()
                .map(|r| {
                    mininautilus::cold_pages::Index::open(
                        dir,
                        r,
                        mininautilus::cold_pages::Verification::PathVerified,
                    )
                })
                .transpose()?;
            let (r, _) = mininautilus::cold_pages::extend(
                &dir.join(format!("fixture-{start}.cold")),
                old.as_ref(),
                &orders,
                &fills,
            )?;
            drop(old);
            if let Some(previous) = reference.as_ref() {
                mininautilus::cold_gc::register_and_sweep_with(
                    dir,
                    previous,
                    &r,
                    mininautilus::cold_pages::Verification::PathVerified,
                )?;
            }
            reference = Some(r);
        }
        let reference = reference.unwrap();
        ColdIndex::open(dir, &reference)?;
        reference
    } else {
        fixture(&dir.join("history.cold"), n)?
    };
    let config = Config {
        max_abs_position: 1_000_000_000,
        max_order_notional: 1_000_000_000,
        private_stale_ms: 1_000_000,
        market_stale_ms: 1_000_000,
        ..Config::default()
    };
    let carry = Carry {
        now: 0,
        position: 0,
        cash: 0,
        killed: false,
        epoch: 0,
        last_target_revision: 0,
        id_floor: n,
        cold: Some(reference.clone()),
    };
    let path = dir.join("events.jsonl");
    frame(&path, &config, &carry)?;
    let mut engine = DurableEngine::recover(&path, None)?;
    engine.set_sync_policy(journal::SyncPolicy::Outbox);
    engine.set_cold_verification(verification)?;
    reconcile(&mut engine, 0)?;
    let mut at = 0;
    let cycles: u64 = args.get(3).map_or(Ok(1), |s| s.parse()).unwrap();
    assert!(n > 0 && (1..=20).contains(&cycles));
    let reclaim = args.get(4).is_some_and(|s| s == "gc");
    let pin = args.get(5).is_some_and(|s| s == "pin");
    let mut readers = Vec::new();
    let mut cold_peak_allocated = 0;
    let mut rotations = Vec::new();
    let mut logical_written = 0;
    let mut latest = dir.join("events.jsonl");
    for cycle in 0..cycles {
        for id in n + 1 + cycle * 200..=n + (cycle + 1) * 200 {
            at += 10;
            engine.process(at, Event::Quote { bid: 99, ask: 100 })?;
            let intent = Intent {
                id,
                side: Side::Buy,
                qty: 1,
                limit: 100,
                based_on_seq: engine.core().seq,
                valid_until: at + 1000,
            };
            let effects = engine.process(at, Event::Submit(intent))?;
            assert!(effects.iter().any(|e| matches!(e, Effect::SendOrder(_))));
            let epoch = engine.core().epoch;
            engine.process(
                at,
                Event::Execution {
                    epoch,
                    venue_seq: engine.core().venue_seq + 1,
                    report: Report::Fill(Fill {
                        execution_id: id,
                        order_id: id,
                        qty: 1,
                        price: 100,
                    }),
                },
            )?;
        }
        reconcile(&mut engine, at)?;
        if pin {
            readers.push(ColdIndex::open_with(
                dir,
                engine.core().cold.as_ref().unwrap(),
                verification,
            )?);
        }
        let started = Instant::now();
        latest = if cycles == 1 {
            dir.join("next.jsonl")
        } else {
            dir.join(format!("next-{cycle}.jsonl"))
        };
        let io_before = app_writes();
        let mut next = if paged && reclaim {
            engine.rotate_into_retaining_paged_with_gc(&latest)?
        } else if paged {
            engine.rotate_into_retaining_paged(&latest)?
        } else if reclaim {
            engine.rotate_into_retaining_with_gc(&latest)?
        } else {
            engine.rotate_into_retaining(&latest)?
        };
        rotations.push(started.elapsed().as_secs_f64() * 1000.);
        logical_written += if paged {
            app_writes().saturating_sub(io_before)
        } else {
            fs::metadata(dir.join(&next.core().cold.as_ref().unwrap().file))?.len()
        };
        reconcile(&mut next, at)?;
        engine = next;
        let allocated = fs::read_dir(dir)?
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().contains(".cold"))
            .map(|entry| entry.metadata().map(|m| m.blocks() * 512))
            .collect::<io::Result<Vec<_>>>()?
            .into_iter()
            .sum::<u64>();
        let pages = dir.join(".cold-pages-v2");
        let pages_allocated = if pages.exists() {
            fs::read_dir(pages)?
                .map(|e| e.and_then(|e| e.metadata()).map(|m| m.blocks() * 512))
                .sum::<io::Result<u64>>()?
        } else {
            0
        };
        cold_peak_allocated = cold_peak_allocated.max(allocated + pages_allocated);
    }
    let rotation_ms = rotations[0];
    let rotation_resources = status();
    let cold = engine.core().cold.clone().unwrap();
    let started = Instant::now();
    let read_before = app_reads();
    let index = ColdIndex::open_with(dir, &cold, verification)?;
    let found = index.fill(n)?;
    assert!(found.is_some());
    let first_ms = started.elapsed().as_secs_f64() * 1000.;
    let first_read = app_reads().saturating_sub(read_before);
    let mut durations = Vec::with_capacity(1000);
    for i in 0..1000 {
        let started = Instant::now();
        assert!(index.fill(1 + i % n)?.is_some());
        durations.push(started.elapsed().as_secs_f64() * 1000.);
    }
    // Same current hot state and exactly 200 explicit tail Tick inputs per H.
    for _ in 0..200 {
        engine.process(at, Event::Tick)?;
    }
    drop(engine);
    let started = Instant::now();
    let restored = DurableEngine::recover(&latest, None)?;
    let replay_ms = started.elapsed().as_secs_f64() * 1000.;
    assert_eq!(restored.core().position, (200 * cycles) as i64);
    println!(
        "{}",
        json!({"schema":"lc_probe_v2","cold_format":if paged {"pages"}else{"legacy"},"verification":format!("{verification:?}"),"tail_inputs":200,"write_counter_scope":if paged {"procfs wchar: cold plus rotation journal writes upper bound"}else{"published cold bytes plus catalogue reported separately"},"history":n,"rotation_ms":rotation_ms,"rotation_resources":rotation_resources,"first_lookup_ms":first_ms,"warm_lookup_ms":durations,"engine_replay_ms":replay_ms,"final_resources":status(),"cold_bytes":fs::metadata(dir.join(&cold.file))?.len(),"cold_logical_write_bytes":logical_written,"first_lookup_logical_read_bytes":first_read,"cycles":cycles,"rotation_durations_ms":rotations,"reclaimed":reclaim,"new_payload_bytes":49*200*cycles,"cold_peak_allocated_bytes":cold_peak_allocated,"pinned_generations":readers.len(),"correct":true})
    );
    Ok(())
}
