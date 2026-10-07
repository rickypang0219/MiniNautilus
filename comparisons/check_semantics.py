#!/usr/bin/env python3
"""Check hand-audited model differences against a saved six-profile audit.

These expectations describe the pinned versions, not universal exchange laws.
Changes should prompt a review of semantics rather than a forced PnL adjustment.
"""
import json
import sys
from pathlib import Path


def check(directory):
    directory=Path(directory)
    def result(case,platform):
        return json.loads((directory/f'{case}.json').read_text())['results'][platform]
    def fills(case,platform):
        return [(f['step'],f['id'],f['qty'],f['price']) for f in result(case,platform)['fills']]
    platforms=['mini','backtrader','backtrader_volume','vnpy','nautilus','nautilus_tick']
    for name,pnl in [('long_round_trip',6),('short_round_trip',9),('reversal',12),('scale_in_out',17)]:
        for platform in platforms:
            assert result(name,platform)['cash']==pnl,(name,platform)
            assert result(name,platform)['position']==0,(name,platform)
    for platform in ('mini','backtrader_volume','nautilus_tick'):
        assert fills('volume_partial',platform)==[(1,1,1,100),(2,1,2,100),(3,1,2,100)]
        assert fills('zero_volume',platform)==[(2,1,2,100)]
        assert result('cancel_after_partial',platform)['position']==1
    for platform in ('backtrader','vnpy'):
        assert fills('volume_partial',platform)==[(1,1,5,100)]
        assert fills('zero_volume',platform)==[(1,1,2,100)]
        assert result('cancel_after_partial',platform)['position']==5
    assert fills('volume_partial','nautilus')==[(1,1,1,100)]
    assert fills('zero_volume','nautilus')==[(1,1,1,100)]
    for platform in platforms:
        assert result('gap_improvement',platform)['cash']==(10 if platform.startswith('nautilus') else 26)
        assert result('same_observation',platform)['position']==(2 if platform.startswith('nautilus') else 0)
        assert result('wrong_aggressor',platform)['position']==(0 if platform in ('mini','nautilus_tick') else 2)
        assert result('shared_liquidity',platform)['position']==(2 if platform in ('mini','nautilus') else 4)
    assert fills('id_priority','mini')==[(1,10,2,100)]
    assert fills('price_priority','mini')==[(1,1,2,99)]
    assert fills('price_priority','nautilus_tick')==[(1,2,2,100)]
    diagnostic=result('reversal','nautilus')['native_diagnostics']
    assert diagnostic['account_pnl']==12
    assert diagnostic['cached_portfolio_pnl']==diagnostic['fresh_portfolio_pnl']==6
    print('Pinned fill-model and native PnL observations verified')


if __name__=='__main__':check(sys.argv[1])
