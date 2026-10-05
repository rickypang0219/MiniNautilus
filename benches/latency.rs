use mininautilus::queue::{self, Bitmap, PopError, PushError, Wait};
use std::{hint::black_box, time::Instant};

fn percentile(sorted: &[u128], numerator: usize, denominator: usize) -> u128 {
    sorted[((sorted.len() - 1) * numerator) / denominator]
}
fn main() {
    let samples = std::env::var("MINI_SAMPLES")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(50_000);
    assert!(samples > 0);
    let pin = std::env::var("MINI_PIN").ok().map(|s| {
        let cpus: Vec<usize> = s
            .split(',')
            .map(|c| c.parse().expect("MINI_PIN=producer_cpu,consumer_cpu"))
            .collect();
        assert_eq!(cpus.len(), 2);
        (cpus[0], cpus[1])
    });
    println!(
        "# queue offer-to-receive latency; saturated producer; includes backpressure; not engine latency"
    );
    println!(
        "# os={}, arch={}, pin={pin:?}, samples={samples}",
        std::env::consts::OS,
        std::env::consts::ARCH
    );
    println!("padded,wait,p50_ns,p99_ns,p999_ns,messages_per_sec,full_retries,peak_backlog");
    for padded in [false, true] {
        for wait in [Wait::Spin, Wait::Yield, Wait::Hybrid] {
            let (mut tx, mut rx) = queue::channel(256, padded);
            let warmup = 5_000;
            let producer = std::thread::spawn(move || {
                if let Some((cpu, _)) = pin {
                    queue::pin_current(cpu).expect("producer affinity failed");
                }
                let mut retries = 0u64;
                let mut peak = 0;
                for index in 0..samples + warmup {
                    let mut message = (index, Instant::now());
                    let mut attempt = 0;
                    loop {
                        match tx.try_push(message) {
                            Ok(()) => {
                                peak = peak.max(tx.len());
                                break;
                            }
                            Err(PushError::Full(value)) => {
                                message = value;
                                retries += 1;
                                attempt += 1;
                                wait.idle(attempt);
                            }
                            Err(PushError::Closed(_)) => panic!("consumer closed"),
                        }
                    }
                }
                (retries, peak)
            });
            let consumer = std::thread::spawn(move || {
                if let Some((_, cpu)) = pin {
                    queue::pin_current(cpu).expect("consumer affinity failed");
                }
                let mut latencies = Vec::with_capacity(samples);
                let mut measured_start = None;
                for expected in 0..samples + warmup {
                    let mut attempt = 0;
                    loop {
                        match rx.try_pop() {
                            Ok((index, start)) => {
                                assert_eq!(index, expected);
                                if expected >= warmup {
                                    measured_start.get_or_insert_with(Instant::now);
                                    latencies.push(start.elapsed().as_nanos());
                                }
                                break;
                            }
                            Err(PopError::Empty) => {
                                attempt += 1;
                                wait.idle(attempt);
                            }
                            Err(PopError::Closed) => panic!("producer closed early"),
                        }
                    }
                }
                (latencies, measured_start.unwrap().elapsed())
            });
            let (retries, peak) = producer.join().unwrap();
            let (mut latencies, duration) = consumer.join().unwrap();
            latencies.sort_unstable();
            println!(
                "{padded},{wait:?},{},{},{},{:.0},{retries},{peak}",
                percentile(&latencies, 50, 100),
                percentile(&latencies, 99, 100),
                percentile(&latencies, 999, 1000),
                samples as f64 / duration.as_secs_f64()
            );
        }
    }
    locality();
}

fn locality() {
    const N: usize = 100_000;
    let contiguous: Vec<u64> = (0..N as u64).collect();
    // Indirection/allocation contrast; allocator placement and prefetching affect results.
    let boxed: Vec<Box<u64>> = contiguous.iter().map(|v| Box::new(*v)).collect();
    let mut dirty = Bitmap::with_capacity(N);
    for i in (0..N).step_by(16) {
        dirty.set(i, true);
    }
    for name in ["contiguous_scan", "boxed_scan", "bitmap_sparse_scan"] {
        let start = Instant::now();
        for _ in 0..100 {
            let sum: u64 = match name {
                "contiguous_scan" => black_box(&contiguous).iter().copied().sum(),
                "boxed_scan" => black_box(&boxed).iter().map(|v| **v).sum(),
                _ => black_box(&dirty).ones().map(|i| contiguous[i]).sum(),
            };
            black_box(sum);
        }
        println!(
            "# {name}: {} ns; bitmap visits 1/16 of entries, not equivalent work",
            start.elapsed().as_nanos()
        );
    }
}
