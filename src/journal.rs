//! Durable input-before-effect processing. Replay NEVER dispatches historical effects.
use crate::{core::Core, model::*};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

fn invalid(reason: impl ToString) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, reason.to_string())
}

// Corruption detection, not authentication. Chaining also detects deleted/reordered records.
fn checksum(previous: u64, bytes: &[u8]) -> u64 {
    previous
        .to_le_bytes()
        .iter()
        .chain(bytes)
        .fold(0xcbf29ce484222325, |h, b| {
            (h ^ *b as u64).wrapping_mul(0x100000001b3)
        })
}

#[derive(Serialize, Deserialize)]
enum Payload {
    Genesis { schema: u32, config: Config },
    Input(RecordedInput),
}
/// Display/audit timestamps. They never drive Core ordering, expiry, or risk.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventTime {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_time_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub received_time_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub fill_event_times: std::collections::BTreeMap<u64, u64>,
}
impl EventTime {
    fn validate(&self) -> io::Result<()> {
        // JavaScript Date range; deliberately no monotonicity requirement.
        if self
            .event_time_ms
            .into_iter()
            .chain(self.received_time_ms)
            .chain(self.fill_event_times.values().copied())
            .any(|t| t > 8_640_000_000_000_000)
            || self.source.as_ref().is_some_and(|s| s.len() > 80)
        {
            return Err(invalid("invalid display timestamp metadata"));
        }
        Ok(())
    }
}
#[derive(Serialize, Deserialize)]
struct RecordedInput {
    #[serde(flatten)]
    input: Envelope,
    // Optional extension inside the checksummed payload; old journals still replay.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    time: Option<EventTime>,
}
#[derive(Serialize, Deserialize)]
struct Frame {
    previous: u64,
    checksum: u64,
    payload: String,
}
#[derive(Serialize, Deserialize)]
struct Snapshot {
    schema: u32,
    checksum: u64,
    core: Core,
}

pub struct DurableEngine {
    core: Core,
    file: File,
    checksum: u64,
    poisoned: bool,
    journal_path: PathBuf,
}

impl DurableEngine {
    pub fn create(path: &Path, config: Config) -> io::Result<Self> {
        let core = Core::new(config.clone()).map_err(invalid)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)?;
        file.try_lock().map_err(io::Error::other)?;
        let mut engine = Self {
            core,
            file,
            checksum: 0,
            poisoned: false,
            journal_path: fs::canonicalize(path)?,
        };
        engine.append(&Payload::Genesis { schema: 1, config })?;
        sync_parent(path)?;
        Ok(engine)
    }

    /// Repairs only a final incomplete line. Complete corrupt records fail closed.
    /// Snapshot is checked against its actual journal prefix; v0 prioritizes validation
    /// over fast startup and still scans the full journal.
    pub fn recover(path: &Path, snapshot_path: Option<&Path>) -> io::Result<Self> {
        let mut file = OpenOptions::new().read(true).write(true).open(path)?;
        file.try_lock().map_err(io::Error::other)?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        let end = bytes.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
        let snapshot: Option<Snapshot> = snapshot_path
            .map(|p| -> io::Result<Snapshot> {
                serde_json::from_slice(&fs::read(p)?).map_err(invalid)
            })
            .transpose()?;
        let (core, previous) = scan(&bytes[..end], snapshot.as_ref())?;
        // Only modify the file after all complete records and snapshot validate.
        if end != bytes.len() {
            file.set_len(end as u64)?;
            file.sync_all()?;
        }
        file.seek(SeekFrom::End(0))?;
        let mut engine = Self {
            core,
            file,
            checksum: previous,
            poisoned: false,
            journal_path: fs::canonicalize(path)?,
        };
        // A recovered local state is not proof of current venue state.
        engine.process(engine.core.now, Event::Disconnect)?;
        Ok(engine)
    }

    pub fn core(&self) -> &Core {
        &self.core
    }

    pub fn process(&mut self, at: Time, event: Event) -> io::Result<Vec<Effect>> {
        self.process_timed(at, event, None)
    }

    pub fn process_timed(
        &mut self,
        at: Time,
        event: Event,
        time: Option<EventTime>,
    ) -> io::Result<Vec<Effect>> {
        if let Some(t) = &time {
            t.validate()?;
        }
        if self.poisoned {
            return Err(io::Error::other("journal failed; restart and reconcile"));
        }
        let input = Envelope {
            seq: self
                .core
                .seq
                .checked_add(1)
                .ok_or_else(|| invalid("sequence exhausted"))?,
            at,
            event,
        };
        let prepared = self.core.prepare(&input).map_err(invalid)?;
        // The Core is still unchanged. The token holds its exclusive borrow while
        // disjoint journal fields are written. On error, dropping it aborts safely.
        append_frame(
            &mut self.file,
            &mut self.checksum,
            &mut self.poisoned,
            &Payload::Input(RecordedInput { input, time }),
        )?;
        // No published state or effect precedes the durable write acknowledgment.
        Ok(prepared.commit())
    }

    fn append(&mut self, payload: &Payload) -> io::Result<()> {
        append_frame(
            &mut self.file,
            &mut self.checksum,
            &mut self.poisoned,
            payload,
        )
    }

    pub fn snapshot(&self, path: &Path) -> io::Result<()> {
        if path.exists() && fs::canonicalize(path)? == self.journal_path {
            return Err(invalid("snapshot cannot overwrite its journal"));
        }
        if self.poisoned {
            return Err(io::Error::other("cannot snapshot failed journal"));
        }
        write_snapshot(
            path,
            &Snapshot {
                schema: 1,
                checksum: self.checksum,
                core: self.core.clone(),
            },
        )
    }
}

// Separate field borrows keep Core exclusively reserved by Prepared until commit.
fn append_frame(
    file: &mut File,
    previous: &mut u64,
    poisoned: &mut bool,
    payload: &Payload,
) -> io::Result<()> {
    let payload = serde_json::to_string(payload).map_err(invalid)?;
    let hash = checksum(*previous, payload.as_bytes());
    let frame = Frame {
        previous: *previous,
        checksum: hash,
        payload,
    };
    let mut bytes = serde_json::to_vec(&frame).map_err(invalid)?;
    bytes.push(b'\n');
    if let Err(error) = file.write_all(&bytes).and_then(|()| file.sync_all()) {
        *poisoned = true;
        return Err(error);
    }
    *previous = hash;
    Ok(())
}

fn write_snapshot(path: &Path, snapshot: &Snapshot) -> io::Result<()> {
    let data = serde_json::to_vec(snapshot).map_err(invalid)?;
    let tmp = path.with_extension(format!("{}.tmp", std::process::id()));
    let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
    let result = (|| {
        file.write_all(&data)?;
        file.sync_all()?;
        fs::rename(&tmp, path)?;
        sync_parent(path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(tmp);
    }
    result
}

fn sync_parent(path: &Path) -> io::Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    File::open(parent)?.sync_all()
}

fn scan(bytes: &[u8], snapshot: Option<&Snapshot>) -> io::Result<(Core, u64)> {
    let mut snapshot_matched = snapshot.is_none();
    let mut previous = 0;
    let mut core = None;
    for (index, line) in bytes
        .split(|b| *b == b'\n')
        .filter(|s| !s.is_empty())
        .enumerate()
    {
        let frame: Frame = serde_json::from_slice(line).map_err(invalid)?;
        if frame.previous != previous
            || checksum(previous, frame.payload.as_bytes()) != frame.checksum
        {
            return Err(invalid("journal checksum chain mismatch"));
        }
        let payload: Payload = serde_json::from_str(&frame.payload).map_err(invalid)?;
        match (index, payload) {
            (0, Payload::Genesis { schema: 1, config }) => {
                core = Some(Core::new(config).map_err(invalid)?)
            }
            (_, Payload::Input(event)) => {
                if let Some(time) = &event.time {
                    time.validate()?;
                }
                core.as_mut()
                    .ok_or_else(|| invalid("missing genesis"))?
                    .apply(&event.input)
                    .map_err(invalid)?;
            }
            _ => return Err(invalid("unsupported schema or duplicate genesis")),
        }
        previous = frame.checksum;
        if let (Some(saved), Some(current)) = (snapshot, &core)
            && saved.core.seq == current.seq
        {
            if saved.schema != 1 || saved.checksum != previous || saved.core != *current {
                return Err(invalid("snapshot disagrees with journal prefix"));
            }
            // Same state; subsequent records replay over the validated snapshot.
            core = Some(saved.core.clone());
            snapshot_matched = true;
        }
    }
    if !snapshot_matched {
        return Err(invalid("snapshot is ahead of journal"));
    }
    let core = core.ok_or_else(|| invalid("empty or torn genesis"))?;
    Ok((core, previous))
}

/// Read-only validated replay. Refuses an active writer or an incomplete tail.
/// No transport effects are returned, and no recovery event is appended.
pub fn replay(path: &Path) -> io::Result<Core> {
    let mut file = File::open(path)?;
    file.try_lock_shared().map_err(io::Error::other)?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    if !bytes.ends_with(b"\n") {
        return Err(invalid("incomplete journal; recover before analysis"));
    }
    Ok(scan(&bytes, None)?.0)
}

/// Create an offline checkpoint without appending to or changing the source journal.
pub fn checkpoint(journal: &Path, output: &Path) -> io::Result<u64> {
    if journal == output
        || output.exists() && fs::canonicalize(journal)? == fs::canonicalize(output)?
    {
        return Err(invalid("snapshot cannot overwrite its journal"));
    }
    let mut file = File::open(journal)?;
    file.try_lock_shared().map_err(io::Error::other)?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    if !bytes.ends_with(b"\n") {
        return Err(invalid("incomplete journal; recover first"));
    }
    let (core, checksum) = scan(&bytes, None)?;
    let seq = core.seq;
    write_snapshot(
        output,
        &Snapshot {
            schema: 1,
            checksum,
            core,
        },
    )?;
    Ok(seq)
}

/// Independent observer of complete journal frames. Never locks or mutates a writer's
/// file, repairs tails, or dispatches effects. Partial final frames wait for more bytes.
pub struct JournalFollower {
    reader: io::BufReader<File>,
    path: PathBuf,
    pending: Vec<u8>,
    previous: u64,
    consumed: u64,
    records: u64,
    core: Option<Core>,
    #[cfg(unix)]
    identity: (u64, u64),
}
impl JournalFollower {
    pub fn open(path: &Path) -> io::Result<Self> {
        let file = File::open(path)?;
        #[cfg(unix)]
        let identity = {
            use std::os::unix::fs::MetadataExt;
            let m = file.metadata()?;
            (m.dev(), m.ino())
        };
        Ok(Self {
            reader: io::BufReader::new(file),
            path: path.to_path_buf(),
            pending: Vec::new(),
            previous: 0,
            consumed: 0,
            records: 0,
            core: None,
            #[cfg(unix)]
            identity,
        })
    }
    pub fn pending_tail(&self) -> bool {
        !self.pending.is_empty()
    }
    pub fn core(&self) -> Option<&Core> {
        self.core.as_ref()
    }
    pub fn poll(
        &mut self,
        max_records: usize,
        mut observe: impl FnMut(&Core, Option<&Envelope>, &[Effect]),
    ) -> io::Result<usize> {
        self.poll_timed(max_records, |c, i, e, _| observe(c, i, e))
    }
    pub fn poll_timed(
        &mut self,
        max_records: usize,
        mut observe: impl FnMut(&Core, Option<&Envelope>, &[Effect], Option<&EventTime>),
    ) -> io::Result<usize> {
        use io::BufRead;
        let metadata = fs::metadata(&self.path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if (metadata.dev(), metadata.ino()) != self.identity {
                return Err(invalid("journal was replaced; restart dashboard to reload"));
            }
        }
        if metadata.len() < self.consumed {
            return Err(invalid(
                "journal was truncated; restart dashboard to reload",
            ));
        }
        let mut count = 0;
        while count < max_records {
            // Limit frame memory, including a malicious/torn line without a newline.
            let available = self.reader.fill_buf()?;
            if available.is_empty() {
                break;
            }
            let n = available
                .iter()
                .position(|b| *b == b'\n')
                .map_or(available.len(), |i| i + 1);
            if self.pending.len() + n > 8 * 1024 * 1024 {
                return Err(invalid("journal frame exceeds observer limit"));
            }
            self.pending.extend_from_slice(&available[..n]);
            self.reader.consume(n);
            self.consumed += n as u64;
            if self.pending.last() != Some(&b'\n') {
                continue;
            }
            let frame: Frame = serde_json::from_slice(&self.pending).map_err(invalid)?;
            if frame.previous != self.previous
                || checksum(self.previous, frame.payload.as_bytes()) != frame.checksum
            {
                return Err(invalid(
                    "journal checksum chain mismatch; showing last validated state",
                ));
            }
            let payload: Payload = serde_json::from_str(&frame.payload).map_err(invalid)?;
            match (self.records, payload) {
                (0, Payload::Genesis { schema: 1, config }) => {
                    self.core = Some(Core::new(config).map_err(invalid)?);
                    observe(self.core.as_ref().unwrap(), None, &[], None);
                }
                (_, Payload::Input(input)) => {
                    let core = self
                        .core
                        .as_mut()
                        .ok_or_else(|| invalid("missing genesis"))?;
                    if let Some(t) = &input.time {
                        t.validate()?;
                    }
                    let effects = core.apply(&input.input).map_err(invalid)?;
                    observe(core, Some(&input.input), &effects, input.time.as_ref());
                }
                _ => return Err(invalid("unsupported schema or duplicate genesis")),
            }
            self.previous = frame.checksum;
            self.records += 1;
            self.pending.clear();
            count += 1;
        }
        Ok(count)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_journal_discards_market_fill_cancel_and_rebuild_transitions() {
        for case in 0..6 {
            let path = std::env::temp_dir().join(format!(
                "mini-prepared-failure-{}-{case}",
                std::process::id()
            ));
            let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
            engine
                .process(0, Event::Quote { bid: 99, ask: 101 })
                .unwrap();
            let intent = Intent {
                id: 1,
                side: Side::Buy,
                qty: 3,
                limit: 100,
                based_on_seq: 1,
                valid_until: 100,
            };
            engine.process(0, Event::Submit(intent.clone())).unwrap();
            engine
                .process(
                    0,
                    Event::Execution {
                        epoch: 0,
                        venue_seq: 1,
                        report: Report::Accepted { id: 1 },
                    },
                )
                .unwrap();
            if case == 5 {
                engine.process(0, Event::Reconnect).unwrap();
            }
            let event = match case {
                0 => Event::Quote { bid: 102, ask: 103 },
                1 => Event::Execution {
                    epoch: 0,
                    venue_seq: 2,
                    report: Report::Fill(Fill {
                        execution_id: 1,
                        order_id: 1,
                        qty: 1,
                        price: 100,
                    }),
                },
                2 => Event::Cancel { id: 1 },
                3 => Event::Quote { bid: 0, ask: 1 }, // would gate only after commit
                4 => Event::Disconnect,
                _ => Event::Reconcile(Reconciliation {
                    epoch: 1,
                    watermark: 1,
                    orders: vec![VenueOrder {
                        intent,
                        filled: 0,
                        lifecycle: Lifecycle::Accepted,
                    }],
                    fills: vec![],
                    position: 0,
                }),
            };
            let original = engine.core.clone();
            let checksum = engine.checksum;
            let bytes = fs::read(&path).unwrap();
            engine.file = File::open(&path).unwrap();
            assert!(engine.process(1, event).is_err());
            assert_eq!(engine.core, original, "case {case}");
            assert_eq!(engine.checksum, checksum);
            assert_eq!(fs::read(&path).unwrap(), bytes);
            assert!(engine.poisoned);
            drop(engine);
            fs::remove_file(path).unwrap();
        }
    }

    #[test]
    fn persistence_error_exposes_no_effects_and_poison_is_sticky() {
        let path =
            std::env::temp_dir().join(format!("mini-journal-failure-{}", std::process::id()));
        let mut engine = DurableEngine::create(&path, Config::default()).unwrap();
        engine
            .process(0, Event::Quote { bid: 99, ask: 101 })
            .unwrap();
        let original = engine.core.clone();
        // Inject a file descriptor that cannot be written; no special devices needed.
        engine.file = File::open(&path).unwrap();
        let event = Event::Submit(Intent {
            id: 1,
            side: Side::Buy,
            qty: 1,
            limit: 100,
            based_on_seq: 1,
            valid_until: 100,
        });
        assert!(engine.process(1, event).is_err());
        assert_eq!(engine.core, original);
        assert!(engine.poisoned);
        assert!(engine.process(1, Event::Tick).is_err());
        drop(engine);
        fs::remove_file(path).unwrap();
    }
}
