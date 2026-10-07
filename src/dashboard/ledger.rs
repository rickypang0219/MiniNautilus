//! Cursor membership is based on ingestion sequence, never wall-clock ordering.
use serde_json::{Value, json};

const KINDS: [&str; 5] = ["trades", "orders", "actions", "signals", "events"];
#[derive(Debug)]
pub struct Query {
    pub kind: String,
    pub limit: usize,
    pub before: Option<String>,
    pub until: Option<u64>,
    pub after: Option<u64>,
    pub from: Option<u64>,
    pub to: Option<u64>,
    pub basis: String,
    pub side: String,
    pub search: String,
}
fn decode(s: &str) -> Result<String, String> {
    let mut bytes = Vec::new();
    let mut it = s.bytes();
    while let Some(b) = it.next() {
        match b {
            b'+' => bytes.push(b' '),
            b'%' => {
                let hi = (it.next().ok_or("bad URL encoding")? as char)
                    .to_digit(16)
                    .ok_or("bad URL encoding")?;
                let lo = (it.next().ok_or("bad URL encoding")? as char)
                    .to_digit(16)
                    .ok_or("bad URL encoding")?;
                bytes.push((hi * 16 + lo) as u8);
            }
            _ => bytes.push(b),
        }
    }
    String::from_utf8(bytes).map_err(|_| "invalid UTF-8 query".into())
}
impl Query {
    pub fn parse(query: &str) -> Result<Self, String> {
        let mut q = Self {
            kind: "trades".into(),
            limit: 50,
            before: None,
            until: None,
            after: None,
            from: None,
            to: None,
            basis: "event".into(),
            side: "all".into(),
            search: String::new(),
        };
        for part in query.split('&').filter(|s| !s.is_empty()) {
            let (k, v) = part.split_once('=').ok_or("invalid query")?;
            let v = decode(v)?;
            match k {
                "kind" => q.kind = v,
                "limit" => q.limit = v.parse().map_err(|_| "invalid limit")?,
                "before"
                    if v.len() == 41
                        && v.as_bytes()[20] == b':'
                        && v.bytes()
                            .enumerate()
                            .all(|(i, b)| i == 20 || b.is_ascii_digit()) =>
                {
                    q.before = Some(v)
                }
                "until" => q.until = Some(v.parse().map_err(|_| "invalid sequence")?),
                "after" => q.after = Some(v.parse().map_err(|_| "invalid sequence")?),
                "from" => q.from = Some(v.parse().map_err(|_| "invalid time")?),
                "to" => q.to = Some(v.parse().map_err(|_| "invalid time")?),
                "basis" => q.basis = v,
                "side" => q.side = v,
                "q" if v.len() <= 256 => q.search = v.to_lowercase(),
                _ => return Err("unsupported query parameter".into()),
            }
        }
        if !KINDS.contains(&q.kind.as_str())
            || !(1..=200).contains(&q.limit)
            || !["event", "received", "engine"].contains(&q.basis.as_str())
            || !["all", "Buy", "Sell"].contains(&q.side.as_str())
            || q.from.zip(q.to).is_some_and(|(a, b)| a > b)
        {
            return Err("invalid history query".into());
        }
        Ok(q)
    }
}
pub fn seq(row: &Value) -> u64 {
    row["seq"]
        .as_str()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0)
}
pub fn cursor(row: &Value) -> String {
    format!(
        "{:020}:{:020}",
        seq(row),
        row["id"]
            .as_str()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(0)
    )
}
pub fn display_time(row: &Value, basis: &str) -> Option<u64> {
    match basis {
        "engine" => row["at"].as_u64(),
        "received" => row["received_time_ms"].as_u64(),
        _ => row["event_time_ms"]
            .as_u64()
            .or_else(|| row["received_time_ms"].as_u64()),
    }
}
pub fn page(view: &Value, query: &Query) -> Value {
    let latest = seq(view);
    let until = query.until.unwrap_or(latest).min(latest);
    let mut rows = Vec::new();
    let mut total = 0;
    let mut newer = 0;
    let mut unavailable = 0;
    for row in view[&query.kind].as_array().into_iter().flatten() {
        if query.side != "all" && row["side"].as_str() != Some(&query.side) {
            continue;
        }
        if !query.search.is_empty() && !row.to_string().to_lowercase().contains(&query.search) {
            continue;
        }
        if query.after.is_some_and(|s| seq(row) <= s) {
            continue;
        }
        let time = display_time(row, &query.basis);
        if (query.from.is_some() || query.to.is_some()) && time.is_none() {
            unavailable += 1;
            continue;
        }
        if query.from.is_some_and(|v| time.is_some_and(|t| t < v))
            || query.to.is_some_and(|v| time.is_some_and(|t| t > v))
        {
            continue;
        }
        if seq(row) > until {
            newer += 1;
            continue;
        }
        total += 1;
        let key = cursor(row);
        if query.before.as_ref().is_none_or(|before| &key < before) {
            rows.push((key, row));
        }
    }
    rows.sort_unstable_by(|a, b| b.0.cmp(&a.0));
    let has_more = rows.len() > query.limit;
    rows.truncate(query.limit);
    json!({"kind":query.kind,"rows":rows.iter().map(|(_,r)|r).collect::<Vec<_>>(),"total":total,"newer":newer,
        "next_cursor":if has_more {rows.last().map(|(k,_)|k)}else{None},"until":until.to_string(),"latest_seq":latest.to_string(),
        "missing_time":unavailable,"limit":query.limit})
}
pub fn summary(view: &Value, query: &Query) -> Value {
    let mut result = serde_json::Map::new();
    for (key, value) in view.as_object().into_iter().flatten() {
        if !KINDS.contains(&key.as_str()) {
            result.insert(key.clone(), value.clone());
        }
    }
    result.insert("ledger".into(), page(view, query));
    Value::Object(result)
}
