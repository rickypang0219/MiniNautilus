"""Integer tick/lot workloads. Actions run AFTER matching the current observation.

Single-price bars remove intrabar OHLC ambiguity. This is a declared mapping of
trade observations to bars, not a claim that bars contain aggressor information.
"""
import random


def submit(id, side, qty, limit):
    return dict(kind="Submit", id=id, side=side, qty=qty, limit=limit)


def cancel(id):
    return dict(kind="Cancel", id=id)


def case(prices, actions=None, volumes=None, takers=None):
    actions = actions or {}
    return {"steps": [dict(price=p, volume=volumes[i] if volumes else 1000,
                           taker=takers[i] if takers else ("Sell" if i == 0 or p <= prices[i-1] else "Buy"),
                           actions=actions.get(i, [])) for i, p in enumerate(prices)]}


def fixtures():
    return {
        "long_round_trip": case([101, 100, 99, 103, 105], {0: [submit(1, "Buy", 2, 100)], 2: [submit(2, "Sell", 2, 103)]}),
        "short_round_trip": case([99, 100, 101, 97, 95], {0: [submit(1, "Sell", 3, 100)], 2: [submit(2, "Buy", 3, 97)]}),
        "reversal": case([101, 100, 99, 103, 104, 101], {0: [submit(1, "Buy", 2, 100)], 2: [submit(2, "Sell", 5, 103)], 4: [submit(3, "Buy", 3, 101)]}),
        "scale_in_out": case([103, 100, 103, 102, 101, 105, 106, 107], {0: [submit(1,"Buy",2,100)], 2: [submit(2,"Buy",1,102)], 4: [submit(3,"Sell",1,105)], 6: [submit(4,"Sell",2,107)]}),
        "cancel_unfilled": case([101, 102, 100, 99], {0: [submit(1,"Buy",2,100)], 1: [cancel(1)]}),
        "unfilled_at_end": case([101, 102, 103], {0: [submit(1,"Buy",2,100)]}),
        "gap_improvement": case([105, 97, 96, 110], {0: [submit(1,"Buy",2,100)], 2: [submit(2,"Sell",2,105)]}),
        "volume_partial": case([101,100,100,100], {0:[submit(1,"Buy",5,100)]}, [100,1,2,2]),
        "zero_volume": case([101,100,100], {0:[submit(1,"Buy",2,100)]}, [100,0,2]),
        "same_observation": case([100,103], {0:[submit(1,"Buy",2,100)]}),
        "wrong_aggressor": case([101,100,99], {0:[submit(1,"Buy",2,100)]}, takers=["Sell","Buy","Buy"]),
        "shared_liquidity": case([101,100], {0:[submit(1,"Buy",2,100),submit(2,"Buy",2,100)]}, [100,2]),
        "id_priority": case([101,100], {0:[submit(20,"Buy",2,100),submit(10,"Buy",2,100)]}, [100,2]),
        "price_priority": case([101,99], {0:[submit(1,"Buy",2,99),submit(2,"Buy",2,100)]}, [100,2]),
        "cancel_after_partial": case([101,100,100,99], {0:[submit(1,"Buy",5,100)],1:[cancel(1)]}, [100,1,2,100]),
    }


def random_passive(seed, n=200):
    """One order at a time, always passive at submission, sufficient liquidity.

    Cancel at the next callback only if not crossed. Generate from observed prices;
    no order instruction depends on a future observation.
    """
    rng = random.Random(seed)
    price, pending, next_id = 100, None, 1
    steps = []
    for i in range(n):
        previous = price
        price = max(20, price + rng.choice([-3,-2,-1,0,1,2,3]))
        actions = []
        if pending:
            crossed = price <= pending["limit"] if pending["side"] == "Buy" else price >= pending["limit"]
            if not crossed:
                actions.append(cancel(pending["id"]))
        pending = None
        if i < n-1 and rng.random() < .65:
            side = rng.choice(["Buy", "Sell"])
            pending = submit(next_id, side, rng.randint(1,5), price + (-1 if side == "Buy" else 1))
            next_id += 1
            actions.append(pending)
        steps.append(dict(price=price, volume=1000, taker="Sell" if price <= previous else "Buy", actions=actions))
    return dict(steps=steps)


def benchmark(n, trading=True):
    # Four-observation round trips, fixed size, all fills at the limit.
    prices = [101,100,101,102] * ((n+3)//4)
    actions = {}
    if trading:
        for i in range(0, n-3, 4):
            actions[i] = [submit(i//2+1,"Buy",1,100)]
            actions[i+2] = [submit(i//2+2,"Sell",1,102)]
    return case(prices[:n], actions)


def random_liquidity(seed, n=200):
    """Multiple resting orders, random IDs, partial fills and cancel races.

    Keep a tiny remaining-size model only to avoid requesting terminal cancels;
    the independent oracle and actual engine still verify the complete ledger.
    """
    rng = random.Random(seed)
    price, next_id, orders = 100, 10000, {}
    steps = []
    for i in range(n):
        price = max(20, price+rng.choice([-2,-1,0,1,2]))
        taker = rng.choice(['Buy','Sell'])
        volume = rng.randint(0,6)
        available = volume
        for id in sorted(orders):
            o = orders[id]
            crosses = price <= o['limit'] if o['side']=='Buy' else price >= o['limit']
            if o['side'] != taker and crosses:
                used = min(available, o['remaining'])
                o['remaining'] -= used
                available -= used
        actions = []
        active = [id for id,o in orders.items() if o['remaining']]
        if active and rng.random()<.6:
            id = rng.choice(active)
            orders[id]['remaining']=0
            actions.append(cancel(id))
        if rng.random()<.7:
            a=submit(next_id,rng.choice(['Buy','Sell']),rng.randint(1,8),price+rng.randint(-3,3))
            next_id-=1
            actions.append(a)
            orders[a['id']]=dict(a,remaining=a['qty'])
        steps.append(dict(price=price,volume=volume,taker=taker,actions=actions))
    return dict(steps=steps)
