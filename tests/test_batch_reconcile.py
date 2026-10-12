"""Bulk discovery must retain the old exact identity/quantity/absence contracts."""
from decimal import Decimal
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'python'))
from mininautilus.binance import BinanceSpot, VenueError


class BatchReconcileTests(unittest.TestCase):
    def setup_venue(self, n=100):
        v=BinanceSpot(session='batch',execute=True)
        v.tick=v.lot=Decimal('1')
        v.base_asset='BTC'
        intents=[dict(id=i,side='Buy',qty=1,limit=100,based_on_seq=0,valid_until=100) for i in range(1,n+1)]
        now=int(time.time()*1000)
        remote=[dict(orderId=i,clientOrderId=v.client_id(i),side='BUY',origQty='1',price='100',executedQty='1',status='FILLED',time=now-1000) for i in range(1,n+1)]
        trades=[dict(id=i,orderId=i,qty='1',price='100',time=now-500,commission='0',commissionAsset='BTC') for i in range(1,n+1)]
        v.request=Mock(side_effect=lambda method,path,params,signed: remote if path.endswith('allOrders') else trades)
        return v,intents,remote,trades

    def test_100_terminal_orders_use_two_requests_and_exact_fills(self):
        v,intents,_,_=self.setup_venue()
        result=v.collect_many(intents)
        self.assertEqual(v.request.call_count,2)
        self.assertEqual(len(result),100)
        self.assertEqual(sum(len(r[1]) for r in result.values()),100)

    def test_mismatched_terms_or_missing_trades_fail_closed(self):
        for failure in ('terms','fills','duplicate'):
            v,intents,remote,trades=self.setup_venue(2)
            if failure=='terms':remote[0]['origQty']='2'
            elif failure=='fills':trades.pop()
            else:trades.append(trades[0])
            with self.assertRaises(VenueError):v.collect_many(intents)

    def test_absent_from_discovery_is_not_proof_of_absence(self):
        v,intents,remote,_=self.setup_venue(2)
        missing=remote.pop()
        v.query=Mock(return_value=missing)
        self.assertEqual(len(v.collect_many(intents)),2)
        v.query.assert_called_once_with(intents[1])

    def test_page_overlap_and_capacity_are_refused(self):
        v,intents,remote,trades=self.setup_venue(1000)
        v.request=Mock(side_effect=[remote,trades,trades])
        with self.assertRaisesRegex(VenueError,'overlap'):v.collect_many(intents)
        with self.assertRaisesRegex(VenueError,'cap'):v.collect_many(intents+[intents[0]])
