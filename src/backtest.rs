//! Non-durable backtest: the production Core and PaperExchange, driven from a bar
//! file and a target series, with no journal, fsync or IPC.
//!
//! Each bar is one market step (Quote, Trade, Heartbeat, Tick) followed by the
//! execution plan from `plan`. The Python `TargetExecutor` builds the identical
//! plan from the same state, so a Python strategy can run either interactively
//! (`mininautilus sim`, one round trip per bar) or, when its targets do not depend
//! on fills, as a precomputed target series here (tests/test_backtest.py).
use crate::{core::Core, model::*, sim::PaperExchange};
use serde::Serialize;
use std::io::{self, BufRead};

/// One price observation in integer ticks/lots. `taker` is the aggressor side of
/// the bar's volume; OHLC data has no aggressor, so converters must choose a rule.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Bar {
    pub at: Time,
    pub price: i64,
    pub volume: i64,
    pub taker: Side,
}

/// Execution policy shared with python/mininautilus/backtest.py.
#[derive(Clone, Debug)]
pub struct Policy {
    /// Buy limit = price + offset, sell limit = price - offset (at least 1 tick).
    pub limit_offset: i64,
    /// Submitted intents expire after this many ms of engine time.
    pub order_ttl_ms: u64,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            limit_offset: 0,
            order_ttl_ms: 60_000,
        }
    }
}

pub fn market_events(bar: &Bar, epoch: u64) -> [Event; 4] {
    [
        Event::Quote {
            bid: bar.price,
            ask: bar.price,
        },
        Event::Trade {
            taker: bar.taker,
            price: bar.price,
            qty: bar.volume,
        },
        // The simulated venue is connected; without this the private-stream
        // staleness gate would close on quiet bars.
        Event::Heartbeat { epoch },
        Event::Tick,
    ]
}

/// Decisions after a bar closes, computed only from the state before any of them
/// is applied: set a changed target, cancel every open order (one-bar time in
/// force), then submit the remaining delta. Core revalidates every event.
pub fn plan(core: &Core, bar: &Bar, target: Option<i64>, policy: &Policy) -> Vec<Event> {
    let mut events = Vec::new();
    let Some(position) = target else {
        return events;
    };
    let revision = match &core.target {
        Some(current) if current.position == position => current.revision,
        _ => {
            let revision = core.last_target_revision + 1;
            events.push(Event::SetTarget(Target {
                revision,
                position,
                valid_until: Time::MAX,
            }));
            revision
        }
    };
    for (id, order) in core.open_orders() {
        if order.pending != Some(PendingAction::Cancel) {
            events.push(Event::Cancel { id: *id });
        }
    }
    let delta = position as i128 - core.position as i128;
    if delta != 0 {
        let side = if delta > 0 { Side::Buy } else { Side::Sell };
        events.push(Event::SubmitTargeted {
            intent: Intent {
                id: core
                    .orders
                    .last_key_value()
                    .map_or(0, |(id, _)| *id)
                    .max(core.id_floor)
                    + 1,
                side,
                qty: delta.unsigned_abs().min(i64::MAX as u128) as i64,
                limit: (bar.price + side.sign() * policy.limit_offset).max(1),
                based_on_seq: core.seq,
                valid_until: bar.at.saturating_add(policy.order_ttl_ms),
            },
            revision,
            expected_position: core.position,
        });
    }
    events
}

#[derive(Clone, Debug, Serialize)]
pub struct LedgerFill {
    pub bar: usize,
    pub at: Time,
    pub order_id: OrderId,
    pub side: Side,
    pub qty: i64,
    pub price: i64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Summary {
    pub bars: usize,
    pub events: u64,
    pub orders: usize,
    pub fills: usize,
    pub cancels: u64,
    pub refusals: u64,
    pub alerts: u64,
    pub position: i64,
    /// i128 values as strings: JSON consumers may parse numbers as f64.
    pub cash_tick_lots: String,
    pub gross_equity_tick_lots: String,
    pub max_drawdown_tick_lots: String,
    pub health: Option<Health>,
}

pub struct Backtest {
    pub core: Core,
    pub venue: PaperExchange,
    pub policy: Policy,
    pub ledger: Option<Vec<LedgerFill>>,
    summary: Summary,
    peak: i128,
    drawdown: i128,
}

impl Backtest {
    pub fn new(config: Config, policy: Policy, keep_ledger: bool) -> Result<Self, String> {
        Ok(Self {
            core: Core::new(config)?,
            venue: PaperExchange::new(),
            policy,
            ledger: keep_ledger.then(Vec::new),
            summary: Summary::default(),
            peak: 0,
            drawdown: 0,
        })
    }

    fn apply(&mut self, at: Time, event: Event, index: usize) -> Result<(), String> {
        let seq = self.core.seq + 1;
        // Ledger rows only for fills this event newly records (not duplicates).
        let fill = match &event {
            Event::Execution {
                report: Report::Fill(fill),
                ..
            } if !self.core.fills.contains_key(&fill.execution_id) => Some(fill.clone()),
            _ => None,
        };
        let trade = match event {
            Event::Trade { taker, price, qty } => Some((taker, price, qty)),
            _ => None,
        };
        let effects = self.core.apply(&Envelope { seq, at, event })?;
        if let (Some(fill), Some(ledger)) = (fill, self.ledger.as_mut())
            && self.core.fills.contains_key(&fill.execution_id)
        {
            ledger.push(LedgerFill {
                bar: index,
                at,
                order_id: fill.order_id,
                side: self.core.orders[&fill.order_id].intent.side,
                qty: fill.qty,
                price: fill.price,
            });
        }
        self.route(at, effects, index)?;
        if let Some((taker, price, qty)) = trade {
            for report in self.venue.trade(self.core.epoch, taker, price, qty)? {
                self.apply(at, report, index)?;
            }
        }
        Ok(())
    }

    /// Count effects, send them to the paper venue, and apply its reports.
    fn route(&mut self, at: Time, effects: Vec<Effect>, index: usize) -> Result<(), String> {
        let mut reports = Vec::new();
        for effect in &effects {
            match effect {
                Effect::Refused { .. } | Effect::SignalRefused { .. } => self.summary.refusals += 1,
                Effect::Alert(_) => self.summary.alerts += 1,
                Effect::SendCancel { .. } => self.summary.cancels += 1,
                _ => {}
            }
            self.venue
                .execute_into(self.core.epoch, effect, &mut reports)?;
        }
        for report in reports {
            self.apply(at, report, index)?;
        }
        Ok(())
    }

    /// Market step for bar `index`, then the plan for `target` at that bar.
    pub fn step(&mut self, index: usize, bar: &Bar, target: Option<i64>) -> Result<(), String> {
        for event in market_events(bar, self.core.epoch) {
            // Market events never produce effects; only the trade can generate
            // venue reports. Skip the general effect/report routing for them.
            let trade = match event {
                Event::Trade { taker, price, qty } => Some((taker, price, qty)),
                _ => None,
            };
            let seq = self.core.seq + 1;
            let effects = self.core.apply(&Envelope {
                seq,
                at: bar.at,
                event,
            })?;
            if !effects.is_empty() {
                // An invalid value gates; count and route like any other event.
                self.route(bar.at, effects, index)?;
            }
            if let Some((taker, price, qty)) = trade {
                for report in self.venue.trade(self.core.epoch, taker, price, qty)? {
                    self.apply(bar.at, report, index)?;
                }
            }
        }
        let equity = self.core.cash + self.core.position as i128 * bar.price as i128;
        self.peak = self.peak.max(equity);
        self.drawdown = self.drawdown.max(self.peak - equity);
        for event in plan(&self.core, bar, target, &self.policy) {
            self.apply(bar.at, event, index)?;
        }
        self.summary.bars = index + 1;
        Ok(())
    }

    pub fn summary(&self, last_price: i64) -> Summary {
        Summary {
            events: self.core.seq,
            orders: self.core.orders.len(),
            fills: self.core.fills.len(),
            position: self.core.position,
            cash_tick_lots: self.core.cash.to_string(),
            gross_equity_tick_lots: (self.core.cash
                + self.core.position as i128 * last_price as i128)
                .to_string(),
            max_drawdown_tick_lots: self.drawdown.to_string(),
            health: Some(self.core.health),
            ..self.summary.clone()
        }
    }
}

fn invalid(line: usize, reason: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, format!("line {line}: {reason}"))
}

/// Binary bar file: this 8-byte magic, then 32-byte little-endian records
/// `at: u64, price: i64, volume: i64, taker: u8 (0 Buy, 1 Sell), 7 zero bytes`.
/// Same validation as the CSV; loading 2.6M bars takes milliseconds, not a second.
pub const BARS_MAGIC: &[u8; 8] = b"MNBARS1\0";
const RECORD: usize = 32;

/// Read either format: binary when the file starts with `BARS_MAGIC`, else CSV.
pub fn load_bars(path: &std::path::Path) -> io::Result<Vec<Bar>> {
    let mut file = std::fs::File::open(path)?;
    let mut magic = [0; 8];
    let binary = io::Read::read_exact(&mut file, &mut magic).is_ok() && &magic == BARS_MAGIC;
    if !binary {
        return read_bars(io::BufReader::new(std::fs::File::open(path)?));
    }
    // Decode in chunks: no second whole-file buffer beside the bars.
    let length = file.metadata()?.len() as usize - BARS_MAGIC.len();
    if !length.is_multiple_of(RECORD) {
        return Err(invalid(
            0,
            "binary bar file length is not a whole record count",
        ));
    }
    let mut bars = Vec::with_capacity(length / RECORD);
    let mut chunk = vec![0; RECORD * 8192];
    let mut remaining = length;
    while remaining > 0 {
        let take = remaining.min(chunk.len());
        io::Read::read_exact(&mut file, &mut chunk[..take])?;
        decode_into(&chunk[..take], &mut bars)?;
        remaining -= take;
    }
    Ok(bars)
}

pub fn decode_bars(body: &[u8]) -> io::Result<Vec<Bar>> {
    if !body.len().is_multiple_of(RECORD) {
        return Err(invalid(
            0,
            "binary bar file length is not a whole record count",
        ));
    }
    let mut bars = Vec::with_capacity(body.len() / RECORD);
    decode_into(body, &mut bars)?;
    Ok(bars)
}

fn decode_into(body: &[u8], bars: &mut Vec<Bar>) -> io::Result<()> {
    let offset = bars.len();
    for (index, record) in body.as_chunks::<RECORD>().0.iter().enumerate() {
        let line = offset + index + 1;
        let field = |i: usize| i64::from_le_bytes(record[i * 8..i * 8 + 8].try_into().unwrap());
        let (at, price, volume) = (field(0), field(1), field(2));
        let taker = match record[24] {
            0 => Side::Buy,
            1 => Side::Sell,
            _ => return Err(invalid(line, "taker byte must be 0 or 1")),
        };
        if at < 0 || price <= 0 || volume <= 0 || record[25..].iter().any(|b| *b != 0) {
            return Err(invalid(
                line,
                "require at >= 0, price > 0, volume > 0, zero padding",
            ));
        }
        if bars.last().is_some_and(|b| b.at >= at as u64) {
            return Err(invalid(line, "bar times must strictly increase"));
        }
        bars.push(Bar {
            at: at as u64,
            price,
            volume,
            taker,
        });
    }
    Ok(())
}

pub fn encode_bars(bars: &[Bar]) -> Vec<u8> {
    let mut out = Vec::with_capacity(BARS_MAGIC.len() + bars.len() * RECORD);
    out.extend_from_slice(BARS_MAGIC);
    for bar in bars {
        out.extend_from_slice(&bar.at.to_le_bytes());
        out.extend_from_slice(&bar.price.to_le_bytes());
        out.extend_from_slice(&bar.volume.to_le_bytes());
        out.push(u8::from(bar.taker == Side::Sell));
        out.extend_from_slice(&[0; 7]);
    }
    out
}

/// CSV `at,price,volume,taker` with a header row; `at` strictly increasing.
pub fn read_bars(mut input: impl BufRead) -> io::Result<Vec<Bar>> {
    let mut bars: Vec<Bar> = Vec::new();
    // One reused line buffer: millions of bars without a String per line.
    let mut buffer = String::new();
    let mut index = 0;
    loop {
        buffer.clear();
        if input.read_line(&mut buffer)? == 0 {
            break;
        }
        index += 1;
        let index = index - 1;
        let line = buffer.trim_end_matches(['\n', '\r']);
        if index == 0 {
            if line.trim() != "at,price,volume,taker" {
                return Err(invalid(1, "header must be at,price,volume,taker"));
            }
            continue;
        }
        if line.is_empty() {
            continue;
        }
        let mut fields = line.split(',');
        let mut next = |name| fields.next().ok_or_else(|| invalid(index + 1, name));
        let number = |text: &str, name| {
            text.trim()
                .parse::<i64>()
                .map_err(|_| invalid(index + 1, name))
        };
        let at = number(next("at")?, "at")?;
        let price = number(next("price")?, "price")?;
        let volume = number(next("volume")?, "volume")?;
        let taker = match next("taker")?.trim() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            _ => return Err(invalid(index + 1, "taker must be Buy or Sell")),
        };
        if at < 0 || price <= 0 || volume <= 0 {
            return Err(invalid(index + 1, "require at >= 0, price > 0, volume > 0"));
        }
        if bars.last().is_some_and(|b| b.at >= at as u64) {
            return Err(invalid(index + 1, "bar times must strictly increase"));
        }
        bars.push(Bar {
            at: at as u64,
            price,
            volume,
            taker,
        });
    }
    Ok(bars)
}

/// CSV `bar,position` with a header: position from that bar index onward.
/// Rows must be in increasing bar order; before the first row there is no target.
pub fn read_targets(input: impl BufRead, bars: usize) -> io::Result<Vec<(usize, i64)>> {
    let mut targets: Vec<(usize, i64)> = Vec::new();
    for (index, line) in input.lines().enumerate() {
        let line = line?;
        if index == 0 {
            if line.trim() != "bar,position" {
                return Err(invalid(1, "header must be bar,position"));
            }
            continue;
        }
        if line.is_empty() {
            continue;
        }
        let (bar, position) = line
            .split_once(',')
            .ok_or_else(|| invalid(index + 1, "expected bar,position"))?;
        let bar: usize = bar.trim().parse().map_err(|_| invalid(index + 1, "bar"))?;
        let position: i64 = position
            .trim()
            .parse()
            .map_err(|_| invalid(index + 1, "position"))?;
        if bar >= bars || targets.last().is_some_and(|(b, _)| *b >= bar) {
            return Err(invalid(index + 1, "bar out of range or not increasing"));
        }
        targets.push((bar, position));
    }
    Ok(targets)
}

/// Run every bar with a sparse target series (changes only).
pub fn run(
    bars: &[Bar],
    targets: &[(usize, i64)],
    config: Config,
    policy: Policy,
    keep_ledger: bool,
) -> Result<(Backtest, Summary), String> {
    let mut backtest = Backtest::new(config, policy, keep_ledger)?;
    let mut current = None;
    let mut changes = targets.iter().peekable();
    for (index, bar) in bars.iter().enumerate() {
        while let Some((_, position)) = changes.next_if(|(b, _)| *b == index) {
            current = Some(*position);
        }
        backtest.step(index, bar, current)?;
    }
    let summary = backtest.summary(bars.last().map_or(0, |b| b.price));
    Ok((backtest, summary))
}
