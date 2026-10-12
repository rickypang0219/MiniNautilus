//! Immutable evidence for orders retired by live journal rotation
//! (docs/adr-live-rotation.md). Read only by the durable engine, never by Core:
//! the engine journals each verdict, so replay never needs this file.
//!
//! Layout (little endian): magic(8) | orders u64 | fills u64 |
//! orders sorted by id: id u64, filled i64, lifecycle u8 (17 B) |
//! fills sorted by execution id: execution_id u64, order_id u64, qty i64, price i64 (32 B) |
//! FNV-1a 64 of every preceding byte.
use crate::model::{Fill, Lifecycle, OrderId, Report, RetiredVerdict};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write},
    os::unix::fs::FileExt,
    path::Path,
};

const MAGIC: &[u8; 8] = b"MNCOLD01";
const HEADER: u64 = 24;
const ORDER: u64 = 17;
const FILL: u64 = 32;

/// Reference stored in the successor genesis (and carried by Core as metadata).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ColdRef {
    /// File name, relative to the journal's directory.
    pub file: String,
    pub checksum: u64,
    pub orders: u64,
    pub fills: u64,
}

/// A retired order's final state.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RetiredOrder {
    pub id: OrderId,
    pub filled: i64,
    pub lifecycle: Lifecycle,
}

/// Explicit fault-test build only. Default production builds compile this away.
/// An arm file permits fixtures to be created before the selected crash boundary.
pub fn fault_point(name: &str) -> io::Result<()> {
    #[cfg(feature = "fault-injection")]
    if std::env::var("MINI_LC_FAULT_ARM_FILE")
        .ok()
        .is_none_or(|p| Path::new(&p).exists())
    {
        if let Ok(selected) = std::env::var("MINI_LC_CRASH_AT")
            && selected == name
        {
            std::process::exit(99);
        }
        if let Ok(selected) = std::env::var("MINI_LC_ERROR_AT")
            && selected == name
        {
            return Err(io::Error::other(format!("injected I/O failure at {name}")));
        }
    }
    let _ = name;
    Ok(())
}

fn invalid(reason: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, reason.to_string())
}

fn fnv(hash: u64, bytes: &[u8]) -> u64 {
    bytes
        .iter()
        .fold(hash, |h, b| (h ^ *b as u64).wrapping_mul(0x100000001b3))
}
const FNV_OFFSET: u64 = 0xcbf29ce484222325;

fn lifecycle_code(lifecycle: Lifecycle) -> io::Result<u8> {
    match lifecycle {
        Lifecycle::Filled => Ok(1),
        Lifecycle::Canceled => Ok(2),
        Lifecycle::Rejected => Ok(3),
        _ => Err(invalid("only terminal orders can be retired")),
    }
}

fn lifecycle_from(code: u8) -> io::Result<Lifecycle> {
    match code {
        1 => Ok(Lifecycle::Filled),
        2 => Ok(Lifecycle::Canceled),
        3 => Ok(Lifecycle::Rejected),
        _ => Err(invalid("corrupt cold lifecycle")),
    }
}

/// Writes a new index without allocating a full-file serialization buffer.
/// The format and full-file integrity check are unchanged.
pub fn write(path: &Path, orders: &[RetiredOrder], fills: &[Fill]) -> io::Result<ColdRef> {
    extend(path, None, orders, fills)
}

/// Merge a verified immutable predecessor with only the current segment in RAM.
/// Working memory is 64 KiB plus the bounded segment slices. Old records are
/// read one at a time; write amplification remains O(history) and is reported.
pub fn extend(
    path: &Path,
    previous: Option<&ColdIndex>,
    orders: &[RetiredOrder],
    fills: &[Fill],
) -> io::Result<ColdRef> {
    if let Some(index) = previous.and_then(|p| p.paged.as_ref()) {
        return crate::cold_pages::extend(path, Some(index), orders, fills).map(|r| r.0);
    }
    if orders.windows(2).any(|w| w[0].id >= w[1].id)
        || fills
            .windows(2)
            .any(|w| w[0].execution_id >= w[1].execution_id)
    {
        return Err(invalid("cold records must be unique and sorted"));
    }
    let (old_orders, old_fills) = previous.map_or((0, 0), |p| (p.orders, p.fills));
    if let (Some(p), Some(first)) = (previous, orders.first())
        && old_orders > 0
        && p.order_at(old_orders - 1)?.id >= first.id
    {
        return Err(invalid("segment order IDs overlap retired orders"));
    }
    let total_orders = old_orders
        .checked_add(orders.len() as u64)
        .ok_or_else(|| invalid("cold order count overflow"))?;
    let total_fills = old_fills
        .checked_add(fills.len() as u64)
        .ok_or_else(|| invalid("cold fill count overflow"))?;
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| invalid("cold index needs a UTF-8 file name"))?;
    let temporary = path.with_file_name(format!(".{name}.tmp"));
    let result = (|| -> io::Result<ColdRef> {
        fault_point("cold_write_before")?;
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&temporary)?;
        let mut out = BufWriter::with_capacity(1 << 16, file);
        let mut checksum = FNV_OFFSET;
        let mut emit = |bytes: &[u8]| -> io::Result<()> {
            checksum = fnv(checksum, bytes);
            out.write_all(bytes)
        };
        emit(MAGIC)?;
        emit(&total_orders.to_le_bytes())?;
        emit(&total_fills.to_le_bytes())?;
        let mut source = previous
            .map(|p| {
                p.file
                    .try_clone()
                    .map(|f| BufReader::with_capacity(1 << 16, f))
            })
            .transpose()?;
        if let Some(reader) = source.as_mut() {
            reader.seek(SeekFrom::Start(HEADER))?;
        }
        for _ in 0..old_orders {
            let mut bytes = [0_u8; 17];
            source.as_mut().unwrap().read_exact(&mut bytes)?;
            let record = RetiredOrder {
                id: ColdIndex::word(&bytes, 0),
                filled: ColdIndex::word(&bytes, 8) as i64,
                lifecycle: lifecycle_from(bytes[16])?,
            };
            emit(&record.id.to_le_bytes())?;
            emit(&record.filled.to_le_bytes())?;
            emit(&[lifecycle_code(record.lifecycle)?])?;
        }
        for record in orders {
            emit(&record.id.to_le_bytes())?;
            emit(&record.filled.to_le_bytes())?;
            emit(&[lifecycle_code(record.lifecycle)?])?;
        }
        let (mut old, mut new) = (0, 0);
        fault_point("cold_write_mid")?;
        if let Some(reader) = source.as_mut() {
            reader.seek(SeekFrom::Start(HEADER + old_orders * ORDER))?;
        }
        let mut next_old = || -> io::Result<Fill> {
            let mut bytes = [0_u8; 32];
            source.as_mut().unwrap().read_exact(&mut bytes)?;
            Ok(Fill {
                execution_id: ColdIndex::word(&bytes, 0),
                order_id: ColdIndex::word(&bytes, 8),
                qty: ColdIndex::word(&bytes, 16) as i64,
                price: ColdIndex::word(&bytes, 24) as i64,
            })
        };
        let mut old_record = if old_fills > 0 {
            Some(next_old()?)
        } else {
            None
        };
        while old_record.is_some() || new < fills.len() {
            let record = match (old_record.as_ref(), fills.get(new)) {
                (Some(a), Some(b)) if a.execution_id == b.execution_id => {
                    return Err(invalid("segment fill IDs overlap retired fills"));
                }
                (Some(a), Some(b)) if a.execution_id < b.execution_id => a.clone(),
                (Some(a), None) => a.clone(),
                (_, Some(b)) => {
                    new += 1;
                    b.clone()
                }
                (None, None) => unreachable!(),
            };
            if old_record
                .as_ref()
                .is_some_and(|r| r.execution_id == record.execution_id)
            {
                old += 1;
                old_record = if old < old_fills {
                    Some(next_old()?)
                } else {
                    None
                };
            }
            for value in [record.execution_id, record.order_id] {
                emit(&value.to_le_bytes())?;
            }
            for value in [record.qty, record.price] {
                emit(&value.to_le_bytes())?;
            }
        }
        out.write_all(&checksum.to_le_bytes())?;
        out.flush()?;
        out.get_ref().sync_all()?;
        fault_point("cold_fsync_after")?;
        fs::rename(&temporary, path)?;
        fault_point("cold_rename_after")?;
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        File::open(parent)?.sync_all()?;
        fault_point("cold_dirsync_after")?;
        Ok(ColdRef {
            file: name.to_string(),
            checksum,
            orders: total_orders,
            fills: total_fills,
        })
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

/// A verified cold index; lookups are binary searches with positioned reads.
pub struct ColdIndex {
    file: File,
    orders: u64,
    fills: u64,
    paged: Option<Box<crate::cold_pages::Index>>,
}

impl ColdIndex {
    /// Opens and fully verifies size, header and checksum against `reference`.
    pub fn open(directory: &Path, reference: &ColdRef) -> io::Result<Self> {
        Self::open_with(directory, reference, crate::cold_pages::Verification::Full)
    }

    pub fn open_with(
        directory: &Path,
        reference: &ColdRef,
        verification: crate::cold_pages::Verification,
    ) -> io::Result<Self> {
        if reference.file.contains('/') || reference.file.starts_with('.') {
            return Err(invalid(
                "cold index must be a plain file next to its journal",
            ));
        }
        let registration = crate::cold_gc::registration(directory)?;
        let mut file = File::open(directory.join(&reference.file))?;
        file.try_lock_shared().map_err(io::Error::other)?;
        drop(registration);
        let length = file.metadata()?.len();
        let mut magic = [0; 8];
        file.read_exact(&mut magic)?;
        file.seek(SeekFrom::Start(0))?;
        if &magic != MAGIC {
            let paged = crate::cold_pages::Index::open(directory, reference, verification)?;
            return Ok(Self {
                file,
                orders: reference.orders,
                fills: reference.fills,
                paged: Some(Box::new(paged)),
            });
        }
        if length != HEADER + ORDER * reference.orders + FILL * reference.fills + 8 {
            return Err(invalid("cold index size mismatch"));
        }
        let mut hash = FNV_OFFSET;
        let mut remaining = length - 8;
        let mut buffer = vec![0; 1 << 16];
        let mut header = Vec::new();
        while remaining > 0 {
            let take = remaining.min(buffer.len() as u64) as usize;
            file.read_exact(&mut buffer[..take])?;
            if header.len() < HEADER as usize {
                header.extend_from_slice(&buffer[..take.min(HEADER as usize - header.len())]);
            }
            hash = fnv(hash, &buffer[..take]);
            remaining -= take as u64;
        }
        let mut stored = [0; 8];
        file.read_exact(&mut stored)?;
        if hash != u64::from_le_bytes(stored) || hash != reference.checksum {
            return Err(invalid("cold index checksum mismatch"));
        }
        if &header[..8] != MAGIC
            || u64::from_le_bytes(header[8..16].try_into().unwrap()) != reference.orders
            || u64::from_le_bytes(header[16..24].try_into().unwrap()) != reference.fills
        {
            return Err(invalid("cold index header mismatch"));
        }
        Ok(Self {
            file,
            orders: reference.orders,
            fills: reference.fills,
            paged: None,
        })
    }

    fn read<const N: usize>(&self, offset: u64) -> io::Result<[u8; N]> {
        let mut bytes = [0; N];
        self.file.read_exact_at(&mut bytes, offset)?;
        Ok(bytes)
    }

    fn word(bytes: &[u8], at: usize) -> u64 {
        u64::from_le_bytes(bytes[at..at + 8].try_into().unwrap())
    }

    pub fn order(&self, id: OrderId) -> io::Result<Option<RetiredOrder>> {
        if let Some(index) = &self.paged {
            return index.order(id);
        }
        let (mut low, mut high) = (0, self.orders);
        while low < high {
            let middle = (low + high) / 2;
            let record: [u8; 17] = self.read(HEADER + middle * ORDER)?;
            let found = Self::word(&record, 0);
            match found.cmp(&id) {
                std::cmp::Ordering::Less => low = middle + 1,
                std::cmp::Ordering::Greater => high = middle,
                std::cmp::Ordering::Equal => {
                    return Ok(Some(RetiredOrder {
                        id,
                        filled: Self::word(&record, 8) as i64,
                        lifecycle: lifecycle_from(record[16])?,
                    }));
                }
            }
        }
        Ok(None)
    }

    pub fn fill(&self, execution_id: u64) -> io::Result<Option<Fill>> {
        if let Some(index) = &self.paged {
            return index.fill(execution_id);
        }
        let base = HEADER + ORDER * self.orders;
        let (mut low, mut high) = (0, self.fills);
        while low < high {
            let middle = (low + high) / 2;
            let record: [u8; 32] = self.read(base + middle * FILL)?;
            let found = Self::word(&record, 0);
            match found.cmp(&execution_id) {
                std::cmp::Ordering::Less => low = middle + 1,
                std::cmp::Ordering::Greater => high = middle,
                std::cmp::Ordering::Equal => {
                    return Ok(Some(Fill {
                        execution_id,
                        order_id: Self::word(&record, 8),
                        qty: Self::word(&record, 16) as i64,
                        price: Self::word(&record, 24) as i64,
                    }));
                }
            }
        }
        Ok(None)
    }

    pub(crate) fn order_at(&self, index: u64) -> io::Result<RetiredOrder> {
        if self.paged.is_some() || index >= self.orders {
            return Err(invalid("legacy row index unavailable"));
        }
        let record: [u8; 17] = self.read(HEADER + index * ORDER)?;
        Ok(RetiredOrder {
            id: Self::word(&record, 0),
            filled: Self::word(&record, 8) as i64,
            lifecycle: lifecycle_from(record[16])?,
        })
    }

    pub(crate) fn publication_parent(&self) -> Option<ColdRef> {
        self.paged.as_ref().and_then(|p| p.publication_parent())
    }

    pub(crate) fn is_paged(&self) -> bool {
        self.paged.is_some()
    }

    pub(crate) fn fill_at(&self, index: u64) -> io::Result<Fill> {
        if self.paged.is_some() || index >= self.fills {
            return Err(invalid("legacy row index unavailable"));
        }
        let record: [u8; 32] = self.read(HEADER + self.orders * ORDER + index * FILL)?;
        Ok(Fill {
            execution_id: Self::word(&record, 0),
            order_id: Self::word(&record, 8),
            qty: Self::word(&record, 16) as i64,
            price: Self::word(&record, 24) as i64,
        })
    }

    /// The verdict for a report about a retired order (ADR table, §3).
    pub fn verdict(&self, report: &Report) -> io::Result<RetiredVerdict> {
        use RetiredVerdict::{Contradiction, Duplicate};
        Ok(match report {
            Report::Fill(fill) => match self.fill(fill.execution_id)? {
                Some(known) if known == *fill => Duplicate,
                _ => Contradiction,
            },
            Report::Canceled {
                id,
                cumulative_filled,
            } => match self.order(*id)? {
                Some(order)
                    if order.filled == *cumulative_filled
                        && matches!(order.lifecycle, Lifecycle::Canceled | Lifecycle::Filled) =>
                {
                    Duplicate
                }
                _ => Contradiction,
            },
            Report::Rejected { id } => match self.order(*id)? {
                Some(order) if order.lifecycle == Lifecycle::Rejected => Duplicate,
                _ => Contradiction,
            },
            Report::Accepted { id } | Report::CancelRejected { id } => match self.order(*id)? {
                Some(order) if order.lifecycle != Lifecycle::Rejected => Duplicate,
                _ => Contradiction,
            },
            Report::Retired { .. } => Contradiction,
        })
    }
}

/// Remove only an explicitly identified superseded cold file, after publication.
/// A verified reader pins its file with a shared lock. Busy readers are skipped;
/// this does not sweep directories, delete journals or guess which file is live.
pub fn remove_superseded(
    directory: &Path,
    previous: &ColdRef,
    current: &ColdRef,
) -> io::Result<bool> {
    let existed = directory.join(&previous.file).exists();
    crate::cold_gc::register_and_sweep(directory, previous, current)?;
    Ok(existed && !directory.join(&previous.file).exists())
}
