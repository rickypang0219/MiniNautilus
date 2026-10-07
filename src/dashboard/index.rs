//! Rebuildable disk-backed cold index. The journal is the durable source of truth.
use super::ledger::{self, Query};
use rusqlite::{Connection, params, params_from_iter, types::Value as SqlValue};
use serde_json::{Value, json};

pub struct Index {
    connection: Connection,
}
fn ordinal(n: u64) -> String {
    format!("{n:020}")
}
impl Index {
    pub fn new() -> rusqlite::Result<Self> {
        // SQLite's empty filename creates a private temporary disk database,
        // removed on close. No shared cache path, engine lock, or stale cache reuse.
        let connection = Connection::open("")?;
        connection.execute_batch("PRAGMA temp_store=FILE; PRAGMA cache_size=-8192;
            CREATE TABLE records(session TEXT NOT NULL,kind TEXT NOT NULL,cursor TEXT NOT NULL,
                seq TEXT NOT NULL,event_ms INTEGER,received_ms INTEGER,engine_ms TEXT,side TEXT,
                search TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(session,kind,cursor)) WITHOUT ROWID;
            CREATE INDEX event_time ON records(session,kind,COALESCE(event_ms,received_ms));
            CREATE INDEX received_time ON records(session,kind,received_ms);")?;
        Ok(Self { connection })
    }
    /// Rows and their published summary are committed under the same observer lock.
    pub fn put(&mut self, session: &str, groups: &[(String, Vec<Value>)]) -> rusqlite::Result<()> {
        let tx = self.connection.transaction()?;
        {
            let mut statement = tx.prepare_cached(
                "INSERT INTO records VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)
                ON CONFLICT(session,kind,cursor) DO UPDATE SET event_ms=excluded.event_ms,
                received_ms=excluded.received_ms,engine_ms=excluded.engine_ms,side=excluded.side,
                search=excluded.search,body=excluded.body WHERE body<>excluded.body",
            )?;
            for (kind, rows) in groups {
                for row in rows {
                    let body = row.to_string();
                    statement.execute(params![
                        session,
                        kind,
                        ledger::cursor(row),
                        ordinal(ledger::seq(row)),
                        row["event_time_ms"].as_i64(),
                        row["received_time_ms"].as_i64(),
                        row["at"].as_u64().map(ordinal),
                        row["side"].as_str(),
                        body.to_lowercase(),
                        body
                    ])?;
                }
            }
        }
        tx.commit()
    }
    pub fn page(&self, session: &str, latest: u64, q: &Query) -> rusqlite::Result<Value> {
        let until = q.until.unwrap_or(latest).min(latest);
        let mut filter = "session=? AND kind=?".to_owned();
        let mut bindings: Vec<SqlValue> = vec![session.to_owned().into(), q.kind.clone().into()];
        if q.side != "all" {
            filter.push_str(" AND side=?");
            bindings.push(q.side.clone().into());
        }
        if !q.search.is_empty() {
            filter.push_str(" AND instr(search,?)>0");
            bindings.push(q.search.clone().into());
        }
        if let Some(after) = q.after {
            filter.push_str(" AND seq>?");
            bindings.push(ordinal(after).into());
        }
        let time = match q.basis.as_str() {
            "engine" => "engine_ms",
            "received" => "received_ms",
            _ => "COALESCE(event_ms,received_ms)",
        };
        let count = |sql: &str, b: &Vec<SqlValue>| -> rusqlite::Result<u64> {
            self.connection
                .query_row(sql, params_from_iter(b), |r| r.get::<_, i64>(0))
                .map(|n| n as u64)
        };
        let missing = if q.from.is_some() || q.to.is_some() {
            count(
                &format!("SELECT COUNT(*) FROM records WHERE {filter} AND {time} IS NULL"),
                &bindings,
            )?
        } else {
            0
        };
        for (sign, value) in [(">=", q.from), ("<=", q.to)] {
            if let Some(v) = value {
                filter.push_str(&format!(" AND {time}{sign}?"));
                bindings.push(if q.basis == "engine" {
                    ordinal(v).into()
                } else {
                    SqlValue::Integer(v.min(i64::MAX as u64) as i64)
                });
            }
        }
        let mut newer_bindings = bindings.clone();
        newer_bindings.push(ordinal(until).into());
        let newer = count(
            &format!("SELECT COUNT(*) FROM records WHERE {filter} AND seq>?"),
            &newer_bindings,
        )?;
        filter.push_str(" AND seq<=?");
        bindings.push(ordinal(until).into());
        let total = count(
            &format!("SELECT COUNT(*) FROM records WHERE {filter}"),
            &bindings,
        )?;
        if let Some(before) = &q.before {
            filter.push_str(" AND cursor<?");
            bindings.push(before.clone().into());
        }
        bindings.push(((q.limit + 1) as i64).into());
        let sql =
            format!("SELECT cursor,body FROM records WHERE {filter} ORDER BY cursor DESC LIMIT ?");
        let mut statement = self.connection.prepare(&sql)?;
        let mut rows: Vec<(String, String)> = statement
            .query_map(params_from_iter(&bindings), |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        let more = rows.len() > q.limit;
        rows.truncate(q.limit);
        let values: Vec<Value> = rows
            .iter()
            .map(|(_, body)| serde_json::from_str(body).expect("index holds serialized JSON"))
            .collect();
        Ok(
            json!({"kind":q.kind,"rows":values,"total":total,"newer":newer,"next_cursor":if more{rows.last().map(|(c,_)|c)}else{None},"until":until.to_string(),"latest_seq":latest.to_string(),"missing_time":missing,"limit":q.limit}),
        )
    }
}
