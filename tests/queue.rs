use mininautilus::queue::*;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

#[test]
fn bounded_fifo_full_empty_and_closed() {
    let (mut tx, mut rx) = channel(2, true);
    assert_eq!(rx.try_pop(), Err(PopError::Empty));
    tx.try_push(1).unwrap();
    tx.try_push(2).unwrap();
    assert_eq!(tx.try_push(3), Err(PushError::Full(3)));
    assert_eq!(rx.try_pop(), Ok(1));
    tx.try_push(3).unwrap();
    drop(tx);
    assert_eq!(rx.try_pop(), Ok(2));
    assert_eq!(rx.try_pop(), Ok(3));
    assert_eq!(rx.try_pop(), Err(PopError::Closed));
}

#[test]
fn cross_thread_payload_visibility_and_repeated_slot_reuse() {
    for padded in [false, true] {
        let (mut tx, mut rx) = channel(8, padded);
        let producer = std::thread::spawn(move || {
            for n in 0..100_000u64 {
                let mut value = [n, n ^ 0xdeadbeef, n * 3];
                loop {
                    match tx.try_push(value) {
                        Ok(()) => break,
                        Err(PushError::Full(v)) => {
                            value = v;
                            std::hint::spin_loop();
                        }
                        Err(PushError::Closed(_)) => panic!("consumer closed"),
                    }
                }
            }
        });
        for n in 0..100_000u64 {
            loop {
                match rx.try_pop() {
                    Ok(value) => {
                        assert_eq!(value, [n, n ^ 0xdeadbeef, n * 3]);
                        break;
                    }
                    Err(PopError::Empty) => std::hint::spin_loop(),
                    Err(PopError::Closed) => panic!("premature closure"),
                }
            }
        }
        producer.join().unwrap();
        assert_eq!(rx.try_pop(), Err(PopError::Closed));
    }
}

#[test]
fn unconsumed_payloads_drop_exactly_once() {
    #[derive(Debug)]
    struct Item(Arc<AtomicUsize>);
    impl Drop for Item {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::Relaxed);
        }
    }
    let count = Arc::new(AtomicUsize::new(0));
    let (mut tx, mut rx) = channel(4, true);
    for _ in 0..3 {
        tx.try_push(Item(count.clone())).unwrap();
    }
    drop(rx.try_pop().unwrap());
    drop(rx);
    assert!(matches!(
        tx.try_push(Item(count.clone())),
        Err(PushError::Closed(_))
    ));
    drop(tx);
    assert_eq!(count.load(Ordering::Relaxed), 4);
}

#[test]
fn bitmap_word_boundaries() {
    let mut bits = Bitmap::with_capacity(130);
    for bit in [0, 63, 64, 129] {
        bits.set(bit, true);
    }
    bits.set(63, false);
    assert_eq!(bits.ones().collect::<Vec<_>>(), vec![0, 64, 129]);
    assert!(!bits.contains(63));
    assert!(!bits.contains(999));
}
