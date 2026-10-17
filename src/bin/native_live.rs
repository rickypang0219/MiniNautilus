use mininautilus::native::{
    self, Result, fail,
    runtime::{self, Options},
    stream,
    venue::{Http, Instrument},
};
use serde_json::json;
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
fn main() {
    if let Err(error) = run() {
        eprintln!("native-live: {error}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let Some(command) = args.first() else {
        return Err(fail(
            "usage: native-live probe | paper | testnet | recover --run-dir DIR --seconds N [--python-strategy] [--flatten]",
        ));
    };
    let mut dir = None;
    let mut manifest = None;
    let mut seconds = 15;
    let mut python = false;
    let mut flatten = false;
    let mut crash = false;
    let mut rotation = native::lifecycle::Policy::default();
    let mut fixture_orders = 10000;
    let mut resolve_unsent = false;
    let mut n = 1;
    while n < args.len() {
        match args[n].as_str() {
            "--manifest" => {
                n += 1;
                manifest = Some(PathBuf::from(
                    args.get(n).ok_or_else(|| fail("missing manifest"))?,
                ));
            }
            "--run-dir" => {
                n += 1;
                dir = Some(PathBuf::from(
                    args.get(n).ok_or_else(|| fail("missing directory"))?,
                ));
            }
            "--seconds" => {
                n += 1;
                seconds = args
                    .get(n)
                    .ok_or_else(|| fail("missing seconds"))?
                    .parse()?;
            }
            "--python-strategy" => python = true,
            "--flatten" => flatten = true,
            "--crash-after-fill" => crash = true,
            "--resolve-unsent" => resolve_unsent = true,
            "--rotate-orders" => {
                n += 1;
                rotation.orders = args
                    .get(n)
                    .ok_or_else(|| fail("missing rotation orders"))?
                    .parse()?;
            }
            "--rotate-seconds" => {
                n += 1;
                rotation.age_seconds = args
                    .get(n)
                    .ok_or_else(|| fail("missing rotation age"))?
                    .parse()?;
            }
            "--rotation-flatten" => rotation.flatten_on_rotation = true,
            "--fixture-orders" => {
                n += 1;
                fixture_orders = args
                    .get(n)
                    .ok_or_else(|| fail("missing fixture order count"))?
                    .parse()?;
            }
            _ => return Err(fail("unknown option")),
        }
        n += 1;
    }
    if cfg!(debug_assertions)
        && matches!(
            command.as_str(),
            "testnet" | "diagnostic" | "recover" | "lifecycle" | "recover-lifecycle"
        )
    {
        return Err(fail("signed runtime requires a verified release build"));
    }
    match command.as_str() {
        "lifecycle-fixture" => println!(
            "{}",
            native::lifecycle::fixture(
                &dir.ok_or_else(|| fail("--run-dir required"))?,
                fixture_orders,
                rotation.orders
            )?
        ),
        "lifecycle" => {
            let result = native::lifecycle::run(
                &dir.ok_or_else(|| fail("--run-dir required"))?,
                Options {
                    signed: true,
                    python,
                    seconds,
                    flatten,
                },
                rotation,
            )?;
            println!("{result}");
        }
        "recover-lifecycle" => {
            println!(
                "{}",
                native::lifecycle::recover(
                    &dir.ok_or_else(|| fail("--run-dir required"))?,
                    flatten
                )?
            );
        }
        "bench" => {
            let m = serde_json::from_slice(&std::fs::read(
                manifest.ok_or_else(|| fail("--manifest required"))?,
            )?)?;
            println!(
                "{}",
                runtime::bench::run(&dir.ok_or_else(|| fail("--run-dir required"))?, m)?
            );
        }
        "probe" => {
            if seconds == 0 || seconds > 60 {
                return Err(fail("probe duration 1..60"));
            }
            let http = Http::new(false)?;
            let i = Instrument::from_meta(&http.info("meta", json!({}))?, "BTC")?;
            let quote = http.quote(&i)?;
            let stop = Arc::new(AtomicBool::new(false));
            let count = Arc::new(AtomicU64::new(0));
            let ready = Arc::new(AtomicBool::new(false));
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            rt.block_on(async {
                let s = stop.clone();
                tokio::spawn(async move {
                    tokio::time::sleep(Duration::from_secs(seconds)).await;
                    s.store(true, Ordering::Release);
                });
                stream::run(
                    false,
                    i.clone(),
                    std::env::var("HL_MAIN_ADDRESS").ok(),
                    stop,
                    |observation, _| {
                        match observation {
                            stream::Observation::Ready => ready.store(true, Ordering::Release),
                            stream::Observation::Quote { .. } => {
                                count.fetch_add(1, Ordering::Relaxed);
                            }
                            _ => {}
                        }
                        Ok(())
                    },
                )
                .await
            })?;
            if !ready.load(Ordering::Acquire) || count.load(Ordering::Acquire) == 0 {
                return Err(fail("stream not ready/no market observations"));
            }
            println!(
                "{}",
                json!({"at_ms":native::wall_ms(),"testnet":true,"rest":true,"websocket_subscriptions":true,"quotes":count.load(Ordering::Relaxed),"instrument":i,"bid_ticks":quote.0,"ask_ticks":quote.1,"signed_actions":0})
            );
        }
        "recover" => {
            println!(
                "{}",
                runtime::recover_with_proof(
                    &dir.ok_or_else(|| fail("--run-dir required"))?,
                    flatten,
                    resolve_unsent
                )?
            );
        }
        "diagnostic" => {
            println!(
                "{}",
                runtime::diagnostic(&dir.ok_or_else(|| fail("--run-dir required"))?, crash)?
            );
        }
        "paper" | "testnet" => {
            let dir = dir.ok_or_else(|| fail("--run-dir required"))?;
            let result = runtime::run(
                &dir,
                Options {
                    signed: command != "paper",
                    python,
                    seconds,
                    flatten,
                },
            )?;
            println!("{}", result);
            if result["failed"] == true {
                return Err(fail("run failed; original journals and report retained"));
            }
        }
        _ => return Err(fail("unknown command")),
    }
    Ok(())
}
