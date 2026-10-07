#!/usr/bin/env python3
"""Native-only NETTING PnL reproducer, observed with NautilusTrader 1.230.0.

No Mini binary is needed. This prints native fills, account PnL and portfolio PnL
for equal/unequal realized cycles, and makes no claim about other package versions.
"""
import importlib.metadata
import json
import sys
from pathlib import Path
from adapters import nautilus
from cases import fixtures, benchmark, case, submit


def reproduce():
    unequal=fixtures()['reversal']
    unequal['steps'][-1]['price']=100
    unequal['steps'][-2]['actions'][0]['limit']=100
    negative=case([101,100,96,97,100,99],{0:[submit(1,'Buy',2,100)],
        2:[submit(2,'Sell',5,97)],4:[submit(3,'Buy',3,99)]})
    zero=case([101,100,99,100,101,100],{0:[submit(1,'Buy',2,100)],
        2:[submit(2,'Sell',5,100)],4:[submit(3,'Buy',3,100)]})
    rows=[]
    for name,w in [('reversal_equal_pnl',fixtures()['reversal']),
                   ('repeated_round_trips',benchmark(8)),
                   ('reversal_unequal_pnl',unequal),
                   ('reversal_equal_losses',negative),('reversal_zero_pnl',zero)]:
        result=nautilus(w,capture_cycles=True)
        rows.append(dict(case=name,workload=w,fills=result['fills'],diagnostics=result['native_diagnostics']))
    return dict(version=importlib.metadata.version('nautilus_trader'),cases=rows)


if __name__=='__main__':
    result=reproduce()
    content=json.dumps(result,indent=2)
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(content+'\n')
    print(content)
