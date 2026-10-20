#!/usr/bin/env python3
"""Independent native SMA arithmetic comparison; separate from order execution.

All use full-window warm-up, long/flat targets, and HOLD the previous target when
averages are equal. This deliberately does not compare library crossover helpers,
which may have different equality/warm-up contracts.
"""
import json
import random
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
from mininautilus.sma import SmaStrategy,Candle


def check(seed,n=1000,fast=5,slow=20):
    import backtrader as bt
    import pandas as pd
    import talib
    import numpy as np
    from nautilus_trader.indicators import SimpleMovingAverage
    rng=random.Random(seed)
    prices=[100]
    for _ in range(n-1):prices.append(max(1,prices[-1]+rng.choice([-2,-1,0,1,2])))
    # Include exact equality plateaus to exercise tie behavior.
    prices[200:240]=[prices[199]]*40
    mini=SmaStrategy(fast,slow,2,'1s',long_only=True)
    nt_fast,nt_slow=SimpleMovingAverage(fast),SimpleMovingAverage(slow)
    mini_targets=[]
    nt_targets=[]
    nt_target=0
    expected=[]
    oracle_target=0
    for i,p in enumerate(prices):
        mini.add(Candle(i*1000,i*1000+999,p))
        mini_targets.append(mini.target)
        nt_fast.update_raw(p);nt_slow.update_raw(p)
        if i>=slow-1:
            difference=sum(prices[i-fast+1:i+1])*slow-sum(prices[i-slow+1:i+1])*fast
            if difference:oracle_target=2 if difference>0 else 0
            difference=nt_fast.value-nt_slow.value
            if difference:nt_target=2 if difference>0 else 0
        expected.append(oracle_target)
        nt_targets.append(nt_target)
    arr=np.array(prices,dtype='float64')
    ta_fast,ta_slow=talib.SMA(arr,fast),talib.SMA(arr,slow)
    ta_targets=[]
    target=0
    for a,b in zip(ta_fast,ta_slow):
        if not np.isnan(b) and a!=b:target=2 if a>b else 0
        ta_targets.append(target)
    bt_targets=[]
    class Native(bt.Strategy):
        def __init__(self):
            self.fast=bt.ind.SMA(period=fast)
            self.slow=bt.ind.SMA(period=slow)
            self.target=0
        def prenext(self):bt_targets.append(0)
        def next(self):
            if self.fast[0]!=self.slow[0]:self.target=2 if self.fast[0]>self.slow[0] else 0
            bt_targets.append(self.target)
    engine=bt.Cerebro(stdstats=False)
    engine.adddata(bt.feeds.PandasData(dataname=pd.DataFrame(dict(open=prices,high=prices,low=prices,close=prices,volume=1000),index=pd.date_range('2024-01-01',periods=n,freq='s'))))
    engine.addstrategy(Native)
    engine.run()
    for name,actual in [('MiniNautilus',mini_targets),('NautilusTrader',nt_targets),('Backtrader',bt_targets),('TA-Lib (vn.py SMA dependency)',ta_targets)]:
        assert actual==expected,(seed,name,next((i for i,(a,b) in enumerate(zip(actual,expected)) if a!=b),None))
    return dict(seed=seed,observations=n,fast=fast,slow=slow,targets_equal=True)


if __name__=='__main__':
    results=[check(s) for s in range(20)]
    output=Path(sys.argv[1])
    output.write_text(json.dumps(results,indent=2))
    print(f'{len(results)} seeds, 20000 observations: native SMA targets agree')
