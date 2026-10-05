//! Deterministic exchange with explicit delivery faults. Quotes do not imply fills:
//! `trade` supplies available liquidity and fills eligible orders in ID order.
use crate::model::*;
use std::collections::BTreeMap;

#[derive(Default)]
pub struct PaperExchange {
    pub orders: BTreeMap<OrderId, VenueOrder>,
    pub fills: Vec<Fill>,
    pub venue_seq: u64,
    pub position: i64,
    pub connected: bool,
    pub drop_next_ack: bool,
    pub duplicate_next_fill: bool,
}

impl PaperExchange {
    pub fn new() -> Self {
        Self {
            connected: true,
            ..Self::default()
        }
    }

    fn deliver(&mut self, epoch: u64, report: Report) -> Option<Event> {
        self.venue_seq += 1;
        if !self.connected {
            return None;
        }
        if matches!(report, Report::Accepted { .. }) && self.drop_next_ack {
            self.drop_next_ack = false;
            return None;
        }
        Some(Event::Execution {
            epoch,
            venue_seq: self.venue_seq,
            report,
        })
    }

    pub fn execute(&mut self, epoch: u64, effect: &Effect) -> Result<Vec<Event>, String> {
        let report = match effect {
            Effect::SendOrder(intent) => {
                if let Some(existing) = self.orders.get(&intent.id) {
                    if existing.intent != *intent {
                        return Err("client ID collision".into());
                    }
                    // Paper venue models idempotent submit; real venues may differ.
                    Report::Accepted { id: intent.id }
                } else {
                    if !self.connected {
                        return Err("gateway disconnected before send".into());
                    }
                    self.orders.insert(
                        intent.id,
                        VenueOrder {
                            intent: intent.clone(),
                            filled: 0,
                            lifecycle: Lifecycle::Accepted,
                        },
                    );
                    Report::Accepted { id: intent.id }
                }
            }
            Effect::SendCancel { id } => {
                if !self.connected {
                    return Err("gateway disconnected before cancel".into());
                }
                match self.orders.get_mut(id) {
                    Some(order) if !order.lifecycle.terminal() => {
                        order.lifecycle = Lifecycle::Canceled;
                        Report::Canceled {
                            id: *id,
                            cumulative_filled: order.filled,
                        }
                    }
                    _ => Report::CancelRejected { id: *id },
                }
            }
            Effect::QueryState { epoch } if self.connected => {
                return Ok(vec![Event::Reconcile(self.snapshot(*epoch))]);
            }
            _ => return Ok(Vec::new()),
        };
        Ok(self.deliver(epoch, report).into_iter().collect())
    }

    /// A taker sell hits resting buys; taker buy lifts resting sells. No impact model.
    pub fn trade(
        &mut self,
        epoch: u64,
        taker: Side,
        price: i64,
        mut qty: i64,
    ) -> Result<Vec<Event>, String> {
        if price <= 0 || qty <= 0 {
            return Err("invalid trade".into());
        }
        let mut generated = Vec::new();
        for order in self.orders.values_mut() {
            if qty == 0 {
                break;
            }
            if order.lifecycle.terminal() || order.intent.side == taker {
                continue;
            }
            let crosses = match order.intent.side {
                Side::Buy => price <= order.intent.limit,
                Side::Sell => price >= order.intent.limit,
            };
            if !crosses {
                continue;
            }
            let filled = qty.min(order.intent.qty - order.filled);
            qty -= filled;
            order.filled += filled;
            order.lifecycle = if order.filled == order.intent.qty {
                Lifecycle::Filled
            } else {
                Lifecycle::Partial
            };
            self.position += order.intent.side.sign() * filled;
            let fill = Fill {
                execution_id: self.fills.len() as u64 + 1,
                order_id: order.intent.id,
                qty: filled,
                price,
            };
            self.fills.push(fill.clone());
            generated.push(Report::Fill(fill));
        }
        let mut events = Vec::new();
        for report in generated {
            if let Some(event) = self.deliver(epoch, report) {
                events.push(event.clone());
                if self.duplicate_next_fill {
                    events.push(event);
                    self.duplicate_next_fill = false;
                }
            }
        }
        Ok(events)
    }

    pub fn snapshot(&self, epoch: u64) -> Reconciliation {
        Reconciliation {
            epoch,
            watermark: self.venue_seq,
            orders: self.orders.values().cloned().collect(),
            fills: self.fills.clone(),
            position: self.position,
        }
    }
}
