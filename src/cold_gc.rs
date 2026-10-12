//! Durable retry catalogue. Registration and collection share a process-wide
//! filesystem lock; actual readers pin roots with shared file locks.
use crate::cold::{ColdIndex, ColdRef, fault_point};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::Path,
};
fn bad(s: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, s)
}
fn hash(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0xcbf29ce484222325, |h, b| {
        (h ^ *b as u64).wrapping_mul(0x100000001b3)
    })
}
fn lock(directory: &Path, exclusive: bool) -> io::Result<File> {
    let f = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(directory.join(if exclusive {
            ".cold-retirement.lock"
        } else {
            ".cold-gc.lock"
        }))?;
    if exclusive {
        f.try_lock().map_err(io::Error::other)?;
    } else {
        f.lock_shared()?;
    }
    Ok(f)
}
pub(crate) fn registration(directory: &Path) -> io::Result<File> {
    lock(directory, false)
}
#[derive(Clone, Serialize, Deserialize)]
struct Catalogue {
    current: ColdRef,
    pending: Vec<ColdRef>,
    retired_pages: Vec<u64>,
}
#[derive(Serialize, Deserialize)]
struct Frame {
    payload: String,
    checksum: u64,
}
#[derive(Clone, Copy, Default, Serialize)]
pub struct Report {
    pub roots_removed: u64,
    pub pages_removed: u64,
    pub pinned_roots: u64,
    pub logical_write_bytes: u64,
    pub page_readers_present: bool,
}
fn load(directory: &Path) -> io::Result<Option<Catalogue>> {
    let p = directory.join(".cold-retirement-v1.json");
    let mut f = match File::open(p) {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e),
    };
    if f.metadata()?.len() > 1048576 {
        return Err(bad("cold catalogue size cap"));
    }
    let mut b = Vec::new();
    f.read_to_end(&mut b)?;
    let frame: Frame = serde_json::from_slice(&b)?;
    if hash(frame.payload.as_bytes()) != frame.checksum {
        return Err(bad("cold catalogue checksum mismatch"));
    }
    let c: Catalogue = serde_json::from_str(&frame.payload)?;
    if c.pending.len() > 64
        || c.retired_pages.len() > 20000
        || c.pending
            .iter()
            .any(|r| r.file == c.current.file || r.file.contains('/') || r.file.starts_with('.'))
    {
        return Err(bad("cold catalogue invalid references/caps"));
    }
    Ok(Some(c))
}
fn save(directory: &Path, c: &Catalogue) -> io::Result<u64> {
    let payload = serde_json::to_string(c)?;
    let bytes = serde_json::to_vec(&Frame {
        checksum: hash(payload.as_bytes()),
        payload,
    })?;
    if bytes.len() > 1048576 {
        return Err(bad("cold catalogue size cap"));
    }
    let temp = directory.join(".cold-retirement-v1.tmp");
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&temp)?;
    fault_point("catalog_before")?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    fault_point("catalog_fsync_after")?;
    fs::rename(temp, directory.join(".cold-retirement-v1.json"))?;
    fault_point("catalog_rename_after")?;
    File::open(directory)?.sync_all()?;
    fault_point("catalog_dirsync_after")?;
    Ok(bytes.len() as u64)
}
/// Register before unlinking. Latest reference must descend from the known current.
pub fn register_and_sweep(
    directory: &Path,
    previous: &ColdRef,
    current: &ColdRef,
) -> io::Result<Report> {
    register_and_sweep_with(
        directory,
        previous,
        current,
        crate::cold_pages::Verification::Full,
    )
}
pub fn register_and_sweep_with(
    directory: &Path,
    previous: &ColdRef,
    current: &ColdRef,
    verification: crate::cold_pages::Verification,
) -> io::Result<Report> {
    if previous.file == current.file {
        return Err(bad("cannot retire current root"));
    }
    // Validate outside exclusive registration lock to avoid lock inversion.
    let current_index = ColdIndex::open_with(directory, current, verification)?;
    let mut magic = [0_u8; 8];
    File::open(directory.join(&current.file))?.read_exact(&mut magic)?;
    let pages = if magic[0] == b'{' {
        crate::cold_pages::retired_pages(directory, previous, current)?
    } else {
        Vec::new()
    };
    drop(current_index);
    let _guard = lock(directory, true)?;
    let mut c = load(directory)?.unwrap_or(Catalogue {
        current: previous.clone(),
        pending: Vec::new(),
        retired_pages: Vec::new(),
    });
    if c.current != *current {
        if c.current != *previous {
            return Err(bad("stale cold retirement lineage"));
        }
        if c.pending.len() >= 64 || c.retired_pages.len() + pages.len() > 20000 {
            return Err(bad("cold retirement debt cap"));
        }
        c.pending.push(previous.clone());
        c.retired_pages.extend(pages);
        c.current = current.clone();
    }
    let mut report = Report {
        logical_write_bytes: save(directory, &c)?,
        ..Report::default()
    };
    sweep_locked(directory, &mut c, &mut report)?;
    Ok(report)
}
/// Caller supplies the live journal's exact current reference, not a file-name hint.
pub fn retry(directory: &Path, current: &ColdRef) -> io::Result<Report> {
    retry_with(directory, current, crate::cold_pages::Verification::Full)
}
pub fn retry_with(
    directory: &Path,
    current: &ColdRef,
    verification: crate::cold_pages::Verification,
) -> io::Result<Report> {
    let verified = ColdIndex::open_with(directory, current, verification)?;
    let parent = verified.publication_parent();
    drop(verified);
    let guard = lock(directory, true)?;
    let catalogue = load(directory)?;
    let previous = match &catalogue {
        Some(c) if c.current != *current => {
            if parent.as_ref() != Some(&c.current) {
                return Err(bad("stale cold GC caller or unsupported missing lineage"));
            }
            Some(c.current.clone())
        }
        None => parent,
        _ => None,
    };
    if let Some(previous) = previous {
        drop(guard);
        return register_and_sweep_with(directory, &previous, current, verification);
    }
    let Some(mut c) = catalogue else {
        return Ok(Report::default());
    };
    let mut report = Report::default();
    sweep_locked(directory, &mut c, &mut report)?;
    Ok(report)
}
fn sweep_locked(directory: &Path, c: &mut Catalogue, r: &mut Report) -> io::Result<()> {
    let mut retained = Vec::new();
    for reference in &c.pending {
        let path = directory.join(&reference.file);
        let file = match File::open(&path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e),
        };
        if file.try_lock().is_err() {
            r.pinned_roots += 1;
            retained.push(reference.clone());
            continue;
        }
        fault_point("gc_before")?;
        fs::remove_file(&path)?;
        r.roots_removed += 1;
        fault_point("gc_after_unlink")?;
    }
    // Conservative sharing policy: no replaced pages until every retired pin is gone.
    // Current readers reference no replaced page. No historical graph scan is needed.
    let page_guard = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(directory.join(".cold-gc.lock"))?;
    let readers_absent = page_guard.try_lock().is_ok();
    r.page_readers_present = !readers_absent;
    if retained.is_empty() && readers_absent {
        for h in &c.retired_pages {
            let path = directory
                .join(".cold-pages-v2")
                .join(format!("{h:016x}.page"));
            match fs::remove_file(path) {
                Ok(()) => r.pages_removed += 1,
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
        }
        if !c.retired_pages.is_empty() {
            File::open(directory.join(".cold-pages-v2"))?.sync_all()?;
        }
        c.retired_pages.clear();
    }
    File::open(directory)?.sync_all()?;
    fault_point("gc_after")?;
    c.pending = retained;
    r.logical_write_bytes += save(directory, c)?;
    Ok(())
}

/// Conservative publication admission. Counts all cold-named files/pages even
/// if they are orphaned; never deletes unregistered evidence to meet the budget.
pub fn preflight(
    directory: &Path,
    current: Option<&ColdRef>,
    orders: usize,
    fills: usize,
    paged: bool,
) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    let _guard = lock(directory, true)?;
    if let Some(c) = load(directory)? {
        if current != Some(&c.current) {
            return Err(bad("cold GC preflight lineage mismatch"));
        }
        if c.pending.len() >= 64 || c.retired_pages.len() >= 19000 {
            return Err(bad(
                "cold GC debt requires reader release before publication",
            ));
        }
    }
    let total_o = current
        .map_or(0, |r| r.orders)
        .checked_add(orders as u64)
        .ok_or_else(|| bad("cold count overflow"))?;
    let total_f = current
        .map_or(0, |r| r.fills)
        .checked_add(fills as u64)
        .ok_or_else(|| bad("cold count overflow"))?;
    let logical = total_o
        .checked_mul(17)
        .and_then(|v| total_f.checked_mul(32).and_then(|f| v.checked_add(f)))
        .and_then(|v| v.checked_add(32))
        .ok_or_else(|| bad("cold size overflow"))?;
    let budget = logical
        .checked_mul(4)
        .and_then(|v| v.checked_add(16 * 1048576))
        .ok_or_else(|| bad("cold size overflow"))?;
    let mut allocated = 0_u64;
    for entry in fs::read_dir(directory)? {
        let e = entry?;
        if e.file_name().to_string_lossy().contains(".cold") && e.file_type()?.is_file() {
            allocated = allocated.saturating_add(e.metadata()?.blocks() * 512);
        }
    }
    let pages = directory.join(".cold-pages-v2");
    if pages.exists() {
        for entry in fs::read_dir(pages)? {
            allocated = allocated.saturating_add(entry?.metadata()?.blocks() * 512);
        }
    }
    let planned = if paged {
        // Monotonic order IDs touch the right spine. Fills may be scattered;
        // at most one touched old leaf per key, capped by existing leaf count.
        let leaves = total_f.div_ceil(60).min(fills as u64);
        let nodes = (orders as u64).div_ceil(120) + leaves + (fills as u64).div_ceil(120) + 6;
        nodes.saturating_mul(4096 * 17).saturating_add(65536)
    } else {
        logical
    };
    if allocated.saturating_add(planned) > budget {
        return Err(bad(
            "cold publication would exceed disk budget; release pinned generations",
        ));
    }
    Ok(())
}
