//! The strategy boundary carries bars and expiring targets, never venue ownership.
use super::{
    Result, fail,
    venue::{Http, Instrument, number, rows},
    wall_ms,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    collections::VecDeque,
    io::{BufRead, BufReader, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar {
    pub open_ms: u64,
    pub close_ms: u64,
    pub price: i64,
}
pub fn bars(http: &Http, instrument: &Instrument, start: u64, end: u64) -> Result<Vec<Bar>> {
    if end < start || end - start > 502 * 60000 {
        return Err(fail("bounded candle window exceeded"));
    }
    let response = http.info(
        "candleSnapshot",
        json!({"req":{"coin":instrument.symbol,"interval":"1m","startTime":start,"endTime":end}}),
    )?;
    let input = rows(&response)?;
    if input.len() > 503 {
        return Err(fail("candle response cap"));
    }
    let mut out = std::collections::BTreeMap::new();
    for row in input {
        let open = number(&row["t"])?;
        let close = number(&row["T"])?;
        if row["s"] != instrument.symbol
            || row["i"] != "1m"
            || !open.is_multiple_of(60000)
            || close != open + 59999
            || open < start
            || open > end
        {
            return Err(fail("candle identity/window"));
        }
        if close >= wall_ms() {
            continue;
        }
        let bar = Bar {
            open_ms: open,
            close_ms: close,
            price: instrument.price(&row["c"])?,
        };
        if out.get(&open).is_some_and(|old| *old != bar) {
            return Err(fail("changed closed candle"));
        }
        out.insert(open, bar);
    }
    let out: Vec<_> = out.into_values().collect();
    if out.windows(2).any(|w| w[1].open_ms - w[0].open_ms != 60000) {
        return Err(fail("missing closed candle"));
    }
    Ok(out)
}
pub struct Sma {
    values: VecDeque<i64>,
    fast: usize,
    slow: usize,
    fast_sum: i128,
    slow_sum: i128,
    pub last: Option<Bar>,
    pub target: i64,
}
impl Sma {
    pub fn new(fast: usize, slow: usize) -> Result<Self> {
        if fast == 0 || fast >= slow || slow > 500 {
            return Err(fail("SMA window"));
        }
        Ok(Self {
            values: VecDeque::with_capacity(slow),
            fast,
            slow,
            fast_sum: 0,
            slow_sum: 0,
            last: None,
            target: 0,
        })
    }
    pub fn add(&mut self, bar: Bar) -> Result<Option<i64>> {
        if bar.price <= 0
            || !bar.open_ms.is_multiple_of(60000)
            || bar.close_ms != bar.open_ms + 59999
        {
            return Err(fail("invalid closed bar"));
        }
        if let Some(last) = &self.last {
            if bar.open_ms < last.open_ms {
                return Ok(None);
            }
            if bar.open_ms == last.open_ms {
                if bar != *last {
                    return Err(fail("changed closed bar"));
                }
                return Ok(None);
            }
            if bar.open_ms - last.open_ms != 60000 {
                return Err(fail("strategy candle gap"));
            }
        }
        if self.values.len() >= self.fast {
            self.fast_sum -= i128::from(self.values[self.values.len() - self.fast]);
        }
        if self.values.len() == self.slow {
            self.slow_sum -= i128::from(self.values.pop_front().unwrap());
        }
        self.values.push_back(bar.price);
        self.fast_sum += i128::from(bar.price);
        self.slow_sum += i128::from(bar.price);
        self.last = Some(bar);
        if self.values.len() < self.slow {
            return Ok(None);
        }
        let difference = self.fast_sum * self.slow as i128 - self.slow_sum * self.fast as i128;
        if difference != 0 {
            self.target = if difference > 0 { 20 } else { -20 };
        }
        Ok(Some(self.target))
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Request {
    pub session: String,
    pub epoch: u64,
    pub revision: u64,
    pub deadline_ms: u64,
    pub bar: Bar,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Answer {
    pub session: String,
    pub epoch: u64,
    pub revision: u64,
    pub deadline_ms: u64,
    pub target: Option<i64>,
}
pub fn validate_answer(request: &Request, answer: &Answer, epoch: u64, now: u64) -> Result<()> {
    if answer.session != request.session
        || answer.epoch != request.epoch
        || answer.epoch != epoch
        || answer.revision != request.revision
        || answer.deadline_ms != request.deadline_ms
        || now > request.deadline_ms
        || answer.target.is_some_and(|t| ![-20, 0, 20].contains(&t))
    {
        return Err(fail("stale/mismatched strategy result"));
    }
    Ok(())
}
pub enum Strategy {
    Native(Sma),
    Python {
        child: std::process::Child,
        reader: BufReader<UnixStream>,
    },
}
impl Strategy {
    pub fn open(python: bool, dir: &Path) -> Result<Self> {
        if !python {
            return Ok(Self::Native(Sma::new(3, 8)?));
        }
        let path = dir.join("strategy.sock");
        let listener = UnixListener::bind(&path)?;
        listener.set_nonblocking(true)?;
        let mut child = Command::new(std::env::var("PYTHON").unwrap_or_else(|_| "python3".into()))
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("LANG", "C.UTF-8")
            .env("PYTHONNOUSERSITE", "1")
            .args(["examples/native_strategy.py", "--socket"])
            .arg(&path)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let started = Instant::now();
        let stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    if started.elapsed() > Duration::from_secs(5) || child.try_wait()?.is_some() {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(fail("strategy process startup failed"));
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(e) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(e.into());
                }
            }
        };
        std::fs::remove_file(path)?;
        stream.set_read_timeout(Some(Duration::from_millis(1200)))?;
        stream.set_write_timeout(Some(Duration::from_millis(1200)))?;
        Ok(Self::Python {
            child,
            reader: BufReader::new(stream),
        })
    }
    pub fn calculate(&mut self, request: &Request) -> Result<Answer> {
        match self {
            Self::Native(sma) => Ok(Answer {
                session: request.session.clone(),
                epoch: request.epoch,
                revision: request.revision,
                deadline_ms: request.deadline_ms,
                target: sma.add(request.bar.clone())?,
            }),
            Self::Python { reader, .. } => {
                let remaining_ms = request.deadline_ms.saturating_sub(wall_ms()).min(1200);
                if remaining_ms == 0 {
                    return Err(fail("strategy deadline expired"));
                }
                let deadline = Instant::now() + Duration::from_millis(remaining_ms);
                reader
                    .get_ref()
                    .set_write_timeout(Some(Duration::from_millis(remaining_ms)))?;
                let mut bytes = serde_json::to_vec(request)?;
                bytes.push(b'\n');
                reader.get_mut().write_all(&bytes)?;
                // read_until alone would be unbounded on hostile strategy output.
                let mut line = Vec::new();
                loop {
                    let remaining = deadline
                        .checked_duration_since(Instant::now())
                        .filter(|d| !d.is_zero())
                        .ok_or_else(|| fail("strategy absolute deadline exceeded"))?;
                    reader.get_ref().set_read_timeout(Some(remaining))?;
                    let available = reader.fill_buf()?;
                    if available.is_empty() {
                        return Err(fail("strategy EOF"));
                    }
                    let count = available
                        .iter()
                        .position(|b| *b == b'\n')
                        .map_or(available.len(), |n| n + 1);
                    if line.len() + count > 8192 {
                        return Err(fail("strategy output cap"));
                    }
                    let done = available[count - 1] == b'\n';
                    line.extend(&available[..count]);
                    reader.consume(count);
                    if done {
                        break;
                    }
                }
                serde_json::from_slice(&line).map_err(|_| fail("strategy output schema"))
            }
        }
    }
}
impl Drop for Strategy {
    fn drop(&mut self) {
        if let Self::Python { child, .. } = self {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}
