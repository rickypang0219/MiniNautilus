"""Closed-loop historical matching must use the real Core, without a second paper venue."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.bridge import Engine


class DepthBacktestTest(unittest.TestCase):
    def test_external_venue_sim_emits_effect_without_optimistic_auto_ack(self):
        with Engine(sim=True,time_mode='historical',extra_args=('--external-venue',)) as engine:
            engine.send_batch(1,[{'Heartbeat':{'epoch':0}},{'Quote':{'bid':99,'ask':100}}])
            effects=engine.send(1,{'Submit':dict(id=1,side='Buy',qty=2,limit=100,based_on_seq=engine.state['seq'],valid_until=10)})
            self.assertTrue(any('SendOrder' in effect for effect in effects))
            self.assertEqual(engine.state['orders']['1']['lifecycle'],'Pending')
            self.assertEqual(engine.state['fills'],{})

    def run_model(self, events, **settings):
        from mininautilus.depth_backtest import replay_depth
        manifest=dict(model='causal-depth-trade-v1',arrival_delay_ms=10,cancel_delay_ms=10,
                      max_book_age_ms=5000,tick='1',lot='1',cash_quantum='0.000001',
                      maker_fee_rate='0.00015',taker_fee_rate='0.00045',max_oracle_age_ms=2000)
        manifest.update(settings)
        with Engine(sim=True,time_mode='historical',extra_args=('--external-venue',)) as engine:
            return replay_depth(engine,events,manifest)

    @staticmethod
    def book(at=1, size='2', ask='100', bid='99'):
        return (at,'book',dict(coin='BTC',time=at,levels=[[dict(px=bid,sz='100')],[dict(px=ask,sz=size)]]))

    def test_own_partial_fill_changes_later_order_quantity_and_cash(self):
        sparse=self.run_model([self.book(size='1'),(1,'target',2),(19,'target',0),(20,'cancel',1)])
        deep=self.run_model([self.book(),(1,'target',2),(19,'target',0),(20,'cancel',1)])
        self.assertEqual([o['intent']['qty'] for o in sparse['orders']],[2,1])
        self.assertEqual([o['intent']['qty'] for o in deep['orders']],[2,2])
        self.assertEqual(sparse['position'],0);self.assertEqual(deep['position'],0)
        self.assertEqual(sparse['gross_cash'],'-1');self.assertEqual(deep['gross_cash'],'-2')

    def test_duplicate_and_future_public_trades_cannot_invent_fills(self):
        trade=dict(coin='BTC',time=30,tid=1,side='A',sz='1',px='100')
        events=[self.book(size='1'),(1,'target',2),(30,'trade',trade),(31,'trade',trade)]
        result=self.run_model(events)
        self.assertEqual(result['position'],2);self.assertEqual(len(result['fills']),2)
        changed=dict(trade,sz='2')
        with self.assertRaisesRegex(ValueError,'changed public trade'):
            self.run_model(events+[(32,'trade',changed)])
        stale=dict(trade,time=1)
        result=self.run_model([self.book(size='1'),(1,'target',2),(30,'trade',stale)])
        self.assertEqual(result['position'],1)

    def test_future_book_changes_do_not_change_past_execution(self):
        before=[self.book(),(1,'target',2)]
        first=self.run_model(before+[self.book(100,ask='101')])
        second=self.run_model(before+[self.book(100,ask='10000')])
        self.assertEqual(first['fills'],second['fills'])

    def test_public_funding_uses_simulated_settlement_position_and_available_oracle(self):
        events=[(1,'oracle',dict(price='100')),self.book(),(1,'target',2),
                (100,'funding',dict(coin='BTC',time=50,fundingRate='0.01'))]
        result=self.run_model(events)
        self.assertEqual(result['funding'],'-2');self.assertEqual(result['funding_settlements'],1)
        self.assertEqual(result['unknown_funding'],[])
        no_oracle=self.run_model(events[1:])
        self.assertEqual(no_oracle['unknown_funding'],[50]);self.assertIsNone(no_oracle['net_marked'])
        # An oracle first seen after settlement cannot retroactively mark it known.
        late=self.run_model(events[1:]+[(99,'oracle',dict(price='100'))])
        self.assertEqual(late['unknown_funding'],[50])

    def test_private_live_fills_and_answers_are_never_simulation_inputs(self):
        from mininautilus.depth_backtest import public_events
        row=dict(kind='ws',received_ms=1,message=dict(channel='userFills',data={'fills':['private']}))
        http=dict(kind='http',path='/info',received_ms=1,request=dict(type='userFillsByTime'),response=['private'])
        self.assertEqual(list(public_events([row,http])),[])

    def test_duplicate_closed_bar_is_one_signal_observation(self):
        bars=[(i*60000+60000,'bar',dict(open_ms=i*60000,close_ms=i*60000+59999,price=100+i)) for i in range(8)]
        result=self.run_model(bars+[bars[-1]])
        self.assertEqual(len(result['signals']),8)

    def test_planned_stop_cancels_remainder_then_flattens_its_own_inventory(self):
        result=self.run_model([self.book(size='1'),(1,'target',2),(20,'stop',None)])
        self.assertEqual(result['position'],0)
        self.assertEqual([o['intent']['side'] for o in result['orders']],['Buy','Sell'])
        self.assertEqual(result['orders'][1]['intent']['qty'],1)
        self.assertEqual(result['orders'][0]['coverage'],'PASSIVE_QUEUE_UNVALIDATED')
        self.assertEqual(result['accounting'],'PASS')
