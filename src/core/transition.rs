//! Owned write set. All recoverable validation errors occur before publication.
//! No raw pointers, shared mutable state, or whole-history copy on ordinary events.
use super::Core;
use crate::model::*;

/// Fixed-size state only. Orders/fills remain in the original Core while preparing.
struct Header {
    seq: u64,
    now: Time,
    health: Health,
    killed: bool,
    epoch: u64,
    venue_seq: u64,
    reconciled_through: u64,
    last_private_at: Time,
    quote: Option<(i64, i64, Time)>,
    position: i64,
    cash: i128,
    target: Option<Target>,
    last_target_revision: u64,
}

impl Header {
    fn new(core: &Core, input: &Envelope) -> Self {
        Self {
            seq: input.seq,
            now: input.at,
            health: core.health,
            killed: core.killed,
            epoch: core.epoch,
            venue_seq: core.venue_seq,
            reconciled_through: core.reconciled_through,
            last_private_at: core.last_private_at,
            quote: core.quote,
            position: core.position,
            cash: core.cash,
            target: core.target.clone(),
            last_target_revision: core.last_target_revision,
        }
    }

    fn commit(self, core: &mut Core) {
        core.seq = self.seq;
        core.now = self.now;
        core.health = self.health;
        core.killed = self.killed;
        core.epoch = self.epoch;
        core.venue_seq = self.venue_seq;
        core.reconciled_through = self.reconciled_through;
        core.last_private_at = self.last_private_at;
        core.quote = self.quote;
        core.position = self.position;
        core.cash = self.cash;
        core.target = self.target;
        core.last_target_revision = self.last_target_revision;
    }
}

enum Writes {
    None,
    Order(OrderId, Order),
    Fill(FillUpdate),
    Orders(Vec<(OrderId, Order)>),
    // Complete reconciliation inherently processes complete history. It builds a
    // replacement, rather than cloning the old Core before building it again.
    Rebuilt(Box<Core>),
}

struct Transition {
    header: Header,
    writes: Writes,
    effects: Vec<Effect>,
}

/// Internal, single-use token: borrowing the Core excludes competing transitions.
/// No state has been mutated until `commit`; dropping the token is a free abort.
pub(crate) struct Prepared<'a> {
    core: &'a mut Core,
    transition: Transition,
}

impl<'a> Prepared<'a> {
    pub(super) fn new(core: &'a mut Core, input: &Envelope) -> Result<Self, String> {
        if core.seq.checked_add(1) != Some(input.seq) || input.at < core.now {
            return Err("non-contiguous engine sequence or clock moved backwards".into());
        }
        let mut transition = Transition::new(core, input);
        if let Err(reason) = transition.handle(core, &input.event) {
            // Preserve the original contract: invalid events consume seq/time,
            // discard ALL staged changes/effects, then gate from the old state.
            transition = Transition::new(core, input);
            transition.gate(&reason);
        }
        Ok(Self { core, transition })
    }

    /// No Result errors or callbacks after publication starts. Collection inserts
    /// may allocate: this is not a promise of OOM/panic recovery or zero allocation.
    pub(crate) fn commit(self) -> Vec<Effect> {
        let Self { core, transition } = self;
        transition.header.commit(core);
        match transition.writes {
            Writes::None => {}
            Writes::Order(id, order) => core.put_order(id, order),
            Writes::Fill(update) => update.commit(core),
            Writes::Orders(orders) => {
                for (id, order) in orders {
                    core.put_order(id, order);
                }
            }
            Writes::Rebuilt(rebuilt) => {
                core.orders = rebuilt.orders;
                core.fills = rebuilt.fills;
                core.index = rebuilt.index;
            }
        }
        transition.effects
    }
}

impl Transition {
    fn new(core: &Core, input: &Envelope) -> Self {
        Self {
            header: Header::new(core, input),
            writes: Writes::None,
            effects: Vec::new(),
        }
    }

    fn gate(&mut self, reason: &str) {
        if self.header.health != Health::Disconnected {
            self.header.health = Health::Reconciling;
        }
        self.effects.push(Effect::Alert(reason.into()));
        self.effects.push(Effect::QueryState {
            epoch: self.header.epoch,
        });
    }

    fn refuse(&mut self, id: OrderId, reason: &str) {
        self.effects.push(Effect::Refused {
            id,
            reason: reason.into(),
        });
    }

    fn submit(&mut self, core: &Core, intent: &Intent) -> Result<(), String> {
        if let Some(reason) = core.refusal(intent, self.header.seq, self.header.now) {
            self.refuse(intent.id, reason);
        } else {
            let deadline = self
                .header
                .now
                .checked_add(core.config.request_timeout_ms)
                .ok_or("deadline overflow")?;
            self.writes = Writes::Order(
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
            self.effects.push(Effect::SendOrder(intent.clone()));
        }
        Ok(())
    }

    fn handle(&mut self, core: &Core, event: &Event) -> Result<(), String> {
        match event {
            Event::MarketUnavailable => self.header.quote = None,
            Event::QuoteObserved {
                bid,
                ask,
                observed_at,
            } => {
                if *bid <= 0 || ask < bid || *observed_at > self.header.now {
                    return Err("invalid observed quote".into());
                }
                self.header.quote = Some((*bid, *ask, *observed_at));
            }
            Event::Trade { price, qty, .. } => {
                if *price <= 0 || *qty <= 0 {
                    return Err("invalid market trade".into());
                }
            }
            Event::Quote { bid, ask } => {
                if *bid <= 0 || ask < bid {
                    return Err("invalid quote".into());
                }
                self.header.quote = Some((*bid, *ask, self.header.now));
            }
            Event::SetTarget(target) => {
                if target.revision <= self.header.last_target_revision
                    || target.valid_until < self.header.now
                    || (target.position as i128).abs() > core.config.max_abs_position as i128
                {
                    self.effects.push(Effect::SignalRefused {
                        revision: target.revision,
                        reason: "stale, expired or out-of-bounds target".into(),
                    });
                } else {
                    self.header.last_target_revision = target.revision;
                    self.header.target = Some(target.clone());
                }
            }
            Event::SubmitTargeted {
                intent,
                revision,
                expected_position,
            } => {
                if let Some(reason) =
                    core.targeted_refusal(intent, *revision, *expected_position, self.header.now)
                {
                    self.refuse(intent.id, reason);
                } else {
                    self.submit(core, intent)?;
                }
            }
            Event::Submit(intent) => self.submit(core, intent)?,
            Event::Cancel { id } => {
                let Some(original) = core.orders.get(id) else {
                    self.refuse(*id, "unknown order");
                    return Ok(());
                };
                if self.header.health == Health::Disconnected
                    || original.lifecycle.terminal()
                    || original.pending == Some(PendingAction::Cancel)
                {
                    self.refuse(*id, "cancel unavailable or already pending");
                } else {
                    let deadline = self
                        .header
                        .now
                        .checked_add(core.config.request_timeout_ms)
                        .ok_or("deadline overflow")?;
                    let mut order = original.clone();
                    order.pending = Some(PendingAction::Cancel);
                    order.deadline = Some(deadline);
                    self.writes = Writes::Order(*id, order);
                    self.effects.push(Effect::SendCancel { id: *id });
                }
            }
            Event::Execution {
                epoch,
                venue_seq,
                report,
            } => {
                if *epoch != self.header.epoch || self.header.health == Health::Disconnected {
                    return Ok(());
                }
                if *venue_seq == 0 {
                    return Err("invalid venue sequence".into());
                }
                if *venue_seq <= self.header.reconciled_through {
                    return Ok(());
                }
                if *venue_seq > self.header.venue_seq.saturating_add(1) {
                    self.gate("private stream sequence gap");
                }
                self.header.venue_seq = self.header.venue_seq.max(*venue_seq);
                self.header.last_private_at = self.header.now;
                self.report(core, report)?;
            }
            Event::Heartbeat { epoch } => {
                if *epoch == self.header.epoch && self.header.health != Health::Disconnected {
                    self.header.last_private_at = self.header.now;
                }
            }
            Event::Tick => {
                let orders: Vec<_> = core
                    .expired_orders(self.header.now)
                    .map(|(id, original)| {
                        let mut order = original.clone();
                        order.uncertain = true;
                        order.deadline = None;
                        (*id, order)
                    })
                    .collect();
                if !orders.is_empty() {
                    self.writes = Writes::Orders(orders);
                    self.gate("order action timeout; outcome unknown");
                }
                if self.header.health == Health::Healthy
                    && self.header.now - self.header.last_private_at > core.config.private_stale_ms
                {
                    self.gate("private stream heartbeat timeout");
                }
            }
            Event::Disconnect => {
                self.header.target = None;
                self.header.health = Health::Disconnected;
                self.header.quote = None;
                self.writes = Writes::Orders(
                    core.open_orders()
                        .filter(|(_, o)| !o.lifecycle.terminal())
                        .map(|(id, original)| {
                            let mut order = original.clone();
                            order.uncertain = true;
                            (*id, order)
                        })
                        .collect(),
                );
            }
            Event::Reconnect => {
                self.header.epoch = self.header.epoch.checked_add(1).ok_or("epoch overflow")?;
                self.header.health = Health::Reconciling;
                self.header.quote = None;
                self.effects.push(Effect::QueryState {
                    epoch: self.header.epoch,
                });
            }
            Event::Reconcile(snapshot) => {
                let rebuilt = core.rebuild(snapshot)?;
                self.header.position = rebuilt.position;
                self.header.cash = rebuilt.cash;
                self.header.venue_seq = snapshot.watermark;
                self.header.reconciled_through = snapshot.watermark;
                self.header.last_private_at = self.header.now;
                self.header.health = Health::Healthy;
                self.writes = Writes::Rebuilt(Box::new(rebuilt));
            }
            Event::Kill => {
                self.header.killed = true;
                self.effects.push(Effect::Alert(
                    "kill latched; new orders disabled; cancels still allowed".into(),
                ));
            }
        }
        Ok(())
    }

    fn report(&mut self, core: &Core, report: &Report) -> Result<(), String> {
        if let Report::Fill(fill) = report {
            if let Some(update) = FillUpdate::prepare(core, fill)? {
                self.writes = Writes::Fill(update);
            }
            return Ok(());
        }
        let (id, unknown) = match report {
            Report::Accepted { id } => (*id, "ack for unknown order"),
            Report::Canceled { id, .. } => (*id, "cancel for unknown order"),
            Report::Rejected { id } => (*id, "reject for unknown order"),
            Report::CancelRejected { id } => (*id, "cancel rejection for unknown order"),
            Report::Fill(_) => unreachable!(),
        };
        let mut order = core.orders.get(&id).ok_or(unknown)?.clone();
        match report {
            Report::Accepted { .. } => {
                if order.lifecycle == Lifecycle::Rejected {
                    return Err("acceptance conflicts with rejected order".into());
                }
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
                cumulative_filled, ..
            } => {
                if order.lifecycle == Lifecycle::Rejected {
                    return Err("cancel confirmation conflicts with rejected order".into());
                }
                if *cumulative_filled < order.filled || *cumulative_filled > order.intent.qty {
                    return Err("inconsistent canceled cumulative quantity".into());
                }
                if *cumulative_filled != order.filled {
                    order.uncertain = true;
                    self.gate("cancel references missing fills");
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
            Report::Rejected { .. } => {
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
            Report::CancelRejected { .. } => {
                if order.pending == Some(PendingAction::Cancel) {
                    order.pending = None;
                    order.deadline = None;
                    order.uncertain = true;
                    self.gate("cancel rejected; query actual order state");
                }
            }
            Report::Fill(_) => unreachable!(),
        }
        self.writes = Writes::Order(id, order);
        Ok(())
    }
}

/// Used both by live transitions and by isolated reconciliation reconstruction.
pub(super) struct FillUpdate {
    order: Order,
    fill: Fill,
    position: i64,
    cash: i128,
}

impl FillUpdate {
    pub(super) fn prepare(core: &Core, fill: &Fill) -> Result<Option<Self>, String> {
        if let Some(previous) = core.fills.get(&fill.execution_id) {
            return if previous == fill {
                Ok(None)
            } else {
                Err("execution ID reused with different payload".into())
            };
        }
        if fill.execution_id == 0 || fill.qty <= 0 || fill.price <= 0 {
            return Err("invalid fill".into());
        }
        let mut order = core
            .orders
            .get(&fill.order_id)
            .ok_or("fill for unknown order")?
            .clone();
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
        let position = core
            .position
            .checked_add(delta)
            .ok_or("position overflow")?;
        let cash = core
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
        Ok(Some(Self {
            order,
            fill: fill.clone(),
            position,
            cash,
        }))
    }

    pub(super) fn commit(self, core: &mut Core) {
        core.position = self.position;
        core.cash = self.cash;
        core.put_order(self.fill.order_id, self.order);
        core.fills.insert(self.fill.execution_id, self.fill);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preparation_and_abort_never_publish_even_the_gate() {
        let mut core = Core::new(Config::default()).unwrap();
        for event in [
            Event::Quote { bid: 99, ask: 101 },
            Event::Quote { bid: 0, ask: 1 },
        ] {
            let original = core.clone();
            let input = Envelope {
                seq: 1,
                at: 1,
                event,
            };
            {
                let prepared = core.prepare(&input).unwrap();
                assert_eq!(*prepared.core, original);
                // Scope exit models a failed journal append: discard prepared state.
            }
            assert_eq!(core, original);
        }
        let effects = core
            .prepare(&Envelope {
                seq: 1,
                at: 1,
                event: Event::Quote { bid: 0, ask: 1 },
            })
            .unwrap()
            .commit();
        assert_eq!(core.seq, 1);
        assert_eq!(core.now, 1);
        assert_eq!(core.health, Health::Reconciling);
        assert!(matches!(
            effects.as_slice(),
            [Effect::Alert(_), Effect::QueryState { .. }]
        ));
    }
}
