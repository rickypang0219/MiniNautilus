"""Small independent Python specification for Mini's documented ID-order model.

This is a correctness oracle, never reported as an external platform benchmark.
"""
from adapters import ledger


def reference(workload):
    orders, fills = {}, []
    for i, step in enumerate(workload['steps']):
        available = step['volume']
        for id in sorted(orders):
            order = orders[id]
            if not available or not order['remaining'] or order['side'] == step['taker']:
                continue
            crosses = step['price'] <= order['limit'] if order['side'] == 'Buy' else step['price'] >= order['limit']
            if crosses:
                quantity = min(order['remaining'], available)
                available -= quantity
                order['remaining'] -= quantity
                fills.append(dict(step=i,id=id,side=order['side'],qty=quantity,price=step['price']))
        for action in step['actions']:
            if action['kind'] == 'Cancel':
                orders[action['id']]['remaining'] = 0
            else:
                assert action['id'] not in orders
                orders[action['id']] = dict(action, remaining=action['qty'])
    return dict(ledger(fills,workload['steps']), fills=fills)
