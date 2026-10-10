//! Opportunistic sync only. Foreground effects still require their own sync.
use std::{
    fs::File,
    io,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

#[derive(Default)]
struct Progress {
    written: AtomicU64,
    durable: AtomicU64,
    failed: AtomicBool,
    stop: AtomicBool,
}

pub(super) struct Flusher {
    progress: Arc<Progress>,
    wake: mpsc::SyncSender<()>,
    worker: Option<thread::JoinHandle<()>>,
    threshold: u64,
}

impl Flusher {
    pub(super) fn start(file: File, interval: Duration, threshold: u64) -> io::Result<Self> {
        Self::with_sync(move || file.sync_all(), interval, threshold)
    }

    fn with_sync(
        mut sync: impl FnMut() -> io::Result<()> + Send + 'static,
        interval: Duration,
        threshold: u64,
    ) -> io::Result<Self> {
        let progress = Arc::new(Progress::default());
        let p = progress.clone();
        let (wake, rx) = mpsc::sync_channel(1);
        let worker = thread::Builder::new()
            .name("journal-sync".into())
            .spawn(move || {
                loop {
                    let _ = rx.recv_timeout(interval);
                    if p.stop.load(Ordering::Acquire) {
                        break;
                    }
                    // Only acknowledge the prefix observed BEFORE entering sync_all.
                    let target = p.written.load(Ordering::Acquire);
                    if target > p.durable.load(Ordering::Acquire) {
                        if sync().is_err() {
                            p.failed.store(true, Ordering::Release);
                            break;
                        }
                        p.durable.fetch_max(target, Ordering::AcqRel);
                    }
                }
            })?;
        Ok(Self {
            progress,
            wake,
            worker: Some(worker),
            threshold,
        })
    }

    pub(super) fn check(&self) -> io::Result<()> {
        if self.progress.failed.load(Ordering::Acquire) {
            Err(io::Error::other(
                "background journal sync failed; restart and reconcile",
            ))
        } else {
            Ok(())
        }
    }

    pub(super) fn written(&self, bytes: u64) -> u64 {
        self.progress.written.store(bytes, Ordering::Release);
        let pending = bytes.saturating_sub(self.progress.durable.load(Ordering::Acquire));
        if pending >= self.threshold {
            let _ = self.wake.try_send(());
        }
        pending
    }

    pub(super) fn acknowledge(&self, bytes: u64) {
        self.progress.durable.fetch_max(bytes, Ordering::AcqRel);
    }
}

impl Drop for Flusher {
    fn drop(&mut self) {
        self.progress.stop.store(true, Ordering::Release);
        let _ = self.wake.try_send(());
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sync_failure_is_sticky_and_never_acknowledged() {
        let flusher = Flusher::with_sync(
            || Err(io::Error::other("injected EIO")),
            Duration::from_millis(1),
            1,
        )
        .unwrap();
        flusher.written(99);
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while flusher.check().is_ok() && std::time::Instant::now() < deadline {
            thread::yield_now();
        }
        assert!(flusher.check().is_err());
        assert_eq!(flusher.progress.durable.load(Ordering::Acquire), 0);
        assert!(flusher.check().is_err());
    }
}
