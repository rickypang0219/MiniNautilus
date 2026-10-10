//! Prometheus text exposition for `serve`/`paper`/`sim` (`MINI_METRICS_ADDR`).
//!
//! Recording is a few relaxed atomic adds per input: no locks, no allocation and
//! no I/O on the event path. A separate thread answers scrapes; a slow or absent
//! scraper never blocks the engine. Values are process-lifetime counters, as
//! Prometheus expects; rates and quantiles are computed at query time.
use crate::{core::Core, journal::Stages, model::*};
use std::{
    fmt::Write as _,
    io::{BufRead, BufReader, Write},
    net::{SocketAddr, TcpListener},
    sync::{
        Arc,
        atomic::{AtomicI64, AtomicU64, Ordering::Relaxed},
    },
    time::Duration,
};

/// Upper bounds in seconds: 1 µs .. 1 s, roughly 1-2-5 per decade.
const BOUNDS: [f64; 19] = [
    1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 0.1,
    0.2, 0.5, 1.0,
];

pub struct Histogram {
    buckets: [AtomicU64; BOUNDS.len() + 1],
    sum_ns: AtomicU64,
}

impl Default for Histogram {
    fn default() -> Self {
        Self {
            buckets: std::array::from_fn(|_| AtomicU64::new(0)),
            sum_ns: AtomicU64::new(0),
        }
    }
}

impl Histogram {
    pub fn observe_ns(&self, ns: u64) {
        let seconds = ns as f64 * 1e-9;
        let slot = BOUNDS
            .iter()
            .position(|b| seconds <= *b)
            .unwrap_or(BOUNDS.len());
        self.buckets[slot].fetch_add(1, Relaxed);
        self.sum_ns.fetch_add(ns, Relaxed);
    }

    fn render(&self, out: &mut String, name: &str, help: &str, labels: &str) {
        let _ = writeln!(out, "# HELP {name} {help}\n# TYPE {name} histogram");
        let mut cumulative = 0;
        let sep = if labels.is_empty() { "" } else { "," };
        for (i, bound) in BOUNDS.iter().enumerate() {
            cumulative += self.buckets[i].load(Relaxed);
            let _ = writeln!(
                out,
                "{name}_bucket{{{labels}{sep}le=\"{bound}\"}} {cumulative}"
            );
        }
        cumulative += self.buckets[BOUNDS.len()].load(Relaxed);
        let _ = writeln!(
            out,
            "{name}_bucket{{{labels}{sep}le=\"+Inf\"}} {cumulative}"
        );
        let braces = if labels.is_empty() {
            String::new()
        } else {
            format!("{{{labels}}}")
        };
        let sum = self.sum_ns.load(Relaxed) as f64 * 1e-9;
        let _ = writeln!(
            out,
            "{name}_sum{braces} {sum}\n{name}_count{braces} {cumulative}"
        );
    }
}

const EVENTS: [&str; 15] = [
    "Quote",
    "QuoteObserved",
    "MarketUnavailable",
    "Trade",
    "Submit",
    "SetTarget",
    "SubmitTargeted",
    "Cancel",
    "Execution",
    "Tick",
    "Heartbeat",
    "Disconnect",
    "Reconnect",
    "Reconcile",
    "Kill",
];
const EFFECTS: [&str; 6] = [
    "SendOrder",
    "SendCancel",
    "QueryState",
    "Refused",
    "SignalRefused",
    "Alert",
];

pub fn event_kind(event: &Event) -> usize {
    match event {
        Event::Quote { .. } => 0,
        Event::QuoteObserved { .. } => 1,
        Event::MarketUnavailable => 2,
        Event::Trade { .. } => 3,
        Event::Submit(_) => 4,
        Event::SetTarget(_) => 5,
        Event::SubmitTargeted { .. } => 6,
        Event::Cancel { .. } => 7,
        Event::Execution { .. } => 8,
        Event::Tick => 9,
        Event::Heartbeat { .. } => 10,
        Event::Disconnect => 11,
        Event::Reconnect => 12,
        Event::Reconcile(_) => 13,
        Event::Kill => 14,
    }
}

fn effect_kind(effect: &Effect) -> usize {
    match effect {
        Effect::SendOrder(_) => 0,
        Effect::SendCancel { .. } => 1,
        Effect::QueryState { .. } => 2,
        Effect::Refused { .. } => 3,
        Effect::SignalRefused { .. } => 4,
        Effect::Alert(_) => 5,
    }
}

#[derive(Default)]
pub struct Metrics {
    events: [AtomicU64; EVENTS.len()],
    effects: [AtomicU64; EFFECTS.len()],
    /// Rust time for one JSON-lines request: parse, every input, response write.
    pub request: Histogram,
    pub response_encode: Histogram,
    pub response_bytes: AtomicU64,
    pub requests: AtomicU64,
    /// Per input (market data, strategy commands and venue reports alike).
    pub prepare: Histogram,
    pub journal_encode: Histogram,
    pub journal_write: Histogram,
    pub journal_sync: Histogram,
    pub commit: Histogram,
    pub syncs: AtomicU64,
    position: AtomicI64,
    health: AtomicI64,
    killed: AtomicI64,
    open_orders: AtomicI64,
    retained_orders: AtomicI64,
    retained_fills: AtomicI64,
    seq: AtomicI64,
}

impl Metrics {
    /// `kind` is `event_kind(&event)`, taken before the event moved into the Core.
    pub fn input(&self, kind: usize, effects: &[Effect], stages: &Stages) {
        self.events[kind].fetch_add(1, Relaxed);
        for effect in effects {
            self.effects[effect_kind(effect)].fetch_add(1, Relaxed);
        }
        self.prepare.observe_ns(stages.prepare_ns);
        self.journal_encode.observe_ns(stages.encode_ns);
        self.journal_write.observe_ns(stages.write_ns);
        self.commit.observe_ns(stages.commit_ns);
        if stages.synced {
            self.journal_sync.observe_ns(stages.sync_ns);
            self.syncs.fetch_add(1, Relaxed);
        }
    }

    /// Gauges are refreshed once per request (O(1): uses the open-order index).
    pub fn state(&self, core: &Core) {
        self.position.store(core.position, Relaxed);
        let health = match core.health {
            Health::Healthy => 0,
            Health::Disconnected => 1,
            Health::Reconciling => 2,
        };
        self.health.store(health, Relaxed);
        self.killed.store(i64::from(core.killed), Relaxed);
        self.open_orders
            .store(core.open_orders().count() as i64, Relaxed);
        self.retained_orders
            .store(core.orders.len() as i64, Relaxed);
        self.retained_fills.store(core.fills.len() as i64, Relaxed);
        self.seq.store(core.seq as i64, Relaxed);
    }

    pub fn render(&self) -> String {
        let mut out = String::with_capacity(16 * 1024);
        let _ = writeln!(
            out,
            "# HELP mini_events_total Inputs applied to the Core by event kind.\n# TYPE mini_events_total counter"
        );
        for (name, count) in EVENTS.iter().zip(&self.events) {
            let _ = writeln!(
                out,
                "mini_events_total{{kind=\"{name}\"}} {}",
                count.load(Relaxed)
            );
        }
        let _ = writeln!(
            out,
            "# HELP mini_effects_total Effects returned by the Core by kind.\n# TYPE mini_effects_total counter"
        );
        for (name, count) in EFFECTS.iter().zip(&self.effects) {
            let _ = writeln!(
                out,
                "mini_effects_total{{kind=\"{name}\"}} {}",
                count.load(Relaxed)
            );
        }
        for (name, help, value) in [
            (
                "mini_requests_total",
                "JSON-lines requests served.",
                &self.requests,
            ),
            (
                "mini_response_bytes_total",
                "Response bytes written.",
                &self.response_bytes,
            ),
            (
                "mini_journal_syncs_total",
                "sync_all calls on the journal.",
                &self.syncs,
            ),
        ] {
            let _ = writeln!(
                out,
                "# HELP {name} {help}\n# TYPE {name} counter\n{name} {}",
                value.load(Relaxed)
            );
        }
        for (name, help, value) in [
            ("mini_position_lots", "Engine position.", &self.position),
            (
                "mini_health",
                "0 healthy, 1 disconnected, 2 reconciling.",
                &self.health,
            ),
            ("mini_killed", "1 when the kill latch is set.", &self.killed),
            (
                "mini_open_orders",
                "Orders that may still fill or reserve risk.",
                &self.open_orders,
            ),
            (
                "mini_retained_orders",
                "Orders held for dedupe/reconciliation.",
                &self.retained_orders,
            ),
            (
                "mini_retained_fills",
                "Fills held for dedupe/reconciliation.",
                &self.retained_fills,
            ),
            ("mini_seq", "Last applied engine sequence.", &self.seq),
        ] {
            let _ = writeln!(
                out,
                "# HELP {name} {help}\n# TYPE {name} gauge\n{name} {}",
                value.load(Relaxed)
            );
        }
        self.request.render(
            &mut out,
            "mini_request_seconds",
            "Rust time per request: parse, inputs, response encode and write.",
            "",
        );
        self.response_encode.render(
            &mut out,
            "mini_response_encode_seconds",
            "Response serialization per request.",
            "",
        );
        for (stage, histogram) in [
            ("prepare", &self.prepare),
            ("journal_encode", &self.journal_encode),
            ("journal_write", &self.journal_write),
            ("journal_sync", &self.journal_sync),
            ("commit", &self.commit),
        ] {
            histogram.render(
                &mut out,
                "mini_input_stage_seconds",
                "Per-input stage time (journal_sync only for synced inputs).",
                &format!("stage=\"{stage}\""),
            );
        }
        out
    }
}

/// Serve `GET /metrics` on `address` from a background thread.
pub fn serve(address: &str) -> std::io::Result<(Arc<Metrics>, SocketAddr)> {
    let listener = TcpListener::bind(address)?;
    let bound = listener.local_addr()?;
    let metrics = Arc::new(Metrics::default());
    let shared = Arc::clone(&metrics);
    std::thread::Builder::new()
        .name("metrics".into())
        .spawn(move || {
            for stream in listener.incoming().flatten() {
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
                let mut reader = BufReader::new(&stream);
                let mut line = String::new();
                if reader.read_line(&mut line).is_err() {
                    continue;
                }
                // Drain headers; the request body is ignored.
                let mut header = String::new();
                while reader.read_line(&mut header).is_ok_and(|n| n > 2) {
                    header.clear();
                }
                let (status, body) = if line.starts_with("GET /metrics") {
                    ("200 OK", shared.render())
                } else {
                    ("404 Not Found", String::from("not found\n"))
                };
                let _ = write!(
                    &stream,
                    "HTTP/1.1 {status}\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        })?;
    Ok((metrics, bound))
}
