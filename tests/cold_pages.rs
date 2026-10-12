use mininautilus::{
    cold::RetiredOrder,
    cold_pages::{self, Index, Verification},
    model::{Fill, Lifecycle},
};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
static ID: AtomicU64 = AtomicU64::new(0);
fn dir() -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "mini-cold-pages-{}-{}",
        std::process::id(),
        ID.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir(&p).unwrap();
    p
}
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
                execution_id: id * 2,
                order_id: id,
                qty: 1,
                price: 100,
            })
            .collect(),
    )
}
#[test]
fn batch_split_lookup_and_full_validation_share_immutable_pages() {
    let d = dir();
    let (o, f) = rows(1, 15000);
    let mut reference = None;
    for (i, (o, f)) in o.chunks(200).zip(f.chunks(200)).enumerate() {
        let old = reference
            .as_ref()
            .map(|r| Index::open(&d, r, Verification::PathVerified).unwrap());
        let (r, _) = cold_pages::extend(&d.join(format!("g{i}.cold")), old.as_ref(), o, f).unwrap();
        reference = Some(r);
    }
    let r = reference.unwrap();
    let full = Index::open(&d, &r, Verification::Full).unwrap();
    for id in [1, 120, 121, 14400, 15000] {
        assert_eq!(full.order(id).unwrap().unwrap().filled, 1);
        assert_eq!(full.fill(id * 2).unwrap().unwrap().order_id, id);
    }
    assert!(full.fill(3).unwrap().is_none());
    assert!(full.order(15001).unwrap().is_none());
    let path = Index::open(&d, &r, Verification::PathVerified).unwrap();
    assert!(path.fill(30000).unwrap().is_some());
    assert!(path.stats().logical_read_bytes < 20000);
    assert!(full.stats().logical_read_bytes > path.stats().logical_read_bytes);
    drop(full);
    drop(path);
    fs::remove_dir_all(d).unwrap();
}
#[test]
fn scattered_execution_keys_preserve_old_values_and_refuse_collisions() {
    let d = dir();
    let (o, f) = rows(1, 400);
    let (r, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    let old = Index::open(&d, &r, Verification::Full).unwrap();
    let (o, mut f) = rows(401, 200);
    for (i, v) in f.iter_mut().enumerate() {
        v.execution_id = 1 + 2 * i as u64;
    }
    let (r, stats) = cold_pages::extend(&d.join("g1.cold"), Some(&old), &o, &f).unwrap();
    assert!(stats.logical_write_bytes < 100000);
    let new = Index::open(&d, &r, Verification::Full).unwrap();
    assert_eq!(new.fill(1).unwrap().unwrap().order_id, 401);
    assert_eq!(old.fill(2).unwrap().unwrap().order_id, 1);
    assert!(old.fill(1).unwrap().is_none());
    let (o, mut f) = rows(601, 1);
    f[0].execution_id = 2;
    assert!(cold_pages::extend(&d.join("g2.cold"), Some(&new), &o, &f).is_err());
    assert!(new.order(601).unwrap().is_none());
    fs::remove_dir_all(d).unwrap();
}
#[test]
fn corrupt_root_and_unvisited_page_are_rejected_at_documented_times() {
    let d = dir();
    let (o, f) = rows(1, 1000);
    let (r, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    let rootbytes = fs::read(d.join("g0.cold")).unwrap();
    let root: serde_json::Value = serde_json::from_slice(&rootbytes).unwrap();
    let h = root["fills"]["hash"].as_u64().unwrap();
    let path = d.join(".cold-pages-v2").join(format!("{h:016x}.page"));
    let mut b = fs::read(&path).unwrap();
    b[70] ^= 1;
    fs::write(&path, b).unwrap();
    assert!(Index::open(&d, &r, Verification::Full).is_err());
    let partial = Index::open(&d, &r, Verification::PathVerified).unwrap();
    assert!(partial.fill(2).is_err());
    assert!(partial.order(1).unwrap().is_some());
    assert!(partial.verify().is_err());
    fs::write(d.join("g0.cold"), b"invalid").unwrap();
    assert!(Index::open(&d, &r, Verification::PathVerified).is_err());
    fs::remove_dir_all(d).unwrap();
}
#[test]
fn publication_retry_and_invalid_inputs_do_not_overwrite_roots() {
    let d = dir();
    let (o, f) = rows(1, 10);
    let (r, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    let before = fs::read(d.join("g0.cold")).unwrap();
    let (r2, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    assert_eq!(r, r2);
    let (o, f) = rows(11, 10);
    assert!(cold_pages::extend(&d.join("g0.cold"), None, &o, &f).is_err());
    assert_eq!(fs::read(d.join("g0.cold")).unwrap(), before);
    fs::remove_dir_all(d).unwrap();
}

#[test]
fn pinned_generations_share_pages_and_retry_catalogue_survives_restart() {
    use mininautilus::cold_gc;
    let d = dir();
    let (o, f) = rows(1, 1000);
    let (mut r, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    let mut pins = Vec::new();
    for cycle in 0..20 {
        let old = Index::open(&d, &r, Verification::PathVerified).unwrap();
        let (o, f) = rows(1001 + cycle * 200, 200);
        let (next, _) =
            cold_pages::extend(&d.join(format!("g{}.cold", cycle + 1)), Some(&old), &o, &f)
                .unwrap();
        pins.push(old);
        cold_gc::register_and_sweep_with(&d, &r, &next, Verification::PathVerified).unwrap();
        r = next;
    }
    for old in &pins {
        assert!(old.order(1).unwrap().is_some());
    }
    let before = fs::read_dir(d.join(".cold-pages-v2")).unwrap().count();
    let report = cold_gc::retry_with(&d, &r, Verification::PathVerified).unwrap();
    assert_eq!(report.pinned_roots, 20);
    assert_eq!(report.pages_removed, 0);
    drop(pins);
    let report = cold_gc::retry_with(&d, &r, Verification::PathVerified).unwrap();
    assert!(report.pages_removed > 0);
    assert_eq!(report.roots_removed, 20);
    assert!(fs::read_dir(d.join(".cold-pages-v2")).unwrap().count() < before);
    let full = Index::open(&d, &r, Verification::Full).unwrap();
    assert_eq!(full.order(5000).unwrap().unwrap().id, 5000);
    assert!(full.fill(10000).unwrap().is_some());
    assert_eq!(
        cold_gc::retry_with(&d, &r, Verification::PathVerified)
            .unwrap()
            .pages_removed,
        0
    );
    fs::remove_dir_all(d).unwrap();
}

#[test]
fn legacy_migration_preserves_original_and_exact_records() {
    let d = dir();
    let (o, f) = rows(1, 1200);
    let old = mininautilus::cold::write(&d.join("legacy.cold"), &o, &f).unwrap();
    let original = fs::read(d.join("legacy.cold")).unwrap();
    let (r, _) = cold_pages::migrate_legacy(&d, &old, &d.join("migrated.cold")).unwrap();
    assert_eq!(fs::read(d.join("legacy.cold")).unwrap(), original);
    let reader = mininautilus::cold::ColdIndex::open(&d, &r).unwrap();
    assert_eq!(reader.fill(2400).unwrap(), Some(f.last().unwrap().clone()));
    assert_eq!(r.orders, old.orders);
    fs::remove_dir_all(d).unwrap();
}

#[test]
fn rollback_export_is_byte_identical_to_legacy_and_never_overwrites() {
    let d = dir();
    let (o, f) = rows(1, 400);
    let (r, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    let index = Index::open(&d, &r, Verification::Full).unwrap();
    let exported = index.export_legacy(&d.join("exported.cold")).unwrap();
    let expected = mininautilus::cold::write(&d.join("expected.cold"), &o, &f).unwrap();
    assert_eq!(exported.checksum, expected.checksum);
    assert_eq!(
        fs::read(d.join("exported.cold")).unwrap(),
        fs::read(d.join("expected.cold")).unwrap()
    );
    assert!(index.export_legacy(&d.join("exported.cold")).is_err());
    fs::remove_dir_all(d).unwrap();
}

#[test]
fn corrupted_retirement_catalogue_refuses_gc_and_preserves_current() {
    let d = dir();
    let (o, f) = rows(1, 10);
    let (r, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    fs::write(d.join(".cold-retirement-v1.json"), b"invalid").unwrap();
    assert!(mininautilus::cold_gc::retry_with(&d, &r, Verification::PathVerified).is_err());
    assert!(Index::open(&d, &r, Verification::Full).is_ok());
    fs::remove_dir_all(d).unwrap();
}

#[test]
fn unregistered_old_reader_prevents_reclamation_of_shared_pages() {
    let d = dir();
    let (o, f) = rows(1, 120);
    let (r0, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    let unregistered = Index::open(&d, &r0, Verification::PathVerified).unwrap();
    let (o, f) = rows(121, 100);
    let (r1, _) = cold_pages::extend(&d.join("g1.cold"), Some(&unregistered), &o, &f).unwrap();
    let old = Index::open(&d, &r1, Verification::PathVerified).unwrap();
    let (o, mut f) = rows(221, 1);
    f[0].execution_id = 1;
    let (r2, _) = cold_pages::extend(&d.join("g2.cold"), Some(&old), &o, &f).unwrap();
    drop(old);
    let report =
        mininautilus::cold_gc::register_and_sweep_with(&d, &r1, &r2, Verification::PathVerified)
            .unwrap();
    assert!(report.page_readers_present);
    assert_eq!(report.pages_removed, 0);
    assert_eq!(unregistered.fill(2).unwrap().unwrap().order_id, 1);
    drop(unregistered);
    let report = mininautilus::cold_gc::retry_with(&d, &r2, Verification::PathVerified).unwrap();
    assert!(report.pages_removed > 0);
    Index::open(&d, &r2, Verification::Full).unwrap();
    fs::remove_dir_all(d).unwrap();
}

#[test]
fn committed_successor_repairs_catalogue_gap_after_publication_crash() {
    let d = dir();
    let (o, f) = rows(1, 10);
    let (r0, _) = cold_pages::extend(&d.join("g0.cold"), None, &o, &f).unwrap();
    let old = Index::open(&d, &r0, Verification::PathVerified).unwrap();
    let (o, f) = rows(11, 10);
    let (r1, _) = cold_pages::extend(&d.join("g1.cold"), Some(&old), &o, &f).unwrap();
    drop(old);
    let report = mininautilus::cold_gc::retry_with(&d, &r1, Verification::PathVerified).unwrap();
    assert_eq!(report.roots_removed, 1);
    assert!(!d.join("g0.cold").exists());
    let old = Index::open(&d, &r1, Verification::PathVerified).unwrap();
    let (o, f) = rows(21, 10);
    let (r2, _) = cold_pages::extend(&d.join("g2.cold"), Some(&old), &o, &f).unwrap();
    drop(old);
    mininautilus::cold_gc::retry_with(&d, &r2, Verification::PathVerified).unwrap();
    assert!(!d.join("g1.cold").exists());
    Index::open(&d, &r2, Verification::Full).unwrap();
    fs::remove_dir_all(d).unwrap();
}
