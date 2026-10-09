use crate::model::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

mod transition;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
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
        })
    }

    /// Prepare a transition without changing published state, then commit it.
    /// Quote/Trade preparation never copies order or execution history.
    pub fn apply(&mut self, input: &Envelope) -> Result<Vec<Effect>, String> {
        Ok(self.prepare(input)?.commit())
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
        for order in self.orders.values() {
            match order.intent.side {
                Side::Buy => hi += order.remaining() as i128,
                Side::Sell => lo -= order.remaining() as i128,
            }
        }
        (lo, hi)
    }

    fn refusal(&self, intent: &Intent, seq: u64, now: Time) -> Option<&'static str> {
        if self.orders.contains_key(&intent.id) {
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
        if self
            .orders
            .values()
            .any(|o| !o.lifecycle.terminal() || o.uncertain)
        {
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
        if rebuilt.position != snapshot.position {
            return Err("venue position disagrees with fills".into());
        }
        Ok(rebuilt)
    }
}
