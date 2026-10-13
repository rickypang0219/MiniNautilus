"""Hyperliquid wire, uncertainty and dedicated-account contract; no credentials."""
import json
from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from mininautilus.hyperliquid import Instrument, Hyperliquid, HyperliquidError, ActionJournal, account_ledger


class ContractTests(unittest.TestCase):
    def test_price_and_size_grid_and_significant_digits(self):
        instrument = Instrument('BTC', 0, 5)
        self.assertEqual(instrument.price('12345'), 123450)
        self.assertEqual(instrument.size('0.001'), 100)
        for bad in ('12345.6', '1.23456', 'NaN', 'Infinity', '-1', '0'):
            with self.subTest(bad=bad), self.assertRaises(HyperliquidError):
                instrument.price(bad)
        for bad in ('0.000001', '-1', 'NaN', 0.1):
            with self.subTest(bad=bad), self.assertRaises(HyperliquidError):
                instrument.size(bad)

    def test_public_transport_cannot_mutate(self):
        v = Hyperliquid(network='mainnet')
        with self.assertRaises(HyperliquidError):
            v.mutate({'type': 'cancel', 'cancels': []}, 'cancel-1')
        with self.assertRaises(ValueError):
            Hyperliquid(network='other')

    def test_action_journal_survives_restart_and_never_resends(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'actions.jsonl'
            with ActionJournal(p) as j:
                n = j.prepare('order-1', {'type': 'order'}, 100)
                with self.assertRaises(HyperliquidError):
                    j.prepare('order-1', {'type': 'order'}, 100)
            with ActionJournal(p) as j:
                self.assertGreater(j.prepare('cancel-1', {'type': 'cancel'}, 99), n)
                with self.assertRaises(HyperliquidError):
                    j.prepare('order-1', {'type': 'order'}, 200)
            lines = p.read_text().splitlines()
            row = json.loads(lines[0]); row['payload']['nonce'] += 1
            p.write_text(json.dumps(row)+'\n'+'\n'.join(lines[1:])+'\n')
            with self.assertRaises(HyperliquidError):
                ActionJournal(p)

    def test_account_ledger_long_partial_close_reversal_funding(self):
        fills = [dict(tid=1, time=1, oid=1, coin='BTC', side='B', sz='2', px='100', startPosition='0', closedPnl='0', fee='1', feeToken='USDC'),
                 dict(tid=2, time=2, oid=2, coin='BTC', side='A', sz='3', px='110', startPosition='2', closedPnl='20', fee='1', feeToken='USDC')]
        funding = [dict(time=3, hash='abc', delta=dict(type='funding', coin='BTC', usdc='0.5'))]
        result = account_ledger('BTC', fills, funding)
        self.assertEqual(result['position'], D('-1'))
        self.assertEqual(result['cash_change'], D('18.5'))
        self.assertEqual(result['entry'], D('110'))
        self.assertEqual(account_ledger('BTC', fills+fills, funding+funding), result)
        fills[1]['closedPnl'] = '21'
        with self.assertRaises(HyperliquidError):
            account_ledger('BTC', fills, funding)

    def test_unknown_order_never_proves_absence(self):
        v = Hyperliquid(); v.instrument = Instrument('BTC', 0, 5)
        v.info = Mock(return_value={'status': 'unknownOid'})
        with self.assertRaisesRegex(HyperliquidError, 'unknown'):
            v.query(dict(id=1, side='Buy', qty=100, limit=1000000))




def gateway(**options):
    v = Hyperliquid(account='0x'+'1'*40, baseline_cash='1000', started_ms=0, **options)
    v.instrument = Instrument('BTC', 0, 5)
    v.tick, v.lot = v.instrument.tick, v.instrument.lot
    return v


def intent(oid=1, side='Buy', qty=100):
    return dict(id=oid, side=side, qty=qty, limit=100000, based_on_seq=1, valid_until=10000)


def remote(v, i, status='filled', remaining='0'):
    return dict(status='order', order=dict(status=status, order=dict(coin='BTC', side='B' if i['side']=='Buy' else 'A',
                cloid=v.client_id(i['id']), oid=i['id']+100, origSz=str(v.instrument.lot*i['qty']),
                sz=remaining, limitPx=str(v.instrument.tick*i['limit']))))


def fill(oid=101, tid=1, side='B', size='0.001', price='10000', before='0', pnl='0'):
    return dict(oid=oid, tid=tid, side=side, sz=size, px=price, startPosition=before, closedPnl=pnl,
                fee='0.006', feeToken='USDC', coin='BTC', time=1)


class HistoryTests(unittest.TestCase):
    def test_changed_fill_and_funding_duplicates_rejected(self):
        f=fill(); changed=dict(f, fee='1')
        with self.assertRaises(HyperliquidError): account_ledger('BTC', [f, changed], [])
        funding=dict(time=1, hash='abc', delta=dict(coin='BTC', type='funding', usdc='1'))
        other=dict(funding, delta=dict(funding['delta'], usdc='2'))
        with self.assertRaises(HyperliquidError): account_ledger('BTC', [], [funding, other])

    def test_partial_cancel_keeps_fill_quantity(self):
        v=gateway(); i=intent(); v.info=Mock(return_value=remote(v,i,'canceled','0.0006'))
        order, fills, _ = v.collect(i, [fill(size='0.0004')])
        self.assertEqual(order['filled'],40); self.assertEqual(order['lifecycle'],'Canceled')
        self.assertEqual(fills[0]['qty'],40)
        with self.assertRaises(HyperliquidError): v.collect(i, [])

    def test_lost_ack_query_by_cloid_and_identity_check(self):
        v=gateway(); i=intent(); r=remote(v,i)
        v.info=Mock(return_value=r); v.query(i)
        self.assertEqual(v.info.call_args.kwargs['oid'],v.client_id(1))
        for field,bad in [('coin','ETH'),('side','A'),('origSz','0.002'),('limitPx','10001'),('cloid','0x'+'0'*32)]:
            with self.subTest(field=field):
                altered=json.loads(json.dumps(r)); altered['order']['order'][field]=bad
                v.info.return_value=altered
                with self.assertRaises(HyperliquidError): v.query(i)

    def test_history_split_preserves_timestamp_boundary(self):
        v=gateway()
        calls=[]
        def info(kind, **params):
            calls.append(params)
            if params['startTime']==0 and params['endTime']==2:
                return [dict(time=1,tid=n) for n in range(2000)]
            return [dict(time=1,tid=1)] if params['endTime']==1 else [dict(time=2,tid=2)]
        v.info=info
        self.assertEqual([r['tid'] for r in v.history('userFillsByTime',end_ms=2)],[1,2])
        self.assertEqual(len(calls),3)
        self.assertTrue(all(not p['aggregateByTime'] for p in calls))

    def test_history_saturated_single_millisecond_fails_closed(self):
        v=gateway(); v.info=Mock(return_value=[dict(time=0,tid=n) for n in range(2000)])
        with self.assertRaisesRegex(HyperliquidError,'saturation'): v.history('userFillsByTime',end_ms=0)

    def test_foreign_fees_positions_and_gaps_rejected(self):
        for field,value in [('feeToken','HYPE'),('coin','ETH'),('startPosition','1'),('side','X'),('sz','0'),('px','NaN')]:
            with self.subTest(field=field), self.assertRaises(HyperliquidError):
                account_ledger('BTC',[dict(fill(),**{field:value})],[])

    def test_short_open_close_and_multiple_partial_fills(self):
        rows=[fill(side='A',size='2',price='100'),
              fill(tid=2,side='B',size='1',price='90',before='-2',pnl='10'),
              fill(tid=3,side='B',size='1',price='80',before='-1',pnl='20')]
        result=account_ledger('BTC',rows,[])
        self.assertEqual(result['position'],0); self.assertEqual(result['realized'],30)
        self.assertEqual(result['cash_change'],D('29.982'))


class MutationTests(unittest.TestCase):
    def test_lost_response_and_explicit_rejection_never_resend(self):
        for response in [TimeoutError(), {'status':'err','response':'bad'},
                         {'status':'ok','response':{'data':{'statuses':[{'error':'reject'}]}}}]:
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as d:
                with ActionJournal(Path(d)/'actions') as actions:
                    v=gateway(execute=True,actions=actions,signer=lambda *a: {})
                    v.post=Mock(side_effect=response if isinstance(response,Exception) else None,
                                return_value=None if isinstance(response,Exception) else response)
                    with self.assertRaises((TimeoutError,HyperliquidError)):
                        v.mutate({'type':'order','orders':[]},'order-1')
                    with self.assertRaises(HyperliquidError):
                        v.mutate({'type':'order','orders':[]},'order-1')
                    self.assertEqual(v.post.call_count,1)

    def test_signing_failure_is_durable_before_network(self):
        with tempfile.TemporaryDirectory() as d, ActionJournal(Path(d)/'actions') as actions:
            v=gateway(execute=True,actions=actions,signer=Mock(side_effect=RuntimeError('sign failed')))
            v.post=Mock()
            with self.assertRaises(RuntimeError): v.mutate({'type':'order'},'order-1')
            v.post.assert_not_called(); self.assertIn('order-1',actions.attempts)

    def test_action_file_lock_torn_tail_and_clock_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'actions'
            with ActionJournal(p) as j:
                with self.assertRaises(BlockingIOError): ActionJournal(p)
                j.prepare('order-1',{'type':'order'},10000)
                with self.assertRaises(HyperliquidError): j.prepare('order-2',{'type':'order'},100)
            with p.open('ab') as f:f.write(b'{')
            with self.assertRaises(HyperliquidError): ActionJournal(p)

    def test_risk_cap_and_collateral(self):
        import time
        v=gateway()
        state=dict(time=int(time.time()*1000),assetPositions=[],marginSummary=dict(accountValue='1000',totalMarginUsed='0'))
        def info(kind,**params):
            return {'userAbstraction':'disabled','activeAssetData':{'leverage':{'type':'cross','value':1}},
                    'clearinghouseState':state,'openOrders':[]}[kind]
        v.info=info
        self.assertFalse(v.admission(intent()))
        with self.assertRaises(HyperliquidError):v.admission(intent(qty=101))
        state['marginSummary']['accountValue']='989'
        with self.assertRaises(HyperliquidError):v.admission(intent())
        state['marginSummary']['accountValue']='1000';state['marginSummary']['totalMarginUsed']='960'
        with self.assertRaises(HyperliquidError):v.admission(intent())
        state['time']=0
        with self.assertRaises(HyperliquidError):v.admission(intent())


class MarketTests(unittest.TestCase):
    def test_closed_window_duplicates_gaps_and_bounds(self):
        from mininautilus.hyperliquid_live import HyperliquidActorAdapter
        v=gateway(); a=HyperliquidActorAdapter(v,'1m');a.exchange_now_ms=lambda:180000
        def bar(t,c='10000'):return dict(s='BTC',i='1m',t=t,T=t+59999,c=c)
        v.info=Mock(return_value=[bar(60000),bar(120000),bar(120000)])
        self.assertEqual(len(a.warmup(2)),2)
        v.info.return_value=[bar(60000),bar(120000),bar(120000,'10001')]
        with self.assertRaisesRegex(ValueError, 'changed duplicate'):a.warmup(2)
        v.info.return_value=[bar(60000)]
        with self.assertRaises(ValueError):a.warmup(2)
        with self.assertRaises(ValueError):a.warmup(502)
        with self.assertRaises(ValueError):HyperliquidActorAdapter(v,'1s')


class RustRecoveryTests(unittest.TestCase):
    def test_paper_recovery_uses_rust_simulator_without_private_requests(self):
        from mininautilus.bridge import Engine
        from mininautilus.live_actor import ActorSession
        from mininautilus.sma import SmaStrategy
        with tempfile.TemporaryDirectory() as d, Engine(Path(d)/'events',paper=True) as engine:
            adapter=Mock()
            adapter.timing=lambda event: {}
            adapter.reconcile.side_effect=AssertionError('paper must never query a live account')
            session=ActorSession(engine,adapter,SmaStrategy(),paper=True,
                identity=('paper','paper','BTC'),now=lambda:1,note=lambda *a,**k:None)
            try:
                session._reconcile()
                self.assertEqual(engine.state['health'],'Healthy')
                adapter.reconcile.assert_not_called()
            finally:
                session.writer.shutdown();session.poller.shutdown()

    def test_crash_before_send_after_send_and_after_fill_long_and_short(self):
        from mininautilus.bridge import Engine
        import time
        for phase in ('before_network','after_network','after_fill'):
            for side in ('Buy','Sell'):
                with self.subTest(phase=phase,side=side), tempfile.TemporaryDirectory() as d:
                    path=Path(d); config=path/'config.json';journal=path/'events.jsonl'
                    config.write_text(json.dumps(dict(max_abs_position=100,max_order_qty=200,max_order_notional=100000000,
                        request_timeout_ms=30000,market_stale_ms=3000,private_stale_ms=30000,max_signal_lag=10)))
                    v=gateway(); i=intent(side=side)
                    f=fill(side='B' if side=='Buy' else 'A')
                    r=remote(v,i)
                    rows=[] if phase=='before_network' else [f]
                    v.open_orders=Mock(return_value=[])
                    v.history=lambda kind:rows if kind=='userFillsByTime' else []
                    v.info=Mock(return_value={'status':'unknownOid'} if not rows else r)
                    v.account_state=lambda:dict(time=int(time.time()*1000),assetPositions=[] if not rows else [
                        dict(type='oneWay',position=dict(coin='BTC',szi='0.001' if side=='Buy' else '-0.001',
                            unrealizedPnl='0',leverage=dict(type='cross',value=1)))],
                        marginSummary=dict(accountValue='999.994' if rows else '1000',totalMarginUsed='10' if rows else '0'))
                    with Engine(journal,config=config,time_mode='historical') as engine:
                        engine.send(0,{'Quote':dict(bid=100000,ask=100000)})
                        i['based_on_seq']=engine.state['seq']
                        effects=engine.send(1,{'Submit':i})
                        self.assertTrue(any('SendOrder' in e for e in effects))
                        if phase=='after_fill':
                            _, fs, _=v.collect(i,rows)
                            engine.send(2,{'Execution':dict(epoch=0,venue_seq=1,report={'Fill':fs[0]})})
                        engine.process.kill();engine.process.wait()
                    with Engine(journal,recover=True,time_mode='historical') as engine:
                        engine.send(3,'Disconnect');engine.send(4,'Reconnect')
                        if phase=='before_network':
                            with self.assertRaisesRegex(HyperliquidError,'unknown order'):v.reconcile(engine.state)
                            self.assertNotEqual(engine.state['health'],'Healthy')
                            self.assertTrue(engine.state['orders']['1']['uncertain'])
                        else:
                            snap=v.reconcile(engine.state)
                            self.assertEqual(snap['absent'],[])
                            engine.send(5,{'Reconcile':snap})
                            self.assertEqual(engine.state['health'],'Healthy')
                            self.assertEqual(engine.state['position'],100 if side=='Buy' else -100)
                            self.assertEqual(len(engine.state['fills']),1)

    def test_actor_short_requires_explicit_perp_capability(self):
        from mininautilus.bridge import Engine
        from mininautilus.live_actor import ActorSession
        from mininautilus.sma import SmaStrategy,Candle
        from mininautilus.offline import Input
        from dataclasses import asdict
        with tempfile.TemporaryDirectory() as d:
            with Engine(Path(d)/'events',time_mode='historical') as engine:
                adapter=Mock();adapter.supports_short=False
                with self.assertRaises(ValueError):
                    ActorSession(engine,adapter,SmaStrategy(),identity=('x','x','x'),now=lambda:0,note=lambda *a,**k:None,allow_short=True)
                adapter.supports_short=True;adapter.timing=lambda event:{}
                strategy=SmaStrategy(1,2,1,'1s',long_only=False);strategy.add(Candle(0,999,101))
                session=ActorSession(engine,adapter,strategy,identity=('hyperliquid-testnet','x','BTC'),
                    now=lambda:1,note=lambda *a,**k:None,allow_short=True,lots=1)
                effects=session._market(Input('market','market',None),dict(backfill=[],candle=asdict(Candle(1000,1999,100)),
                     quote=dict(bid=99,ask=100),exchange_now_ms=2000))
                self.assertTrue(any(e.get('SendOrder',{}).get('side')=='Sell' for e in effects))
                session.writer.shutdown();session.poller.shutdown()


class OwnershipTests(unittest.TestCase):
    def test_foreign_round_trip_cannot_pass_heartbeat_via_net_position(self):
        v=gateway()
        rows=[fill(),fill(oid=102,tid=2,side='A',before='0.001')]
        v.history=lambda kind: rows if kind=='userFillsByTime' else []
        v.account_state=Mock(return_value=dict(assetPositions=[],
            marginSummary=dict(accountValue='999.988')))
        with self.assertRaisesRegex(HyperliquidError,'unowned'):
            v.audit_account()

    def test_same_account_cannot_run_two_local_writers(self):
        from mininautilus.hyperliquid import account_writer_lock
        with tempfile.TemporaryDirectory() as d:
            with account_writer_lock('0x'+'1'*40, directory=d):
                with self.assertRaises(BlockingIOError):
                    with account_writer_lock('0x'+'1'*40, directory=d):pass
                with account_writer_lock('0x'+'2'*40, directory=d):pass


class PreflightTests(unittest.TestCase):
    def test_environment_names_network_and_agent_identity(self):
        from mininautilus.hyperliquid import testnet_credentials
        key = '0x'+'01'*32  # public disposable fixture, never funded or sent
        address = '0x'+'3'*40
        env = dict(HL_NETWORK='testnet', HL_MAIN_ADDRESS='0x'+'1'*40,
                   HL_API_ADDRESS=address, HL_API_PRIVATE_KEY=key)
        self.assertEqual(testnet_credentials(env)[0], env['HL_MAIN_ADDRESS'])
        for field, value in [('HL_NETWORK','mainnet'), ('HL_API_ADDRESS','bad'),
                             ('HL_MAIN_ADDRESS','bad'), ('HL_API_PRIVATE_KEY','invalid')]:
            with self.subTest(field=field), self.assertRaises((ValueError, HyperliquidError)):
                testnet_credentials(dict(env, **{field:value}))

    def test_account_mode_and_leverage_preflight(self):
        v=gateway()
        v.account_state=Mock(return_value={'assetPositions': []})
        v.open_orders=Mock(return_value=[])
        for mode in ('default', 'disabled'):
            v.info=Mock(side_effect=lambda kind, **kw: mode if kind=='userAbstraction' else
                        {'leverage': {'type':'cross','value':1}})
            v.check_account_mode()
        for mode in ('unifiedAccount','portfolioMargin','unknown'):
            v.info=Mock(return_value=mode)
            with self.assertRaises(HyperliquidError): v.check_account_mode()

    def test_configuration_cannot_change_leverage_with_exposure(self):
        v=gateway(execute=True)
        v.info=Mock(return_value='default')
        v.account_state=Mock(return_value={'assetPositions': [{'position': {'szi':'0.001'}}]})
        v.open_orders=Mock(return_value=[])
        v.mutate=Mock()
        with self.assertRaises(HyperliquidError):v.configure_testnet_leverage()
        v.mutate.assert_not_called()


class SigningWireTests(unittest.TestCase):
    def test_canonical_order_wire_and_reduce_only(self):
        v=gateway();v.admission=Mock(return_value=True);v.mutate=Mock(return_value={'resting':{'oid':1}})
        i=intent(side='Sell');v.submit(i)
        action,key=v.mutate.call_args.args
        self.assertEqual(key,'order-1')
        self.assertEqual(action,dict(type='order',grouping='na',orders=[dict(a=0,b=False,p='10000',s='0.001',
                       r=True,t={'limit':{'tif':'Gtc'}},c=v.client_id(1))]))
        self.assertEqual(len(v.client_id(1)),34)
        self.assertNotEqual(v.client_id(1),v.client_id(2))
        v.admission.assert_called_once()

    def test_production_mutation_guard_and_redacted_transport(self):
        import urllib.error
        from unittest.mock import patch
        with self.assertRaises(HyperliquidError):Hyperliquid(network='mainnet',execute=True)
        v=gateway(network='mainnet');opener=Mock()
        opener.open.side_effect=urllib.error.URLError('SECRET SIGNATURE')
        with patch('urllib.request.build_opener',return_value=opener):
            with self.assertRaises(HyperliquidError) as caught:v.info('meta')
        self.assertNotIn('SECRET',str(caught.exception))
        self.assertEqual(opener.open.call_count,1)
        with self.assertRaises(HyperliquidError):v.post('/exchange',{})


if __name__ == '__main__':
    unittest.main()
