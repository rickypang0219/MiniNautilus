#!/usr/bin/env python3
"""Demonstrate explicit model/configuration remedies using native engines.

The default audit is unchanged; these are separate alternative profiles.
"""
import json
import sys
from pathlib import Path
from adapters import backtrader, nautilus, mini
from cases import fixtures, case, submit
from run import verify_ledger, compare


def run(output):
    rows=[]
    for name in ['volume_partial','zero_volume','shared_liquidity','id_priority','price_priority','cancel_after_partial']:
        w=fixtures()[name]
        profiles=dict(mini=mini(w),
            backtrader_default=backtrader(w),
            backtrader_fixed_filler=backtrader(w,True),
            backtrader_shared_filler=backtrader(w,'shared'),
            nautilus_bar=nautilus(w),
            nautilus_tick=nautilus(w,data_kind='trade'),
            nautilus_tick_consuming=nautilus(w,data_kind='trade',liquidity_consumption=True))
        for result in profiles.values():verify_ledger(result,w)
        if name in ['volume_partial','zero_volume','shared_liquidity','cancel_after_partial']:
            assert all(compare(profiles['backtrader_shared_filler'],profiles['mini']).values()),name
            if name != 'volume_partial':
                assert all(compare(profiles['nautilus_tick_consuming'],profiles['mini']).values()),name
            else:
                # Version-pinned observation, not a passing model-equivalence check:
                # a distinct same-price/same-size trade fails to refresh L1 consumption.
                assert profiles['nautilus_tick_consuming']['position'] == 3
        for result in profiles.values():
            for key in ('elapsed_seconds','wall_seconds','probe_elapsed_seconds'):result.pop(key,None)
        rows.append(dict(case=name,workload=w,results=profiles))
    refresh=[]
    for volumes, expected in [([100,1,2,2],3),([100,1,2,3],6),
                              ([100,2,2,2],2),([100,2,1,2],5)]:
        w=case([101,100,100,100],{0:[submit(1,'Buy',6,100)]},volumes)
        r=nautilus(w,data_kind='trade',liquidity_consumption=True)
        verify_ledger(r,w)
        assert r['position'] == expected, (volumes,r)
        # The adapter creates unique TradeIds and timestamps for each observation.
        r.pop('elapsed_seconds',None)
        refresh.append(dict(workload=w,result=r,available_trade_volume=sum(volumes[1:])))
    Path(output).write_text(json.dumps(dict(profiles=rows,l1_refresh=refresh),indent=2)+'\n')
    print('Shared-volume remedy verified; pinned Nautilus L1 refresh failure reproduced in distinct ticks')


if __name__=='__main__':run(sys.argv[1])
