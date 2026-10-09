//! B4: the backtest runner's timing model, pinned by fixtures.
//!
//! Bar i: Quote, Trade (resting orders may fill), Heartbeat, Tick; then the plan
//! for bar i's target (SetTarget, Cancel open orders, SubmitTargeted). An order
//! submitted at bar i's close is first exposed to bar i+1's trade, and that trade
//! is matched BEFORE bar i+1's cancel: a cancel can lose the race to a fill.
use mininautilus::{
    backtest::{self, Bar, Policy},
    model::*,
};

fn bar(index: u64, price: i64, volume: i64, taker: Side) -> Bar {
    Bar {
        at: index * 60_000,
        price,
        volume,
        taker,
    }
}

fn run(bars: &[Bar], targets: &[(usize, i64)]) -> backtest::Backtest {
    let (run, summary) =
        backtest::run(bars, targets, Config::default(), Policy::default(), true).unwrap();
    assert_eq!(summary.health, Some(Health::Healthy));
    assert_eq!(run.core.position, run.venue.position);
    run
}

#[test]
fn order_fills_on_the_next_bar_before_that_bars_cancel() {
    // Target +2 at bar 0 -> buy 2 @100 rests. Bar 1 sells 2 @100: filled in bar 1's
    // market step, so bar 1's plan has nothing left to cancel.
    let bars = [bar(0, 100, 5, Side::Buy), bar(1, 100, 2, Side::Sell)];
    let run = run(&bars, &[(0, 2)]);
    let ledger = run.ledger.unwrap();
    assert_eq!(ledger.len(), 1);
    assert_eq!((ledger[0].bar, ledger[0].qty, ledger[0].price), (1, 2, 100));
    assert_eq!(run.core.position, 2);
    assert_eq!(run.core.orders[&1].lifecycle, Lifecycle::Filled);
}

#[test]
fn same_bar_never_fills_its_own_decision() {
    // Bar 0's trade happens before bar 0's decision: no look-ahead fill.
    let run = run(&[bar(0, 100, 50, Side::Sell)], &[(0, 1)]);
    assert!(run.ledger.unwrap().is_empty());
    assert_eq!(run.core.position, 0);
    assert_eq!(run.core.orders[&1].lifecycle, Lifecycle::Accepted);
}

#[test]
fn partial_fill_then_cancel_then_resubmit_the_remainder() {
    // Bar 1 fills 1 of 3; bar 1's plan cancels the rest and submits order 2 for
    // the remaining 2. Bar 2's taker is a buyer, which cannot hit a resting buy, so
    // bar 2's plan cancels order 2 and submits order 3; bar 3's seller fills it
    // at the trade price (99), better than its limit (100).
    let bars = [
        bar(0, 100, 5, Side::Buy),
        bar(1, 100, 1, Side::Sell),
        bar(2, 100, 9, Side::Buy),
        bar(3, 99, 9, Side::Sell),
    ];
    let run = run(&bars, &[(0, 3)]);
    let ledger = run.ledger.unwrap();
    let fills: Vec<_> = ledger
        .iter()
        .map(|f| (f.bar, f.order_id, f.qty, f.price))
        .collect();
    assert_eq!(fills, [(1, 1, 1, 100), (3, 3, 2, 99)]);
    assert_eq!(run.core.orders[&1].lifecycle, Lifecycle::Canceled);
    assert_eq!(run.core.orders[&2].lifecycle, Lifecycle::Canceled);
    assert_eq!(run.core.position, 3);
}

#[test]
fn bar_and_target_files_are_validated() {
    let ok = "at,price,volume,taker\n0,100,1,Buy\n60000,101,2,Sell\n";
    let bars = backtest::read_bars(ok.as_bytes()).unwrap();
    assert_eq!(bars[1], bar(1, 101, 2, Side::Sell));
    for bad in [
        "time,price,volume,taker\n",
        "at,price,volume,taker\n0,100,1,Buy\n0,100,1,Buy\n",
        "at,price,volume,taker\n0,0,1,Buy\n",
        "at,price,volume,taker\n0,100,1,Both\n",
    ] {
        assert!(backtest::read_bars(bad.as_bytes()).is_err(), "{bad}");
    }
    assert_eq!(
        backtest::read_targets("bar,position\n0,1\n1,-1\n".as_bytes(), 2).unwrap(),
        [(0, 1), (1, -1)]
    );
    assert!(backtest::read_targets("bar,position\n1,1\n0,1\n".as_bytes(), 2).is_err());
    assert!(backtest::read_targets("bar,position\n5,1\n".as_bytes(), 2).is_err());
}
