//! Hyperliquid native USDC Testnet boundary. Complete conservative reconciliation.
use super::{
    Result, fail,
    signing::{Action, Attempts, LimitType, Signer, TimeInForce, WireOrder},
    wall_ms,
};
use crate::{core::Core, model::*};
use reqwest::blocking::Client;
use rust_decimal::{Decimal, prelude::ToPrimitive};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Read,
    path::Path,
    str::FromStr,
    sync::atomic::{AtomicU8, Ordering},
    time::{Duration, Instant},
};

pub fn dec(v: &Value) -> Result<Decimal> {
    let s = v.as_str().ok_or_else(|| fail("decimal string required"))?;
    let d = Decimal::from_str(s).map_err(|_| fail("invalid decimal"))?;
    if d.abs() > Decimal::from(1_000_000_000_000i64) {
        return Err(fail("decimal outside supported bound"));
    }
    Ok(d)
}
pub fn number(v: &Value) -> Result<u64> {
    v.as_u64()
        .filter(|n| *n <= i64::MAX as u64)
        .ok_or_else(|| fail("invalid integer"))
}
pub fn text(v: &Value) -> Result<&str> {
    v.as_str().ok_or_else(|| fail("string required"))
}
pub fn rows(v: &Value) -> Result<&Vec<Value>> {
    v.as_array().ok_or_else(|| fail("array required"))
}
fn cancel_terminal_race(status: &Value, asset: u64) -> bool {
    status["error"].as_str()
        == Some(
            format!("Order was never placed, already canceled, or filled. asset={asset}").as_str(),
        )
}
pub fn units(value: Decimal, increment: Decimal) -> Result<i64> {
    let result = value
        .checked_div(increment)
        .ok_or_else(|| fail("unit conversion"))?;
    if result.fract() != Decimal::ZERO {
        return Err(fail("off-grid units"));
    }
    result.to_i64().ok_or_else(|| fail("unit overflow"))
}
pub fn wire(v: Decimal) -> String {
    v.normalize().to_string()
}
pub fn fresh(time: &Value, now: u64) -> Result<()> {
    let t = number(time)?;
    if t > now || now - t > 5000 {
        return Err(fail("stale/future venue snapshot"));
    }
    Ok(())
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Instrument {
    pub symbol: String,
    pub asset: u64,
    pub sz_decimals: u32,
}
impl Instrument {
    pub fn tick(&self) -> Decimal {
        Decimal::new(1, 6 - self.sz_decimals)
    }
    pub fn lot(&self) -> Decimal {
        Decimal::new(1, self.sz_decimals)
    }
    pub fn price(&self, v: &Value) -> Result<i64> {
        let p = dec(v)?;
        if p <= Decimal::ZERO
            || (p.fract() != Decimal::ZERO
                && p.normalize().mantissa().unsigned_abs().to_string().len() > 5)
        {
            return Err(fail("price precision/sign"));
        }
        units(p, self.tick())
    }
    pub fn size(&self, v: &Value) -> Result<i64> {
        let n = units(dec(v)?, self.lot())?;
        if n <= 0 {
            return Err(fail("nonpositive size"));
        }
        Ok(n)
    }
    pub fn from_meta(meta: &Value, symbol: &str) -> Result<Self> {
        let found: Vec<_> = rows(&meta["universe"])?
            .iter()
            .enumerate()
            .filter(|(_, r)| r["name"] == symbol)
            .collect();
        if found.len() != 1 || found[0].1["isDelisted"] == true {
            return Err(fail("unknown/delisted instrument"));
        }
        let n = number(&found[0].1["szDecimals"])?;
        if n > 6 {
            return Err(fail("unsupported precision"));
        }
        Ok(Self {
            symbol: symbol.into(),
            asset: found[0].0 as u64,
            sz_decimals: n as u32,
        })
    }
}
#[derive(Clone)]
pub struct Http {
    client: Client,
    base: String,
    pub recorder: Option<super::record::Recorder>,
    #[cfg(test)]
    test_transport: bool,
}
impl Http {
    pub fn new(mainnet: bool) -> Result<Self> {
        let mut builder = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(8));
        if let Ok(path) = std::env::var("SSL_CERT_FILE") {
            for certificate in reqwest::Certificate::from_pem_bundle(&std::fs::read(path)?)? {
                builder = builder.add_root_certificate(certificate);
            }
        }
        Ok(Self {
            client: builder.build()?,
            recorder: None,
            #[cfg(test)]
            test_transport: false,
            base: if mainnet {
                "https://api.hyperliquid.xyz"
            } else {
                "https://api.hyperliquid-testnet.xyz"
            }
            .into(),
        })
    }
    pub fn post(&self, path: &str, payload: &Value) -> Result<Value> {
        self.post_with_timeout(path, payload, Duration::from_secs(8))
    }
    fn post_with_timeout(&self, path: &str, payload: &Value, timeout: Duration) -> Result<Value> {
        let started = Instant::now();
        let started_ms = wall_ms();
        let allowed_mutation = self.base == "https://api.hyperliquid-testnet.xyz";
        #[cfg(test)]
        let allowed_mutation = allowed_mutation || self.test_transport;
        if !matches!(path, "/info" | "/exchange") || (path == "/exchange" && !allowed_mutation) {
            return Err(fail("mutation destination disabled"));
        }
        let response = self
            .client
            .post(format!("{}{path}", self.base))
            .timeout(timeout)
            .json(payload)
            .send()
            .map_err(|e| {
                std::io::Error::new(
                    if e.is_timeout() {
                        std::io::ErrorKind::TimedOut
                    } else {
                        std::io::ErrorKind::ConnectionAborted
                    },
                    "HTTP transport; outcome may be unknown",
                )
            })?;
        if !response.status().is_success() {
            let status = response.status().as_u16();
            if let Some(recorder) = &self.recorder {
                recorder.http_error(
                    path,
                    payload,
                    status,
                    started_ms,
                    started.elapsed().as_nanos() as u64,
                )?;
            }
            return Err(fail(&format!("HTTP {status}; outcome may be unknown")));
        }
        let mut raw = Vec::new();
        response
            .take(4 * 1024 * 1024 + 1)
            .read_to_end(&mut raw)
            .map_err(|_| {
                std::io::Error::new(
                    std::io::ErrorKind::ConnectionReset,
                    "HTTP body failure; outcome may be unknown",
                )
            })?;
        if raw.len() > 4 * 1024 * 1024 {
            return Err(fail("HTTP response cap"));
        }
        let value = serde_json::from_slice(&raw).map_err(|_| fail("HTTP JSON schema"))?;
        if let Some(recorder) = &self.recorder {
            recorder.response(
                path,
                payload,
                &value,
                started_ms,
                started.elapsed().as_nanos() as u64,
            )?;
        }
        Ok(value)
    }
    pub fn info(&self, kind: &str, mut args: Value) -> Result<Value> {
        args["type"] = kind.into();
        self.post("/info", &args)
    }
    pub fn quote(&self, instrument: &Instrument) -> Result<(i64, i64)> {
        parse_quote(
            instrument,
            &self.info("l2Book", json!({"coin":instrument.symbol}))?,
            wall_ms(),
        )
    }
}
/// Existing H1 diagnostic/closeout collar: 0.1%, rounded outward to whole USD.
/// Strategy resting limits are unchanged. Admission still enforces the USD40 cap.
pub fn closeout_limit(instrument: &Instrument, ticks: i64, buy: bool) -> Result<i64> {
    if ticks <= 0 {
        return Err(fail("nonpositive closeout reference"));
    }
    let reference = Decimal::from(ticks) * instrument.tick();
    let price = reference * Decimal::new(if buy { 1001 } else { 999 }, 3);
    instrument.price(&json!(wire(if buy { price.ceil() } else { price.floor() })))
}
pub fn parse_quote(i: &Instrument, book: &Value, now: u64) -> Result<(i64, i64)> {
    if book["coin"] != i.symbol {
        return Err(fail("wrong book instrument"));
    }
    fresh(&book["time"], now)?;
    let bid = i.price(&book["levels"][0][0]["px"])?;
    let ask = i.price(&book["levels"][1][0]["px"])?;
    if bid > ask {
        return Err(fail("crossed book"));
    }
    Ok((bid, ask))
}
pub fn unique(input: &[Value], funding: bool) -> Result<Vec<Value>> {
    let mut seen = BTreeMap::new();
    for row in input {
        let key = if funding {
            format!(
                "{}:{}:{}",
                number(&row["time"])?,
                text(&row["hash"])?,
                text(&row["delta"]["coin"])?
            )
        } else {
            number(&row["tid"])?.to_string()
        };
        if seen.get(&key).is_some_and(|v| v != row) {
            return Err(fail("changed duplicate venue fact"));
        }
        seen.insert(key, row.clone());
    }
    Ok(seen.into_values().collect())
}
#[derive(Debug, Serialize)]
pub struct Ledger {
    #[serde(serialize_with = "decimal_string")]
    pub position: Decimal,
    #[serde(serialize_with = "decimal_string")]
    pub entry: Decimal,
    #[serde(serialize_with = "decimal_string")]
    pub realized: Decimal,
    #[serde(serialize_with = "decimal_string")]
    pub fees: Decimal,
    #[serde(serialize_with = "decimal_string")]
    pub funding: Decimal,
    #[serde(serialize_with = "decimal_string")]
    pub cash_change: Decimal,
}
fn decimal_string<S: serde::Serializer>(v: &Decimal, s: S) -> std::result::Result<S::Ok, S::Error> {
    s.serialize_str(&wire(*v))
}
pub fn account_ledger(symbol: &str, fills: &[Value], funding: &[Value]) -> Result<Ledger> {
    let fills = chronological_fills(fills)?;
    let mut out = Ledger {
        position: Decimal::ZERO,
        entry: Decimal::ZERO,
        realized: Decimal::ZERO,
        fees: Decimal::ZERO,
        funding: Decimal::ZERO,
        cash_change: Decimal::ZERO,
    };
    for f in &fills {
        number(&f["time"])?;
        number(&f["oid"])?;
        if f["coin"] != symbol
            || f["feeToken"] != "USDC"
            || dec(&f["startPosition"])? != out.position
        {
            return Err(fail("foreign fill/fee or position gap"));
        }
        let qty = dec(&f["sz"])?;
        let px = dec(&f["px"])?;
        if qty <= Decimal::ZERO || px <= Decimal::ZERO {
            return Err(fail("fill amount"));
        }
        let sign = match text(&f["side"])? {
            "B" => Decimal::ONE,
            "A" => -Decimal::ONE,
            _ => return Err(fail("fill side")),
        };
        let delta = qty * sign;
        let closing = out.position != Decimal::ZERO
            && out.position.is_sign_positive() != delta.is_sign_positive();
        let pnl = if closing {
            out.position.abs().min(qty)
                * (px - out.entry)
                * if out.position > Decimal::ZERO {
                    Decimal::ONE
                } else {
                    -Decimal::ONE
                }
        } else {
            Decimal::ZERO
        };
        let reported = dec(&f["closedPnl"])?;
        if (pnl - reported).abs() > Decimal::new(1, 6) {
            return Err(fail("realized PnL disagreement"));
        }
        let after = out.position + delta;
        if out.position == Decimal::ZERO
            || out.position.is_sign_positive() == delta.is_sign_positive()
        {
            out.entry = (out.position.abs() * out.entry + qty * px) / after.abs();
        } else if after == Decimal::ZERO {
            out.entry = Decimal::ZERO;
        } else if after.is_sign_positive() != out.position.is_sign_positive() {
            out.entry = px;
        }
        out.position = after;
        out.realized += reported;
        out.fees += dec(&f["fee"])?;
    }
    for f in unique(funding, true)? {
        if f["delta"]["coin"] != symbol || f["delta"]["type"] != "funding" {
            return Err(fail("foreign funding"));
        }
        out.funding += dec(&f["delta"]["usdc"])?;
    }
    out.cash_change = out.realized - out.fees + out.funding;
    Ok(out)
}
/// Trade IDs are opaque, not sequence numbers. Within one exchange millisecond
/// require a unique position chain; never guess when multiple starts are possible.
fn chronological_fills(input: &[Value]) -> Result<Vec<Value>> {
    let mut groups: BTreeMap<u64, BTreeMap<Decimal, Value>> = BTreeMap::new();
    for row in unique(input, false)? {
        let group = groups.entry(number(&row["time"])?).or_default();
        if group.insert(dec(&row["startPosition"])?, row).is_some() {
            return Err(fail("ambiguous same-time fill chronology"));
        }
    }
    let mut position = Decimal::ZERO;
    let mut out = Vec::new();
    for mut group in groups.into_values() {
        while !group.is_empty() {
            let row = group
                .remove(&position)
                .ok_or_else(|| fail("fill position/time gap"))?;
            let qty = dec(&row["sz"])?;
            if qty <= Decimal::ZERO {
                return Err(fail("nonpositive chronological fill"));
            }
            position += match text(&row["side"])? {
                "B" => qty,
                "A" => -qty,
                _ => return Err(fail("fill side")),
            };
            out.push(row);
        }
    }
    Ok(out)
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Session {
    pub session: String,
    pub account: String,
    pub agent: String,
    pub started_ms: u64,
    pub baseline_cash: String,
    pub instrument: Instrument,
    pub max_position_lots: i64,
    pub max_notional: String,
    pub max_loss: String,
    #[serde(default)]
    pub nonce_floor: u64,
    #[serde(default)]
    pub risk_cash_floor: Option<String>,
}
impl Session {
    pub fn validate(&self) -> Result<()> {
        if let Some(floor) = &self.risk_cash_floor
            && dec(&json!(floor))? > dec(&json!(self.baseline_cash))?
        {
            return Err(fail("carried cumulative loss budget exhausted"));
        }
        if self.session.len() != 16
            || !self.session.bytes().all(|b| b.is_ascii_hexdigit())
            || self.instrument.symbol != "BTC"
            || self.instrument.sz_decimals != 5
            || self.max_position_lots != 20
            || dec(&json!(self.max_notional))? != Decimal::from(40)
            || dec(&json!(self.max_loss))? != Decimal::from(10)
            || dec(&json!(self.baseline_cash))? < Decimal::from(100)
            || self.started_ms == 0
        {
            return Err(fail("unsupported session configuration"));
        }
        for a in [&self.account, &self.agent] {
            if a.len() != 42
                || !a.starts_with("0x")
                || !a[2..].bytes().all(|b| b.is_ascii_hexdigit())
            {
                return Err(fail("invalid account/agent address"));
            }
        }
        Ok(())
    }
    pub fn cloid(&self, id: u64) -> String {
        format!("0x{}{:016x}", self.session, id)
    }
    pub fn risk_floor(&self) -> Result<Decimal> {
        let local = dec(&json!(self.baseline_cash))? - dec(&json!(self.max_loss))?;
        Ok(match &self.risk_cash_floor {
            Some(floor) => local.max(dec(&json!(floor))?),
            None => local,
        })
    }
}
// Volatile, session-local evidence. A restart/reconciliation rebuilds it from REST.
struct TerminalFact {
    order: VenueOrder,
    remote: Value,
    proof_sha256: String,
}
fn terminal_proof(order: &VenueOrder, remote: &Value, all: &[Value]) -> Result<String> {
    let oid = number(&remote["order"]["oid"])?;
    let selected: Vec<_> = all
        .iter()
        .filter(|f| f["oid"].as_u64() == Some(oid))
        .cloned()
        .collect();
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(&(
        order,
        remote,
        unique(&selected, false)?,
    ))?)))
}
pub struct Venue {
    pub http: Http,
    pub session: Session,
    signer: Signer,
    attempts: Attempts,
    pub owned_oids: BTreeSet<u64>,
    pub last_ledger: Option<Ledger>,
    last_request: Instant,
    resolve_unattempted: bool,
    terminal_facts: BTreeMap<u64, TerminalFact>,
}
impl Venue {
    fn locally_unsent(&self, core: &Core, id: u64) -> bool {
        if !self.resolve_unattempted {
            return false;
        }
        let o = &core.orders[&id];
        matches!(o.lifecycle, Lifecycle::Pending | Lifecycle::Rejected)
            && o.filled == 0
            && !core.fills.values().any(|f| f.order_id == id)
            && !self.attempts.entries.contains_key(&format!("order-{id}"))
    }
    pub fn enable_local_unsent_proof(&mut self) {
        self.resolve_unattempted = true;
    }
    pub fn open(session: Session, dir: &Path) -> Result<Self> {
        session.validate()?;
        if std::env::var("HL_NETWORK").as_deref() != Ok("testnet")
            || std::env::var("HL_MAIN_ADDRESS").ok().as_deref() != Some(&session.account)
            || std::env::var("HL_API_ADDRESS").ok().as_deref() != Some(&session.agent)
        {
            return Err(fail("Testnet session environment mismatch"));
        }
        let signer = Signer::new(
            &std::env::var("HL_API_PRIVATE_KEY").map_err(|_| fail("signing key missing"))?,
            &session.agent,
        )?;
        let mut v = Self {
            http: Http::new(false)?,
            session,
            signer,
            attempts: Attempts::open(&dir.join("actions.jsonl"))?,
            owned_oids: BTreeSet::new(),
            last_ledger: None,
            last_request: Instant::now() - Duration::from_secs(1),
            resolve_unattempted: false,
            terminal_facts: BTreeMap::new(),
        };
        v.attempts.carry_nonce(v.session.nonce_floor);
        v.check_binding()?;
        v.check_mode()?;
        v.check_leverage()?;
        Ok(v)
    }
    pub fn nonce_floor(&self) -> u64 {
        self.attempts.nonce_floor()
    }
    fn info(&mut self, kind: &str, args: Value) -> Result<Value> {
        self.info_until(kind, args, None)
    }
    fn info_until(
        &mut self,
        kind: &str,
        mut args: Value,
        deadline: Option<Instant>,
    ) -> Result<Value> {
        // Preserve the H1 pacing budget in the reference migration; network tuning is separate.
        let elapsed = self.last_request.elapsed();
        if elapsed < Duration::from_millis(100) {
            std::thread::sleep(Duration::from_millis(100) - elapsed);
        }
        self.last_request = Instant::now();
        let timeout = match deadline {
            Some(deadline) => deadline
                .checked_duration_since(Instant::now())
                .filter(|d| !d.is_zero())
                .ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "order visibility deadline; absence unresolved",
                    )
                })?,
            None => Duration::from_secs(8),
        };
        args["type"] = kind.into();
        self.http.post_with_timeout("/info", &args, timeout)
    }
    fn user(&self) -> Value {
        json!({"user":self.session.account})
    }
    pub fn check_binding(&mut self) -> Result<()> {
        let r = self.info("userRole", json!({"user":self.session.agent}))?;
        if r["role"] != "agent"
            || !text(&r["data"]["user"])?.eq_ignore_ascii_case(&self.session.account)
        {
            return Err(fail("agent/account binding mismatch"));
        }
        Ok(())
    }
    fn check_mode(&mut self) -> Result<()> {
        let args = self.user();
        let r = self.info("userAbstraction", args)?;
        if r != "default" && r != "disabled" {
            return Err(fail("unsupported account mode"));
        }
        Ok(())
    }
    fn check_leverage(&mut self) -> Result<()> {
        let r = self.info(
            "activeAssetData",
            json!({"user":self.session.account,"coin":self.session.instrument.symbol}),
        )?;
        if r["leverage"] != json!({"type":"cross","value":1}) {
            return Err(fail("cross leverage 1 required"));
        }
        Ok(())
    }
    pub fn account(&mut self) -> Result<Value> {
        let args = self.user();
        let s = self.info("clearinghouseState", args)?;
        fresh(&s["time"], wall_ms())?;
        for row in rows(&s["assetPositions"])? {
            let p = &row["position"];
            if row["type"] != "oneWay"
                || p["coin"] != self.session.instrument.symbol
                || p["leverage"] != json!({"type":"cross","value":1})
            {
                return Err(fail("foreign position/leverage"));
            }
        }
        if rows(&s["assetPositions"])?.len() > 1 {
            return Err(fail("multiple positions"));
        }
        Ok(s)
    }
    pub fn open_orders(&mut self) -> Result<Vec<Value>> {
        let args = self.user();
        Ok(rows(&self.info("openOrders", args)?)?.clone())
    }
    pub fn validate(&self, i: &Intent) -> Result<()> {
        self.validate_for_position(i, 0).map(|_| ())
    }
    fn validate_for_position(&self, i: &Intent, position_lots: i64) -> Result<bool> {
        let instrument = &self.session.instrument;
        if i.id == 0 || i.qty <= 0 || i.limit <= 0 {
            return Err(fail("intent fields"));
        }
        let qty = Decimal::from(i.qty) * instrument.lot();
        let price = Decimal::from(i.limit) * instrument.tick();
        instrument.size(&json!(wire(qty)))?;
        instrument.price(&json!(wire(price)))?;
        let notional = qty * price;
        let reducing = position_lots != 0
            && position_lots.signum() != i.side.sign()
            && i.qty.unsigned_abs() <= position_lots.unsigned_abs();
        if (notional < Decimal::from(10) && !reducing)
            || notional > dec(&json!(self.session.max_notional))?
        {
            return Err(fail("order notional outside cap/minimum"));
        }
        Ok(reducing)
    }
    fn admission(&mut self, i: &Intent) -> Result<(bool, u64)> {
        self.check_mode()?;
        self.check_leverage()?;
        let s = self.account()?;
        let position = position(&s)?;
        let lots = units(position, self.session.instrument.lot())?;
        let reducing = self.validate_for_position(i, lots)?;
        let change = i
            .qty
            .checked_mul(i.side.sign())
            .ok_or_else(|| fail("quantity overflow"))?;
        if lots.unsigned_abs().max(
            lots.checked_add(change)
                .ok_or_else(|| fail("position overflow"))?
                .unsigned_abs(),
        ) > self.session.max_position_lots as u64
        {
            return Err(fail("position cap"));
        }
        let nav = dec(&s["marginSummary"]["accountValue"])?;
        let used = dec(&s["marginSummary"]["totalMarginUsed"])?;
        if nav < Decimal::from(100)
            || nav < self.session.risk_floor()?
            || used < Decimal::ZERO
            || nav - used < dec(&json!(self.session.max_notional))? * Decimal::from(2)
        {
            return Err(fail("collateral/loss budget"));
        }
        if !self.open_orders()?.is_empty() {
            return Err(fail("open order reservation domain busy"));
        }
        Ok((reducing, number(&s["time"])?))
    }
    fn mutate(
        &mut self,
        action: &Action,
        key: &str,
        admission: Option<(u64, u64)>,
        permit: Option<&AtomicU8>,
    ) -> Result<Value> {
        if permit.is_some_and(|p| p.load(Ordering::Acquire) != 1) {
            return Err(fail("dispatch gate closed"));
        }
        self.check_binding()?;
        if let Some((observed, deadline)) = admission {
            fresh(&json!(observed), wall_ms())?;
            if wall_ms() >= deadline {
                return Err(fail("intent expired during admission"));
            }
        }
        let now = wall_ms();
        let expiry = now
            .checked_add(5000)
            .ok_or_else(|| fail("expiry overflow"))?
            .min(admission.map_or(u64::MAX, |(_, deadline)| deadline));
        let (nonce, expiry) = self.attempts.prepare_until(key, action, now, expiry)?;
        let signature = self.signer.sign(action, nonce, expiry)?;
        if wall_ms() >= expiry {
            return Err(fail("signature expired before transport; reconcile"));
        }
        if let Some((observed, deadline)) = admission {
            fresh(&json!(observed), wall_ms())?;
            if wall_ms() >= deadline {
                return Err(fail("intent expired during admission"));
            }
        }
        if permit.is_some_and(|p| p.load(Ordering::Acquire) != 1) {
            return Err(fail(
                "dispatch gate closed after durable attempt; reconcile",
            ));
        }
        let result=self.http.post("/exchange",&json!({"action":action,"nonce":nonce,"signature":signature,"vaultAddress":null,"expiresAfter":expiry}))?;
        if result["status"] != "ok" {
            return Err(fail("action rejected; reconcile without retry"));
        }
        let statuses = rows(&result["response"]["data"]["statuses"])?;
        if statuses.len() != 1 {
            return Err(fail("action response rejected/ambiguous"));
        }
        // This cancel rejection is not evidence of absence or terminality.
        // Only cancel() may accept it, after checking the original order below.
        if statuses[0].get("error").is_some()
            && !(matches!(action, Action::Cancel { .. })
                && cancel_terminal_race(&statuses[0], self.session.instrument.asset))
        {
            return Err(fail("action response rejected/ambiguous"));
        }
        Ok(statuses[0].clone())
    }
    pub fn submit(&mut self, i: &Intent) -> Result<()> {
        self.submit_guarded(i, None)
    }
    pub fn submit_guarded(&mut self, i: &Intent, permit: Option<&AtomicU8>) -> Result<()> {
        if wall_ms() >= i.valid_until {
            return Err(fail("intent expired before admission"));
        }
        let (reduce, time) = self.admission(i)?;
        let instrument = &self.session.instrument;
        let action = Action::order(WireOrder {
            a: instrument.asset,
            b: i.side == Side::Buy,
            p: wire(Decimal::from(i.limit) * instrument.tick()),
            s: wire(Decimal::from(i.qty) * instrument.lot()),
            r: reduce,
            t: LimitType {
                limit: TimeInForce { tif: "Gtc".into() },
            },
            c: self.session.cloid(i.id),
        });
        let status = self.mutate(
            &action,
            &format!("order-{}", i.id),
            Some((time, i.valid_until)),
            permit,
        )?;
        let ack = status
            .get("resting")
            .or_else(|| status.get("filled"))
            .ok_or_else(|| fail("unknown order acknowledgement"))?;
        self.owned_oids.insert(number(&ack["oid"])?);
        Ok(())
    }
    pub fn query(&mut self, i: &Intent) -> Result<Value> {
        // Acknowledgement and read-side visibility need not become observable together.
        // Retry only this read of the original cloid; never infer absence or resend an action.
        // The execution owner stays busy throughout, so no new strategy action is dispatched.
        let deadline = Instant::now() + Duration::from_secs(5);
        let r = loop {
            let mut response = self.info_until(
                "orderStatus",
                json!({"user":self.session.account,"oid":self.session.cloid(i.id)}),
                Some(deadline),
            )?;
            if response["status"] == "unknownOid" {
                // An opaque exchange OID discovered in original-cloid fills is
                // only a lookup hint. The full response must pass all terms below.
                let lo = self.session.started_ms;
                let hi = wall_ms();
                let facts=self.info_until("userFillsByTime",json!({"user":self.session.account,"startTime":lo,"endTime":hi,"aggregateByTime":false}),Some(deadline))?;
                let facts = rows(&facts)?;
                if facts.len() >= 2000 {
                    return Err(fail(
                        "lookup history saturated; full reconciliation required",
                    ));
                }
                let mut oid = None;
                for fill in unique(facts, false)? {
                    if number(&fill["time"])? < lo || number(&fill["time"])? > hi {
                        return Err(fail("lookup fact outside session window"));
                    }
                    if fill["cloid"] != self.session.cloid(i.id) {
                        continue;
                    }
                    if fill["coin"] != self.session.instrument.symbol
                        || fill["side"] != if i.side == Side::Buy { "B" } else { "A" }
                    {
                        return Err(fail("lookup fill identity mismatch"));
                    }
                    let candidate = number(&fill["oid"])?;
                    if oid.is_some_and(|old| old != candidate) {
                        return Err(fail("multiple venue IDs for original cloid"));
                    }
                    oid = Some(candidate);
                }
                if let Some(oid) = oid {
                    response = self.info_until(
                        "orderStatus",
                        json!({"user":self.session.account,"oid":oid}),
                        Some(deadline),
                    )?;
                }
            }
            if response["status"] == "order" {
                break response;
            }
            if response["status"] != "unknownOid" {
                return Err(fail("invalid order status response"));
            }
            if Instant::now() >= deadline {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "unknown order; absence unresolved",
                )
                .into());
            }
        };
        let result = &r["order"];
        let order = &result["order"];
        let instrument = &self.session.instrument;
        if order["cloid"] != self.session.cloid(i.id)
            || order["coin"] != instrument.symbol
            || order["side"] != if i.side == Side::Buy { "B" } else { "A" }
            || instrument.size(&order["origSz"])? != i.qty
            || instrument.price(&order["limitPx"])? != i.limit
        {
            return Err(fail("order identity/terms mismatch"));
        }
        lifecycle(text(&result["status"])?, 0)?;
        self.owned_oids.insert(number(&order["oid"])?);
        Ok(result.clone())
    }
    pub fn cancel(&mut self, i: &Intent) -> Result<()> {
        let r = self.query(i)?;
        if lifecycle(text(&r["status"])?, 0)?.terminal() {
            return Ok(());
        }
        let action = Action::cancel(self.session.instrument.asset, self.session.cloid(i.id));
        let response = self.mutate(&action, &format!("cancel-{}", i.id), None, None)?;
        if response != "success" && !cancel_terminal_race(&response, self.session.instrument.asset)
        {
            return Err(fail("unknown cancel acknowledgement"));
        }
        // An action acknowledgement may precede orderStatus visibility. Query;
        // never issue another cancellation or invent terminality from the ack.
        let started = Instant::now();
        loop {
            let status = self.query(i)?;
            if lifecycle(text(&status["status"])?, 0)?.terminal() {
                return Ok(());
            }
            if started.elapsed() >= Duration::from_secs(5) {
                return Err(fail("cancel not terminal; reconcile original session"));
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    pub fn history(&mut self, kind: &str) -> Result<Vec<Value>> {
        let mut windows = vec![(self.session.started_ms, wall_ms())];
        let mut all = Vec::new();
        let mut requests = 0;
        while let Some((lo, hi)) = windows.pop() {
            requests += 1;
            if requests > 64 || lo > hi {
                return Err(fail("history request/window cap"));
            }
            let mut args = json!({"user":self.session.account,"startTime":lo,"endTime":hi});
            if kind == "userFillsByTime" {
                args["aggregateByTime"] = false.into();
            }
            let page = self.info(kind, args)?;
            let page = rows(&page)?;
            for r in page {
                let t = number(&r["time"])?;
                if t < lo || t > hi {
                    return Err(fail("history outside window"));
                }
            }
            let limit = if kind == "userFillsByTime" { 2000 } else { 500 };
            if page.len() >= limit {
                if lo == hi {
                    return Err(fail("equal timestamp saturation"));
                }
                let mid = lo + (hi - lo) / 2;
                windows.extend([(mid + 1, hi), (lo, mid)]);
                continue;
            }
            all.extend(page.iter().cloned());
            if all.len() > 10000 {
                return Err(fail("history fact cap"));
            }
        }
        unique(&all, kind != "userFillsByTime")
    }
    pub fn collect(
        &mut self,
        i: &Intent,
        rows: &[Value],
    ) -> Result<(VenueOrder, Vec<Fill>, BTreeSet<u64>)> {
        let remote = self.query(i)?;
        collect_order(&self.session.instrument, i, &remote, rows)
    }
    pub fn poll(&mut self, core: &Core) -> Result<Vec<Report>> {
        if core.orders.is_empty() {
            return Ok(vec![]);
        }
        let mut observed = Vec::new();
        for attempt in 0..3 {
            observed.extend(self.history("userFillsByTime")?);
            // Preserve every observed fact across reads; a changed duplicate is fatal.
            let all = unique(&observed, false)?;
            match self.poll_orders(core, &all) {
                // Only missing cumulative fill visibility is retryable here.
                Err(error)
                    if attempt < 2
                        && error
                            .downcast_ref::<std::io::Error>()
                            .is_some_and(|e| e.kind() == std::io::ErrorKind::WouldBlock) => {}
                result => return result,
            }
        }
        unreachable!()
    }
    fn remember_terminal(&mut self, order: VenueOrder, remote: Value, all: &[Value]) -> Result<()> {
        if !order.lifecycle.terminal() {
            return Err(fail("cache requires terminal order"));
        }
        let proof_sha256 = terminal_proof(&order, &remote, all)?;
        self.terminal_facts.insert(
            order.intent.id,
            TerminalFact {
                order,
                remote,
                proof_sha256,
            },
        );
        Ok(())
    }
    fn poll_orders(&mut self, core: &Core, all: &[Value]) -> Result<Vec<Report>> {
        let mut reports = Vec::new();
        let mut owned = BTreeSet::new();
        for o in core.orders.values() {
            let raw = if let Some(fact) = self.terminal_facts.get(&o.intent.id) {
                if fact.order.intent != o.intent
                    || fact.order.filled != o.filled
                    || fact.order.lifecycle != o.lifecycle
                    || o.pending.is_some()
                    || o.uncertain
                    || terminal_proof(&fact.order, &fact.remote, all)? != fact.proof_sha256
                {
                    return Err(fail(
                        "terminal cache/core/history proof mismatch; full reconciliation required",
                    ));
                }
                fact.remote.clone()
            } else {
                self.query(&o.intent)?
            };
            let (remote, fills, ids) =
                collect_order(&self.session.instrument, &o.intent, &raw, all)?;
            owned.extend(ids);
            if remote.lifecycle.terminal()
                && remote.lifecycle == o.lifecycle
                && remote.filled == o.filled
                && o.pending.is_none()
                && !o.uncertain
                && fills
                    .iter()
                    .all(|f| core.fills.get(&f.execution_id) == Some(f))
            {
                self.remember_terminal(remote.clone(), raw, all)?;
            }
            for f in fills {
                if core.fills.get(&f.execution_id) != Some(&f) {
                    reports.push(Report::Fill(f));
                }
            }
            match remote.lifecycle {
                Lifecycle::Accepted | Lifecycle::Partial => {
                    reports.push(Report::Accepted { id: o.intent.id })
                }
                Lifecycle::Canceled => reports.push(Report::Canceled {
                    id: o.intent.id,
                    cumulative_filled: remote.filled,
                }),
                Lifecycle::Rejected => reports.push(Report::Rejected { id: o.intent.id }),
                _ => {}
            }
        }
        if owned
            != all
                .iter()
                .map(|f| number(&f["tid"]))
                .collect::<Result<BTreeSet<_>>>()?
        {
            return Err(fail("foreign/unowned fill during regular poll"));
        }
        Ok(reports)
    }
    fn check_ledger(&mut self, fills: &[Value]) -> Result<Ledger> {
        let funding = self.history("userFunding")?;
        let ledger = account_ledger(&self.session.instrument.symbol, fills, &funding)?;
        let account = self.account()?;
        let actual = dec(&account["marginSummary"]["accountValue"])? - unrealized(&account)?;
        if (actual - dec(&json!(self.session.baseline_cash))? - ledger.cash_change).abs()
            > Decimal::new(1, 6)
            || position(&account)? != ledger.position
        {
            return Err(fail("account position/cash/funding discrepancy"));
        }
        Ok(ledger)
    }
    pub fn heartbeat(&mut self) -> Result<()> {
        let fills = self.history("userFillsByTime")?;
        for f in &fills {
            if !self.owned_oids.contains(&number(&f["oid"])?) {
                return Err(fail("foreign/unowned historical fill"));
            }
        }
        self.last_ledger = Some(self.check_ledger(&fills)?);
        Ok(())
    }
    pub fn reconcile(&mut self, core: &Core) -> Result<Reconciliation> {
        // Never let a cached status bypass the full terminal REST protocol.
        self.terminal_facts.clear();
        if core.orders.len() > 1000 {
            return Err(fail("session order cap"));
        }
        let known: BTreeSet<_> = core
            .orders
            .keys()
            .map(|id| self.session.cloid(*id))
            .collect();
        for o in self.open_orders()? {
            if o["coin"] != self.session.instrument.symbol || !known.contains(text(&o["cloid"])?) {
                return Err(fail("foreign open order"));
            }
            if core
                .orders
                .keys()
                .any(|id| self.locally_unsent(core, *id) && o["cloid"] == self.session.cloid(*id))
            {
                return Err(fail("venue order contradicts durable no-attempt proof"));
            }
        }
        for o in core.orders.values() {
            if !self.locally_unsent(core, o.intent.id) {
                self.cancel(&o.intent)?;
            }
        }
        let all = self.history("userFillsByTime")?;
        let mut orders = Vec::new();
        let mut fills = Vec::new();
        let mut owned = BTreeSet::new();
        let mut absent = Vec::new();
        for o in core.orders.values() {
            if self.locally_unsent(core, o.intent.id) {
                if o.lifecycle == Lifecycle::Pending {
                    absent.push(o.intent.id);
                } else {
                    orders.push(VenueOrder {
                        intent: o.intent.clone(),
                        filled: 0,
                        lifecycle: Lifecycle::Rejected,
                    });
                }
                continue;
            }
            let raw = self.query(&o.intent)?;
            let (remote, fs, ids) = collect_order(&self.session.instrument, &o.intent, &raw, &all)?;
            if !remote.lifecycle.terminal() {
                return Err(fail("account not quiescent"));
            }
            self.remember_terminal(remote.clone(), raw, &all)?;
            orders.push(remote);
            fills.extend(fs);
            owned.extend(ids);
        }
        let all_ids: Result<BTreeSet<u64>> = all.iter().map(|f| number(&f["tid"])).collect();
        if owned != all_ids? {
            return Err(fail("unowned historical fill"));
        }
        let ledger = self.check_ledger(&all)?;
        if !self.open_orders()?.is_empty() {
            return Err(fail("nonquiescent open orders"));
        }
        let position = units(ledger.position, self.session.instrument.lot())?;
        self.last_ledger = Some(ledger);
        Ok(Reconciliation {
            epoch: core.epoch,
            watermark: core.venue_seq,
            orders,
            fills,
            position,
            absent,
        })
    }
}
pub fn lifecycle(s: &str, filled: i64) -> Result<Lifecycle> {
    Ok(match s {
        "open" => {
            if filled > 0 {
                Lifecycle::Partial
            } else {
                Lifecycle::Accepted
            }
        }
        "filled" => Lifecycle::Filled,
        "rejected" => Lifecycle::Rejected,
        "canceled"
        | "marginCanceled"
        | "vaultWithdrawalCanceled"
        | "openInterestCapCanceled"
        | "selfTradeCanceled"
        | "reduceOnlyCanceled"
        | "siblingFilledCanceled"
        | "delistedCanceled"
        | "scheduledCancel" => Lifecycle::Canceled,
        _ => return Err(fail("unsupported lifecycle")),
    })
}
pub fn collect_order(
    instrument: &Instrument,
    i: &Intent,
    remote: &Value,
    all: &[Value],
) -> Result<(VenueOrder, Vec<Fill>, BTreeSet<u64>)> {
    let oid = number(&remote["order"]["oid"])?;
    let selected: Vec<_> = all
        .iter()
        .filter(|f| f["oid"].as_u64() == Some(oid))
        .cloned()
        .collect();
    let selected = unique(&selected, false)?;
    let mut fills = Vec::new();
    let mut ids = BTreeSet::new();
    let mut filled = 0i64;
    for f in selected {
        if f["coin"] != instrument.symbol
            || f["side"] != if i.side == Side::Buy { "B" } else { "A" }
        {
            return Err(fail("fill identity mismatch"));
        }
        let tid = number(&f["tid"])?;
        let id = tid
            .checked_add(1)
            .filter(|n| *n <= i64::MAX as u64)
            .ok_or_else(|| fail("fill ID overflow"))?;
        let qty = instrument.size(&f["sz"])?;
        filled = filled
            .checked_add(qty)
            .ok_or_else(|| fail("cumulative overflow"))?;
        ids.insert(tid);
        fills.push(Fill {
            execution_id: id,
            order_id: i.id,
            qty,
            price: instrument.price(&f["px"])?,
        });
    }
    let remaining = units(dec(&remote["order"]["sz"])?, instrument.lot())?;
    if remaining < 0 || remaining > i.qty || filled > i.qty {
        return Err(fail(&format!(
            "cumulative fill history mismatch: order={} qty={} remaining={remaining} observed={filled}",
            i.id, i.qty
        )));
    }
    if filled != i.qty - remaining {
        return Err(std::io::Error::new(
            std::io::ErrorKind::WouldBlock,
            format!(
                "cumulative fill history incomplete: order={} expected={} observed={filled}",
                i.id,
                i.qty - remaining
            ),
        )
        .into());
    }
    let lifecycle = lifecycle(text(&remote["status"])?, filled)?;
    if (lifecycle == Lifecycle::Filled && filled != i.qty)
        || (lifecycle == Lifecycle::Rejected && filled != 0)
    {
        return Err(fail("terminal quantity mismatch"));
    }
    Ok((
        VenueOrder {
            intent: i.clone(),
            filled,
            lifecycle,
        },
        fills,
        ids,
    ))
}
pub fn position(account: &Value) -> Result<Decimal> {
    let p = rows(&account["assetPositions"])?;
    if p.is_empty() {
        Ok(Decimal::ZERO)
    } else {
        dec(&p[0]["position"]["szi"])
    }
}
pub fn unrealized(account: &Value) -> Result<Decimal> {
    let p = rows(&account["assetPositions"])?;
    if p.is_empty() {
        Ok(Decimal::ZERO)
    } else {
        dec(&p[0]["position"]["unrealizedPnl"])
    }
}

#[cfg(test)]
#[path = "venue_tests.rs"]
mod tests;
