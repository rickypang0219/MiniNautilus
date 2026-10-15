//! Controlled raw-frame benchmark using the production writer/execution route.
//! The sink is simulated; these observations are not exchange latency.
use super::*;
use sha2::{Digest, Sha256};
#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Manifest {
    pub schema: u32,
    pub rate: u64,
    pub seconds: u64,
    pub warmup_seconds: u64,
    pub order_every: u64,
    pub price_ticks: i64,
    pub capacities: Vec<usize>,
    pub sync: String,
}
impl Manifest {
    pub fn validate(&self) -> Result<()> {
        if self.schema != 1
            || self.rate == 0
            || self.rate > 10000
            || self.seconds == 0
            || self.seconds > 600
            || self.warmup_seconds >= self.seconds
            || self.order_every == 0
            || self.price_ticks != 800000
            || self.capacities != [128, 32, 32, 1024]
            || self.sync != "outbox"
        {
            return Err(fail("unsupported frozen benchmark manifest"));
        }
        Ok(())
    }
}
pub fn cpu_ns() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    }; // Linux process CPU, all threads.
    // SAFETY: clock_gettime writes one valid timespec; no shared state or pointers escape.
    if unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut ts) } != 0 {
        return 0;
    }
    ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64
}
pub fn rss_kib() -> Option<u64> {
    std::fs::read_to_string("/proc/self/status")
        .ok()?
        .lines()
        .find(|l| l.starts_with("VmHWM:"))?
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()
}
struct Input {
    market: Market,
    target: Option<i64>,
    index: u64,
    scheduled: Instant,
    admitted: Instant,
}
pub fn run(dir: &Path, manifest: Manifest) -> Result<Value> {
    manifest.validate()?;
    std::fs::create_dir(dir)?;
    save_json(&dir.join("manifest.json"), &manifest)?;
    let mut engine = DurableEngine::create(&dir.join("journal.jsonl"), config())?;
    engine.set_sync_policy(SyncPolicy::Outbox);
    let (telemetry, logger) = Telemetry::start(&dir.join("trace.jsonl"))?;
    let circuit = Arc::new(Circuit::default());
    let (tx, rx) = bounded::<Input>(128);
    let (critical_tx, critical_rx) = bounded(32);
    let (job_tx, job_rx) = bounded(32);
    let execution = {
        let circuit = circuit.clone();
        let telemetry = telemetry.clone();
        thread::spawn(move || execute(None, job_rx, critical_tx, circuit, telemetry))
    };
    let mut writer = Writer {
        engine,
        jobs: job_tx,
        circuit: circuit.clone(),
        telemetry: telemetry.clone(),
        busy: false,
        reconcile: false,
        stream_ready: true,
        poll_requested: false,
        target: None,
        processed: 0,
        market_count: 0,
        critical_count: 0,
    };
    writer.process(Event::Heartbeat { epoch: 0 }, Instant::now())?;
    let origin = Instant::now();
    let wall = wall_ms();
    let cpu = cpu_ns();
    let offered = Arc::new(AtomicU64::new(0));
    let admitted = Arc::new(AtomicU64::new(0));
    let high = Arc::new(AtomicU64::new(0));
    let producer = {
        let manifest = manifest.clone();
        let circuit = circuit.clone();
        let offered = offered.clone();
        let admitted = admitted.clone();
        let high = high.clone();
        thread::spawn(move || -> Result<String> {
            let instrument = Instrument {
                symbol: "BTC".into(),
                asset: 3,
                sz_decimals: 5,
            };
            let mut hash = Sha256::new();
            for n in 0..manifest.rate * manifest.seconds {
                let scheduled = origin + Duration::from_nanos(n * 1_000_000_000 / manifest.rate);
                if let Some(delay) = scheduled.checked_duration_since(Instant::now()) {
                    thread::sleep(delay);
                }
                let event_time = wall + n * 1000 / manifest.rate;
                let raw = format!(
                    "{{\"channel\":\"l2Book\",\"data\":{{\"coin\":\"BTC\",\"time\":{event_time},\"levels\":[[{{\"px\":\"80000\"}}],[{{\"px\":\"80000\"}}]]}}}}"
                );
                let received = Instant::now();
                offered.fetch_add(1, Ordering::Relaxed);
                let decoded: Value = serde_json::from_str(&raw)?;
                let (bid, ask) =
                    venue::parse_quote(&instrument, &decoded["data"], wall_ms().max(event_time))?;
                let target = if n % manifest.order_every == 0 {
                    Some([20, 0, -20, 0][(n / manifest.order_every % 4) as usize])
                } else {
                    None
                };
                hash.update(
                    format!(
                        "{n}:800000:{}\n",
                        target.map_or_else(|| "none".into(), |v| v.to_string())
                    )
                    .as_bytes(),
                );
                let input = Input {
                    market: Market {
                        bid,
                        ask,
                        observed_at: wall_ms(),
                        exchange_time: event_time,
                        received,
                    },
                    target,
                    index: n,
                    scheduled,
                    admitted: Instant::now(),
                };
                circuit.publish(&tx, input)?;
                admitted.fetch_add(1, Ordering::Relaxed);
                high.fetch_max(tx.len() as u64, Ordering::Relaxed);
            }
            Ok(hex::encode(hash.finalize()))
        })
    };
    let mut complete = 0;
    let mut source_done = false;
    let mut failure = None;
    let result: Result<()> = (|| {
        while !source_done || writer.busy {
            if circuit.failed.load(Ordering::Acquire) {
                return Err(fail("benchmark circuit closed"));
            }
            for _ in 0..4 {
                if let Ok(message) = critical_rx.try_recv() {
                    writer.control(message)?;
                } else {
                    break;
                }
            }
            let item = match rx.recv_timeout(Duration::from_millis(1)) {
                Ok(item) => item,
                Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
                Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
                    source_done = true;
                    continue;
                }
            };
            let n = item.index;
            let received = item.market.received;
            let queue_ns = item.admitted.elapsed().as_nanos() as u64;
            if n % manifest.rate == 0 {
                writer.process(Event::Heartbeat { epoch: 0 }, Instant::now())?;
            }
            if n % (manifest.rate / 20).max(1) == 0 {
                writer.process(Event::Tick, Instant::now())?;
            }
            if let Some(target) = item.target {
                let request = Request {
                    session: "controlled".into(),
                    epoch: 0,
                    revision: n + 1,
                    deadline_ms: wall_ms() + 1200,
                    bar: strategy::Bar {
                        open_ms: 0,
                        close_ms: wall_ms(),
                        price: 800000,
                    },
                };
                let answer = Answer {
                    session: request.session.clone(),
                    epoch: 0,
                    revision: n + 1,
                    deadline_ms: request.deadline_ms,
                    target: Some(target),
                };
                writer.control(Control::Target {
                    request,
                    answer,
                    received,
                })?;
            }
            writer.market(item.market)?;
            writer.dispatch_target()?;
            complete += 1;
            telemetry.emit(json!({"kind":"market_sample","index":n,"included":n>=manifest.warmup_seconds*manifest.rate,"queue_ns":queue_ns,"receive_to_writer_complete_ns":received.elapsed().as_nanos() as u64,"scheduled_to_complete_ns":item.scheduled.elapsed().as_nanos() as u64,"producer_overrun_ns":received.duration_since(item.scheduled).as_nanos() as u64}));
        }
        Ok(())
    })();
    if let Err(e) = result {
        failure = Some(e.to_string());
        circuit.trip("benchmark failed");
    }
    drop(rx);
    let hash = match producer.join() {
        Ok(Ok(hash)) => Some(hash),
        Ok(Err(e)) => {
            failure = Some(e.to_string());
            None
        }
        Err(_) => {
            failure = Some("producer panic".into());
            None
        }
    };
    let (closed, receiver) = bounded(0);
    drop(receiver);
    drop(std::mem::replace(&mut writer.jobs, closed));
    execution.join().map_err(|_| fail("execution panic"))?;
    while let Ok(message) = critical_rx.try_recv() {
        writer.control(message)?;
    }
    writer.engine.sync()?;
    let elapsed = origin.elapsed().as_secs_f64();
    let cpu = cpu_ns() - cpu;
    let core = writer.engine.core();
    let actions:Vec<_>=core.orders.values().map(|o|json!({"id":o.intent.id,"side":o.intent.side,"qty":o.intent.qty,"limit":o.intent.limit,"filled":o.filled,"lifecycle":o.lifecycle})).collect();
    let report = json!({"schema":1,"runtime":"rust-native-controlled","failure":failure,"counts":{"offered":offered.load(Ordering::Relaxed),"admitted":admitted.load(Ordering::Relaxed),"completed":complete,"rejected":offered.load(Ordering::Relaxed)-admitted.load(Ordering::Relaxed),"inflight":admitted.load(Ordering::Relaxed)-complete},"input_sha256":hash,"queue_high_water":high.load(Ordering::Relaxed),"cpu_ns":cpu,"elapsed_seconds":elapsed,"rss_peak_kib":rss_kib(),"telemetry_dropped":telemetry.dropped.load(Ordering::Relaxed),"final":{"position":core.position,"cash":core.cash,"orders":core.orders.len(),"fills":core.fills.len(),"health":core.health},"actions":actions});
    drop(writer.telemetry);
    drop(telemetry);
    logger.join().map_err(|_| fail("logger panic"))??;
    save_json(&dir.join("raw-report.json"), &report)?;
    Ok(report)
}
