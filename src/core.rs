use crate::model::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

mod transition;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
// Derived (inherent) functions; the trait impls below rebuild the order index.
#[serde(remote = "Self")]
pub struct Core {
    pub config: Config,
    pub seq: u64,
    pub now: Time,
    pub health: Health,
    pub killed: bool,
    pub epoch: u64,
    pub venue_seq: u64,
    pub reconciled_through: u64,
    pub last_private_at: Time,
    pub quote: Option<(i64, i64, Time)>,
    pub orders: BTreeMap<OrderId, Order>,
    pub fills: BTreeMap<u64, Fill>,
    pub position: i64,
    #[serde(default)]
    pub target: Option<Target>,
    #[serde(default)]
    pub last_target_revision: u64,
    /// Tick-lot cash, without fees; i128 prevents multiplying two i64s from overflowing.
    pub cash: i128,
    /// Session opening balances carried over by journal rotation (H4). Venue
    /// reconciliation positions are relative to the session's opening position.
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub opening_position: i64,
    #[serde(default, skip_serializing_if = "is_zero_i128")]
    pub opening_cash: i128,
    /// Client order IDs up to this value belong to rotated-out sessions.
    #[serde(default, skip_serializing_if = "is_zero_u64")]
    pub id_floor: OrderId,
    /// Derived from `orders`; never serialized. Code that mutates `orders` directly
    /// (outside transitions) must call `reindex` before applying further events.
    #[serde(skip)]
    index: OrderIndex,
    /// Opt-in record of written order/fill IDs for compact transport responses.
    #[serde(skip)]
    changes: ChangeLog,
}

/// Order and fill IDs written since the last `take_changes`. `replaced` means a
/// full reconciliation swapped the history; observers must reload full state.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Changes {
    pub orders: BTreeSet<OrderId>,
    pub fills: BTreeSet<u64>,
    pub replaced: bool,
}

/// Observer bookkeeping, not trading state: excluded from equality and serde.
#[derive(Clone, Debug, Default)]
struct ChangeLog(Option<Changes>);
impl PartialEq for ChangeLog {
    fn eq(&self, _: &Self) -> bool {
        true
    }
}
impl Eq for ChangeLog {}

fn is_zero_i64(v: &i64) -> bool {
    *v == 0
}
fn is_zero_i128(v: &i128) -> bool {
    *v == 0
}
fn is_zero_u64(v: &u64) -> bool {
    *v == 0
}

/// Account state a rotated journal starts from. Rotation requires a healthy,
/// fully resolved book, so no order, fill or reservation needs to carry over.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Carry {
    pub now: Time,
    pub position: i64,
    pub cash: i128,
    pub killed: bool,
    pub epoch: u64,
    pub last_target_revision: u64,
    pub id_floor: OrderId,
}

impl Serialize for Core {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        Core::serialize(self, serializer)
    }
}

impl<'de> Deserialize<'de> for Core {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let mut core = Core::deserialize(deserializer)?;
        core.reindex();
        Ok(core)
    }
}

/// Orders that can still reserve exposure: not terminal, or terminal but uncertain.
/// Exactly the orders for which `Order::remaining` may be non-zero.
fn is_open(order: &Order) -> bool {
    !order.lifecycle.terminal() || order.uncertain
}

/// Secondary indexes so risk, timers and barriers scan open orders, not history.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct OrderIndex {
    open: BTreeSet<OrderId>,
    deadlines: BTreeSet<(Time, OrderId)>,
}

impl OrderIndex {
    fn build(orders: &BTreeMap<OrderId, Order>) -> Self {
        let mut index = Self::default();
        for (id, order) in orders {
            index.add(*id, order);
        }
        index
    }

    fn add(&mut self, id: OrderId, order: &Order) {
        if is_open(order) {
            self.open.insert(id);
        }
        if let Some(deadline) = order.deadline {
            self.deadlines.insert((deadline, id));
        }
    }
}

impl Core {
    /// Only use a fresh core with a known empty paper account. Recovery uses a gate.
    pub fn new(config: Config) -> Result<Self, String> {
        if config.max_abs_position <= 0
            || config.max_order_qty <= 0
            || config.max_order_notional <= 0
            || config.request_timeout_ms == 0
            || config.market_stale_ms == 0
            || config.private_stale_ms == 0
        {
            return Err("invalid configuration".into());
        }
        Ok(Self {
            config,
            seq: 0,
            now: 0,
            health: Health::Healthy,
            killed: false,
            epoch: 0,
            venue_seq: 0,
            reconciled_through: 0,
            last_private_at: 0,
            quote: None,
            orders: BTreeMap::new(),
            fills: BTreeMap::new(),
            position: 0,
            target: None,
            last_target_revision: 0,
            cash: 0,
            opening_position: 0,
            opening_cash: 0,
            id_floor: 0,
            index: OrderIndex::default(),
            changes: ChangeLog::default(),
        })
    }

    /// A healthy book with nothing open: history can move to an archive.
    pub fn rotation_ready(&self) -> Result<(), &'static str> {
        if self.health != Health::Healthy {
            return Err("rotation requires a healthy, reconciled engine");
        }
        if !self.index.open.is_empty() {
            return Err("rotation requires every order to be terminal and certain");
        }
        Ok(())
    }

    pub fn carry(&self) -> Carry {
        Carry {
            now: self.now,
            position: self.position,
            cash: self.cash,
            killed: self.killed,
            epoch: self.epoch,
            last_target_revision: self.last_target_revision,
            id_floor: self
                .orders
                .last_key_value()
                .map_or(self.id_floor, |(id, _)| *id)
                .max(self.id_floor),
        }
    }

    /// The first state of a rotated journal: no history, carried balances and latch.
    pub fn from_carry(config: Config, carry: &Carry) -> Result<Self, String> {
        let mut core = Core::new(config)?;
        core.now = carry.now;
        core.last_private_at = carry.now;
        core.position = carry.position;
        core.cash = carry.cash;
        core.opening_position = carry.position;
        core.opening_cash = carry.cash;
        core.killed = carry.killed;
        core.epoch = carry.epoch;
        core.last_target_revision = carry.last_target_revision;
        core.id_floor = carry.id_floor;
        Ok(core)
    }

    /// Start recording written order/fill IDs. Off by default so in-process
    /// backtests that never drain the log do not accumulate it.
    pub fn track_changes(&mut self) {
        self.changes.0.get_or_insert_with(Changes::default);
    }

    /// Drain the IDs written since the previous call; `None` when not tracking.
    pub fn take_changes(&mut self) -> Option<Changes> {
        self.changes.0.as_mut().map(std::mem::take)
    }

    /// Rebuild derived indexes after editing `orders` outside a transition.
    pub fn reindex(&mut self) {
        self.index = OrderIndex::build(&self.orders);
    }

    /// True when the derived indexes match a full scan of `orders` (test oracle).
    pub fn index_consistent(&self) -> bool {
        self.index == OrderIndex::build(&self.orders)
    }

    /// Orders that can still fill or reserve risk, in ID order. O(open), not O(history).
    pub fn open_orders(&self) -> impl Iterator<Item = (&OrderId, &Order)> {
        self.index.open.iter().map(|id| (id, &self.orders[id]))
    }

    /// Orders whose submit/cancel deadline is at or before `now`, in deadline order.
    fn expired_orders(&self, now: Time) -> impl Iterator<Item = (&OrderId, &Order)> {
        self.index
            .deadlines
            .range(..=(now, OrderId::MAX))
            .map(|(_, id)| (id, &self.orders[id]))
    }

    /// The only write path for `orders` inside transitions; keeps indexes in step.
    fn put_order(&mut self, id: OrderId, order: Order) {
        match self.orders.get_mut(&id) {
            Some(slot) => {
                // Touch the index only where membership actually changes.
                if is_open(slot) != is_open(&order) {
                    if is_open(&order) {
                        self.index.open.insert(id);
                    } else {
                        self.index.open.remove(&id);
                    }
                }
                if slot.deadline != order.deadline {
                    if let Some(due) = slot.deadline {
                        self.index.deadlines.remove(&(due, id));
                    }
                    if let Some(due) = order.deadline {
                        self.index.deadlines.insert((due, id));
                    }
                }
                *slot = order;
            }
            None => {
                self.index.add(id, &order);
                self.orders.insert(id, order);
            }
        }
        if let Some(changes) = &mut self.changes.0 {
            changes.orders.insert(id);
        }
    }

    fn put_fill(&mut self, fill: Fill) {
        if let Some(changes) = &mut self.changes.0 {
            changes.fills.insert(fill.execution_id);
        }
        self.fills.insert(fill.execution_id, fill);
    }

    fn replace_history(&mut self, rebuilt: Core) {
        self.orders = rebuilt.orders;
        self.fills = rebuilt.fills;
        self.index = rebuilt.index;
        if let Some(changes) = &mut self.changes.0 {
            *changes = Changes {
                replaced: true,
                ..Changes::default()
            };
        }
    }

    /// Prepare a transition without changing published state, then commit it.
    /// Quote/Trade preparation never copies order or execution history.
    pub fn apply(&mut self, input: &Envelope) -> Result<Vec<Effect>, String> {
        if self.apply_header_only(input) {
            return Ok(Vec::new());
        }
        Ok(self.prepare(input)?.commit())
    }

    /// In-place commit for valid market/heartbeat/idle-timer events, which change
    /// only fixed-size header fields and produce no effects. Once validated they
    /// cannot fail, so there is nothing to stage. Anything else (an invalid value,
    /// a due deadline, a stale private stream, a bad envelope) returns false and
    /// takes the general prepare/commit path, which owns all gating behaviour.
    /// The journal always uses prepare/commit; tests/transition_equivalence.rs
    /// checks this path against the frozen reference on every event.
    fn apply_header_only(&mut self, input: &Envelope) -> bool {
        if self.seq.checked_add(1) != Some(input.seq) || input.at < self.now {
            return false;
        }
        let now = input.at;
        match input.event {
            Event::Quote { bid, ask } if bid > 0 && ask >= bid => {
                self.quote = Some((bid, ask, now));
            }
            Event::Trade { price, qty, .. } if price > 0 && qty > 0 => {}
            Event::Heartbeat { epoch } => {
                if epoch == self.epoch && self.health != Health::Disconnected {
                    self.last_private_at = now;
                }
            }
            Event::Tick
                if self
                    .index
                    .deadlines
                    .first()
                    .is_none_or(|(due, _)| *due > now)
                    && !(self.health == Health::Healthy
                        && now - self.last_private_at > self.config.private_stale_ms) => {}
            _ => return false,
        }
        self.seq = input.seq;
        self.now = now;
        true
    }

    /// The exclusive borrow prevents state changes between prepare and commit.
    /// Dropping this value (for example after a journal failure) changes nothing.
    pub(crate) fn prepare(&mut self, input: &Envelope) -> Result<transition::Prepared<'_>, String> {
        transition::Prepared::new(self, input)
    }

    /// Independent buy/sell bounds: offsetting pending orders cannot net away risk.
    pub fn exposure_bounds(&self) -> (i128, i128) {
        let mut lo = self.position as i128;
        let mut hi = lo;
        for (_, order) in self.open_orders() {
            match order.intent.side {
                Side::Buy => hi += order.remaining() as i128,
                Side::Sell => lo -= order.remaining() as i128,
            }
        }
        (lo, hi)
    }

    fn refusal(&self, intent: &Intent, seq: u64, now: Time) -> Option<&'static str> {
        if (self.id_floor > 0 && intent.id <= self.id_floor) || self.orders.contains_key(&intent.id)
        {
            return Some("client order ID already used");
        }
        if self.killed || self.health != Health::Healthy {
            return Some("trading gate is closed");
        }
        if intent.id == 0 || intent.qty <= 0 || intent.limit <= 0 {
            return Some("invalid ticks/lots/ID");
        }
        if intent.qty > self.config.max_order_qty
            || intent.qty as i128 * intent.limit as i128 > self.config.max_order_notional as i128
        {
            return Some("order limit exceeded");
        }
        if intent.valid_until < now
            || intent.based_on_seq >= seq
            || (seq - 1).saturating_sub(intent.based_on_seq) > self.config.max_signal_lag
        {
            return Some("expired or stale strategy intent");
        }
        if self
            .quote
            .is_none_or(|(_, _, at)| now - at > self.config.market_stale_ms)
        {
            return Some("market data stale");
        }
        if now - self.last_private_at > self.config.private_stale_ms {
            return Some("private stream stale");
        }
        let (mut lo, mut hi) = self.exposure_bounds();
        match intent.side {
            Side::Buy => hi += intent.qty as i128,
            Side::Sell => lo -= intent.qty as i128,
        }
        let limit = self.config.max_abs_position as i128;
        if lo < -limit || hi > limit {
            return Some("position plus reservations exceeds limit");
        }
        None
    }

    fn targeted_refusal(
        &self,
        intent: &Intent,
        revision: u64,
        expected_position: i64,
        now: Time,
    ) -> Option<&'static str> {
        let Some(target) = &self.target else {
            return Some("no active target");
        };
        if target.revision != revision || target.valid_until < now {
            return Some("target superseded or expired");
        }
        if expected_position != self.position {
            return Some("position changed since intent calculation");
        }
        if !self.index.open.is_empty() {
            return Some("previous order unresolved; cancel/replace barrier closed");
        }
        let delta = target.position as i128 - self.position as i128;
        if delta == 0
            || intent.qty as i128 != delta.abs()
            || intent.side.sign() as i128 != delta.signum()
        {
            return Some("intent does not match current target delta");
        }
        None
    }

    fn fill(&mut self, fill: &Fill) -> Result<(), String> {
        if let Some(update) = transition::FillUpdate::prepare(self, fill)? {
            update.commit(self);
        }
        Ok(())
    }

    fn rebuild(&self, snapshot: &Reconciliation) -> Result<Core, String> {
        if self.health != Health::Reconciling
            || snapshot.epoch != self.epoch
            || snapshot.watermark < self.venue_seq
        {
            return Err("stale reconciliation or no active recovery".into());
        }
        let mut rebuilt = Core::new(self.config.clone())?;
        (rebuilt.position, rebuilt.cash) = (self.opening_position, self.opening_cash);
        (rebuilt.opening_position, rebuilt.opening_cash) =
            (self.opening_position, self.opening_cash);
        rebuilt.id_floor = self.id_floor;
        let mut ids = BTreeSet::new();
        for remote in &snapshot.orders {
            let local = self
                .orders
                .get(&remote.intent.id)
                .ok_or("unknown venue order; manual adoption required")?;
            if remote.intent != local.intent
                || !ids.insert(remote.intent.id)
                || remote.lifecycle == Lifecycle::Pending
                || remote.filled < 0
                || remote.filled > remote.intent.qty
            {
                return Err("invalid venue order history".into());
            }
            if local.lifecycle.terminal() && remote.lifecycle != local.lifecycle {
                return Err("snapshot regresses or changes a confirmed terminal order".into());
            }
            rebuilt.orders.insert(
                remote.intent.id,
                Order {
                    intent: remote.intent.clone(),
                    filled: 0,
                    lifecycle: Lifecycle::Accepted,
                    pending: None,
                    deadline: None,
                    uncertain: false,
                },
            );
        }
        // Proven never received: terminal before fills are applied, so a fill that
        // names one of these orders fails closed instead of reviving it.
        for id in &snapshot.absent {
            let local = self.orders.get(id).ok_or("absent order unknown locally")?;
            if !ids.insert(*id)
                || local.lifecycle != Lifecycle::Pending
                || local.filled != 0
                || self.fills.values().any(|f| f.order_id == *id)
            {
                return Err("venue reports absent an order it acknowledged or filled".into());
            }
            rebuilt.orders.insert(
                *id,
                Order {
                    intent: local.intent.clone(),
                    filled: 0,
                    lifecycle: Lifecycle::Rejected,
                    pending: None,
                    deadline: None,
                    uncertain: false,
                },
            );
        }
        if ids.len() != self.orders.len() {
            return Err(
                "local order absent from complete venue history; outcome unresolved".into(),
            );
        }
        for fill in &snapshot.fills {
            rebuilt.fill(fill)?;
        }
        for remote in &snapshot.orders {
            let order = rebuilt.orders.get_mut(&remote.intent.id).unwrap();
            if order.filled != remote.filled
                || (remote.lifecycle == Lifecycle::Filled && order.filled != order.intent.qty)
                || (remote.lifecycle == Lifecycle::Partial
                    && (order.filled == 0 || order.filled == order.intent.qty))
                || (matches!(remote.lifecycle, Lifecycle::Accepted | Lifecycle::Rejected)
                    && order.filled != 0)
                || (order.filled == order.intent.qty && remote.lifecycle != Lifecycle::Filled)
            {
                return Err("venue history does not balance".into());
            }
            order.lifecycle = remote.lifecycle;
        }
        for (id, fill) in &self.fills {
            if rebuilt.fills.get(id) != Some(fill) {
                return Err("snapshot omitted or changed a known fill".into());
            }
        }
        // Venue positions are session-relative (from the journal's genesis).
        if rebuilt.position as i128 - self.opening_position as i128 != snapshot.position as i128 {
            return Err("venue position disagrees with fills".into());
        }
        // Built during preparation so commit only moves the replacement in.
        rebuilt.reindex();
        Ok(rebuilt)
    }
}
