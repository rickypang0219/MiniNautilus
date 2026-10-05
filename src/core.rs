use crate::model::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

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
            cash: 0,
        })
    }

    /// Transactional reference implementation. Clone cost is intentional in v0.
    /// There is no I/O or wall clock access in this state machine.
    pub fn apply(&mut self, input: &Envelope) -> Result<Vec<Effect>, String> {
        if self.seq.checked_add(1) != Some(input.seq) || input.at < self.now {
            return Err("non-contiguous engine sequence or clock moved backwards".into());
        }
        let mut next = self.clone();
        next.seq = input.seq;
        next.now = input.at;
        let mut effects = Vec::new();
        if let Err(reason) = next.handle(&input.event, &mut effects) {
            // An invalid external report must not partially mutate accounting or
            // disappear silently. Consume it and gate the account for reconciliation.
            next = self.clone();
            next.seq = input.seq;
            next.now = input.at;
            effects.clear();
            next.gate(&reason, &mut effects);
        }
        *self = next;
        Ok(effects)
    }

    fn gate(&mut self, reason: &str, out: &mut Vec<Effect>) {
        if self.health != Health::Disconnected {
            self.health = Health::Reconciling;
        }
        out.push(Effect::Alert(reason.into()));
        out.push(Effect::QueryState { epoch: self.epoch });
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

    fn refusal(&self, intent: &Intent) -> Option<&'static str> {
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
        if intent.valid_until < self.now
            || intent.based_on_seq >= self.seq
            || (self.seq - 1).saturating_sub(intent.based_on_seq) > self.config.max_signal_lag
        {
            return Some("expired or stale strategy intent");
        }
        if self
            .quote
            .is_none_or(|(_, _, at)| self.now - at > self.config.market_stale_ms)
        {
            return Some("market data stale");
        }
        if self.now - self.last_private_at > self.config.private_stale_ms {
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

    fn handle(&mut self, event: &Event, out: &mut Vec<Effect>) -> Result<(), String> {
        match event {
            Event::Trade { price, qty, .. } => {
                if *price <= 0 || *qty <= 0 {
                    return Err("invalid market trade".into());
                }
            }
            Event::Quote { bid, ask } => {
                if *bid <= 0 || ask < bid {
                    return Err("invalid quote".into());
                }
                self.quote = Some((*bid, *ask, self.now));
            }
            Event::Submit(intent) => {
                if let Some(reason) = self.refusal(intent) {
                    out.push(Effect::Refused {
                        id: intent.id,
                        reason: reason.into(),
                    });
                } else {
                    let deadline = self
                        .now
                        .checked_add(self.config.request_timeout_ms)
                        .ok_or("deadline overflow")?;
                    self.orders.insert(
                        intent.id,
                        Order {
                            intent: intent.clone(),
                            filled: 0,
                            lifecycle: Lifecycle::Pending,
                            pending: Some(PendingAction::Submit),
                            deadline: Some(deadline),
                            uncertain: false,
                        },
                    );
                    out.push(Effect::SendOrder(intent.clone()));
                }
            }
            Event::Cancel { id } => {
                let Some(order) = self.orders.get_mut(id) else {
                    out.push(Effect::Refused {
                        id: *id,
                        reason: "unknown order".into(),
                    });
                    return Ok(());
                };
                if self.health == Health::Disconnected
                    || order.lifecycle.terminal()
                    || order.pending == Some(PendingAction::Cancel)
                {
                    out.push(Effect::Refused {
                        id: *id,
                        reason: "cancel unavailable or already pending".into(),
                    });
                } else {
                    order.pending = Some(PendingAction::Cancel);
                    order.deadline = Some(
                        self.now
                            .checked_add(self.config.request_timeout_ms)
                            .ok_or("deadline overflow")?,
                    );
                    out.push(Effect::SendCancel { id: *id });
                }
            }
            Event::Execution {
                epoch,
                venue_seq,
                report,
            } => {
                if *epoch != self.epoch || self.health == Health::Disconnected {
                    return Ok(());
                }
                if *venue_seq == 0 {
                    return Err("invalid venue sequence".into());
                }
                if *venue_seq <= self.reconciled_through {
                    return Ok(());
                }
                if *venue_seq > self.venue_seq.saturating_add(1) {
                    self.gate("private stream sequence gap", out);
                }
                self.venue_seq = self.venue_seq.max(*venue_seq);
                self.last_private_at = self.now;
                self.report(report, out)?;
            }
            Event::Heartbeat { epoch } => {
                if *epoch == self.epoch && self.health != Health::Disconnected {
                    self.last_private_at = self.now;
                }
            }
            Event::Tick => {
                let mut timed_out = false;
                for order in self.orders.values_mut() {
                    if order.deadline.is_some_and(|at| at <= self.now) {
                        order.uncertain = true;
                        order.deadline = None;
                        timed_out = true;
                    }
                }
                if timed_out {
                    self.gate("order action timeout; outcome unknown", out);
                }
                if self.health == Health::Healthy
                    && self.now - self.last_private_at > self.config.private_stale_ms
                {
                    self.gate("private stream heartbeat timeout", out);
                }
            }
            Event::Disconnect => {
                self.health = Health::Disconnected;
                self.quote = None;
                for order in self.orders.values_mut().filter(|o| !o.lifecycle.terminal()) {
                    order.uncertain = true;
                }
            }
            Event::Reconnect => {
                self.epoch = self.epoch.checked_add(1).ok_or("epoch overflow")?;
                self.health = Health::Reconciling;
                self.quote = None;
                out.push(Effect::QueryState { epoch: self.epoch });
            }
            Event::Reconcile(snapshot) => self.reconcile(snapshot)?,
            Event::Kill => {
                self.killed = true;
                out.push(Effect::Alert(
                    "kill latched; new orders disabled; cancels still allowed".into(),
                ));
            }
        }
        Ok(())
    }

    fn report(&mut self, report: &Report, out: &mut Vec<Effect>) -> Result<(), String> {
        match report {
            Report::Fill(fill) => self.fill(fill)?,
            Report::Accepted { id } => {
                let order = self.orders.get_mut(id).ok_or("ack for unknown order")?;
                if !order.lifecycle.terminal() {
                    order.lifecycle = if order.filled == 0 {
                        Lifecycle::Accepted
                    } else {
                        Lifecycle::Partial
                    };
                    if order.pending == Some(PendingAction::Submit) {
                        order.pending = None;
                        order.deadline = None;
                    }
                }
            }
            Report::Canceled {
                id,
                cumulative_filled,
            } => {
                let order = self.orders.get_mut(id).ok_or("cancel for unknown order")?;
                if *cumulative_filled < order.filled || *cumulative_filled > order.intent.qty {
                    return Err("inconsistent canceled cumulative quantity".into());
                }
                if *cumulative_filled != order.filled {
                    order.uncertain = true;
                    self.gate("cancel references missing fills", out);
                } else {
                    order.lifecycle = if order.filled == order.intent.qty {
                        Lifecycle::Filled
                    } else {
                        Lifecycle::Canceled
                    };
                    order.pending = None;
                    order.deadline = None;
                    order.uncertain = false;
                }
            }
            Report::Rejected { id } => {
                let order = self.orders.get_mut(id).ok_or("reject for unknown order")?;
                if order.lifecycle == Lifecycle::Rejected {
                    return Ok(());
                }
                if order.filled != 0 || order.lifecycle != Lifecycle::Pending {
                    return Err("submit rejection conflicts with accepted order".into());
                }
                order.lifecycle = Lifecycle::Rejected;
                order.pending = None;
                order.deadline = None;
                order.uncertain = false;
            }
            Report::CancelRejected { id } => {
                let order = self
                    .orders
                    .get_mut(id)
                    .ok_or("cancel rejection for unknown order")?;
                if order.pending == Some(PendingAction::Cancel) {
                    order.pending = None;
                    order.deadline = None;
                    order.uncertain = true;
                    self.gate("cancel rejected; query actual order state", out);
                }
            }
        }
        Ok(())
    }

    fn fill(&mut self, fill: &Fill) -> Result<(), String> {
        if let Some(previous) = self.fills.get(&fill.execution_id) {
            return if previous == fill {
                Ok(())
            } else {
                Err("execution ID reused with different payload".into())
            };
        }
        if fill.execution_id == 0 || fill.qty <= 0 || fill.price <= 0 {
            return Err("invalid fill".into());
        }
        let order = self
            .orders
            .get_mut(&fill.order_id)
            .ok_or("fill for unknown order")?;
        if fill.qty > order.intent.qty - order.filled {
            return Err("overfill".into());
        }
        if order.lifecycle.terminal() {
            return Err("new fill after terminal report; reconcile".into());
        }
        if (order.intent.side == Side::Buy && fill.price > order.intent.limit)
            || (order.intent.side == Side::Sell && fill.price < order.intent.limit)
        {
            return Err("fill violates limit price".into());
        }
        let delta = order.intent.side.sign() * fill.qty;
        self.position = self
            .position
            .checked_add(delta)
            .ok_or("position overflow")?;
        self.cash = self
            .cash
            .checked_sub(delta as i128 * fill.price as i128)
            .ok_or("cash overflow")?;
        order.filled += fill.qty;
        order.lifecycle = if order.filled == order.intent.qty {
            Lifecycle::Filled
        } else {
            Lifecycle::Partial
        };
        if order.lifecycle == Lifecycle::Filled || order.pending == Some(PendingAction::Submit) {
            order.pending = None;
            order.deadline = None;
        }
        self.fills.insert(fill.execution_id, fill.clone());
        Ok(())
    }

    fn reconcile(&mut self, snapshot: &Reconciliation) -> Result<(), String> {
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
        self.orders = rebuilt.orders;
        self.fills = rebuilt.fills;
        self.position = rebuilt.position;
        self.cash = rebuilt.cash;
        self.venue_seq = snapshot.watermark;
        self.reconciled_through = snapshot.watermark;
        self.last_private_at = self.now;
        self.health = Health::Healthy;
        Ok(())
    }
}
