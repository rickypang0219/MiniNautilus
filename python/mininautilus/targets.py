"""Versioned target commands; Rust revalidates these against its current state."""
from .bridge import TERMINAL, next_order_id, open_orders  # noqa: F401 (TERMINAL re-exported)


def target_event(state, position, ttl_ms=3000):
    return {'SetTarget':dict(revision=state['last_target_revision']+1,
                            position=position,valid_until=state['now']+ttl_ms)}


def target_order(state, *, price=None):
    target=state.get('target')
    if (not target or target['valid_until'] < state['now'] or state['health']!='Healthy'
            or state['killed'] or state['quote'] is None): return None
    if any(True for _ in open_orders(state)): return None
    delta=target['position']-state['position']
    if not delta:return None
    bid,ask,_=state['quote']
    intent=dict(id=next_order_id(state),side='Buy' if delta>0 else 'Sell',qty=abs(delta),
                limit=price if price is not None else (ask if delta>0 else bid),
                based_on_seq=state['seq'],valid_until=min(state['now']+3000,target['valid_until']))
    return {'SubmitTargeted':dict(intent=intent,revision=target['revision'],expected_position=state['position'])}
