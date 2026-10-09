use serde::{Deserialize, Serialize};

pub type OrderId = u64;
pub type Time = u64; // Monotonic engine milliseconds, including during replay.

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Side {
    Buy,
    Sell,
}
impl Side {
    pub fn sign(self) -> i64 {
        if self == Self::Buy { 1 } else { -1 }
    }
}

/// Integer ticks and lots; v0 has exactly one account and one instrument.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Intent {
    pub id: OrderId,
    pub side: Side,
    pub qty: i64,
    pub limit: i64,
    pub based_on_seq: u64,
    pub valid_until: Time,
}

/// Strategy output is a versioned target, not an instruction to buy again.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Target {
    pub revision: u64,
    pub position: i64,
    pub valid_until: Time,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Lifecycle {
    Pending,
    Accepted,
    Partial,
    Filled,
    Canceled,
    Rejected,
}
impl Lifecycle {
    pub fn terminal(self) -> bool {
        matches!(self, Self::Filled | Self::Canceled | Self::Rejected)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum PendingAction {
    Submit,
    Cancel,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Health {
    Healthy,
    Disconnected,
    Reconciling,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Order {
    pub intent: Intent,
    pub filled: i64,
    pub lifecycle: Lifecycle,
    pub pending: Option<PendingAction>,
    pub deadline: Option<Time>,
    pub uncertain: bool,
}
impl Order {
    pub fn remaining(&self) -> i64 {
        if self.lifecycle.terminal() && !self.uncertain {
            0
        } else {
            self.intent.qty - self.filled
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fill {
    pub execution_id: u64,
    pub order_id: OrderId,
    pub qty: i64,
    pub price: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Report {
    Accepted { id: OrderId },
    Fill(Fill),
    Canceled { id: OrderId, cumulative_filled: i64 },
    Rejected { id: OrderId },
    CancelRejected { id: OrderId },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct VenueOrder {
    pub intent: Intent,
    pub filled: i64,
    pub lifecycle: Lifecycle,
}

/// Complete history at an atomic venue barrier, NOT an ordinary open-orders response.
/// The adapter must buffer reports after `watermark` until this snapshot is applied.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reconciliation {
    pub epoch: u64,
    pub watermark: u64,
    pub orders: Vec<VenueOrder>,
    pub fills: Vec<Fill>,
    pub position: i64,
    /// Local orders the venue proves it never received: queried by client order
    /// ID after every request that could create them has expired (for Binance,
    /// after the signed request's recvWindow). Only never-acknowledged, unfilled
    /// orders may be listed; they become terminal (Rejected) and their IDs stay used.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub absent: Vec<OrderId>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Event {
    Quote {
        bid: i64,
        ask: i64,
    },
    QuoteObserved {
        bid: i64,
        ask: i64,
        observed_at: Time,
    },
    MarketUnavailable,
    Trade {
        taker: Side,
        price: i64,
        qty: i64,
    },
    Submit(Intent),
    SetTarget(Target),
    SubmitTargeted {
        intent: Intent,
        revision: u64,
        expected_position: i64,
    },
    Cancel {
        id: OrderId,
    },
    Execution {
        epoch: u64,
        venue_seq: u64,
        report: Report,
    },
    Tick,
    Heartbeat {
        epoch: u64,
    },
    Disconnect,
    Reconnect,
    Reconcile(Reconciliation),
    Kill,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Envelope {
    pub seq: u64,
    pub at: Time,
    pub event: Event,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Effect {
    SendOrder(Intent),
    SendCancel { id: OrderId },
    QueryState { epoch: u64 },
    Refused { id: OrderId, reason: String },
    SignalRefused { revision: u64, reason: String },
    Alert(String),
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Config {
    pub max_abs_position: i64,
    pub max_order_qty: i64,
    pub max_order_notional: i64,
    pub request_timeout_ms: u64,
    pub market_stale_ms: u64,
    pub private_stale_ms: u64,
    pub max_signal_lag: u64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            max_abs_position: 10,
            max_order_qty: 10,
            max_order_notional: 1_000_000,
            request_timeout_ms: 100,
            market_stale_ms: 1_000,
            private_stale_ms: 5_000,
            max_signal_lag: 100,
        }
    }
}
