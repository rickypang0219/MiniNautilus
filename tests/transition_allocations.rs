//! Allocation guard for the steady-state market-data path (not journal/IPC).
use mininautilus::{core::Core, model::*};
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;

thread_local! {
    static TRACK: Cell<bool> = const { Cell::new(false) };
    static COUNTS: Cell<(usize, usize)> = const { Cell::new((0, 0)) };
}
struct Counted;
fn record(alloc: bool) {
    let _ = TRACK.try_with(|track| {
        if track.get() {
            let _ = COUNTS.try_with(|counts| {
                let (a, d) = counts.get();
                counts.set((a + usize::from(alloc), d + usize::from(!alloc)));
            });
        }
    });
}
// SAFETY: All allocation operations are forwarded unchanged to System. Counters
// are thread-local fixed-size Cells and neither allocate nor access payloads.
unsafe impl GlobalAlloc for Counted {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        record(true);
        unsafe { System.alloc(layout) }
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        record(true);
        unsafe { System.alloc_zeroed(layout) }
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        record(false);
        unsafe { System.dealloc(ptr, layout) }
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        record(true);
        unsafe { System.realloc(ptr, layout, size) }
    }
}
#[global_allocator]
static ALLOCATOR: Counted = Counted;

#[test]
fn market_events_do_not_allocate_or_free_history() {
    for history in [0, 100, 5000] {
        let mut core = Core::new(Config::default()).unwrap();
        // Populate cold history outside the measured scope; values are never
        // consulted by these events. Allocation counts must not depend on size.
        for id in 1..=history {
            core.orders.insert(
                id,
                Order {
                    intent: Intent {
                        id,
                        side: Side::Buy,
                        qty: 1,
                        limit: 100,
                        based_on_seq: 0,
                        valid_until: 100,
                    },
                    filled: 1,
                    lifecycle: Lifecycle::Filled,
                    pending: None,
                    deadline: None,
                    uncertain: false,
                },
            );
            core.fills.insert(
                id,
                Fill {
                    execution_id: id,
                    order_id: id,
                    qty: 1,
                    price: 100,
                },
            );
        }
        core.position = history as i64;
        core.cash = -(history as i128) * 100;
        COUNTS.with(|c| c.set((0, 0)));
        TRACK.with(|t| t.set(true));
        for event in [
            Event::Quote { bid: 99, ask: 101 },
            Event::QuoteObserved {
                bid: 99,
                ask: 101,
                observed_at: 0,
            },
            Event::Trade {
                taker: Side::Buy,
                price: 100,
                qty: 1,
            },
            Event::MarketUnavailable,
            Event::Heartbeat { epoch: 0 },
        ] {
            let effects = core
                .apply(&Envelope {
                    seq: core.seq + 1,
                    at: 0,
                    event,
                })
                .unwrap();
            assert!(effects.is_empty());
        }
        TRACK.with(|t| t.set(false));
        let counts = COUNTS.with(Cell::get);
        assert_eq!(counts, (0, 0), "history={history}");
        assert_eq!(core.orders.len(), history as usize);
        assert_eq!(core.fills.len(), history as usize);
    }
}
