//! LC-v2 cold component probe; no venue calls, no startup/resume claim.
use mininautilus::{
    cold::RetiredOrder,
    cold_gc,
    cold_pages::{self, Index, Verification},
    model::{Fill, Lifecycle},
};
use serde_json::json;
use std::{fs, io, os::unix::fs::MetadataExt, path::Path, time::Instant};
fn rows(start: u64, n: u64) -> (Vec<RetiredOrder>, Vec<Fill>) {
    (
        (start..start + n)
            .map(|id| RetiredOrder {
                id,
                filled: 1,
                lifecycle: Lifecycle::Filled,
            })
            .collect(),
        (start..start + n)
            .map(|id| Fill {
                execution_id: id,
                order_id: id,
                qty: 1,
                price: 100,
            })
            .collect(),
    )
}
fn allocated(d: &Path) -> io::Result<u64> {
    let mut sum = 0;
    for e in fs::read_dir(d)? {
        let e = e?;
        let m = e.metadata()?;
        if m.is_file() {
            sum += m.blocks() * 512;
        } else if e.file_name() == ".cold-pages-v2" {
            for p in fs::read_dir(e.path())? {
                sum += p?.metadata()?.blocks() * 512;
            }
        }
    }
    Ok(sum)
}
fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let d = Path::new(args.get(1).expect("directory"));
    let h: u64 = args.get(2).expect("H").parse().unwrap();
    let pin = args.get(3).is_some_and(|s| s == "pin");
    assert!([1000, 100000, 1000000].contains(&h));
    fs::create_dir(d)?;
    let mut reference = None;
    let mut fixture_writes = 0;
    for start in (1..=h).step_by(10000) {
        let (o, f) = rows(start, (h + 1 - start).min(10000));
        let old = reference
            .as_ref()
            .map(|r| Index::open(d, r, Verification::PathVerified))
            .transpose()?;
        let (r, s) = cold_pages::extend(
            &d.join(format!("fixture-{start}.cold")),
            old.as_ref(),
            &o,
            &f,
        )?;
        fixture_writes += s.logical_write_bytes;
        drop(old);
        if let Some(previous) = reference.as_ref() {
            cold_gc::register_and_sweep_with(d, previous, &r, Verification::PathVerified)?;
        }
        reference = Some(r);
    }
    let mut r = reference.unwrap();
    Index::open(d, &r, Verification::Full)?.verify()?;
    let mut durations = Vec::new();
    let mut writes = 0;
    let mut buffer = 0;
    let mut pins = Vec::new();
    let mut peak = allocated(d)?;
    let mut gc_ms = Vec::new();
    for cycle in 0..20 {
        let old = Index::open(d, &r, Verification::PathVerified)?;
        let (o, f) = rows(h + 1 + cycle * 200, 200);
        cold_gc::preflight(d, Some(&r), o.len(), f.len(), true)?;
        let started = Instant::now();
        let (next, s) =
            cold_pages::extend(&d.join(format!("cycle-{cycle}.cold")), Some(&old), &o, &f)?;
        writes += s.logical_write_bytes;
        buffer = buffer.max(s.working_buffer_bytes);
        peak = peak.max(allocated(d)?);
        if pin {
            pins.push(old);
        } else {
            drop(old);
        }
        let gc_started = Instant::now();
        let report = cold_gc::register_and_sweep_with(d, &r, &next, Verification::PathVerified)?;
        writes += report.logical_write_bytes;
        gc_ms.push(gc_started.elapsed().as_secs_f64() * 1000.);
        durations.push(started.elapsed().as_secs_f64() * 1000.);
        r = next;
        peak = peak.max(allocated(d)?);
    }
    let started = Instant::now();
    let full = Index::open(d, &r, Verification::Full)?;
    assert!(full.fill(h)?.is_some());
    let full_ms = started.elapsed().as_secs_f64() * 1000.;
    let full_read = full.stats().logical_read_bytes;
    drop(full);
    let started = Instant::now();
    let path = Index::open(d, &r, Verification::PathVerified)?;
    assert!(path.fill(h)?.is_some());
    let first_ms = started.elapsed().as_secs_f64() * 1000.;
    let first_read = path.stats().logical_read_bytes;
    let mut warm = Vec::new();
    for i in 0..1000 {
        let t = Instant::now();
        assert!(path.fill(1 + i % h)?.is_some());
        warm.push(t.elapsed().as_secs_f64() * 1000.);
    }
    for reader in &pins {
        assert!(reader.fill(1)?.is_some());
    }
    let pinned = pins.len();
    let steady = allocated(d)?;
    drop(pins);
    let gc = cold_gc::retry_with(d, &r, Verification::PathVerified)?;
    Index::open(d, &r, Verification::Full)?.verify()?;
    let status = fs::read_to_string("/proc/self/status")?;
    let rss = |key: &str| {
        status
            .lines()
            .find(|l| l.starts_with(key))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|n| n.parse::<u64>().ok())
            .map(|n| n * 1024)
    };
    println!(
        "{}",
        json!({"schema":"lc_pages_component_probe_v2","history":h,"cycles":20,"new_payload_bytes":49*200*20,"fixture_write_bytes":fixture_writes,"cold_logical_write_bytes":writes,"post_measure_cleanup_write_bytes":gc.logical_write_bytes,"rotation_durations_ms":durations,"gc_durations_ms":gc_ms,"full_first_lookup_ms":full_ms,"full_first_lookup_read_bytes":full_read,"path_first_lookup_ms":first_ms,"path_first_lookup_read_bytes":first_read,"warm_lookup_ms":warm,"buffer_max_single_allocation_bytes":buffer,"cold_peak_allocated_bytes":peak,"cold_steady_allocated_bytes":steady,"post_pin_release_allocated_bytes":allocated(d)?,"pinned_generations":pinned,"rss_bytes":rss("VmRSS:"),"hwm_bytes":rss("VmHWM:"),"current_reference":r,"correct":true,"limitations":["Component API: not full actor or durable journal rotation","Buffer field is largest observed segment allocation, not entire process working set","Logical writes exclude one-time fixture; pin-release cleanup separately included by analyzer"]})
    );
    Ok(())
}
