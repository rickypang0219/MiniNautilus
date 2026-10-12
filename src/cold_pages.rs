//! Immutable, batch-updated cold B+ trees. Full verification is the default.
//! PathVerified is an explicit offline integrity experiment, not live approval.
use crate::cold::{ColdRef, RetiredOrder, fault_point};
use crate::model::{Fill, Lifecycle};
use serde::{Deserialize, Serialize};
use std::{
    cell::Cell,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

const PAGE: usize = 4096;
const CAP: usize = 120;
const OFFSET: usize = 32;
const MAGIC: &[u8; 8] = b"MNCPAG02";
fn bad(s: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, s)
}
fn hash(b: &[u8]) -> u64 {
    b.iter().fold(0xcbf29ce484222325, |h, b| {
        (h ^ *b as u64).wrapping_mul(0x100000001b3)
    })
}
fn word(b: &[u8], n: usize) -> u64 {
    u64::from_le_bytes(b[n..n + 8].try_into().unwrap())
}
fn terminal(l: Lifecycle) -> io::Result<u64> {
    match l {
        Lifecycle::Filled => Ok(1),
        Lifecycle::Canceled => Ok(2),
        Lifecycle::Rejected => Ok(3),
        _ => Err(bad("nonterminal cold order")),
    }
}
fn lifecycle(n: u64) -> io::Result<Lifecycle> {
    match n {
        1 => Ok(Lifecycle::Filled),
        2 => Ok(Lifecycle::Canceled),
        3 => Ok(Lifecycle::Rejected),
        _ => Err(bad("invalid cold lifecycle")),
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
struct Link {
    max: u64,
    hash: u64,
    count: u64,
    level: u64,
}
impl Link {
    fn row(self) -> [u64; 4] {
        [self.max, self.hash, self.count, self.level]
    }
    fn from(r: [u64; 4]) -> Self {
        Self {
            max: r[0],
            hash: r[1],
            count: r[2],
            level: r[3],
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
struct Root {
    magic: String,
    orders: Option<Link>,
    fills: Option<Link>,
    parent: Option<ColdRef>,
    retired_pages: Vec<u64>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verification {
    Full,
    PathVerified,
}
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct Stats {
    pub logical_read_bytes: u64,
    pub logical_write_bytes: u64,
    pub pages_written: u64,
    pub working_buffer_bytes: usize,
}

pub struct Index {
    directory: PathBuf,
    root: Root,
    reference: ColdRef,
    _pin: File,
    _store_reader: File,
    stats: Cell<Stats>,
}
impl Index {
    pub fn open(
        directory: &Path,
        reference: &ColdRef,
        verification: Verification,
    ) -> io::Result<Self> {
        if reference.file.contains('/') || reference.file.starts_with('.') {
            return Err(bad("invalid cold root filename"));
        }
        let registration = crate::cold_gc::registration(directory)?;
        if !fs::symlink_metadata(directory.join(".cold-pages-v2"))?
            .file_type()
            .is_dir()
        {
            return Err(bad("cold page store must be a real directory"));
        }
        let mut pin = File::open(directory.join(&reference.file))?;
        pin.try_lock_shared().map_err(io::Error::other)?;
        if pin.metadata()?.len() > 65536 {
            return Err(bad("oversized cold root"));
        }
        let mut bytes = Vec::new();
        pin.read_to_end(&mut bytes)?;
        if hash(&bytes) != reference.checksum {
            return Err(bad("cold root checksum mismatch"));
        }
        let root: Root = serde_json::from_slice(&bytes)?;
        if root.magic != "MNCOLD02"
            || root.orders.map_or(0, |r| r.count) != reference.orders
            || root.fills.map_or(0, |r| r.count) != reference.fills
        {
            return Err(bad("cold root count or version mismatch"));
        }
        let index = Self {
            directory: directory.to_owned(),
            root,
            reference: reference.clone(),
            _pin: pin,
            _store_reader: registration,
            stats: Cell::new(Stats {
                logical_read_bytes: bytes.len() as u64,
                ..Stats::default()
            }),
        };
        if verification == Verification::Full {
            index.verify()?;
        }
        Ok(index)
    }
    fn page_path(&self, h: u64) -> PathBuf {
        self.directory
            .join(".cold-pages-v2")
            .join(format!("{h:016x}.page"))
    }
    fn read_page(&self, link: Link, kind: u64) -> io::Result<Vec<[u64; 4]>> {
        if link.level > 16 || link.count == 0 {
            return Err(bad("invalid cold tree link"));
        }
        let mut file = File::open(self.page_path(link.hash))?;
        if file.metadata()?.len() != PAGE as u64 {
            return Err(bad("cold page length mismatch"));
        }
        let mut b = [0; PAGE];
        file.read_exact(&mut b)?;
        let mut stats = self.stats.get();
        stats.logical_read_bytes += PAGE as u64;
        self.stats.set(stats);
        if &b[..8] != MAGIC
            || hash(&b[..PAGE - 8]) != link.hash
            || word(&b, PAGE - 8) != link.hash
            || word(&b, 8) != kind
            || word(&b, 16) != link.level
        {
            return Err(bad("cold page checksum/type mismatch"));
        }
        let n = word(&b, 24) as usize;
        if n == 0 || n > CAP || b[OFFSET + 32 * n..PAGE - 8].iter().any(|v| *v != 0) {
            return Err(bad("cold page invalid count/padding"));
        }
        let rows: Vec<_> = (0..n)
            .map(|i| std::array::from_fn(|j| word(&b, OFFSET + i * 32 + j * 8)))
            .collect();
        let count = if link.level == 0 {
            n as u64
        } else {
            rows.iter()
                .try_fold(0_u64, |s, r| s.checked_add(r[2]))
                .ok_or_else(|| bad("cold page count overflow"))?
        };
        if rows.last().unwrap()[0] != link.max
            || count != link.count
            || rows.windows(2).any(|w| w[0][0] >= w[1][0])
            || (link.level > 0
                && rows
                    .iter()
                    .any(|r| r[3].checked_add(1) != Some(link.level) || r[2] == 0))
        {
            return Err(bad("cold page range/count mismatch"));
        }
        if link.level == 0 {
            for r in &rows {
                if kind == 1 {
                    lifecycle(r[2])?;
                    if r[3] != 0 || (r[1] as i64) < 0 {
                        return Err(bad("invalid order page values"));
                    }
                } else if (r[2] as i64) <= 0 || (r[3] as i64) <= 0 {
                    return Err(bad("invalid fill page values"));
                }
            }
        }
        Ok(rows)
    }
    fn verify_at(&self, link: Link, kind: u64, lower: Option<u64>) -> io::Result<()> {
        let rows = self.read_page(link, kind)?;
        if lower.is_some_and(|n| rows[0][0] <= n) {
            return Err(bad("overlapping cold page range"));
        }
        if link.level == 0 {
            for r in rows {
                if kind == 1 {
                    lifecycle(r[2])?;
                    if r[3] != 0 || (r[1] as i64) < 0 {
                        return Err(bad("invalid cold order row"));
                    }
                } else if (r[2] as i64) <= 0 || (r[3] as i64) <= 0 {
                    return Err(bad("invalid cold fill row"));
                }
            }
        } else {
            let mut previous = lower;
            for r in rows {
                self.verify_at(Link::from(r), kind, previous)?;
                previous = Some(r[0]);
            }
        }
        Ok(())
    }
    pub fn verify(&self) -> io::Result<()> {
        if let Some(r) = self.root.orders {
            self.verify_at(r, 1, None)?;
        }
        if let Some(r) = self.root.fills {
            self.verify_at(r, 2, None)?;
        }
        Ok(())
    }
    fn lookup(&self, mut link: Option<Link>, key: u64, kind: u64) -> io::Result<Option<[u64; 4]>> {
        while let Some(r) = link {
            if key > r.max {
                return Ok(None);
            }
            let rows = self.read_page(r, kind)?;
            let at = rows.partition_point(|v| v[0] < key);
            if r.level == 0 {
                return Ok(rows.get(at).filter(|v| v[0] == key).copied());
            }
            link = rows.get(at).map(|r| Link::from(*r));
        }
        Ok(None)
    }
    pub fn order(&self, id: u64) -> io::Result<Option<RetiredOrder>> {
        self.lookup(self.root.orders, id, 1)?
            .map(|r| {
                Ok(RetiredOrder {
                    id: r[0],
                    filled: r[1] as i64,
                    lifecycle: lifecycle(r[2])?,
                })
            })
            .transpose()
    }
    pub fn fill(&self, id: u64) -> io::Result<Option<Fill>> {
        Ok(self.lookup(self.root.fills, id, 2)?.map(|r| Fill {
            execution_id: r[0],
            order_id: r[1],
            qty: r[2] as i64,
            price: r[3] as i64,
        }))
    }
    pub(crate) fn publication_parent(&self) -> Option<ColdRef> {
        self.root.parent.clone()
    }

    pub fn stats(&self) -> Stats {
        self.stats.get()
    }
}

struct Writer<'a> {
    directory: &'a Path,
    source: Option<&'a Index>,
    stats: Stats,
    retired: Vec<u64>,
}
impl Writer<'_> {
    fn page(&mut self, kind: u64, level: u64, rows: &[[u64; 4]]) -> io::Result<Link> {
        if rows.is_empty() || rows.len() > CAP {
            return Err(bad("invalid cold page write"));
        }
        let mut b = [0; PAGE];
        b[..8].copy_from_slice(MAGIC);
        for (i, n) in [kind, level, rows.len() as u64].iter().enumerate() {
            b[8 + i * 8..16 + i * 8].copy_from_slice(&n.to_le_bytes());
        }
        for (i, r) in rows.iter().enumerate() {
            for (j, n) in r.iter().enumerate() {
                b[OFFSET + i * 32 + j * 8..OFFSET + i * 32 + j * 8 + 8]
                    .copy_from_slice(&n.to_le_bytes());
            }
        }
        let h = hash(&b[..PAGE - 8]);
        b[PAGE - 8..].copy_from_slice(&h.to_le_bytes());
        let path = self
            .directory
            .join(".cold-pages-v2")
            .join(format!("{h:016x}.page"));
        fault_point("page_before")?;
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut f) => {
                self.stats.logical_write_bytes += PAGE as u64;
                self.stats.pages_written += 1;
                f.write_all(&b[..PAGE / 2])?;
                fault_point("page_mid")?;
                f.write_all(&b[PAGE / 2..])?;
                f.sync_all()?;
            }
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                if fs::read(&path)? != b {
                    return Err(bad("conflicting immutable cold page"));
                }
                File::open(&path)?.sync_all()?;
            }
            Err(e) => return Err(e),
        }
        if fs::read(&path)? != b {
            return Err(bad("new cold page verification failed"));
        }
        self.stats.logical_read_bytes += PAGE as u64;
        fault_point("page_fsync_after")?;
        let count = if level == 0 {
            rows.len() as u64
        } else {
            rows.iter().map(|r| r[2]).sum()
        };
        Ok(Link {
            max: rows.last().unwrap()[0],
            hash: h,
            count,
            level,
        })
    }
    fn pack(&mut self, kind: u64, level: u64, rows: &[[u64; 4]]) -> io::Result<Vec<Link>> {
        rows.chunks(CAP)
            .map(|r| self.page(kind, level, r))
            .collect()
    }
    fn insert(&mut self, old: Link, kind: u64, new: &[[u64; 4]]) -> io::Result<Vec<Link>> {
        if new.is_empty() {
            return Ok(vec![old]);
        }
        let rows = self
            .source
            .ok_or_else(|| bad("missing old tree"))?
            .read_page(old, kind)?;
        let links = if old.level == 0 {
            let mut merged = Vec::with_capacity(rows.len() + new.len());
            let (mut a, mut b) = (0, 0);
            while a < rows.len() || b < new.len() {
                if a < rows.len() && b < new.len() && rows[a][0] == new[b][0] {
                    return Err(bad("cold key overlaps retired records"));
                }
                if b == new.len() || (a < rows.len() && rows[a][0] < new[b][0]) {
                    merged.push(rows[a]);
                    a += 1;
                } else {
                    merged.push(new[b]);
                    b += 1;
                }
            }
            self.stats.working_buffer_bytes =
                self.stats.working_buffer_bytes.max(merged.capacity() * 32);
            self.pack(kind, 0, &merged)?
        } else {
            let mut output = Vec::new();
            let mut used = 0;
            for (i, r) in rows.iter().enumerate() {
                let end = if i + 1 == rows.len() {
                    new.len()
                } else {
                    used + new[used..].partition_point(|v| v[0] <= r[0])
                };
                for child in self.insert(Link::from(*r), kind, &new[used..end])? {
                    output.push(child.row());
                }
                used = end;
            }
            self.pack(kind, old.level, &output)?
        };
        if !links.iter().any(|r| r.hash == old.hash) {
            self.retired.push(old.hash);
        }
        Ok(links)
    }
    fn tree(&mut self, old: Option<Link>, kind: u64, new: &[[u64; 4]]) -> io::Result<Option<Link>> {
        let mut links = if let Some(r) = old {
            self.insert(r, kind, new)?
        } else {
            self.pack(kind, 0, new)?
        };
        while links.len() > 1 {
            let rows: Vec<_> = links.iter().map(|r| r.row()).collect();
            links = self.pack(kind, links[0].level + 1, &rows)?;
        }
        Ok(links.first().copied())
    }
}

/// Bounded segment updates. Does not reclaim historical pages or remove journals.
pub fn extend(
    path: &Path,
    previous: Option<&Index>,
    orders: &[RetiredOrder],
    fills: &[Fill],
) -> io::Result<(ColdRef, Stats)> {
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| bad("invalid cold root filename"))?;
    if name.starts_with('.') {
        return Err(bad("cold root must be a plain filename"));
    }
    if orders.len() > 10000
        || fills.len() > 10000
        || orders.windows(2).any(|w| w[0].id >= w[1].id)
        || fills
            .windows(2)
            .any(|w| w[0].execution_id >= w[1].execution_id)
    {
        return Err(bad("unsorted/oversized cold batch"));
    }
    let directory = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    if let Some(p) = previous
        && fs::canonicalize(&p.directory)? != fs::canonicalize(directory)?
    {
        return Err(bad("cold tree update requires same directory"));
    }
    let store = directory.join(".cold-pages-v2");
    match fs::create_dir(&store) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e),
    }
    if !fs::symlink_metadata(&store)?.file_type().is_dir() {
        return Err(bad("cold page store must not be a symlink"));
    }
    let mut writer = Writer {
        directory,
        source: previous,
        retired: Vec::new(),
        stats: Stats {
            working_buffer_bytes: PAGE,
            ..Stats::default()
        },
    };
    let order_rows: Vec<_> = orders
        .iter()
        .map(|r| Ok([r.id, r.filled as u64, terminal(r.lifecycle)?, 0]))
        .collect::<io::Result<_>>()?;
    if orders.iter().any(|r| r.filled < 0) || fills.iter().any(|f| f.qty <= 0 || f.price <= 0) {
        return Err(bad("invalid cold record"));
    }
    if previous
        .and_then(|p| p.root.orders)
        .zip(orders.first())
        .is_some_and(|(r, f)| r.max >= f.id)
    {
        return Err(bad("order IDs must advance retired floor"));
    }
    let fill_rows: Vec<_> = fills
        .iter()
        .map(|r| [r.execution_id, r.order_id, r.qty as u64, r.price as u64])
        .collect();
    let root = Root {
        parent: previous.map(|p| p.reference.clone()),
        magic: "MNCOLD02".into(),
        orders: writer.tree(previous.and_then(|p| p.root.orders), 1, &order_rows)?,
        fills: writer.tree(previous.and_then(|p| p.root.fills), 2, &fill_rows)?,
        retired_pages: writer.retired.clone(),
    };
    File::open(directory.join(".cold-pages-v2"))?.sync_all()?;
    fault_point("pages_dirsync_after")?;
    let bytes = serde_json::to_vec(&root)?;
    if bytes.len() > 65536 {
        return Err(bad("cold root metadata cap exceeded"));
    }
    let temporary = path.with_extension("cold-root-tmp");
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
    {
        Ok(mut file) => {
            writer.stats.logical_write_bytes += bytes.len() as u64;
            file.write_all(&bytes)?;
            file.sync_all()?;
        }
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            if fs::read(&temporary)? != bytes {
                return Err(bad("partial/conflicting cold root temporary survivor"));
            }
            File::open(&temporary)?.sync_all()?;
        }
        Err(e) => return Err(e),
    }
    fault_point("root_fsync_after")?;
    if path.exists() {
        if fs::read(path)? != bytes {
            return Err(bad("conflicting cold root survivor"));
        }
        fs::remove_file(&temporary)?;
    } else {
        fs::rename(&temporary, path)?;
    }
    fault_point("root_rename_after")?;
    File::open(directory)?.sync_all()?;
    fault_point("root_dirsync_after")?;
    Ok((
        ColdRef {
            file: path
                .file_name()
                .and_then(|s| s.to_str())
                .ok_or_else(|| bad("invalid cold root path"))?
                .into(),
            checksum: hash(&bytes),
            orders: root.orders.map_or(0, |r| r.count),
            fills: root.fills.map_or(0, |r| r.count),
        },
        writer.stats,
    ))
}

/// Read-only legacy import. Intermediate roots/original are preserved on failure.
/// The caller must bind the final reference in a durable journal before adoption.
pub fn migrate_legacy(
    directory: &Path,
    reference: &ColdRef,
    destination: &Path,
) -> io::Result<(ColdRef, Stats)> {
    let legacy = crate::cold::ColdIndex::open(directory, reference)?;
    let mut result = None;
    let mut totals = Stats::default();
    let mut at = 0;
    loop {
        let mut orders = Vec::new();
        let mut fills = Vec::new();
        for i in at..(at + 10000).min(reference.orders) {
            orders.push(legacy.order_at(i)?);
        }
        for i in at..(at + 10000).min(reference.fills) {
            fills.push(legacy.fill_at(i)?);
        }
        if orders.is_empty() && fills.is_empty() && result.is_some() {
            break;
        }
        let old = result
            .as_ref()
            .map(|r| Index::open(directory, r, Verification::PathVerified))
            .transpose()?;
        let intermediate = directory.join(format!(
            "{}.migration-{at}.cold",
            destination
                .file_name()
                .and_then(|s| s.to_str())
                .ok_or_else(|| bad("invalid migration path"))?
        ));
        let (r, stats) = extend(&intermediate, old.as_ref(), &orders, &fills)?;
        totals.logical_write_bytes += stats.logical_write_bytes;
        totals.pages_written += stats.pages_written;
        totals.working_buffer_bytes = totals.working_buffer_bytes.max(stats.working_buffer_bytes);
        result = Some(r);
        at += 10000;
        if at >= reference.orders.max(reference.fills) {
            break;
        }
    }
    let r = result.unwrap();
    let full = Index::open(directory, &r, Verification::Full)?;
    for i in 0..reference.orders {
        let o = legacy.order_at(i)?;
        let found = full
            .order(o.id)?
            .ok_or_else(|| bad("migration lost order"))?;
        if found.filled != o.filled || found.lifecycle != o.lifecycle {
            return Err(bad("migration order mismatch"));
        }
    }
    for i in 0..reference.fills {
        let f = legacy.fill_at(i)?;
        if full.fill(f.execution_id)? != Some(f) {
            return Err(bad("migration fill mismatch"));
        }
    }
    let bytes = fs::read(directory.join(&r.file))?;
    let mut out = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)?;
    out.write_all(&bytes)?;
    out.sync_all()?;
    File::open(directory)?.sync_all()?;
    totals.logical_write_bytes += bytes.len() as u64;
    totals.logical_read_bytes = full.stats().logical_read_bytes;
    Ok((
        ColdRef {
            file: destination.file_name().unwrap().to_str().unwrap().into(),
            ..r
        },
        totals,
    ))
}

/// Verified publication metadata used by catalogue retirement. No directory guesses.
pub(crate) fn retired_pages(
    directory: &Path,
    previous: &ColdRef,
    current: &ColdRef,
) -> io::Result<Vec<u64>> {
    let index = Index::open(directory, current, Verification::PathVerified)?;
    if index.root.parent.as_ref() != Some(previous) {
        return Err(bad("cold root retirement parent mismatch"));
    }
    Ok(index.root.retired_pages.clone())
}

impl Index {
    fn walk(
        &self,
        link: Option<Link>,
        kind: u64,
        visit: &mut impl FnMut([u64; 4]) -> io::Result<()>,
    ) -> io::Result<()> {
        if let Some(link) = link {
            let rows = self.read_page(link, kind)?;
            if link.level == 0 {
                for row in rows {
                    visit(row)?;
                }
            } else {
                for row in rows {
                    self.walk(Some(Link::from(row)), kind, visit)?;
                }
            }
        }
        Ok(())
    }
    /// Full verified streaming rollback export. Never overwrites the destination.
    pub fn export_legacy(&self, path: &Path) -> io::Result<ColdRef> {
        self.verify()?;
        let mut out = std::io::BufWriter::with_capacity(
            65536,
            OpenOptions::new().write(true).create_new(true).open(path)?,
        );
        let mut checksum = 0xcbf29ce484222325;
        let mut emit = |bytes: &[u8]| -> io::Result<()> {
            checksum = bytes
                .iter()
                .fold(checksum, |h, b| (h ^ *b as u64).wrapping_mul(0x100000001b3));
            out.write_all(bytes)
        };
        emit(b"MNCOLD01")?;
        emit(&self.reference.orders.to_le_bytes())?;
        emit(&self.reference.fills.to_le_bytes())?;
        self.walk(self.root.orders, 1, &mut |r| {
            emit(&r[0].to_le_bytes())?;
            emit(&r[1].to_le_bytes())?;
            emit(&[r[2] as u8])
        })?;
        self.walk(self.root.fills, 2, &mut |r| {
            for n in r {
                emit(&n.to_le_bytes())?;
            }
            Ok(())
        })?;
        out.write_all(&checksum.to_le_bytes())?;
        out.flush()?;
        out.get_ref().sync_all()?;
        File::open(path.parent().unwrap_or(Path::new(".")))?.sync_all()?;
        Ok(ColdRef {
            file: path
                .file_name()
                .and_then(|p| p.to_str())
                .ok_or_else(|| bad("invalid export path"))?
                .into(),
            checksum,
            orders: self.reference.orders,
            fills: self.reference.fills,
        })
    }
}
