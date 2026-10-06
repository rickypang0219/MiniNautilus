//! Read-only, localhost post-trade projection. No dependency on the execution process.
use crate::{core::Core, journal::JournalFollower, model::*};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    fs,
    io::{self, Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const HISTORY: usize = 1200;

fn millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}
fn read_json(path: &Path) -> Value {
    fs::read(path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null)
}
fn number(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| v.as_str()?.parse().ok())
        .filter(|v| v.is_finite() && *v > 0.)
}
#[derive(Default)]
pub struct Projection {
    core: Option<Core>,
    history: VecDeque<Value>,
    actions: VecDeque<Value>,
    signals: VecDeque<Value>,
    first_seen: BTreeMap<u64, u64>,
    last_quote: Option<(i64, i64, u64)>,
    refusals: u64,
    recoveries: u64,
    last_sample: Option<u64>,
}
impl Projection {
    pub fn observe(&mut self, core: &Core, input: Option<&Envelope>, effects: &[Effect]) {
        let old_fill_count = self.core.as_ref().map_or(0, |c| c.fills.len());
        if let Some((bid, ask, at)) = core.quote {
            self.last_quote = Some((bid, ask, at));
        }
        for id in core.fills.keys() {
            self.first_seen.entry(*id).or_insert(core.now);
        }
        if let Some(input) = input {
            if matches!(input.event, Event::SetTarget(_)) {
                self.signals.push_back(
                    json!({"seq":core.seq.to_string(),"at":core.now,"event":input.event,"accepted":!effects.iter().any(|e| matches!(e, Effect::SignalRefused{..}))}),
                );
            }
            if matches!(input.event, Event::Reconcile(_)) && core.health == Health::Healthy {
                self.recoveries += 1;
            }
            self.refusals += effects
                .iter()
                .filter(|e| matches!(e, Effect::Refused { .. } | Effect::SignalRefused { .. }))
                .count() as u64;
            if !matches!(
                input.event,
                Event::Quote { .. }
                    | Event::QuoteObserved { .. }
                    | Event::Tick
                    | Event::Heartbeat { .. }
                    | Event::Trade { .. }
            ) || !effects.is_empty()
            {
                // Reconcile carries potentially large full history; show its identity/counts.
                let event = match &input.event {
                    Event::Reconcile(s) => {
                        json!({"Reconcile":{"epoch":s.epoch,"orders":s.orders.len(),"fills":s.fills.len(),"position":s.position}})
                    }
                    other => serde_json::to_value(other).unwrap(),
                };
                self.actions.push_back(
                    json!({"seq":core.seq.to_string(),"at":core.now,"event":event,"effects":effects}),
                );
            }
        }
        // Compact old chart samples while preserving the full time span. Ledger rows are never sampled.
        let important = old_fill_count != core.fills.len()
            || self
                .core
                .as_ref()
                .is_none_or(|old| old.position != core.position || old.target != core.target);
        if important
            || self
                .last_sample
                .is_none_or(|at| core.now.saturating_sub(at) >= 250)
        {
            let mark = self
                .last_quote
                .map(|q| if core.position < 0 { q.1 } else { q.0 });
            let gross = mark
                .map(|m| core.cash + core.position as i128 * m as i128)
                .or((core.position == 0).then_some(core.cash));
            if self.history.len() >= HISTORY {
                let last = self.history.len() - 1;
                self.history = self
                    .history
                    .drain(..)
                    .enumerate()
                    .filter_map(|(i, p)| (i % 2 == 0 || i == last).then_some(p))
                    .collect();
            }
            self.history.push_back(
                json!({"at":core.now,"position":core.position,"target":core.target.as_ref().map(|t|t.position),"pnl":gross.map(|n|n as f64),"fills":core.fills.len()}),
            );
            self.last_sample = Some(core.now);
        }
        self.core = Some(core.clone());
    }
    pub fn view(&self, metadata: &Value, audit: &Value) -> Value {
        let Some(core) = &self.core else {
            return json!({"ready":false});
        };
        let tick = number(&metadata["tick"]).or_else(|| number(&metadata["parameters"][2]));
        let lot = number(&metadata["lot"]).or_else(|| number(&metadata["parameters"][3]));
        let scale = tick.zip(lot).map(|(a, b)| a * b);
        let symbol = metadata["symbol"]
            .as_str()
            .or_else(|| metadata["parameters"][0].as_str())
            .unwrap_or("Unspecified instrument");
        let quote_asset = ["FDUSD", "USDT", "USDC", "BTC", "ETH", "BNB"]
            .into_iter()
            .find(|q| symbol.ends_with(q));
        let base_asset = quote_asset.map(|q| symbol.trim_end_matches(q));
        let unit = if scale.is_some() {
            quote_asset.unwrap_or("quote units")
        } else {
            "tick·lots"
        };
        let mut pos = 0_i64;
        let mut average = 0_f64;
        let mut realized = 0_f64;
        for fill in core.fills.values() {
            let side = core.orders[&fill.order_id].intent.side;
            let delta = side.sign() * fill.qty;
            let price = fill.price as f64;
            if pos == 0 || pos.signum() == delta.signum() {
                average = (average * pos.unsigned_abs() as f64
                    + price * delta.unsigned_abs() as f64)
                    / (pos.unsigned_abs() as f64 + delta.unsigned_abs() as f64);
            } else {
                realized += pos.unsigned_abs().min(delta.unsigned_abs()) as f64
                    * (price - average)
                    * pos.signum() as f64;
                if delta.unsigned_abs() > pos.unsigned_abs() {
                    average = price;
                } else if delta.unsigned_abs() == pos.unsigned_abs() {
                    average = 0.;
                }
            }
            pos += delta;
        }
        let mark = self
            .last_quote
            .map(|q| if core.position < 0 { q.1 } else { q.0 });
        let exact = mark
            .map(|m| core.cash + core.position as i128 * m as i128)
            .or((core.position == 0).then_some(core.cash));
        let multiplier = scale.unwrap_or(1.);
        let gross = exact.map(|v| v as f64 * multiplier);
        let realized = realized * multiplier;
        let unrealized = gross.map(|g| g - realized);
        let mut fees = BTreeMap::<u64, Value>::new();
        if let Some(trades) = audit["raw_trades"].as_array() {
            for trade in trades {
                if let Some(id) = trade["id"].as_u64().and_then(|i| i.checked_add(1)) {
                    fees.insert(id, trade.clone());
                }
            }
        }
        let fee_complete = scale.is_some()
            && audit["raw_trades"]
                .as_array()
                .is_some_and(|t| t.len() == fees.len())
            && fees.len() == core.fills.len()
            && core.fills.iter().all(|(id, f)| {
                fees.get(id).is_some_and(|t| {
                    t["symbol"].as_str() == Some(symbol)
                        && t["isBuyer"].as_bool()
                            == Some(core.orders[&f.order_id].intent.side == Side::Buy)
                        && number(&t["qty"])
                            .zip(lot)
                            .is_some_and(|(q, l)| (q / l - f.qty as f64).abs() < 1e-6)
                        && number(&t["price"])
                            .zip(tick)
                            .is_some_and(|(p, t)| (p / t - f.price as f64).abs() < 1e-6)
                })
            })
            && (!core.fills.is_empty() || audit.is_object());
        let fee_quote = if fee_complete {
            fees.values().try_fold(0., |total, t| {
                let amount = t["commission"]
                    .as_str()?
                    .parse::<f64>()
                    .ok()
                    .filter(|v| v.is_finite() && *v >= 0.)?;
                let asset = t["commissionAsset"].as_str()?;
                if Some(asset) == quote_asset {
                    Some(total + amount)
                } else if Some(asset) == base_asset {
                    Some(total + amount * number(&t["price"])?)
                } else if amount == 0. {
                    Some(total)
                } else {
                    None
                }
            })
        } else {
            None
        };
        let trades:Vec<_>=core.fills.values().rev().map(|f|json!({
            "id":f.execution_id.to_string(),"order_id":f.order_id.to_string(),"side":core.orders[&f.order_id].intent.side,
            "qty":f.qty,"price":f.price,"at":self.first_seen[&f.execution_id],
            "fee":fees.get(&f.execution_id).filter(|_| fee_complete).map(|t|json!({"amount":t["commission"],"asset":t["commissionAsset"]}))
        })).collect();
        let orders:Vec<_>=core.orders.values().rev().map(|o|json!({"id":o.intent.id.to_string(),"side":o.intent.side,"qty":o.intent.qty,"filled":o.filled,"price":o.intent.limit,"status":o.lifecycle,"pending":o.pending,"uncertain":o.uncertain})).collect();
        let mut history: Vec<Value> = self
            .history
            .iter()
            .map(|p| {
                let mut p = p.clone();
                if let Some(n) = p["pnl"].as_f64() {
                    p["pnl"] = json!(n * multiplier);
                }
                p
            })
            .collect();
        // Always include the latest state, even when the final quote is inside
        // the sampling interval. Summary metrics and the chart endpoint must agree.
        let latest = json!({"at":core.now,"position":core.position,"target":core.target.as_ref().map(|t|t.position),"pnl":gross,"fills":core.fills.len()});
        if history.last() != Some(&latest) {
            if history.len() >= HISTORY {
                history.remove(1);
            }
            history.push(latest);
        }
        let (lower, upper) = core.exposure_bounds();
        json!({"ready":true,"seq":core.seq.to_string(),"engine_time":core.now,"symbol":symbol,"mode":metadata["mode"].as_str().or_else(|| metadata["parameters"][1].as_str()).unwrap_or("journal"),
            "tick":tick,"lot":lot,"unit":unit,"health":core.health,"killed":core.killed,"position":core.position,
            "target":core.target,"lower":lower.to_string(),"upper":upper.to_string(),"refusals":self.refusals,"recoveries":self.recoveries,
            "fill_count":core.fills.len(),"order_count":core.orders.len(),"open_orders":core.orders.values().filter(|o|!o.lifecycle.terminal()||o.uncertain).count(),
            "pnl":{"gross":gross,"realized":realized,"unrealized":unrealized,"net":gross.zip(fee_quote).map(|(g,f)|g-f),"fees":fee_quote,"exact_gross_tick_lots":exact.map(|n|n.to_string())},
            "mark":mark,"mark_at":self.last_quote.map(|q|q.2),"mark_stale":core.quote.is_none()||self.last_quote.is_none_or(|q|core.now.saturating_sub(q.2)>core.config.market_stale_ms),
            "history":history,
            "trades":trades,"orders":orders,"actions":self.actions.iter().rev().collect::<Vec<_>>(),"signals":self.signals.iter().rev().collect::<Vec<_>>(),"chart_limit":HISTORY})
    }
}

struct Session {
    path: PathBuf,
    follower: JournalFollower,
    projection: Projection,
    updated: u128,
    error: Option<String>,
    cached: Value,
    signature: (u64, u128, u128),
}
#[derive(Default)]
struct Store {
    sessions: BTreeMap<String, Value>,
    views: BTreeMap<String, Value>,
}
fn id(path: &Path) -> String {
    let n = path
        .to_string_lossy()
        .bytes()
        .fold(0xcbf29ce484222325_u64, |h, b| {
            (h ^ b as u64).wrapping_mul(0x100000001b3)
        });
    format!("{n:016x}")
}
fn discover(root: &Path, depth: usize, out: &mut Vec<PathBuf>) {
    if out.len() >= 128 || depth > 4 {
        return;
    }
    if root.is_file() {
        if root.extension().is_some_and(|e| e == "jsonl") {
            let mut buf = [0; 512];
            if fs::File::open(root)
                .and_then(|mut f| f.read(&mut buf))
                .ok()
                .is_some_and(|n| String::from_utf8_lossy(&buf[..n]).contains("\\\"Genesis\\\""))
            {
                out.push(root.to_path_buf());
            }
        }
    } else if let Ok(entries) = fs::read_dir(root) {
        for entry in entries.flatten() {
            if entry.file_type().is_ok_and(|t| !t.is_symlink()) {
                discover(&entry.path(), depth + 1, out);
            }
        }
    }
}
fn modified(path: &Path) -> u128 {
    fs::metadata(path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_millis())
}
fn worker(root: PathBuf, store: Arc<Mutex<Store>>) {
    let mut sessions = BTreeMap::<String, Session>::new();
    let mut discovery = Instant::now() - Duration::from_secs(3);
    loop {
        if discovery.elapsed() >= Duration::from_secs(2) {
            let mut paths = Vec::new();
            discover(&root, 0, &mut paths);
            paths.sort();
            for path in paths {
                let key = id(&path);
                if !sessions.contains_key(&key)
                    && let Ok(follower) = JournalFollower::open(&path)
                {
                    sessions.insert(
                        key,
                        Session {
                            path,
                            follower,
                            projection: Projection::default(),
                            updated: 0,
                            error: None,
                            cached: Value::Null,
                            signature: (0, 0, 0),
                        },
                    );
                }
            }
            discovery = Instant::now();
        }
        for (key, s) in &mut sessions {
            let count = if s.error.is_none() {
                match s.follower.poll(512, |core, input, effects| {
                    s.projection.observe(core, input, effects)
                }) {
                    Ok(n) => n,
                    Err(e) => {
                        s.error = Some(e.to_string());
                        0
                    }
                }
            } else {
                0
            };
            if count > 0 {
                s.updated = millis();
            }
            let parent = s.path.parent().unwrap_or(Path::new("."));
            let meta = parent.join("session.json");
            let audit = parent.join("exchange-audit.json");
            let signature = (
                s.follower.core().map_or(0, |c| c.seq),
                modified(&meta),
                modified(&audit),
            );
            if signature != s.signature || s.cached.is_null() {
                s.cached = s.projection.view(&read_json(&meta), &read_json(&audit));
                s.signature = signature;
            }
            let mut view = s.cached.clone();
            view["catching_up"] = json!(count == 512);
            view["pending_tail"] = json!(s.follower.pending_tail());
            view["updated_at"] = json!(s.updated);
            view["error"] = json!(s.error);
            view["id"] = json!(key);
            let name = s
                .path
                .strip_prefix(&root)
                .ok()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or(&s.path)
                .to_string_lossy()
                .to_string();
            view["name"] = json!(name);
            let info = json!({"id":key,"name":name,"symbol":view["symbol"],"mode":view["mode"],"health":view["health"],"seq":view["seq"],"updated_at":s.updated,"error":s.error,"modified_at":modified(&s.path)});
            if let Ok(mut shared) = store.lock() {
                shared.sessions.insert(key.clone(), info);
                shared.views.insert(key.clone(), view);
            }
        }
        thread::sleep(Duration::from_millis(200));
    }
}
fn respond(mut stream: TcpStream, store: Arc<Mutex<Store>>) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    let mut request = Vec::new();
    let mut buf = [0; 1024];
    while !request.windows(4).any(|s| s == b"\r\n\r\n") {
        let n = stream.read(&mut buf)?;
        if n == 0 {
            return Ok(());
        }
        request.extend_from_slice(&buf[..n]);
        if request.len() > 8192 {
            return Ok(());
        }
    }
    let request = String::from_utf8_lossy(&request);
    let mut first = request.lines().next().unwrap_or("").split_whitespace();
    let method = first.next().unwrap_or("");
    let path = first.next().unwrap_or("");
    let (status, kind, body) = if method != "GET" {
        (405, "text/plain", "Read-only dashboard".to_owned())
    } else {
        match path {
            "/" => (
                200,
                "text/html; charset=utf-8",
                include_str!("../web/index.html").to_owned(),
            ),
            "/app.css" => (200, "text/css", include_str!("../web/app.css").to_owned()),
            "/app.js" => (
                200,
                "text/javascript",
                include_str!("../web/app.js").to_owned(),
            ),
            "/api/sessions" => {
                let s = store.lock().unwrap();
                (200,"application/json",json!({"sessions":s.sessions.values().collect::<Vec<_>>(),"server_time":millis()}).to_string())
            }
            p if p.starts_with("/api/session/") => {
                let key = &p[13..];
                let s = store.lock().unwrap();
                match s.views.get(key) {
                    Some(v) => (200, "application/json", v.to_string()),
                    None => (
                        404,
                        "application/json",
                        json!({"error":"Unknown session"}).to_string(),
                    ),
                }
            }
            _ => (404, "text/plain", "Not found".to_owned()),
        }
    };
    write!(
        stream,
        "HTTP/1.1 {status} OK\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )?;
    Ok(())
}
pub fn serve(root: &Path, port: u16) -> io::Result<()> {
    let root = fs::canonicalize(root)?;
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    println!(
        "MiniNautilus dashboard: http://{} (read-only)",
        listener.local_addr()?
    );
    let store = Arc::new(Mutex::new(Store::default()));
    let clone = store.clone();
    thread::spawn(move || worker(root, clone));
    let clients = Arc::new(AtomicUsize::new(0));
    for stream in listener.incoming() {
        let stream = stream?;
        if clients.load(Ordering::Relaxed) >= 16 {
            continue;
        }
        clients.fetch_add(1, Ordering::Relaxed);
        let clients = clients.clone();
        let store = store.clone();
        thread::spawn(move || {
            let _ = respond(stream, store);
            clients.fetch_sub(1, Ordering::Relaxed);
        });
    }
    Ok(())
}
