//! Durable input-before-effect processing. Replay NEVER dispatches historical effects.
use crate::{
    core::{Carry, Core},
    model::*,
};
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
    Genesis {
        schema: u32,
        config: Config,
        /// Present when this journal continues a rotated predecessor (H4).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        carry: Option<Carried>,
    },
    Input(RecordedInput),
    /// Final record of a rotated journal; nothing may follow it.
    Closed {
        successor: String,
    },
}

#[derive(Serialize, Deserialize)]
struct Carried {
    // Nested, not flattened: serde's flatten buffer cannot hold i128 cash.
    carry: Carry,
    /// Chain link to the predecessor's last record, for audit tooling.
    predecessor_checksum: u64,
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

/// When the journal is forced to stable storage (see docs/acceptance.md, L3).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum SyncPolicy {
    /// `sync_all` after every input (the original contract).
    #[default]
    EveryInput,
    /// Every input is written before its effects are published, but `sync_all`
    /// runs only before an input whose effects leave the process (SendOrder,
    /// SendCancel). That sync also covers every earlier unsynced input, so any
    /// external action is preceded by its durable cause history. A process crash
    /// loses nothing (the OS still holds written pages); an OS/power failure may
    /// lose a suffix of inputs that produced no external action, which recovery's
    /// forced Disconnect and venue reconciliation re-establish. A torn middle
    /// frame still fails closed at recovery.
    Outbox,
}
impl std::str::FromStr for SyncPolicy {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        match s {
            "every" => Ok(Self::EveryInput),
            "outbox" => Ok(Self::Outbox),
            _ => Err("sync policy must be every or outbox".into()),
        }
    }
}

/// Wall time of each durable-path stage for one input (L2 profiling).
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct Stages {
    pub prepare_ns: u64,
    pub encode_ns: u64,
    pub write_ns: u64,
    pub sync_ns: u64,
    pub commit_ns: u64,
    pub synced: bool,
}

pub struct DurableEngine {
    core: Core,
    file: File,
    checksum: u64,
    poisoned: bool,
    journal_path: PathBuf,
    sync: SyncPolicy,
    unsynced: bool,
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
            sync: SyncPolicy::EveryInput,
            unsynced: false,
        };
        engine.append(&Payload::Genesis {
            schema: 1,
            config,
            carry: None,
        })?;
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
            sync: SyncPolicy::EveryInput,
            unsynced: false,
        };
        // A recovered local state is not proof of current venue state.
        engine.process(engine.core.now, Event::Disconnect)?;
        Ok(engine)
    }

    pub fn core(&self) -> &Core {
        &self.core
    }

    /// Record written order/fill IDs for compact responses (not journaled state).
    pub fn track_changes(&mut self) {
        self.core.track_changes();
    }

    pub fn take_changes(&mut self) -> Option<crate::core::Changes> {
        self.core.take_changes()
    }

    pub fn process(&mut self, at: Time, event: Event) -> io::Result<Vec<Effect>> {
        self.process_timed(at, event, None)
    }

    pub fn set_sync_policy(&mut self, policy: SyncPolicy) {
        self.sync = policy;
    }

    /// Force every written input to stable storage (for example at shutdown).
    pub fn sync(&mut self) -> io::Result<()> {
        sync_file(&self.file, &mut self.unsynced, &mut self.poisoned)
    }

    pub fn process_timed(
        &mut self,
        at: Time,
        event: Event,
        time: Option<EventTime>,
    ) -> io::Result<Vec<Effect>> {
        self.process_staged(at, event, time, None)
    }

    /// `process_timed` that also reports how long each stage took.
    pub fn process_profiled(
        &mut self,
        at: Time,
        event: Event,
        time: Option<EventTime>,
    ) -> io::Result<(Vec<Effect>, Stages)> {
        let mut stages = Stages::default();
        let effects = self.process_staged(at, event, time, Some(&mut stages))?;
        Ok((effects, stages))
    }

    fn process_staged(
        &mut self,
        at: Time,
        event: Event,
        time: Option<EventTime>,
        mut stages: Option<&mut Stages>,
    ) -> io::Result<Vec<Effect>> {
        let clock = stages.as_ref().map(|_| std::time::Instant::now());
        let lap = |field: fn(&mut Stages) -> &mut u64, stages: &mut Option<&mut Stages>| {
            if let (Some(stages), Some(clock)) = (stages.as_deref_mut(), clock) {
                let total = clock.elapsed().as_nanos() as u64;
                let spent: u64 = stages.prepare_ns
                    + stages.encode_ns
                    + stages.write_ns
                    + stages.sync_ns
                    + stages.commit_ns;
                *field(stages) = total - spent;
            }
        };
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
        lap(|s| &mut s.prepare_ns, &mut stages);
        let sync = self.sync == SyncPolicy::EveryInput
            || prepared
                .effects()
                .iter()
                .any(|effect| matches!(effect, Effect::SendOrder(_) | Effect::SendCancel { .. }));
        // The Core is still unchanged. The token holds its exclusive borrow while
        // disjoint journal fields are written. On error, dropping it aborts safely.
        let (bytes, hash) = encode_frame(
            self.checksum,
            &Payload::Input(RecordedInput { input, time }),
        )?;
        lap(|s| &mut s.encode_ns, &mut stages);
        if let Err(error) = self.file.write_all(&bytes) {
            self.poisoned = true;
            return Err(error);
        }
        self.checksum = hash;
        self.unsynced = true;
        lap(|s| &mut s.write_ns, &mut stages);
        if sync {
            sync_file(&self.file, &mut self.unsynced, &mut self.poisoned)?;
        }
        lap(|s| &mut s.sync_ns, &mut stages);
        // No published state or effect precedes the durable write acknowledgment
        // (under Outbox: no external effect precedes it).
        let effects = prepared.commit();
        lap(|s| &mut s.commit_ns, &mut stages);
        if let Some(stages) = stages {
            stages.synced = sync;
        }
        Ok(effects)
    }

    fn append(&mut self, payload: &Payload) -> io::Result<()> {
        append_frame(
            &mut self.file,
            &mut self.checksum,
            &mut self.poisoned,
            payload,
        )
    }

    /// Syncs the journal first: a snapshot must never be ahead of durable input.
    pub fn snapshot(&mut self, path: &Path) -> io::Result<()> {
        if path.exists() && fs::canonicalize(path)? == self.journal_path {
            return Err(invalid("snapshot cannot overwrite its journal"));
        }
        if self.poisoned {
            return Err(io::Error::other("cannot snapshot failed journal"));
        }
        self.sync()?;
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

// Disjoint field borrows: callable while a Prepared token borrows the Core.
fn sync_file(file: &File, unsynced: &mut bool, poisoned: &mut bool) -> io::Result<()> {
    if *unsynced {
        if let Err(error) = file.sync_all() {
            *poisoned = true;
            return Err(error);
        }
        *unsynced = false;
    }
    Ok(())
}

/// One checksummed journal line and its chained hash.
fn encode_frame(previous: u64, payload: &Payload) -> io::Result<(Vec<u8>, u64)> {
    let payload = serde_json::to_string(payload).map_err(invalid)?;
    let hash = checksum(previous, payload.as_bytes());
    let frame = Frame {
        previous,
        checksum: hash,
        payload,
    };
    let mut bytes = serde_json::to_vec(&frame).map_err(invalid)?;
    bytes.push(b'\n');
    Ok((bytes, hash))
}

fn append_frame(
    file: &mut File,
    previous: &mut u64,
    poisoned: &mut bool,
    payload: &Payload,
) -> io::Result<()> {
    let (bytes, hash) = encode_frame(*previous, payload)?;
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

fn genesis(config: Config, carry: Option<Carried>) -> io::Result<Core> {
    match carry {
        None => Core::new(config),
        Some(carried) => Core::from_carry(config, &carried.carry),
    }
    .map_err(invalid)
}

/// Replays a journal; `Scanned::closed` names the successor of a rotated one.
struct Scanned {
    core: Core,
    checksum: u64,
    closed: Option<String>,
}

fn scan(bytes: &[u8], snapshot: Option<&Snapshot>) -> io::Result<(Core, u64)> {
    let scanned = scan_all(bytes, snapshot)?;
    if let Some(successor) = scanned.closed {
        return Err(invalid(format!(
            "journal was rotated; continue from successor {successor}"
        )));
    }
    Ok((scanned.core, scanned.checksum))
}

fn scan_all(bytes: &[u8], snapshot: Option<&Snapshot>) -> io::Result<Scanned> {
    let mut snapshot_matched = snapshot.is_none();
    let mut previous = 0;
    let mut core = None;
    let mut closed = None;
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
            (
                0,
                Payload::Genesis {
                    schema: 1,
                    config,
                    carry,
                },
            ) => core = Some(genesis(config, carry)?),
            (_, Payload::Closed { successor }) => closed = Some(successor),
            (_, Payload::Input(_)) if closed.is_some() => {
                return Err(invalid("input after journal was closed by rotation"));
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
    Ok(Scanned {
        core,
        checksum: previous,
        closed,
    })
}

/// Offline H4 rotation: archive `old` and start `new` from its carried account
/// state. Requires no other writer, a complete journal, and a healthy book with
/// no open orders (for example after a clean shutdown reconciliation). The old
/// journal is closed first (naming its successor); if the process stops before
/// `new` exists, running the same rotation again completes it.
pub fn rotate(old: &Path, new: &Path) -> io::Result<Core> {
    let mut file = OpenOptions::new().read(true).write(true).open(old)?;
    file.try_lock().map_err(io::Error::other)?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    if !bytes.ends_with(b"\n") {
        return Err(invalid("incomplete journal; recover before rotating"));
    }
    let scanned = scan_all(&bytes, None)?;
    let successor = new.display().to_string();
    match &scanned.closed {
        Some(existing) if *existing != successor => {
            return Err(invalid(format!("journal already rotated to {existing}")));
        }
        Some(_) if new.exists() => return Err(invalid("rotation already completed")),
        Some(_) => {}
        None => {
            scanned.core.rotation_ready().map_err(invalid)?;
            let mut checksum = scanned.checksum;
            let mut poisoned = false;
            file.seek(SeekFrom::End(0))?;
            append_frame(
                &mut file,
                &mut checksum,
                &mut poisoned,
                &Payload::Closed {
                    successor: successor.clone(),
                },
            )?;
        }
    }
    let config = scanned.core.config.clone();
    let carry = scanned.core.carry();
    let core = Core::from_carry(config.clone(), &carry).map_err(invalid)?;
    let mut next = OpenOptions::new().write(true).create_new(true).open(new)?;
    let (mut checksum, mut poisoned) = (0, false);
    append_frame(
        &mut next,
        &mut checksum,
        &mut poisoned,
        &Payload::Genesis {
            schema: 1,
            config,
            carry: Some(Carried {
                carry,
                predecessor_checksum: scanned.checksum,
            }),
        },
    )?;
    sync_parent(new)?;
    Ok(core)
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
    // A rotated (closed) journal is an archive: its final state is still valid.
    Ok(scan_all(&bytes, None)?.core)
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
                (
                    0,
                    Payload::Genesis {
                        schema: 1,
                        config,
                        carry,
                    },
                ) => {
                    self.core = Some(genesis(config, carry)?);
                    observe(self.core.as_ref().unwrap(), None, &[], None);
                }
                // A rotated journal ends here; the successor is a separate session.
                (_, Payload::Closed { .. }) => {}
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
                    absent: vec![],
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
