"""Always-on O(delta) ledger audit; independent full oracle on replacements.

Terminal ID/fill evidence is retained, matching protocol 2's mirror. This does
not claim bounded session memory or replace Rust's authoritative risk checks.
"""
from collections import defaultdict


def full_audit(state,limit,*,long_only=False,cash_required=True):
    quantities=defaultdict(int)
    position=state.get('opening_position',0)
    cash=state.get('opening_cash',0)
    for key,fill in state['fills'].items():
        order=state['orders'].get(str(fill['order_id']))
        if (order is None or int(key)!=fill['execution_id'] or fill['qty']<=0 or fill['price']<=0):
            raise RuntimeError('invalid or unknown fill invariant failed')
        quantities[fill['order_id']]+=fill['qty']
        sign=1 if order['intent']['side']=='Buy' else -1
        position+=sign*fill['qty'];cash-=sign*fill['qty']*fill['price']
    for key,order in state['orders'].items():
        intent=order['intent'];filled=quantities[int(key)]
        if int(key)!=intent['id'] or filled!=order['filled'] or not 0<=filled<=intent['qty']:
            raise RuntimeError('fill accounting invariant failed')
    if position!=state['position'] or (cash_required and cash!=state['cash']):
        raise RuntimeError('position/cash accounting invariant failed')
    if not (-limit<=position<=limit) or (long_only and position<0):
        raise RuntimeError('position limit invariant failed')
    return position,cash


class IncrementalAudit:
    def __init__(self,limit,*,long_only=False):
        self.limit,self.long_only=limit,long_only
        self.state=None
        self.orders,self.fills,self.quantities={},{},defaultdict(int)
        self.position,self.cash=0,0
        self.full_scans,self.entries_checked=0,0
        self.seq=0

    def _order(self,key,order):
        intent=order['intent'];identity=(intent['side'],intent['qty'],intent['limit'])
        if int(key)!=intent['id'] or intent['side'] not in ('Buy','Sell') or intent['qty']<=0:
            raise RuntimeError('order identity invariant failed')
        old=self.orders.get(int(key))
        if old is not None and old!=identity: raise RuntimeError('order terms changed')
        self.orders[int(key)]=identity

    def _fill(self,key,fill):
        identity=fill['order_id'],fill['qty'],fill['price']
        if int(key)!=fill['execution_id'] or fill['qty']<=0 or fill['price']<=0:
            raise RuntimeError('fill identity invariant failed')
        old=self.fills.get(int(key))
        if old is not None:
            if old!=identity: raise RuntimeError('duplicate fill payload changed')
            return
        order=self.orders.get(fill['order_id'])
        if order is None: raise RuntimeError('unknown fill order')
        self.fills[int(key)]=identity
        self.quantities[fill['order_id']]+=fill['qty']
        sign=1 if order[0]=='Buy' else -1
        self.position+=sign*fill['qty'];self.cash-=sign*fill['qty']*fill['price']

    def _check_order(self,key,order):
        filled=self.quantities[int(key)]
        if filled!=order['filled'] or not 0<=filled<=order['intent']['qty']:
            raise RuntimeError('fill accounting invariant failed')

    def _balances(self,state):
        if self.position!=state['position'] or self.cash!=state['cash']:
            raise RuntimeError('position/cash accounting invariant failed')
        if not -self.limit<=self.position<=self.limit or (self.long_only and self.position<0):
            raise RuntimeError('position limit invariant failed')
        if state['seq']<self.seq: raise RuntimeError('audit sequence regressed')
        self.seq=state['seq']

    def reset(self,state):
        # Independent oracle first; full replacements are intentionally O(history).
        full_audit(state,self.limit,long_only=self.long_only)
        self.state=state;self.orders={};self.fills={};self.quantities=defaultdict(int)
        self.position=state.get('opening_position',0);self.cash=state.get('opening_cash',0)
        self.seq=state['seq'];self.full_scans+=1
        for key,order in state['orders'].items(): self._order(key,order)
        for key,fill in state['fills'].items(): self._fill(key,fill)
        self._balances(state)

    def check(self,state,delta):
        if state is not self.state or delta is None:
            self.reset(state);return
        touched=set(delta['orders'])
        for key,order in delta['orders'].items():
            self._order(key,order);self.entries_checked+=1
        for key,fill in delta['fills'].items():
            self._fill(key,fill);self.entries_checked+=1
            touched.add(str(fill['order_id']))
        for key in touched: self._check_order(key,state['orders'][key])
        self._balances(state)
