"""Diagnostic cycle-identity aggregation for this single-instrument USD fixture.

This is an independently checked correction approach, not a patch to Nautilus.
Read only snapshots produced by the local test engine: they are trusted pickle
payloads. Do not use this function to load arbitrary downloaded pickle files.
"""
import pickle
from decimal import Decimal


def inspect_cycles(cache, instrument_id, account_id):
    cycles={}
    positions=[]
    for position_id in cache.position_snapshot_ids(instrument_id):
        positions.extend(pickle.loads(payload) for payload in cache.position_snapshot_bytes(position_id))
    # Current state follows archived snapshots, so it wins ties for the same cycle.
    positions.extend(cache.positions(instrument_id=instrument_id,account_id=account_id))
    for position in positions:
        if position.account_id != account_id or position.instrument_id != instrument_id:
            continue
        # Archived position IDs can have generated suffixes; PnL amount and the
        # archive object's ID are not stable identities for a trading cycle.
        key=(str(account_id),str(instrument_id),str(position.opening_order_id),position.ts_opened)
        previous=cycles.get(key)
        if previous is None or position.ts_last>=previous.ts_last:
            cycles[key]=position
    rows=[dict(position_id=str(p.id),opening_order_id=str(p.opening_order_id),
               ts_opened=p.ts_opened,ts_last=p.ts_last,closed=p.is_closed,
               realized_pnl=str(p.realized_pnl.as_decimal()) if p.realized_pnl else '0')
          for p in cycles.values()]
    return dict(cycles=rows,cycle_count=len(rows),
                realized_pnl=float(sum((Decimal(r['realized_pnl']) for r in rows),Decimal(0))))


class DuplicateSnapshots:
    """Read-only diagnostic proxy proving that duplicate archives count once."""
    def __init__(self,cache):self.cache=cache
    def position_snapshot_ids(self,*args):return self.cache.position_snapshot_ids(*args)
    def position_snapshot_bytes(self,*args):
        values=self.cache.position_snapshot_bytes(*args)
        return values+values
    def positions(self,**kwargs):return self.cache.positions(**kwargs)
