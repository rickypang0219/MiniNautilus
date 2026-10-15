//! Bounded WebSocket transport, including the managed environment's HTTP CONNECT proxy.
use super::{
    Result, fail,
    venue::{Instrument, number, parse_quote},
    wall_ms,
};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};
use tokio_tungstenite::{
    Connector, MaybeTlsStream, WebSocketStream,
    tungstenite::{Message, client::IntoClientRequest, protocol::WebSocketConfig},
};

async fn send(ws: &mut WebSocketStream<MaybeTlsStream<TcpStream>>, message: Message) -> Result<()> {
    tokio::time::timeout(Duration::from_secs(2), ws.send(message)).await??;
    Ok(())
}
pub async fn connect(mainnet: bool) -> Result<WebSocketStream<MaybeTlsStream<TcpStream>>> {
    let host = if mainnet {
        "api.hyperliquid.xyz"
    } else {
        "api.hyperliquid-testnet.xyz"
    };
    let proxy = std::env::var("HTTPS_PROXY")
        .or_else(|_| std::env::var("https_proxy"))
        .ok()
        .filter(|s| !s.is_empty());
    let mut stream = if let Some(proxy) = proxy {
        let p = reqwest::Url::parse(&proxy)?;
        if p.scheme() != "http" || !p.username().is_empty() || p.password().is_some() {
            return Err(fail("unsupported authenticated/non-HTTP proxy"));
        }
        let mut tcp = TcpStream::connect((
            p.host_str().ok_or_else(|| fail("proxy host"))?,
            p.port_or_known_default().unwrap_or(80),
        ))
        .await?;
        tcp.write_all(
            format!("CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n").as_bytes(),
        )
        .await?;
        let mut header = Vec::new();
        while !header.ends_with(b"\r\n\r\n") {
            if header.len() >= 8192 {
                return Err(fail("proxy header cap"));
            }
            header.push(tcp.read_u8().await?);
        }
        if !header.starts_with(b"HTTP/1.1 200 ") && !header.starts_with(b"HTTP/1.0 200 ") {
            return Err(fail("proxy CONNECT rejected"));
        }
        tcp
    } else {
        TcpStream::connect((host, 443)).await?
    };
    stream.flush().await?;
    stream.set_nodelay(true)?;
    let mut tls = native_tls::TlsConnector::builder();
    if let Ok(path) = std::env::var("SSL_CERT_FILE") {
        let bytes = std::fs::read(path)?;
        // Native TLS accepts one PEM certificate at a time; retain platform roots too.
        for part in
            bytes
                .split_inclusive(|b| *b == b'\n')
                .fold(Vec::<Vec<u8>>::new(), |mut acc, line| {
                    if line.starts_with(b"-----BEGIN CERTIFICATE-----") {
                        acc.push(Vec::new());
                    }
                    if let Some(last) = acc.last_mut() {
                        last.extend(line);
                    }
                    acc
                })
        {
            if let Ok(cert) = native_tls::Certificate::from_pem(&part) {
                tls.add_root_certificate(cert);
            }
        }
    }
    let config = WebSocketConfig::default()
        .max_message_size(Some(65536))
        .max_frame_size(Some(65536));
    let (socket, _) = tokio_tungstenite::client_async_tls_with_config(
        format!("wss://{host}/ws").into_client_request()?,
        stream,
        Some(config),
        Some(Connector::NativeTls(tls.build()?)),
    )
    .await?;
    Ok(socket)
}
#[derive(Debug)]
pub enum Observation {
    Quote {
        bid: i64,
        ask: i64,
        observed_at: u64,
        exchange_time: u64,
    },
    PrivateWake,
    Ready,
}
/// This stream is a wakeup for REST-verified private facts, never a fabricated venue sequence.
pub async fn run(
    mainnet: bool,
    instrument: Instrument,
    account: Option<String>,
    stop: Arc<AtomicBool>,
    mut publish: impl FnMut(Observation, Instant) -> Result<()>,
) -> Result<()> {
    let mut ws = tokio::time::timeout(Duration::from_secs(10), connect(mainnet)).await??;
    let mut subscriptions = vec![
        json!({"type":"l2Book","coin":instrument.symbol,"nSigFigs":null,"mantissa":null,"fast":false}),
    ];
    if let Some(user) = account {
        let user = user.to_ascii_lowercase();
        subscriptions.push(json!({"type":"orderUpdates","user":user}));
        subscriptions.push(json!({"type":"userFills","user":user,"aggregateByTime":false}));
    }
    for s in &subscriptions {
        send(
            &mut ws,
            Message::Text(
                json!({"method":"subscribe","subscription":s})
                    .to_string()
                    .into(),
            ),
        )
        .await?;
    }
    let mut confirmed = vec![false; subscriptions.len()];
    let mut ready = false;
    let opened = Instant::now();
    let mut last = Instant::now();
    let mut ping = Instant::now();
    while !stop.load(Ordering::Acquire) {
        if last.elapsed() > Duration::from_secs(45)
            || (!ready && opened.elapsed() > Duration::from_secs(10))
        {
            return Err(fail("WebSocket silence/subscription timeout"));
        }
        if ping.elapsed() > Duration::from_secs(20) {
            send(&mut ws, Message::Text("{\"method\":\"ping\"}".into())).await?;
            ping = Instant::now();
        }
        let message = match tokio::time::timeout(Duration::from_millis(100), ws.next()).await {
            Err(_) => continue,
            Ok(None) => return Err(fail("WebSocket closed")),
            Ok(Some(m)) => m?,
        };
        let received = Instant::now();
        last = received;
        let raw = match message {
            Message::Text(t) => t,
            Message::Ping(b) => {
                send(&mut ws, Message::Pong(b)).await?;
                continue;
            }
            Message::Pong(_) => continue,
            _ => return Err(fail("unexpected WebSocket frame")),
        };
        let event: Value = serde_json::from_str(&raw)?;
        match event["channel"].as_str() {
            Some("subscriptionResponse") => {
                let d = &event["data"];
                if d["method"] != "subscribe" {
                    return Err(fail("subscription method mismatch"));
                }
                let idx = subscriptions
                    .iter()
                    .position(|s| *s == d["subscription"])
                    .ok_or_else(|| fail("unexpected subscription"))?;
                confirmed[idx] = true;
                if !ready && confirmed.iter().all(|v| *v) {
                    ready = true;
                    publish(Observation::Ready, received)?;
                }
            }
            Some("l2Book") => {
                let (bid, ask) = parse_quote(&instrument, &event["data"], wall_ms())?;
                publish(
                    Observation::Quote {
                        bid,
                        ask,
                        observed_at: wall_ms(),
                        exchange_time: number(&event["data"]["time"])?,
                    },
                    received,
                )?;
            }
            Some("orderUpdates" | "userFills") if subscriptions.len() > 1 => {
                publish(Observation::PrivateWake, received)?
            }
            Some("pong") => {}
            _ => return Err(fail("unknown WebSocket channel")),
        }
    }
    let _ = tokio::time::timeout(Duration::from_secs(2), ws.close(None)).await;
    Ok(())
}
