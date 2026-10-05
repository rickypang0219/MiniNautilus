//! Best-effort cold-path output. Queue-full counts drops instead of blocking core.
//! Never use this channel for recovery records, fills, or risk state.
use std::{
    fs::OpenOptions,
    io::{self, Write},
    path::Path,
    sync::mpsc::{self, SyncSender, TrySendError},
    thread::{self, JoinHandle},
};

pub struct Telemetry {
    sender: SyncSender<String>,
    worker: JoinHandle<io::Result<()>>,
    pub dropped: u64,
}
impl Telemetry {
    pub fn start(path: &Path, capacity: usize) -> io::Result<Self> {
        let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
        let (sender, receiver) = mpsc::sync_channel::<String>(capacity);
        let worker = thread::spawn(move || {
            for line in receiver {
                writeln!(file, "{line}")?;
            }
            file.flush()
        });
        Ok(Self {
            sender,
            worker,
            dropped: 0,
        })
    }
    pub fn emit(&mut self, line: String) {
        if let Err(TrySendError::Full(_) | TrySendError::Disconnected(_)) =
            self.sender.try_send(line)
        {
            self.dropped += 1;
        }
    }
    pub fn finish(self) -> io::Result<u64> {
        drop(self.sender);
        self.worker
            .join()
            .map_err(|_| io::Error::other("telemetry worker panicked"))??;
        Ok(self.dropped)
    }
}
