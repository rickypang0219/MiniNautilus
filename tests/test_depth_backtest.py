"""Closed-loop historical matching must use the real Core, without a second paper venue."""
from pathlib import Path
import sys
import tempfile
import json
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
        manifest=dict(model='causal-depth-trade-v2',arrival_delay_ms=10,cancel_delay_ms=10,
                      max_book_age_ms=5000,tick='1',lot='1',cash_quantum='0.000001',
                      maker_fee_rate='0.00015',taker_fee_rate='0.00045',max_oracle_age_ms=2000)
        calls=settings.pop('capture_calls',None)
        manifest.update(settings)
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json'
            config.write_text(json.dumps(dict(max_abs_position=20,max_order_qty=40,max_order_notional=40000000,request_timeout_ms=20000,market_stale_ms=5000,private_stale_ms=30000,max_signal_lag=10000)))
            with Engine(sim=True,config=config,time_mode='historical',extra_args=('--external-venue',)) as engine:
                if calls is not None:
                    send=engine.send
                    def observed_send(at,event,**kwargs):
                        calls.append((at,event,engine.state['position']))
                        return send(at,event,**kwargs)
                    engine.send=observed_send
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

    def test_minimum_topup_is_refused_once_without_blocking_small_reduction(self):
        events=[self.book(size='0.00018',bid='80000',ask='80001'),(1,'target',20),(20,'cancel',1),(40,'target',0)]
        result=self.run_model(events,tick='0.1',lot='0.00001',minimum_open_notional_usdc='10')
        self.assertEqual([o['intent']['qty'] for o in result['orders']],[20,18])
        self.assertEqual(result['position'],0)
        self.assertEqual(len(result['minimum_refusals']),1)
        self.assertEqual(result['minimum_refusals'][0]['qty'],2)
        # A small held position can still be closed below the opening minimum.
        result=self.run_model([self.book(size='0.00002',bid='80000',ask='80001'),(1,'target',20),(20,'cancel',1),(40,'stop',None)],
                              tick='0.1',lot='0.00001',minimum_open_notional_usdc='10')
        self.assertEqual([o['intent']['qty'] for o in result['orders']],[20,18,2])
        self.assertEqual(result['position'],0)

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

    def test_warmup_batch_publishes_only_latest_target_without_intermediate_order(self):
        from mininautilus.depth_backtest import public_events
        at=540001
        rows=[dict(kind='ws',received_ms=at,message=dict(channel='l2Book',data=self.book(at)[2]))]
        for i,price in enumerate([100]*6+[101,102,90]):
            rows.append(dict(kind='strategy',received_ms=at,request=dict(bar=dict(open_ms=i*60000,close_ms=i*60000+59999,price=price))))
        rows.append(dict(kind='strategy_publication',available_ms=at,bar_open_ms=480000))
        result=self.run_model(public_events(rows))
        self.assertEqual([o['intent']['side'] for o in result['orders']],['Sell'])
        self.assertEqual(result['position'],-20)

    def test_stop_during_submit_cannot_confirm_cancel_before_order_arrival(self):
        result=self.run_model([self.book(),(1,'target',2),(20,'stop',None)],arrival_delay_ms=100,cancel_delay_ms=1)
        self.assertEqual(result['position'],0)
        self.assertGreaterEqual(result['orders'][0]['cancel_effective_ms'],result['orders'][0]['arrived_ms'])
        self.assertEqual(result['orders'][1]['intent']['qty'],2)

    def test_exchange_book_time_matches_without_leaking_late_quote_into_order_terms(self):
        from mininautilus.depth_backtest import public_events
        first=dict(kind='ws',received_ms=1,message=dict(channel='l2Book',data=self.book()[2]))
        delayed=dict(kind='ws',received_ms=100,message=dict(channel='l2Book',data=self.book(2,ask='99',bid='98')[2]))
        events=list(public_events([first,delayed]))+[(1,'target',2)]
        result=self.run_model(events)
        self.assertEqual(result['orders'][0]['intent']['limit'],100)
        self.assertEqual(result['fills'][0]['price'],99)

    def test_private_report_delay_preserves_physical_funding_inventory(self):
        calls=[]
        events=[(1,'oracle',dict(price='100')),self.book(),(1,'target',2),
                (100,'funding',dict(coin='BTC',time=50,fundingRate='0.01'))]
        result=self.run_model(events,report_delay_ms=1000,capture_calls=calls)
        deliveries=[at for at,event,_ in calls if isinstance(event,dict) and 'Fill' in event.get('Execution',{}).get('report',{})]
        self.assertEqual(deliveries,[1011])
        self.assertEqual(result['funding'],'-2')
        self.assertTrue(any(at==100 and position==0 for at,_,position in calls))
