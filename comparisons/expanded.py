#!/usr/bin/env python3
"""Multi-order native audit plus exact parity with the pre-refactor Mini binary."""
import argparse
import hashlib
import json
import random
from pathlib import Path
import adapters
from cases import case, submit, cancel
from run import verify_ledger, compare


def batch_passive(seed, count=500):
    rng=random.Random(seed)
    price, next_id, pending, steps = 100, 1, {}, []
    for i in range(count):
        previous=price
        price=max(20, price+rng.choice([-1,0,1]))
        for id, order in list(pending.items()):
            if (price<=order['limit'] if order['side']=='Buy' else price>=order['limit']):
                del pending[id]
        actions=[]
        if pending and rng.random()<.65:
            id=rng.choice(list(pending))
            actions.append(cancel(id))
            del pending[id]
        if i<count-1:
            for _ in range(rng.randint(0,3)):
                side=rng.choice(['Buy','Sell'])
                order=submit(next_id,side,rng.randint(1,5),price+(-1 if side=='Buy' else 1)*rng.randint(1,3))
                next_id+=1
                pending[order['id']]=order
                actions.append(order)
        steps.append(dict(price=price,volume=1_000_000,taker='Sell' if price<=previous else 'Buy',actions=actions))
    return dict(steps=steps)


def run(output, baseline, seeds=50):
    output=Path(output); output.mkdir(parents=True,exist_ok=False)
    workloads={
        'two_sides_one_trade':case([101,100,100],{0:[submit(1,'Buy',2,100),submit(2,'Sell',2,102)],
            1:[submit(3,'Sell',2,100)]},[100,2,2],['Sell','Sell','Buy']),
        'cancel_replace':case([102,101,100,102],{0:[submit(1,'Buy',3,100)],
            1:[cancel(1),submit(2,'Buy',2,100)],2:[submit(3,'Sell',2,102)]}),
        'submit_cancel_same_callback':case([100,100,101],{0:[submit(1,'Buy',2,100),cancel(1)]}),
        'cancel_many_resting':case([100,101,100,99],{0:[submit(1,'Buy',2,98),submit(2,'Buy',2,99),submit(3,'Sell',2,102)],
            1:[cancel(1),cancel(2),cancel(3)]}),
    }
    workloads.update({f'batch_{seed}':batch_passive(seed) for seed in range(seeds)})
    drivers=dict(mini=adapters.mini,backtrader=adapters.backtrader,
        backtrader_volume=lambda w:adapters.backtrader(w,True),vnpy=adapters.vnpy,
        nautilus=lambda w:adapters.nautilus(w),nautilus_tick=lambda w:adapters.nautilus(w,data_kind='trade'))
    rows=[]
    for name,w in workloads.items():
        results={label:driver(w) for label,driver in drivers.items()}
        for result in results.values():verify_ledger(result,w)
        current=adapters.ROOT
        try:
            adapters.ROOT=Path(baseline).resolve()
            old=adapters.mini(w)
        finally:adapters.ROOT=current
        unchanged=all(compare(old,results['mini']).values())
        assert unchanged, ('architecture regression',name)
        parity={label:compare(r,results['mini']) for label,r in results.items()}
        if name.startswith('batch_') or name in ('cancel_replace', 'cancel_many_resting'):
            assert all(all(p.values()) for p in parity.values()), (name, parity)
        # Also retain a multiset comparison: order of independent fills may differ.
        def normalized(r):return sorted((f['step'],f['id'],f['side'],f['qty'],f['price']) for f in r['fills'])
        unordered={label:normalized(r)==normalized(results['mini']) for label,r in results.items()}
        record=dict(name=name,workload=w,results=results,baseline_mini=old,architecture_unchanged=unchanged,
                    parity=parity,unordered_fills_equal=unordered)
        (output/f'{name}.json').write_text(json.dumps(record,indent=2)+'\n')
        rows.append({k:v for k,v in record.items() if k not in ('workload','results','baseline_mini')})
        print(name,{p:all(v.values()) for p,v in parity.items()},flush=True)
    summary=dict(cases=rows,input_digest=hashlib.sha256(json.dumps(workloads,sort_keys=True).encode()).hexdigest())
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True)
    p.add_argument('--baseline-root',required=True)
    p.add_argument('--seeds',type=int,default=50)
    args=p.parse_args();run(args.output,args.baseline_root,args.seeds)
