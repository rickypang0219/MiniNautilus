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
    io::{self, Read, Write},
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

/// Writes `path` atomically (temporary file, fsync, rename, directory fsync).
/// Inputs must be strictly increasing by key; identical inputs give identical bytes.
pub fn write(path: &Path, orders: &[RetiredOrder], fills: &[Fill]) -> io::Result<ColdRef> {
    if orders.windows(2).any(|w| w[0].id >= w[1].id)
        || fills
            .windows(2)
            .any(|w| w[0].execution_id >= w[1].execution_id)
    {
        return Err(invalid("cold records must be unique and sorted"));
    }
    let mut bytes = Vec::with_capacity(
        (HEADER + ORDER * orders.len() as u64 + FILL * fills.len() as u64 + 8) as usize,
    );
    bytes.extend_from_slice(MAGIC);
    bytes.extend_from_slice(&(orders.len() as u64).to_le_bytes());
    bytes.extend_from_slice(&(fills.len() as u64).to_le_bytes());
    for order in orders {
        bytes.extend_from_slice(&order.id.to_le_bytes());
        bytes.extend_from_slice(&order.filled.to_le_bytes());
        bytes.push(lifecycle_code(order.lifecycle)?);
    }
    for fill in fills {
        for value in [fill.execution_id, fill.order_id] {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        for value in [fill.qty, fill.price] {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
    }
    let checksum = fnv(FNV_OFFSET, &bytes);
    bytes.extend_from_slice(&checksum.to_le_bytes());
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| invalid("cold index needs a UTF-8 file name"))?;
    let temporary = path.with_file_name(format!(".{name}.tmp"));
    {
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
    }
    fs::rename(&temporary, path)?;
    if let Some(parent) = path.parent() {
        let parent = if parent.as_os_str().is_empty() {
            Path::new(".")
        } else {
            parent
        };
        File::open(parent)?.sync_all()?;
    }
    Ok(ColdRef {
        file: name.to_string(),
        checksum,
        orders: orders.len() as u64,
        fills: fills.len() as u64,
    })
}

/// A verified cold index; lookups are binary searches with positioned reads.
pub struct ColdIndex {
    file: File,
    orders: u64,
    fills: u64,
}

impl ColdIndex {
    /// Opens and fully verifies size, header and checksum against `reference`.
    pub fn open(directory: &Path, reference: &ColdRef) -> io::Result<Self> {
        if reference.file.contains('/') || reference.file.starts_with('.') {
            return Err(invalid(
                "cold index must be a plain file next to its journal",
            ));
        }
        let mut file = File::open(directory.join(&reference.file))?;
        let length = file.metadata()?.len();
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

    /// Every record, in key order (used when the next rotation extends the index).
    pub fn records(&self) -> io::Result<(Vec<RetiredOrder>, Vec<Fill>)> {
        let mut orders = Vec::with_capacity(self.orders as usize);
        for index in 0..self.orders {
            let record: [u8; 17] = self.read(HEADER + index * ORDER)?;
            orders.push(RetiredOrder {
                id: Self::word(&record, 0),
                filled: Self::word(&record, 8) as i64,
                lifecycle: lifecycle_from(record[16])?,
            });
        }
        let base = HEADER + ORDER * self.orders;
        let mut fills = Vec::with_capacity(self.fills as usize);
        for index in 0..self.fills {
            let record: [u8; 32] = self.read(base + index * FILL)?;
            fills.push(Fill {
                execution_id: Self::word(&record, 0),
                order_id: Self::word(&record, 8),
                qty: Self::word(&record, 16) as i64,
                price: Self::word(&record, 24) as i64,
            });
        }
        Ok((orders, fills))
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
