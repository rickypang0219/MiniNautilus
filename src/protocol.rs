//! JSON-lines responses for the Python bridge.
//!
//! Protocol 2 sends the complete `Core` once (startup, and after a full
//! reconciliation replaced history). Every other response carries the fixed-size
//! header plus only the orders and fills written while handling that request, so
//! response size no longer grows with retained history. Applying each delta to the
//! previous state reproduces `serde_json::to_value(core)` exactly (tests/protocol.rs).
use crate::{
    core::{Changes, Core},
    model::*,
};
use serde::Serialize;
use std::collections::BTreeMap;

pub const PROTOCOL: u32 = 2;

/// Every non-map field of `Core` except `config`, which never changes.
#[derive(Serialize)]
pub struct Header<'a> {
    pub seq: u64,
    pub now: Time,
    pub health: Health,
    pub killed: bool,
    pub epoch: u64,
    pub venue_seq: u64,
    pub reconciled_through: u64,
    pub last_private_at: Time,
    pub quote: Option<(i64, i64, Time)>,
    pub position: i64,
    pub target: &'a Option<Target>,
    pub last_target_revision: u64,
    pub cash: i128,
}

impl<'a> Header<'a> {
    pub fn of(core: &'a Core) -> Self {
        Self {
            seq: core.seq,
            now: core.now,
            health: core.health,
            killed: core.killed,
            epoch: core.epoch,
            venue_seq: core.venue_seq,
            reconciled_through: core.reconciled_through,
            last_private_at: core.last_private_at,
            quote: core.quote,
            position: core.position,
            target: &core.target,
            last_target_revision: core.last_target_revision,
            cash: core.cash,
        }
    }
}

#[derive(Serialize)]
pub struct Delta<'a> {
    pub header: Header<'a>,
    pub orders: BTreeMap<OrderId, &'a Order>,
    pub fills: BTreeMap<u64, &'a Fill>,
}

#[derive(Serialize)]
#[serde(untagged)]
pub enum Response<'a> {
    Full {
        protocol: u32,
        effects: Vec<Effect>,
        state: &'a Core,
    },
    Delta {
        effects: Vec<Effect>,
        delta: Delta<'a>,
    },
}

impl<'a> Response<'a> {
    /// The complete state; used at startup, after history replacement, and when
    /// compact responses are disabled.
    pub fn full(core: &'a Core, effects: Vec<Effect>) -> Self {
        Self::Full {
            protocol: PROTOCOL,
            effects,
            state: core,
        }
    }

    /// Compact when the change log is available and history was not replaced.
    pub fn compact(core: &'a Core, effects: Vec<Effect>, changes: Option<Changes>) -> Self {
        let Some(changes) = changes.filter(|c| !c.replaced) else {
            return Self::full(core, effects);
        };
        Self::Delta {
            effects,
            delta: Delta {
                header: Header::of(core),
                orders: changes
                    .orders
                    .into_iter()
                    .map(|id| (id, &core.orders[&id]))
                    .collect(),
                fills: changes
                    .fills
                    .into_iter()
                    .map(|id| (id, &core.fills[&id]))
                    .collect(),
            },
        }
    }
}
