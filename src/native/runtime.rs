//! Native account writer. Network and strategy workers cannot mutate the Core.
use super::{
    Result, fail,
    signing::account_lock,
    strategy::{self, Answer, Request, Strategy},
    stream::{self, Observation},
    venue::{self, Http, Instrument, Session, Venue},
    wall_ms,
};
use crate::{
    core::Core,
    journal::{DurableEngine, EventTime, SyncPolicy},
    model::*,
};
use crossbeam_channel::{Receiver, Sender, bounded};
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

pub struct Circuit {
    pub permit: AtomicU8,
    pub failed: AtomicBool,
    source_generation: AtomicU64,
    reason: Mutex<Option<String>>,
}
impl Default for Circuit {
    fn default() -> Self {
        Self {
            permit: AtomicU8::new(0),
            failed: AtomicBool::new(false),
            source_generation: AtomicU64::new(0),
            reason: Mutex::new(None),
        }
    }
}
impl Circuit {
    pub fn halt(&self) {
        self.permit.store(3, Ordering::Release);
    }
    pub fn close(&self) {
        let _ = self
            .permit
            .compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
    pub fn allow(&self) {
        let _ = self
            .permit
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire);
    }
    pub fn trip(&self, reason: impl Into<String>) {
        self.permit.store(2, Ordering::Release);
        self.failed.store(true, Ordering::Release);
        if let Ok(mut r) = self.reason.lock()
            && r.is_none()
        {
            *r = Some(reason.into());
        }
    }
    fn pause_source(&self) -> u64 {
        let generation = self.source_generation.fetch_add(1, Ordering::AcqRel) + 1;
        for value in [0, 1] {
            if self
                .permit
                .compare_exchange(value, 4, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                break;
            }
        }
        generation
    }
    fn resume_source(&self, generation: u64) -> bool {
        if self.source_generation.load(Ordering::Acquire) != generation {
            return false;
        }
        let _ = self
            .permit
            .compare_exchange(4, 0, Ordering::AcqRel, Ordering::Acquire);
        if self.source_generation.load(Ordering::Acquire) != generation {
            self.close();
            return false;
        }
        true
    }
    pub fn reason(&self) -> Option<String> {
        self.reason.lock().ok().and_then(|r| r.clone())
    }
    pub fn publish<T>(&self, queue: &Sender<T>, message: T) -> Result<()> {
        queue.try_send(message).map_err(|_| {
            self.trip("bounded critical/input queue unavailable");
            fail("bounded critical/input queue unavailable")
        })
    }
}
#[derive(Clone)]
pub struct Telemetry {
    queue: Sender<Value>,
    pub dropped: Arc<AtomicU64>,
}
impl Telemetry {
    pub fn emit(&self, value: Value) {
        if self.queue.try_send(value).is_err() {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
    pub fn start(path: &Path) -> Result<(Self, thread::JoinHandle<Result<()>>)> {
        let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
        let (queue, receiver) = bounded(1024);
        let worker = thread::spawn(move || {
            for row in receiver {
                serde_json::to_writer(&mut file, &row)?;
                file.write_all(b"\n")?;
            }
            file.sync_all()?;
            Ok(())
        });
        Ok((
            Self {
                queue,
                dropped: Arc::new(AtomicU64::new(0)),
            },
            worker,
        ))
    }
}
#[derive(Debug)]
struct Market {
    generation: u64,
    bid: i64,
    ask: i64,
    observed_at: u64,
    exchange_time: u64,
    received: Instant,
}
enum Control {
    Ready {
        generation: u64,
    },
    Expired {
        generation: u64,
    },
    Wake,
    Done {
        epoch: u64,
        reports: Vec<Report>,
        heartbeat: bool,
    },
    Reconciled(Reconciliation),
    Target {
        request: Request,
        answer: Answer,
        received: Instant,
    },
}
enum Job {
    Submit {
        intent: Intent,
        epoch: u64,
        received: Instant,
    },
    Cancel {
        intent: Intent,
        epoch: u64,
    },
    Refresh {
        core: Box<Core>,
        reconcile: bool,
        audit: bool,
    },
}
fn execute(
    mut venue: Option<Venue>,
    jobs: Receiver<Job>,
    critical: Sender<Control>,
    circuit: Arc<Circuit>,
    telemetry: Telemetry,
) -> Option<Venue> {
    for job in jobs {
        let result: Result<Control> = (|| match job {
            Job::Submit {
                intent,
                epoch,
                received,
            } => {
                if circuit.permit.load(Ordering::Acquire) != 1 || wall_ms() >= intent.valid_until {
                    return Err(fail(
                        "stale queued action blocked; durable intent requires reconciliation",
                    ));
                }
                let started = Instant::now();
                let reports = if let Some(venue) = venue.as_mut() {
                    venue.submit_guarded(&intent, Some(&circuit.permit))?;
                    vec![Report::Accepted { id: intent.id }]
                } else {
                    vec![
                        Report::Accepted { id: intent.id },
                        Report::Fill(Fill {
                            execution_id: intent.id,
                            order_id: intent.id,
                            qty: intent.qty,
                            price: intent.limit,
                        }),
                    ]
                };
                telemetry.emit(json!({"kind":"execution_complete","id":intent.id,"local_receive_to_ack_ns":received.elapsed().as_nanos() as u64,"executor_ns":started.elapsed().as_nanos() as u64,"receive_to_transport_handoff_ns":if venue.is_none(){Some(started.duration_since(received).as_nanos() as u64)}else{None},"signed":venue.is_some(),"boundary":"HTTP response, includes remote wait; NOT socket handoff"}));
                Ok(Control::Done {
                    epoch,
                    reports,
                    heartbeat: false,
                })
            }
            Job::Cancel { intent, epoch } => {
                if let Some(venue) = venue.as_mut() {
                    venue.cancel(&intent)?;
                    Ok(Control::Done {
                        epoch,
                        reports: vec![],
                        heartbeat: false,
                    })
                } else {
                    Ok(Control::Done {
                        epoch,
                        reports: vec![Report::Canceled {
                            id: intent.id,
                            cumulative_filled: 0,
                        }],
                        heartbeat: false,
                    })
                }
            }
            Job::Refresh {
                core,
                reconcile,
                audit,
            } => {
                if let Some(venue) = venue.as_mut() {
                    if reconcile {
                        Ok(Control::Reconciled(venue.reconcile(&core)?))
                    } else {
                        let reports = venue.poll(&core)?;
                        if audit {
                            venue.heartbeat()?;
                        }
                        Ok(Control::Done {
                            epoch: core.epoch,
                            reports,
                            heartbeat: audit,
                        })
                    }
                } else if reconcile {
                    if core
                        .orders
                        .values()
                        .any(|o| !o.lifecycle.terminal() || o.uncertain)
                    {
                        return Err(fail("paper restart with unresolved simulated action"));
                    }
                    Ok(Control::Reconciled(Reconciliation {
                        epoch: core.epoch,
                        watermark: core.venue_seq,
                        orders: core
                            .orders
                            .values()
                            .map(|o| VenueOrder {
                                intent: o.intent.clone(),
                                filled: o.filled,
                                lifecycle: o.lifecycle,
                            })
                            .collect(),
                        fills: core.fills.values().cloned().collect(),
                        position: core.position,
                        absent: vec![],
                    }))
                } else {
                    Ok(Control::Done {
                        epoch: core.epoch,
                        reports: vec![],
                        heartbeat: true,
                    })
                }
            }
        })();
        match result {
            Ok(message) => {
                if circuit.publish(&critical, message).is_err() {
                    break;
                }
            }
            Err(error) => {
                circuit.trip(error.to_string());
                break;
            }
        }
    }
    venue
}
struct Writer {
    engine: DurableEngine,
    jobs: Sender<Job>,
    circuit: Arc<Circuit>,
    telemetry: Telemetry,
    busy: bool,
    reconcile: bool,
    stream_ready: bool,
    source_generation: u64,
    reconciled_generation: u64,
    reconnect_pending: bool,
    stale_market_discarded: u64,
    poll_requested: bool,
    target: Option<(i64, u64, Instant)>,
    processed: u64,
    market_count: u64,
    critical_count: u64,
}
impl Writer {
    fn process(&mut self, event: Event, received: Instant) -> Result<()> {
        self.process_with_time(event, received, None)
    }
    fn process_with_time(
        &mut self,
        event: Event,
        received: Instant,
        time: Option<EventTime>,
    ) -> Result<()> {
        let now = wall_ms().max(self.engine.core().now);
        let queue_ns = received.elapsed().as_nanos() as u64;
        let started = Instant::now();
        let kind = match &event {
            Event::QuoteObserved { .. } => "quote",
            Event::SubmitTargeted { .. } | Event::Submit(_) => "submit",
            Event::Execution { .. } => "execution",
            Event::Tick => "tick",
            Event::Reconcile(_) => "reconcile",
            _ => "control",
        };
        let (effects, s) = self.engine.process_profiled(now, event, time)?;
        self.processed += 1;
        self.telemetry.emit(json!({"kind":"writer","input":kind,"seq":self.engine.core().seq,"epoch":self.engine.core().epoch,"at_ms":now,"receive_to_writer_ns":queue_ns,"request_ns":started.elapsed().as_nanos() as u64,"prepare_ns":s.prepare_ns,"encode_ns":s.encode_ns,"write_ns":s.write_ns,"sync_ns":s.sync_ns,"commit_ns":s.commit_ns,"sync_bytes":s.sync_pending_bytes,"health":self.engine.core().health,"position":self.engine.core().position}));
        if self.engine.core().health != Health::Healthy {
            self.circuit.close();
        }
        for effect in effects {
            match effect {
                Effect::SendOrder(intent) => {
                    if self.busy {
                        return Err(fail("executor reservation violated"));
                    }
                    self.circuit.publish(
                        &self.jobs,
                        Job::Submit {
                            intent,
                            epoch: self.engine.core().epoch,
                            received,
                        },
                    )?;
                    self.busy = true;
                }
                Effect::SendCancel { id } => {
                    if self.busy {
                        return Err(fail("executor reservation violated"));
                    }
                    let intent = self.engine.core().orders[&id].intent.clone();
                    self.circuit.publish(
                        &self.jobs,
                        Job::Cancel {
                            intent,
                            epoch: self.engine.core().epoch,
                        },
                    )?;
                    self.busy = true;
                }
                Effect::QueryState { .. } => self.reconcile = true,
                Effect::Alert(reason) => {
                    self.telemetry.emit(json!({"kind":"alert","reason":reason}));
                    if self.engine.core().health != Health::Healthy {
                        self.circuit.trip("Core requested reconciliation");
                    }
                }
                Effect::Refused { id, reason } => self
                    .telemetry
                    .emit(json!({"kind":"refused","id":id,"reason":reason})),
                Effect::SignalRefused { revision, reason } => self
                    .telemetry
                    .emit(json!({"kind":"signal_refused","revision":revision,"reason":reason})),
            }
        }
        Ok(())
    }
    fn control(&mut self, message: Control) -> Result<()> {
        self.critical_count += 1;
        match message {
            Control::Ready { generation } => {
                if generation == self.source_generation {
                    self.stream_ready = true;
                    self.telemetry.emit(json!({"kind":"source_ready","generation":generation,"epoch":self.engine.core().epoch,"at_ms":wall_ms()}));
                }
            }
            Control::Expired { generation } => {
                if generation <= self.source_generation {
                    return Err(fail("non-increasing source generation"));
                }
                self.source_generation = generation;
                self.stream_ready = false;
                self.reconnect_pending = true;
                self.target = None;
                self.telemetry.emit(
                    json!({"kind":"source_expired","generation":generation,"at_ms":wall_ms()}),
                );
            }
            Control::Wake => self.poll_requested = true,
            Control::Done {
                epoch,
                reports,
                heartbeat,
            } => {
                self.busy = false;
                if epoch != self.engine.core().epoch {
                    return Err(fail("late executor result; reconciliation required"));
                }
                for report in reports {
                    self.process(
                        Event::Execution {
                            epoch,
                            venue_seq: self
                                .engine
                                .core()
                                .venue_seq
                                .checked_add(1)
                                .ok_or_else(|| fail("report sequence overflow"))?,
                            report,
                        },
                        Instant::now(),
                    )?;
                }
                if heartbeat {
                    self.process(Event::Heartbeat { epoch }, Instant::now())?;
                }
            }
            Control::Reconciled(snapshot) => {
                self.busy = false;
                self.reconcile = false;
                self.process(Event::Reconcile(snapshot), Instant::now())?;
                if self.engine.core().health == Health::Healthy {
                    self.reconciled_generation = self.source_generation;
                    self.telemetry.emit(json!({"kind":"source_reconciled","generation":self.source_generation,"epoch":self.engine.core().epoch,"at_ms":wall_ms()}));
                }
            }
            Control::Target {
                request,
                answer,
                received,
            } => {
                if self.reconnect_pending
                    || request.epoch < self.engine.core().epoch
                    || self.source_generation
                        != self.circuit.source_generation.load(Ordering::Acquire)
                {
                    self.telemetry.emit(json!({"kind":"stale_strategy_discarded","epoch":request.epoch,"current_epoch":self.engine.core().epoch}));
                    return Ok(());
                }
                strategy::validate_answer(&request, &answer, self.engine.core().epoch, wall_ms())?;
                if let Some(target) = answer.target {
                    if request.bar.close_ms > wall_ms() || wall_ms() - request.bar.close_ms > 65000
                    {
                        return Err(fail("stale strategy bar"));
                    }
                    self.target = Some((target, request.bar.close_ms + 65000, received));
                    self.process(
                        Event::SetTarget(Target {
                            revision: request.revision,
                            position: target,
                            valid_until: request.bar.close_ms + 65000,
                        }),
                        received,
                    )?;
                }
            }
        }
        Ok(())
    }
    fn advance_reconnect(&mut self) -> Result<()> {
        if self.reconnect_pending && !self.busy {
            // Consume the previous executor result in its original epoch first.
            self.process(Event::Disconnect, Instant::now())?;
            self.process(Event::Reconnect, Instant::now())?;
            self.reconnect_pending = false;
        }
        Ok(())
    }
    fn market(&mut self, message: Market) -> Result<()> {
        if message.generation != self.source_generation {
            self.stale_market_discarded += 1;
            self.telemetry.emit(json!({"kind":"stale_market_discarded","generation":message.generation,"current_generation":self.source_generation}));
            return Ok(());
        }
        self.market_count += 1;
        self.process_with_time(
            Event::QuoteObserved {
                bid: message.bid,
                ask: message.ask,
                observed_at: message.observed_at,
            },
            message.received,
            Some(EventTime {
                event_time_ms: Some(message.exchange_time),
                received_time_ms: Some(message.observed_at),
                source: Some("hyperliquid-testnet:BTC:l2Book".into()),
                ..EventTime::default()
            }),
        )
    }
    fn dispatch_target(&mut self) -> Result<()> {
        if self.busy
            || self.reconnect_pending
            || self.source_generation != self.circuit.source_generation.load(Ordering::Acquire)
            || self.source_generation != self.reconciled_generation
            || !self.stream_ready
            || self.reconcile
            || self.poll_requested
            || self.circuit.failed.load(Ordering::Acquire)
            || self.engine.core().health != Health::Healthy
        {
            return Ok(());
        }
        if !self.circuit.resume_source(self.source_generation) {
            return Ok(());
        }
        self.circuit.allow();
        if self.circuit.permit.load(Ordering::Acquire) != 1 {
            return Ok(());
        }
        let Some((target, deadline, received)) = self.target else {
            return Ok(());
        };
        let core = self.engine.core();
        if wall_ms() > deadline || core.open_orders().next().is_some() {
            return Ok(());
        }
        let Some((bid, ask, observed)) = core.quote else {
            return Ok(());
        };
        if wall_ms().saturating_sub(observed) > core.config.market_stale_ms {
            return Ok(());
        }
        let delta = target - core.position;
        if delta == 0 {
            return Ok(());
        }
        if core.orders.len() >= 1000 {
            return Err(fail(
                "bounded session order cap; close and start a verified new session",
            ));
        }
        let id = core
            .orders
            .keys()
            .next_back()
            .copied()
            .unwrap_or(core.id_floor)
            .checked_add(1)
            .ok_or_else(|| fail("order id overflow"))?;
        let intent = Intent {
            id,
            side: if delta > 0 { Side::Buy } else { Side::Sell },
            qty: delta.abs(),
            limit: if delta > 0 { ask } else { bid },
            based_on_seq: core.seq,
            valid_until: wall_ms() + 3000,
        };
        self.process(
            Event::SubmitTargeted {
                intent,
                revision: core.last_target_revision,
                expected_position: core.position,
            },
            received,
        )
    }
}
pub struct Options {
    pub signed: bool,
    pub python: bool,
    pub seconds: u64,
    pub flatten: bool,
}
pub struct Segment {
    pub journal: std::path::PathBuf,
    pub orders: usize,
    pub bytes: u64,
    pub age_seconds: u64,
    pub flatten_on_rotation: bool,
    pub nonce_floor: u64,
    pub risk_cash_floor: Option<String>,
    pub expected_cash: Option<String>,
}
impl Segment {
    pub fn can_drain(&self, core: &Core, busy: bool, reconcile: bool) -> bool {
        !busy
            && !reconcile
            && core.health == Health::Healthy
            && (self.flatten_on_rotation || (core.position == 0 && core.rotation_ready().is_ok()))
    }
    pub fn due(&self, core: &Core, age: Duration) -> Result<bool> {
        Ok(core.orders.len() >= self.orders
            || std::fs::metadata(&self.journal)?.len() >= self.bytes
            || age >= Duration::from_secs(self.age_seconds))
    }
}
pub fn config() -> Config {
    Config {
        max_abs_position: 20,
        max_order_qty: 40,
        max_order_notional: 40_000_000,
        request_timeout_ms: 20000,
        market_stale_ms: 5000,
        private_stale_ms: 30000,
        max_signal_lag: 10000,
    }
}
pub fn save_json(path: &Path, value: &impl serde::Serialize) -> Result<()> {
    let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    File::open(path.parent().ok_or_else(|| fail("file parent"))?)?.sync_all()?;
    Ok(())
}
pub fn new_session(http: &Http, instrument: Instrument) -> Result<Session> {
    let account = std::env::var("HL_MAIN_ADDRESS").map_err(|_| fail("account missing"))?;
    let agent = std::env::var("HL_API_ADDRESS").map_err(|_| fail("agent missing"))?;
    let started_ms = wall_ms();
    let state = http.info("clearinghouseState", json!({"user":account}))?;
    venue::fresh(&state["time"], wall_ms())?;
    if venue::position(&state)? != rust_decimal::Decimal::ZERO
        || !venue::rows(&state["assetPositions"])?.is_empty()
        || !venue::rows(&http.info("openOrders", json!({"user":account}))?)?.is_empty()
    {
        return Err(fail(
            "fresh session requires flat account with zero open orders; resume original session",
        ));
    }
    let mut random = [0u8; 8];
    File::open("/dev/urandom")?.read_exact(&mut random)?;
    let session = Session {
        session: hex::encode(random),
        account,
        agent,
        started_ms,
        baseline_cash: venue::text(&state["marginSummary"]["accountValue"])?.into(),
        instrument,
        max_position_lots: 20,
        max_notional: "40".into(),
        max_loss: "10".into(),
        nonce_floor: 0,
        risk_cash_floor: None,
    };
    session.validate()?;
    Ok(session)
}
struct SourceControl {
    epoch: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
    critical: Sender<Control>,
    circuit: Arc<Circuit>,
}
fn candle_worker(
    http: Http,
    instrument: Instrument,
    session: String,
    start_revision: u64,
    mut strategy: Strategy,
    control: SourceControl,
) -> Result<()> {
    let SourceControl {
        epoch,
        stop,
        critical,
        circuit,
    } = control;
    let mut last = None;
    let mut revision = start_revision;
    let mut cached = std::collections::VecDeque::<strategy::Bar>::new();
    while !stop.load(Ordering::Acquire) {
        let boundary = wall_ms() / 60000 * 60000;
        let start = last.map_or(boundary - 9 * 60000, |v: u64| v.saturating_sub(60000));
        let fetched = strategy::bars(&http, &instrument, start, boundary - 1)?;
        if let Some(recorder) = &http.recorder {
            let snapshot = http.info("metaAndAssetCtxs", json!({}))?;
            let asset = instrument.asset as usize;
            if snapshot[0]["universe"][asset]["name"] != instrument.symbol {
                return Err(fail("funding context instrument mismatch"));
            }
            let context = &snapshot[1][asset];
            for key in ["funding", "markPx", "oraclePx"] {
                venue::dec(&context[key])?;
            }
            recorder.append(json!({"kind":"asset_context","coin":instrument.symbol,"available_ms":wall_ms(),"context":context,"meaning":"current public funding estimate, not settled account cashflow"}))?;
        }
        if last.is_none()
            && (fetched.len() < 8 || fetched.last().is_none_or(|b| b.close_ms < boundary - 60001))
        {
            return Err(fail("incomplete bounded strategy warmup"));
        }
        let mut latest = None;
        for bar in fetched {
            if let Some(previous) = cached.iter().find(|b| b.open_ms == bar.open_ms)
                && *previous != bar
            {
                return Err(fail("previously closed candle changed"));
            }
            if last.is_some_and(|v| bar.open_ms <= v) {
                continue;
            }
            if last.is_some_and(|v| bar.open_ms != v + 60000) {
                return Err(fail("strategy candle gap"));
            }
            let received = Instant::now();
            revision += 1;
            let request = Request {
                session: session.clone(),
                epoch: epoch.load(Ordering::Acquire),
                revision,
                deadline_ms: wall_ms() + 1200,
                bar: bar.clone(),
            };
            let answer = strategy.calculate(&request)?;
            strategy::validate_answer(&request, &answer, request.epoch, wall_ms())?;
            if let Some(recorder) = &http.recorder {
                recorder.append(json!({"kind":"strategy","request":request,"answer":answer,"received_ms":wall_ms()}))?;
            }
            last = Some(bar.open_ms);
            cached.push_back(bar);
            if cached.len() > 503 {
                cached.pop_front();
            }
            latest = Some(Control::Target {
                request,
                answer,
                received,
            });
        }
        if let Some(message) = latest {
            circuit.publish(&critical, message)?;
        }
        // Once the latest closed bar is cached, wait for the next boundary.
        // Retry an unpublished trailing bar without refetching the full history.
        let next_fetch = if last.is_some_and(|open| open + 60000 == boundary) {
            boundary + 61000
        } else {
            wall_ms() + 2000
        };
        while wall_ms() < next_fetch && !stop.load(Ordering::Acquire) {
            thread::sleep(Duration::from_millis(100));
        }
    }
    Ok(())
}
/// A failing run retains its journals and returns failure even if explicit cleanup succeeds.
pub fn run(dir: &Path, options: Options) -> Result<Value> {
    run_segment(dir, options, None)
}
pub fn run_segment(dir: &Path, options: Options, segment: Option<&Segment>) -> Result<Value> {
    if options.seconds == 0 || options.seconds > 7200 {
        return Err(fail("runtime duration must be 1..7200 seconds"));
    }
    let mut http = Http::new(false)?;
    let instrument = Instrument::from_meta(&http.info("meta", json!({}))?, "BTC")?;
    let lock = if options.signed && segment.is_none() {
        Some(account_lock(
            &std::env::var("HL_MAIN_ADDRESS").map_err(|_| fail("account missing"))?,
        )?)
    } else {
        None
    };
    let session = if options.signed {
        std::fs::create_dir(dir)?;
        let mut s = new_session(&http, instrument.clone())?;
        if let Some(segment) = segment {
            if let Some(expected) = &segment.expected_cash
                && (venue::dec(&json!(expected))? - venue::dec(&json!(s.baseline_cash))?).abs()
                    > rust_decimal::Decimal::new(1, 6)
            {
                return Err(fail("cash changed between reconciled segments"));
            }
            s.nonce_floor = segment.nonce_floor;
            s.risk_cash_floor = segment.risk_cash_floor.clone();
            s.validate()?;
        }
        save_json(&dir.join("session.json"), &s)?;
        if s.instrument != instrument {
            return Err(fail("instrument metadata changed"));
        }
        Some(s)
    } else {
        std::fs::create_dir(dir)?;
        None
    };
    let mut engine = if let Some(segment) = segment {
        std::os::unix::fs::symlink(&segment.journal, dir.join("journal.jsonl"))?;
        if segment.journal.exists() {
            DurableEngine::recover(&segment.journal, None)?
        } else {
            DurableEngine::create(&segment.journal, config())?
        }
    } else {
        DurableEngine::create(&dir.join("journal.jsonl"), config())?
    };
    let recorder = super::record::Recorder::create(&dir.join("observations.jsonl"))?;
    http.recorder = Some(recorder.clone());
    if engine.core().config != config() {
        return Err(fail("native Core config mismatch"));
    }
    engine.set_sync_policy(SyncPolicy::Outbox);
    let venue = if let Some(s) = &session {
        let mut venue = Venue::open(s.clone(), dir)?;
        venue.http.recorder = Some(recorder.clone());
        Some(venue)
    } else {
        None
    };
    let strategy = Strategy::open(options.python, dir)?;
    let tag = "run";
    let (telemetry, logger) = Telemetry::start(&dir.join(format!("{tag}-trace.jsonl")))?;
    let circuit = Arc::new(Circuit::default());
    let stop = Arc::new(AtomicBool::new(false));
    let epoch = Arc::new(AtomicU64::new(engine.core().epoch + 1));
    let (market_tx, market_rx) = bounded(128);
    let (critical_tx, critical_rx) = bounded(32);
    let (job_tx, job_rx) = bounded(32);
    let worker = {
        let critical = critical_tx.clone();
        let circuit = circuit.clone();
        let telemetry = telemetry.clone();
        thread::spawn(move || execute(venue, job_rx, critical, circuit, telemetry))
    };
    let ws_worker = {
        let stop = stop.clone();
        let circuit = circuit.clone();
        let critical = critical_tx.clone();
        let instrument = instrument.clone();
        let account = session.as_ref().map(|s| s.account.clone());
        let recorder = recorder.clone();
        thread::spawn(move || {
            let result: Result<()> = (|| {
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()?;
                let mut generation = 0;
                rt.block_on(stream::run(
                    false,
                    instrument,
                    account,
                    stop,
                    |event, received| match event {
                        Observation::Raw(event) => recorder.append(json!({"kind":"ws","received_ms":wall_ms(),"generation":generation,"message":event})),
                        Observation::Quote {
                            bid,
                            ask,
                            observed_at,
                            exchange_time,
                        } => circuit.publish(
                            &market_tx,
                            Market {
                                generation,
                                bid,
                                ask,
                                observed_at,
                                exchange_time,
                                received,
                            },
                        ),
                        Observation::Ready => {
                            circuit.publish(&critical, Control::Ready { generation })
                        }
                        Observation::Expired => {
                            generation = circuit.pause_source();
                            circuit.publish(&critical, Control::Expired { generation })
                        }
                        Observation::PrivateWake => {
                            // Wakeups may coalesce; verified private facts themselves never travel on this queue.
                            let _ = critical.try_send(Control::Wake);
                            Ok(())
                        }
                    },
                ))
            })();
            if let Err(e) = result {
                circuit.trip(e.to_string());
            }
        })
    };
    let strategy_worker = {
        let stop = stop.clone();
        let circuit = circuit.clone();
        let critical = critical_tx.clone();
        let epoch = epoch.clone();
        let revision = engine.core().last_target_revision;
        let session = session
            .as_ref()
            .map_or_else(|| "paper".into(), |s| s.session.clone());
        thread::spawn(move || {
            if let Err(e) = candle_worker(
                http,
                instrument,
                session,
                revision,
                strategy,
                SourceControl {
                    epoch,
                    stop,
                    critical,
                    circuit: circuit.clone(),
                },
            ) {
                circuit.trip(e.to_string());
            }
        })
    };
    let signal_worker = {
        let stop = stop.clone();
        let circuit = circuit.clone();
        thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(async {
                let mut terminate =
                    tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                        .unwrap();
                let mut interrupt =
                    tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
                        .unwrap();
                while !stop.load(Ordering::Acquire) {
                    let wait = futures_util::future::select(
                        Box::pin(terminate.recv()),
                        Box::pin(interrupt.recv()),
                    );
                    if tokio::time::timeout(Duration::from_millis(100), wait)
                        .await
                        .is_ok()
                    {
                        circuit.halt();
                        stop.store(true, Ordering::Release);
                        break;
                    }
                }
            });
        })
    };
    let mut writer = Writer {
        engine,
        jobs: job_tx,
        circuit: circuit.clone(),
        telemetry: telemetry.clone(),
        busy: false,
        reconcile: false,
        stream_ready: false,
        source_generation: 0,
        reconciled_generation: 0,
        reconnect_pending: false,
        stale_market_discarded: 0,
        poll_requested: false,
        target: None,
        processed: 0,
        market_count: 0,
        critical_count: 0,
    };
    let started = Instant::now();
    let mut tick = Instant::now();
    let mut poll = Instant::now() - Duration::from_secs(2);
    let mut audit = Instant::now() - Duration::from_secs(11);
    let mut rotation_requested = None::<Instant>;
    let mut rotation_ready = false;
    let outcome: Result<()> = (|| {
        writer.process(
            Event::Heartbeat {
                epoch: writer.engine.core().epoch,
            },
            Instant::now(),
        )?;
        writer.process(Event::Reconnect, Instant::now())?;
        epoch.store(writer.engine.core().epoch, Ordering::Release);
        while started.elapsed() < Duration::from_secs(options.seconds)
            && !stop.load(Ordering::Acquire)
            && !circuit.failed.load(Ordering::Acquire)
        {
            let mut handled = false;
            for _ in 0..4 {
                if let Ok(message) = critical_rx.try_recv() {
                    writer.control(message)?;
                    handled = true;
                } else {
                    break;
                }
            }
            if let Ok(message) = market_rx.try_recv() {
                writer.market(message)?;
                handled = true;
            }
            if !handled {
                crossbeam_channel::select! {recv(critical_rx)->msg=>writer.control(msg.map_err(|_|fail("critical worker unavailable"))?)?,recv(market_rx)->msg=>writer.market(msg.map_err(|_|fail("market worker unavailable"))?)?,default(Duration::from_millis(10))=>{}}
            }
            if tick.elapsed() >= Duration::from_millis(50) {
                writer.process(Event::Tick, Instant::now())?;
                tick = Instant::now();
            }
            writer.advance_reconnect()?;
            epoch.store(writer.engine.core().epoch, Ordering::Release);
            if let Some(segment) = segment {
                if rotation_requested.is_none()
                    && segment.due(writer.engine.core(), started.elapsed())?
                {
                    rotation_requested = Some(Instant::now());
                    writer.target = None;
                    if !writer.busy {
                        circuit.close();
                    }
                }
                if let Some(requested) = rotation_requested {
                    writer.target = None;
                    if segment.can_drain(writer.engine.core(), writer.busy, writer.reconcile) {
                        rotation_ready = true;
                        break;
                    }
                    if requested.elapsed() >= Duration::from_secs(60) {
                        return Err(fail("rotation quiescence deadline; no successor published"));
                    }
                }
            }
            if rotation_requested.is_none() {
                writer.dispatch_target()?;
            }
            if !writer.busy
                && (writer.reconcile
                    || writer.poll_requested
                    || poll.elapsed() >= Duration::from_secs(1))
            {
                let do_audit = writer.poll_requested || audit.elapsed() >= Duration::from_secs(10);
                writer.poll_requested = false;
                circuit.publish(
                    &writer.jobs,
                    Job::Refresh {
                        core: Box::new(writer.engine.core().clone()),
                        reconcile: writer.reconcile,
                        audit: do_audit,
                    },
                )?;
                writer.busy = true;
                poll = Instant::now();
                if do_audit {
                    audit = Instant::now();
                }
            }
        }
        Ok(())
    })();
    if let Err(e) = outcome {
        circuit.trip(e.to_string());
    }
    circuit.halt();
    stop.store(true, Ordering::Release);
    let active_seconds = started.elapsed().as_secs_f64();
    // Stop production, join the one execution owner, then reconcile original durable identities.
    let _ = ws_worker.join();
    let _ = strategy_worker.join();
    let _ = signal_worker.join();
    let (closed_jobs, closed_receiver) = bounded(0);
    drop(closed_receiver);
    drop(std::mem::replace(&mut writer.jobs, closed_jobs));
    let mut venue = worker.join().map_err(|_| fail("executor panicked"))?;
    while let Ok(message) = critical_rx.try_recv() {
        if let Err(e) = writer.control(message) {
            circuit.trip(e.to_string());
        }
    }
    let mut engine = writer.engine;
    let cleanup = (|| -> Result<()> {
        let flatten =
            options.flatten || (rotation_ready && segment.is_some_and(|s| s.flatten_on_rotation));
        if !options.signed && flatten {
            flatten_paper(&mut engine)?;
        }
        engine.process(wall_ms().max(engine.core().now), Event::Disconnect)?;
        if let Some(v) = venue.as_mut() {
            cleanup_signed(&mut engine, v, flatten)
        } else {
            engine.sync().map_err(Into::into)
        }
    })();
    if let Err(e) = &cleanup {
        circuit.trip(format!("cleanup unresolved: {e}"));
    }
    let report = json!({"schema":1,"runtime":"rust-native","signed":options.signed,"python_strategy":options.python,"requested_seconds":options.seconds,"active_seconds":active_seconds,"processed":writer.processed,"market_events":writer.market_count,"source_reconnects":writer.source_generation,"stale_market_discarded":writer.stale_market_discarded,"critical_events":writer.critical_count,"market_queued_at_stop":market_rx.len(),"critical_queued_at_stop":critical_rx.len(),"position":engine.core().position,"orders":engine.core().orders.len(),"fills":engine.core().fills.len(),"health":engine.core().health,"failed":circuit.failed.load(Ordering::Acquire),"failure":circuit.reason(),"cleanup_verified":cleanup.is_ok(),"ledger":venue.as_ref().and_then(|v|v.last_ledger.as_ref()),"telemetry_dropped":telemetry.dropped.load(Ordering::Relaxed),"rotation_requested":rotation_requested.is_some(),"rotation_ready":rotation_ready && cleanup.is_ok() && engine.core().position==0 && engine.core().rotation_ready().is_ok() && engine.core().reconciled_through==engine.core().venue_seq,"rotation_drain_started":rotation_ready,"signal_stop":stop.load(Ordering::Acquire)&&!rotation_ready&&active_seconds<options.seconds as f64,"nonce_floor":venue.as_ref().map_or(0, Venue::nonce_floor),"id_floor":engine.core().carry().id_floor});
    drop(writer.telemetry);
    drop(telemetry);
    logger
        .join()
        .map_err(|_| fail("telemetry worker panicked"))??;
    save_json(&dir.join(format!("{tag}-report.json")), &report)?;
    recorder.sync()?;
    drop(lock);
    Ok(report)
}
pub fn cleanup_signed(engine: &mut DurableEngine, venue: &mut Venue, flatten: bool) -> Result<()> {
    let at = wall_ms().max(engine.core().now);
    engine.process(at, Event::Reconnect)?;
    let reconciliation = venue.reconcile(engine.core())?;
    engine.process(
        wall_ms().max(engine.core().now),
        Event::Reconcile(reconciliation),
    )?;
    if flatten && engine.core().position != 0 {
        let (bid, ask) = venue.http.quote(&venue.session.instrument)?;
        engine.process(wall_ms(), Event::Quote { bid, ask })?;
        let core = engine.core();
        let delta = -core.position;
        let intent = Intent {
            id: core
                .orders
                .keys()
                .next_back()
                .copied()
                .unwrap_or(core.id_floor)
                + 1,
            side: if delta > 0 { Side::Buy } else { Side::Sell },
            qty: delta.abs(),
            limit: venue::closeout_limit(
                &venue.session.instrument,
                if delta > 0 { ask } else { bid },
                delta > 0,
            )?,
            based_on_seq: core.seq,
            valid_until: wall_ms() + 3000,
        };
        let effects = engine.process(wall_ms(), Event::Submit(intent.clone()))?;
        if !effects.iter().any(|e| matches!(e, Effect::SendOrder(_))) {
            return Err(fail("flatten refused by Core"));
        }
        venue.submit(&intent)?;
        let started = Instant::now();
        loop {
            for report in venue.poll(engine.core())? {
                engine.process(
                    wall_ms(),
                    Event::Execution {
                        epoch: engine.core().epoch,
                        venue_seq: engine.core().venue_seq + 1,
                        report,
                    },
                )?;
            }
            if engine.core().position == 0
                && engine
                    .core()
                    .orders
                    .values()
                    .all(|o| o.lifecycle.terminal())
            {
                break;
            }
            if started.elapsed() > Duration::from_secs(30) {
                return Err(fail("flatten unresolved; preserve session"));
            }
            thread::sleep(Duration::from_millis(200));
        }
        engine.process(wall_ms(), Event::Reconnect)?;
        let snapshot = venue.reconcile(engine.core())?;
        engine.process(wall_ms(), Event::Reconcile(snapshot))?;
    }
    engine.sync()?;
    let account = venue.account()?;
    let opens = venue.open_orders()?;
    if !opens.is_empty()
        || venue::position(&account)?
            != rust_decimal::Decimal::from(engine.core().position) * venue.session.instrument.lot()
    {
        return Err(fail("final independent venue check mismatch"));
    }
    if flatten && venue::position(&account)? != rust_decimal::Decimal::ZERO {
        return Err(fail("final venue position not flat"));
    }
    Ok(())
}
fn flatten_paper(engine: &mut DurableEngine) -> Result<()> {
    let core = engine.core();
    if core.position == 0 {
        return Ok(());
    }
    if core.health != Health::Healthy
        || core
            .orders
            .values()
            .any(|o| !o.lifecycle.terminal() || o.uncertain)
    {
        return Err(fail("paper cleanup unresolved"));
    }
    let Some((bid, ask, _)) = core.quote else {
        return Err(fail("paper flatten has no quote"));
    };
    let delta = -core.position;
    let i = Intent {
        id: core
            .orders
            .keys()
            .next_back()
            .copied()
            .unwrap_or(core.id_floor)
            + 1,
        side: if delta > 0 { Side::Buy } else { Side::Sell },
        qty: delta.abs(),
        limit: if delta > 0 { ask } else { bid },
        based_on_seq: core.seq,
        valid_until: wall_ms() + 3000,
    };
    let effects = engine.process(wall_ms(), Event::Submit(i.clone()))?;
    if !effects.iter().any(|e| matches!(e, Effect::SendOrder(_))) {
        return Err(fail("paper flatten refused"));
    }
    engine.process(
        wall_ms(),
        Event::Execution {
            epoch: engine.core().epoch,
            venue_seq: engine.core().venue_seq + 1,
            report: Report::Fill(Fill {
                execution_id: i.id,
                order_id: i.id,
                qty: i.qty,
                price: i.limit,
            }),
        },
    )?;
    Ok(())
}
pub fn recover_only(dir: &Path, flatten: bool) -> Result<Value> {
    recover_with_proof(dir, flatten, false)
}
pub fn recover_with_proof(dir: &Path, flatten: bool, resolve_unsent: bool) -> Result<Value> {
    let session: Session = serde_json::from_slice(&std::fs::read(dir.join("session.json"))?)?;
    let _lock = account_lock(&session.account)?;
    recover_locked_with_proof(dir, flatten, resolve_unsent)
}
pub(super) fn recover_locked(dir: &Path, flatten: bool) -> Result<Value> {
    recover_locked_with_proof(dir, flatten, false)
}
fn recover_locked_with_proof(dir: &Path, flatten: bool, resolve_unsent: bool) -> Result<Value> {
    if !dir.join("actions.jsonl").is_file() || !dir.join("journal.jsonl").is_file() {
        return Err(fail(
            "original action/journal history missing; recovery cannot create replacements",
        ));
    }
    let session: Session = serde_json::from_slice(&std::fs::read(dir.join("session.json"))?)?;
    let mut venue = Venue::open(session, dir)?;
    if resolve_unsent {
        venue.enable_local_unsent_proof();
    }
    let recorder = super::record::Recorder::create(
        &dir.join(format!("recovery-observations-{}.jsonl", wall_ms())),
    )?;
    venue.http.recorder = Some(recorder.clone());
    let mut engine = DurableEngine::recover(&dir.join("journal.jsonl"), None)?;
    if engine.core().config != config() {
        return Err(fail("recovery config mismatch"));
    }
    engine.set_sync_policy(SyncPolicy::Outbox);
    let started = Instant::now();
    let result = cleanup_signed(&mut engine, &mut venue, flatten);
    let report = json!({"runtime":"rust-native-recovery","at_ms":wall_ms(),"elapsed_seconds":started.elapsed().as_secs_f64(),"position":engine.core().position,"orders":engine.core().orders.len(),"fills":engine.core().fills.len(),"cleanup_verified":result.is_ok(),"failure":result.as_ref().err().map(ToString::to_string),"ledger":venue.last_ledger});
    save_json(&dir.join(format!("recovery-{}.json", wall_ms())), &report)?;
    recorder.sync()?;
    result?;
    Ok(report)
}
pub fn diagnostic(dir: &Path, crash_after_fill: bool) -> Result<Value> {
    let account = std::env::var("HL_MAIN_ADDRESS").map_err(|_| fail("account missing"))?;
    let _lock = account_lock(&account)?;
    let http = Http::new(false)?;
    let instrument = Instrument::from_meta(&http.info("meta", json!({}))?, "BTC")?;
    std::fs::create_dir(dir)?;
    let session = new_session(&http, instrument)?;
    save_json(&dir.join("session.json"), &session)?;
    let mut venue = Venue::open(session, dir)?;
    let mut engine = DurableEngine::create(&dir.join("journal.jsonl"), config())?;
    engine.set_sync_policy(SyncPolicy::Outbox);
    let outcome: Result<()> = (|| {
        engine.process(wall_ms(), Event::Heartbeat { epoch: 0 })?;
        for target in [20, 0, -20, 0] {
            let (bid, ask) = venue.http.quote(&venue.session.instrument)?;
            engine.process(wall_ms(), Event::Quote { bid, ask })?;
            let core = engine.core();
            let delta = target - core.position;
            let intent = Intent {
                id: core.orders.len() as u64 + 1,
                side: if delta > 0 { Side::Buy } else { Side::Sell },
                qty: delta.abs(),
                limit: venue::closeout_limit(
                    &venue.session.instrument,
                    if delta > 0 { ask } else { bid },
                    delta > 0,
                )?,
                based_on_seq: core.seq,
                valid_until: wall_ms() + 3000,
            };
            let effects = engine.process(wall_ms(), Event::Submit(intent.clone()))?;
            if !effects.iter().any(|e| matches!(e, Effect::SendOrder(_))) {
                return Err(fail("diagnostic refused by Core"));
            }
            venue.submit(&intent)?;
            let start = Instant::now();
            loop {
                let reports = venue.poll(engine.core())?;
                if crash_after_fill && reports.iter().any(|r| matches!(r, Report::Fill(_))) {
                    // Real process loss AFTER venue fill, BEFORE local fill persistence. Restart must query original IDs.
                    save_json(
                        &dir.join("injected-crash.json"),
                        &json!({"at_ms":wall_ms(),"point":"venue_fill_before_local_report","exit_code":86,"completed":false}),
                    )?;
                    std::process::exit(86);
                }
                for report in reports {
                    engine.process(
                        wall_ms(),
                        Event::Execution {
                            epoch: engine.core().epoch,
                            venue_seq: engine.core().venue_seq + 1,
                            report,
                        },
                    )?;
                }
                if engine.core().position == target
                    && engine
                        .core()
                        .orders
                        .values()
                        .all(|o| o.lifecycle.terminal())
                {
                    break;
                }
                if start.elapsed() > Duration::from_secs(30) {
                    return Err(fail("diagnostic fill deadline"));
                }
                thread::sleep(Duration::from_millis(200));
            }
        }
        Ok(())
    })();
    let cleanup = cleanup_signed(&mut engine, &mut venue, true);
    let report = json!({"runtime":"rust-native-diagnostic","at_ms":wall_ms(),"position":engine.core().position,"orders":engine.core().orders.len(),"fills":engine.core().fills.len(),"completed":outcome.is_ok(),"cleanup_verified":cleanup.is_ok(),"failure":outcome.as_ref().err().map(ToString::to_string),"cleanup_failure":cleanup.as_ref().err().map(ToString::to_string),"ledger":venue.last_ledger});
    save_json(&dir.join("diagnostic-report.json"), &report)?;
    cleanup?;
    outcome?;
    Ok(report)
}

#[path = "bench.rs"]
pub mod bench;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn expired_queued_order_is_never_dispatched() {
        let (jobs, receiver) = bounded(1);
        let (critical, reports) = bounded(1);
        let (trace, _) = bounded(1);
        let telemetry = Telemetry {
            queue: trace,
            dropped: Arc::new(AtomicU64::new(0)),
        };
        let circuit = Arc::new(Circuit::default());
        circuit.allow();
        jobs.send(Job::Submit {
            intent: Intent {
                id: 1,
                side: Side::Buy,
                qty: 20,
                limit: 800000,
                based_on_seq: 1,
                valid_until: wall_ms() - 1,
            },
            epoch: 0,
            received: Instant::now(),
        })
        .unwrap();
        drop(jobs);
        execute(None, receiver, critical, circuit.clone(), telemetry);
        assert!(
            circuit.failed.load(Ordering::Acquire),
            "expired intent escaped the native dispatch boundary"
        );
        assert!(
            reports.try_recv().is_err(),
            "expired order received simulated acceptance"
        );
    }
}

#[cfg(test)]
mod isolation_tests {
    use super::*;
    #[test]
    fn slow_strategy_and_full_telemetry_do_not_block_private_writer() {
        let dir = std::env::temp_dir().join(format!("native-isolation-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("journal.jsonl");
        let mut engine = DurableEngine::create(&path, config()).unwrap();
        engine.set_sync_policy(SyncPolicy::Outbox);
        let (jobs, dispatch) = bounded(32);
        let (trace, _blocked_sink) = bounded(1024);
        let circuit = Arc::new(Circuit::default());
        circuit.allow();
        let telemetry = Telemetry {
            queue: trace,
            dropped: Arc::new(AtomicU64::new(0)),
        };
        for _ in 0..1100 {
            telemetry.emit(json!({"blocked":"cold sink"}));
        }
        let mut writer = Writer {
            engine,
            jobs,
            circuit,
            telemetry,
            busy: false,
            reconcile: false,
            stream_ready: true,
            source_generation: 0,
            reconciled_generation: 0,
            reconnect_pending: false,
            stale_market_discarded: 0,
            poll_requested: false,
            target: None,
            processed: 0,
            market_count: 0,
            critical_count: 0,
        };
        writer
            .process(Event::Heartbeat { epoch: 0 }, Instant::now())
            .unwrap();
        writer
            .process(
                Event::Quote {
                    bid: 800000,
                    ask: 800000,
                },
                Instant::now(),
            )
            .unwrap();
        let core = writer.engine.core();
        let intent = Intent {
            id: 1,
            side: Side::Buy,
            qty: 20,
            limit: 800000,
            based_on_seq: core.seq,
            valid_until: wall_ms() + 3000,
        };
        writer
            .process(Event::Submit(intent), Instant::now())
            .unwrap();
        assert!(matches!(dispatch.recv().unwrap(), Job::Submit { .. }));
        let finished = Arc::new(AtomicBool::new(false));
        let flag = finished.clone();
        let slow = thread::spawn(move || {
            thread::sleep(Duration::from_secs(1));
            flag.store(true, Ordering::Release);
        });
        writer
            .control(Control::Done {
                epoch: 0,
                reports: vec![Report::Fill(Fill {
                    execution_id: 1,
                    order_id: 1,
                    qty: 20,
                    price: 800000,
                })],
                heartbeat: true,
            })
            .unwrap();
        assert_eq!(writer.engine.core().position, 20);
        assert!(!finished.load(Ordering::Acquire));
        assert!(writer.telemetry.dropped.load(Ordering::Acquire) > 0);
        let expected = writer.engine.core().clone();
        writer.engine.sync().unwrap();
        drop(writer);
        assert_eq!(crate::journal::replay(&path).unwrap(), expected);
        slow.join().unwrap();
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[cfg(test)]
#[path = "expiry_tests.rs"]
mod expiry_tests;
