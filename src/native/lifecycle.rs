//! Flat, independently reconciled segments with one account owner for the whole run.
//! No automatic replacement host: an operator must fence the old signer first.
use super::{
    Result, fail,
    runtime::{self, Options, Segment},
    signing::account_lock,
    venue::{self, Http, Instrument, Session},
    wall_ms,
};
use crate::journal;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

pub fn digest(path: &Path) -> Result<String> {
    Ok(hex::encode(Sha256::digest(fs::read(path)?)))
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Pointer {
    pub schema: u32,
    pub account: String,
    pub agent: String,
    pub index: u64,
    pub phase: String,
    pub nonce_floor: u64,
    pub risk_cash_floor: String,
    pub expected_cash: String,
    pub previous_seal: Option<String>,
}
pub fn publish(root: &Path, pointer: &Pointer) -> Result<()> {
    let payload = serde_json::to_value(pointer)?;
    let sha256 = hex::encode(Sha256::digest(serde_json::to_vec(&payload)?));
    // A leftover temp file is evidence of an interrupted publication; don't overwrite it.
    let path = root.join(".current.tmp");
    let row = json!({"payload":payload,"sha256":sha256});
    let file = match OpenOptions::new().create_new(true).write(true).open(&path) {
        Ok(mut file) => {
            serde_json::to_writer(&mut file, &row)?;
            file.write_all(b"\n")?;
            file
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            if serde_json::from_slice::<Value>(&fs::read(&path)?)? != row {
                return Err(fail(
                    "interrupted pointer differs from requested publication",
                ));
            }
            OpenOptions::new().write(true).open(&path)?
        }
        Err(error) => return Err(error.into()),
    };
    file.sync_all()?;
    fs::rename(path, root.join("current.json"))?;
    File::open(root)?.sync_all()?;
    Ok(())
}
pub fn load(root: &Path) -> Result<Pointer> {
    let value: Value = serde_json::from_slice(&fs::read(root.join("current.json"))?)?;
    let sha256 = hex::encode(Sha256::digest(serde_json::to_vec(&value["payload"])?));
    if value["sha256"] != sha256 {
        return Err(fail("lifecycle pointer corruption"));
    }
    let p: Pointer = serde_json::from_value(value["payload"].clone())?;
    if p.schema != 1
        || p.index > 100000
        || ((p.index > 0) != p.previous_seal.is_some())
        || !matches!(p.phase.as_str(), "active" | "rotating" | "stopped")
    {
        return Err(fail("lifecycle pointer schema/phase"));
    }
    if let Some(previous) = &p.previous_seal
        && (p.index == 0 || digest(&directory(root, p.index - 1).join("seal.json"))? != *previous)
    {
        return Err(fail("lifecycle predecessor seal mismatch"));
    }
    if p.index > 0 {
        let dir = directory(root, p.index - 1);
        let seal: Value = serde_json::from_slice(&fs::read(dir.join("seal.json"))?)?;
        for (name, key) in [
            ("session.json", "session_sha256"),
            ("actions.jsonl", "actions_sha256"),
            ("run-report.json", "report_sha256"),
        ] {
            if seal[key] != digest(&dir.join(name))? {
                return Err(fail("sealed segment evidence changed"));
            }
        }
        if seal["journal_sha256"] != digest(&journal_path(root, p.index - 1))? {
            return Err(fail("sealed journal changed"));
        }
        if seal["nonce_floor"].as_u64() != Some(p.nonce_floor)
            || seal["account"] != p.account
            || seal["agent"] != p.agent
        {
            return Err(fail("lifecycle nonce/account carry mismatch"));
        }
    }
    Ok(p)
}
pub fn directory(root: &Path, index: u64) -> PathBuf {
    root.join(format!("segment-{index:06}"))
}
pub fn journal_path(root: &Path, index: u64) -> PathBuf {
    root.join(format!("journal-{index:06}.jsonl"))
}

pub struct Policy {
    pub orders: usize,
    pub bytes: u64,
    pub age_seconds: u64,
    pub flatten_on_rotation: bool,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            orders: 800,
            bytes: 64 * 1024 * 1024,
            age_seconds: 1800,
            flatten_on_rotation: false,
        }
    }
}
impl Policy {
    pub fn validate(&self) -> Result<()> {
        if self.orders == 0
            || self.orders > 800
            || self.bytes == 0
            || self.age_seconds == 0
            || self.age_seconds > 1800
        {
            return Err(fail(
                "rotation soft thresholds may tighten but cannot exceed 800 orders/1800 seconds",
            ));
        }
        Ok(())
    }
}
/// Verify flat terminal facts before invoking existing checksummed journal rotation.
pub fn rotate(root: &Path, index: u64) -> Result<()> {
    let old = journal_path(root, index);
    let new = journal_path(root, index + 1);
    let core = journal::replay(&old)?;
    if core.position != 0 || core.killed || core.reconciled_through != core.venue_seq {
        return Err(fail(
            "native rotation requires flat, unlatched, freshly reconciled state",
        ));
    }
    core.rotation_ready().map_err(fail)?;
    journal::rotate_retaining(&old, &new)?;
    Ok(())
}
pub fn run(root: &Path, options: Options, policy: Policy) -> Result<Value> {
    policy.validate()?;
    if !options.signed {
        return Err(fail(
            "lifecycle supervisor currently requires explicit Testnet execution",
        ));
    }
    if options.seconds == 0 || options.seconds > 86400 {
        return Err(fail("lifecycle duration 1..86400 seconds"));
    }
    let account = std::env::var("HL_MAIN_ADDRESS").map_err(|_| fail("account missing"))?;
    let _owner = account_lock(&account)?;
    fs::create_dir(root)?;
    let root = fs::canonicalize(root)?;
    let http = Http::new(false)?;
    let instrument = Instrument::from_meta(&http.info("meta", json!({}))?, "BTC")?;
    let baseline = runtime::new_session(&http, instrument)?; // readonly independent flat check
    let mut pointer = Pointer {
        schema: 1,
        account,
        agent: baseline.agent,
        index: 0,
        phase: "active".into(),
        nonce_floor: 0,
        risk_cash_floor: venue::wire(
            venue::dec(&json!(baseline.baseline_cash))? - rust_decimal::Decimal::from(10),
        ),
        expected_cash: baseline.baseline_cash,
        previous_seal: None,
    };
    publish(&root, &pointer)?;
    runtime::save_json(
        &root.join("policy.json"),
        &json!({"orders":policy.orders,"bytes":policy.bytes,"age_seconds":policy.age_seconds,"flatten_on_rotation":policy.flatten_on_rotation,"requested_seconds":options.seconds,"signed":options.signed,"python":options.python,"created_ms":wall_ms()}),
    )?;
    let started = Instant::now();
    let mut reports = Vec::new();
    while started.elapsed() < Duration::from_secs(options.seconds) {
        let remaining = (options.seconds as f64 - started.elapsed().as_secs_f64()).ceil() as u64;
        let dir = directory(&root, pointer.index);
        let report = runtime::run_segment(
            &dir,
            Options {
                signed: options.signed,
                python: options.python,
                seconds: remaining.min(7200),
                flatten: options.flatten,
            },
            Some(&Segment {
                journal: journal_path(&root, pointer.index),
                orders: policy.orders,
                bytes: policy.bytes,
                age_seconds: policy.age_seconds,
                flatten_on_rotation: policy.flatten_on_rotation,
                nonce_floor: pointer.nonce_floor,
                risk_cash_floor: Some(pointer.risk_cash_floor.clone()),
                expected_cash: Some(pointer.expected_cash.clone()),
            }),
        )?;
        reports.push(report.clone());
        if report["failed"] == true || report["cleanup_verified"] != true {
            return Err(fail(
                "segment failed; recover original current pointer, no successor started",
            ));
        }
        if report["rotation_ready"] != true || report["signal_stop"] == true {
            break;
        }
        let session: Session = serde_json::from_slice(&fs::read(dir.join("session.json"))?)?;
        if session.account != pointer.account || session.agent != pointer.agent {
            return Err(fail("segment binding changed"));
        }
        let seal = json!({"schema":1,"index":pointer.index,"account":pointer.account,"agent":pointer.agent,"previous_seal":pointer.previous_seal,
            "session_sha256":digest(&dir.join("session.json"))?,"actions_sha256":digest(&dir.join("actions.jsonl"))?,
            "report_sha256":digest(&dir.join("run-report.json"))?,"nonce_floor":report["nonce_floor"],"id_floor":report["id_floor"],"ledger":report["ledger"]});
        runtime::save_json(&dir.join("prepared.json"), &seal)?;
        pointer.phase = "rotating".into();
        publish(&root, &pointer)?;
        pointer = repair_rotation(&root, pointer)?;
    }
    pointer.phase = "stopped".into();
    publish(&root, &pointer)?;
    let position = reports.last().map(|r| r["position"].clone());
    let result = json!({"schema":1,"runtime":"native-lifecycle","elapsed_seconds":started.elapsed().as_secs_f64(),"rotations":pointer.index,"segments":reports,"position":position,"status":"COMPLETED_BOUNDED_RUN"});
    runtime::save_json(&root.join("lifecycle-report.json"), &result)?;
    Ok(result)
}

/// Original current segment only. This does not authorize a replacement signer.
pub fn recover(root: &Path, flatten: bool) -> Result<Value> {
    let root = fs::canonicalize(root)?;
    let mut pointer = load(&root)?;
    if std::env::var("HL_MAIN_ADDRESS").as_deref() != Ok(&pointer.account)
        || std::env::var("HL_API_ADDRESS").as_deref() != Ok(&pointer.agent)
    {
        return Err(fail("lifecycle account/agent mismatch"));
    }
    let _owner = account_lock(&pointer.account)?;
    if pointer.phase == "rotating" {
        pointer = repair_rotation(&root, pointer)?;
    }
    let dir = directory(&root, pointer.index);
    if !dir.join("session.json").exists() && pointer.index > 0 {
        let core = journal::replay(&journal_path(&root, pointer.index))?;
        if core.seq != 0 || core.position != 0 || !core.orders.is_empty() || !core.fills.is_empty()
        {
            return Err(fail("prepared successor is not an untouched flat genesis"));
        }
        let http = Http::new(false)?;
        let instrument = Instrument::from_meta(&http.info("meta", json!({}))?, "BTC")?;
        let fresh = runtime::new_session(&http, instrument)?;
        if (venue::dec(&json!(fresh.baseline_cash))? - venue::dec(&json!(pointer.expected_cash))?)
            .abs()
            > rust_decimal::Decimal::new(1, 6)
        {
            return Err(fail("prepared successor cash discrepancy"));
        }
        return Ok(
            json!({"runtime":"lifecycle-recovery","status":"PREPARED_NO_SIGNER","position":0,"cleanup_verified":true}),
        );
    }
    if fs::canonicalize(dir.join("journal.jsonl"))? != journal_path(&root, pointer.index) {
        return Err(fail("current journal pointer mismatch"));
    }
    runtime::recover_locked(&dir, flatten)
}

/// Resume only a proven publication transaction; never infer a lost mutation.
pub fn repair_rotation(root: &Path, mut pointer: Pointer) -> Result<Pointer> {
    if pointer.phase != "rotating" {
        return Err(fail("not a rotation transaction"));
    }
    let dir = directory(root, pointer.index);
    let mut seal: Value = serde_json::from_slice(&fs::read(dir.join("prepared.json"))?)?;
    for (name, key) in [
        ("session.json", "session_sha256"),
        ("actions.jsonl", "actions_sha256"),
        ("run-report.json", "report_sha256"),
    ] {
        if seal[key] != digest(&dir.join(name))? {
            return Err(fail("prepared facts changed"));
        }
    }
    let old = journal_path(root, pointer.index);
    let new = journal_path(root, pointer.index + 1);
    let current = journal::replay(&old)?;
    if current.position != 0 || current.killed || current.reconciled_through != current.venue_seq {
        return Err(fail("prepared rotation not quiescent"));
    }
    current.rotation_ready().map_err(fail)?;
    if !new.exists() {
        journal::rotate_retaining(&old, &new)?;
    }
    let bytes = fs::read(&old)?;
    let last = bytes
        .split(|b| *b == b'\n')
        .rfind(|b| !b.is_empty())
        .ok_or_else(|| fail("empty predecessor"))?;
    let frame: Value = serde_json::from_slice(last)?;
    let payload: Value = serde_json::from_str(
        frame["payload"]
            .as_str()
            .ok_or_else(|| fail("frame payload"))?,
    )?;
    if payload["Closed"]["successor"] != new.display().to_string() {
        return Err(fail("predecessor did not publish this successor"));
    }
    let next = journal::replay(&new)?;
    if next.seq != 0
        || !next.orders.is_empty()
        || !next.fills.is_empty()
        || next.cash != current.cash
        || next.position != 0
        || next.id_floor != current.carry().id_floor
        || next.epoch != current.epoch
        || next.last_target_revision != current.last_target_revision
    {
        return Err(fail("successor genesis differs from prepared carry"));
    }
    if let Some(reference) = &next.cold {
        crate::cold::ColdIndex::open(root, reference)?;
    }
    seal["journal_sha256"] = digest(&old)?.into();
    let final_path = dir.join("seal.json");
    if final_path.exists() {
        if serde_json::from_slice::<Value>(&fs::read(&final_path)?)? != seal {
            return Err(fail("existing seal mismatch"));
        }
    } else {
        runtime::save_json(&final_path, &seal)?;
    }
    let session: Session = serde_json::from_slice(&fs::read(dir.join("session.json"))?)?;
    if session.account != pointer.account || session.agent != pointer.agent {
        return Err(fail("prepared account binding"));
    }
    pointer.previous_seal = Some(digest(&final_path)?);
    pointer.index += 1;
    pointer.nonce_floor = seal["nonce_floor"]
        .as_u64()
        .ok_or_else(|| fail("prepared nonce floor"))?;
    pointer.expected_cash = venue::wire(
        venue::dec(&json!(session.baseline_cash))? + venue::dec(&seal["ledger"]["cash_change"])?,
    );
    pointer.phase = "active".into();
    publish(root, &pointer)?;
    Ok(pointer)
}

/// Offline production-Core retention drill. No market, signer, or alpha claims.
pub fn fixture(root: &Path, count: u64, segment_orders: usize) -> Result<Value> {
    use crate::{
        journal::{DurableEngine, SyncPolicy},
        model::*,
    };
    if count == 0
        || count > 100000
        || !count.is_multiple_of(2)
        || segment_orders < 2
        || !segment_orders.is_multiple_of(2)
    {
        return Err(fail("fixture requires positive even order counts"));
    }
    fs::create_dir(root)?;
    let root = fs::canonicalize(root)?;
    let mut engine = DurableEngine::create(&journal_path(&root, 0), runtime::config())?;
    engine.set_sync_policy(SyncPolicy::Outbox);
    let started = Instant::now();
    let mut index = 0;
    let mut max_recovery = 0.0f64;
    let mut at = 1;
    for id in 1..=count {
        at += 1;
        engine.process(
            at,
            Event::Heartbeat {
                epoch: engine.core().epoch,
            },
        )?;
        engine.process(
            at,
            Event::Quote {
                bid: 800000,
                ask: 800000,
            },
        )?;
        let intent = Intent {
            id,
            side: if id % 2 == 1 { Side::Buy } else { Side::Sell },
            qty: 20,
            limit: 800000,
            based_on_seq: engine.core().seq,
            valid_until: at + 1000,
        };
        let effects = engine.process(at, Event::Submit(intent))?;
        if !effects.iter().any(|e| matches!(e, Effect::SendOrder(_))) {
            return Err(fail("fixture submit refused"));
        }
        engine.process(
            at,
            Event::Execution {
                epoch: engine.core().epoch,
                venue_seq: engine.core().venue_seq + 1,
                report: Report::Fill(Fill {
                    execution_id: id,
                    order_id: id,
                    qty: 20,
                    price: 800000,
                }),
            },
        )?;
        if engine.core().orders.len() >= segment_orders && id < count {
            engine.process(at, Event::Disconnect)?;
            engine.process(at, Event::Reconnect)?;
            let c = engine.core();
            let snapshot = Reconciliation {
                epoch: c.epoch,
                watermark: c.venue_seq,
                orders: c
                    .orders
                    .values()
                    .map(|o| VenueOrder {
                        intent: o.intent.clone(),
                        filled: o.filled,
                        lifecycle: o.lifecycle,
                    })
                    .collect(),
                fills: c.fills.values().cloned().collect(),
                position: 0,
                absent: vec![],
            };
            engine.process(at, Event::Reconcile(snapshot))?;
            drop(engine);
            rotate(&root, index)?;
            index += 1;
            let recovery = Instant::now();
            engine = DurableEngine::recover(&journal_path(&root, index), None)?;
            max_recovery = max_recovery.max(recovery.elapsed().as_secs_f64());
            engine.set_sync_policy(SyncPolicy::Outbox);
            engine.process(at, Event::Reconnect)?;
            engine.process(
                at,
                Event::Reconcile(Reconciliation {
                    epoch: engine.core().epoch,
                    watermark: engine.core().venue_seq,
                    orders: vec![],
                    fills: vec![],
                    position: 0,
                    absent: vec![],
                }),
            )?;
            if engine.core().id_floor != id
                || engine.core().position != 0
                || engine.core().cash != 0
            {
                return Err(fail("rotation economic/id carry mismatch"));
            }
        }
    }
    engine.sync()?;
    let report = json!({"schema":1,"scope":"offline production Core/journal rotation; no venue or strategy","orders":count,"rotations":index,"retained_orders":engine.core().orders.len(),"position":engine.core().position,"cash_tick_lots":engine.core().cash.to_string(),"max_local_recovery_seconds":max_recovery,"elapsed_seconds":started.elapsed().as_secs_f64(),"rss_hwm_kib":runtime::bench::rss_kib()});
    runtime::save_json(&root.join("fixture-report.json"), &report)?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pointer_rejects_damage_and_missing_predecessor() {
        let root = std::env::temp_dir().join(format!("native-lineage-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir(&root).unwrap();
        let mut pointer = Pointer {
            schema: 1,
            account: "a".into(),
            agent: "b".into(),
            index: 0,
            phase: "active".into(),
            nonce_floor: 10,
            risk_cash_floor: "990".into(),
            expected_cash: "1000".into(),
            previous_seal: None,
        };
        publish(&root, &pointer).unwrap();
        assert_eq!(load(&root).unwrap().nonce_floor, 10);
        let mut corrupt: Value =
            serde_json::from_slice(&fs::read(root.join("current.json")).unwrap()).unwrap();
        corrupt["payload"]["nonce_floor"] = 0.into();
        fs::write(
            root.join("current.json"),
            serde_json::to_vec(&corrupt).unwrap(),
        )
        .unwrap();
        assert!(load(&root).is_err());
        pointer.index = 1;
        pointer.previous_seal = Some("missing".into());
        publish(&root, &pointer).unwrap();
        assert!(load(&root).is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
