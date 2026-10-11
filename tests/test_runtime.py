"""Bounded coordinator scheduling, overload and uncertain delivery contracts."""
import asyncio
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.offline import Input
from mininautilus.runtime import Coordinator


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_execution_does_not_block_writer(self):
        started, release, seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def apply(item):
            if item.payload=='Submit': return [{'SendOrder':{'id':1}}]
            if item.payload=='Fill': seen.set()
            return []
        async def execute(effect):
            started.set(); await release.wait()
        actor=Coordinator(apply,execute).start()
        actor.offer(Input('strategy','market','Submit'))
        await asyncio.wait_for(started.wait(),1)
        actor.offer(Input('private','private','Fill'))
        await asyncio.wait_for(seen.wait(),1)
        release.set(); await actor.finish()
        self.assertEqual(actor.counts['admitted'],actor.counts['completed'])

    async def test_overflow_gates_and_never_dispatches_queued_orders(self):
        started, release, gated=asyncio.Event(),asyncio.Event(),asyncio.Event()
        calls=[]
        async def apply(item):
            if item.payload=='Disconnect': gated.set(); return []
            return [{'SendOrder':{'id':item.payload}}] if isinstance(item.payload,int) else []
        async def execute(effect):
            calls.append(effect['SendOrder']['id']); started.set(); await release.wait()
        actor=Coordinator(apply,execute,effect_capacity=1).start()
        actor.offer(Input('market','market',1)); await asyncio.wait_for(started.wait(),1)
        actor.offer(Input('market','market',2)); actor.offer(Input('market','market',3))
        await asyncio.wait_for(gated.wait(),1)
        self.assertTrue(actor.gated)
        release.set(); await actor.finish()
        self.assertEqual(calls,[1])
        self.assertEqual(actor.effects_abandoned,2)
        self.assertLessEqual(actor.effect_high_water,1)

    async def test_fifo_within_source_and_fair_critical_delivery(self):
        seen=[]
        async def apply(item): seen.append((item.source,item.payload)); return []
        async def execute(effect): pass
        actor=Coordinator(apply,execute)
        for n in range(12): actor.offer(Input('market','market',n,source_seq=n+1))
        actor.offer(Input('market','private','same-source-later',source_seq=13))
        actor.offer(Input('timer','timer','Tick'))
        actor.start(); await actor.finish()
        self.assertLess(seen.index(('timer','Tick')),8)
        self.assertEqual([p for s,p in seen if s=='market'],list(range(12))+['same-source-later'])

    async def test_byte_limits_reserved_capacity_and_sequence_gap(self):
        async def apply(item): return []
        async def execute(effect): pass
        actor=Coordinator(apply,execute,capacity=5,reserve=2,max_message=1024,max_bytes=4096)
        for n in range(2): self.assertTrue(actor.offer(Input('market','market','Quote',source_seq=n+1)))
        self.assertFalse(actor.offer(Input('market','market','Quote',source_seq=3)))
        self.assertTrue(actor.offer(Input('private','private','Fill',source_seq=1)))
        self.assertFalse(actor.offer(Input('other','market','x'*2000)))
        actor.start(); await actor.finish()
        self.assertEqual(actor.counts['offered'],actor.counts['admitted']+actor.counts['rejected'])
        self.assertEqual(actor.counts['admitted'],actor.counts['completed']+actor.counts['pending'])
        self.assertLessEqual(actor.high_water,actor.capacity)
        second=Coordinator(apply,execute)
        second.offer(Input('private','private','Fill',source_seq=1))
        self.assertFalse(second.offer(Input('private','private','Fill',source_seq=3)))
        self.assertTrue(second.gated)
        second.start(); await second.finish()

    async def test_timeout_preserves_uncertainty_and_shutdown_is_bounded(self):
        gated=asyncio.Event()
        async def apply(item):
            if item.payload=='Disconnect': gated.set(); return []
            return [{'SendOrder':{'id':1}}]
        async def execute(effect): await asyncio.Future()
        actor=Coordinator(apply,execute,execution_timeout=.01).start()
        actor.offer(Input('market','market','Submit'))
        await asyncio.wait_for(gated.wait(),1)
        await asyncio.wait_for(actor.finish(),1)
        self.assertTrue(actor.gated)
        self.assertEqual(actor.execution_failures,1)
        self.assertEqual(actor.effects_started,1)

    async def test_blocking_executor_timeout_does_not_spawn_a_replacement(self):
        import threading
        from mininautilus.runtime import BlockingExecutor
        started,release=threading.Event(),threading.Event()
        def blocked(effect): started.set();release.wait();return []
        shim=BlockingExecutor(blocked)
        task=asyncio.create_task(shim({'SendOrder':{'id':1}}))
        await asyncio.to_thread(started.wait,1)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(shim.busy)
        with self.assertRaises(RuntimeError): await shim({'SendOrder':{'id':2}})
        release.set()

    async def test_real_journal_lost_ack_reconciles_without_resubmission(self):
        from concurrent.futures import ThreadPoolExecutor
        import tempfile
        from mininautilus.bridge import Engine
        from mininautilus.offline import FakeVenue
        with tempfile.TemporaryDirectory() as directory:
            journal=Path(directory)/'events'
            venue=FakeVenue(mode='lost_ack')
            with Engine(journal,time_mode='historical',env={'MINI_ORDER_TRACE':str(Path(directory)/'trace')}) as engine:
                engine.send(0,{'Quote':{'bid':99,'ask':101}})
                pool=ThreadPoolExecutor(max_workers=1)
                loop=asyncio.get_running_loop();gated=asyncio.Event()
                def apply_sync(item):
                    if item.payload=='Submit':
                        payload={'Submit':dict(id=1,side='Buy',qty=1,limit=100,based_on_seq=engine.state['seq'],valid_until=1000)}
                    else: payload=item.payload
                    effects=engine.send(1,payload)
                    if any('SendOrder' in effect for effect in effects):
                        self.assertEqual(engine.last_trace['actions'][0]['durable_seq'],engine.state['seq'])
                    return effects
                async def apply(item):
                    effects=await loop.run_in_executor(pool,apply_sync,item)
                    # asyncio Events must be published on their owning loop.
                    if item.payload=='Disconnect': gated.set()
                    return effects
                actor=Coordinator(apply,venue.execute,clock_ms=lambda:1).start()
                actor.offer(Input('strategy','market','Submit'))
                await asyncio.wait_for(gated.wait(),2)
                await actor.finish();pool.shutdown()
                self.assertTrue(actor.gated)
                self.assertEqual(engine.state['orders']['1']['filled'],0)
                self.assertEqual(venue.position,1)
            with Engine(journal,recover=True,time_mode='historical') as recovered:
                self.assertEqual(recovered.state['health'],'Disconnected')
                recovered.send(recovered.state['now'],'Reconnect')
                snapshot=venue.snapshot(recovered.state['epoch'],0,recovered.state['orders'])
                recovered.send(recovered.state['now'],{'Reconcile':snapshot})
                self.assertEqual(recovered.state['health'],'Healthy')
                self.assertEqual(recovered.state['position'],1)
                self.assertEqual(len(recovered.state['fills']),1)
            self.assertEqual(venue.calls,1,'recovery must not resend historical effects')

    async def test_killed_engine_close_still_releases_process_and_pipes(self):
        import tempfile
        from mininautilus.bridge import Engine
        with tempfile.TemporaryDirectory() as directory:
            engine=Engine(Path(directory)/'journal',time_mode='historical')
            engine.send(0,'Tick')
            engine.process.kill();engine.process.wait()
            with self.assertRaises((BrokenPipeError,RuntimeError)):
                engine.send(0,'Tick')
            engine.close()
            self.assertTrue(engine.process.stdin.closed)
            self.assertTrue(engine.process.stdout.closed)

    async def test_foreign_identity_never_reaches_writer(self):
        called=[]
        async def apply(item): called.append(item.payload);return []
        async def execute(effect): pass
        actor=Coordinator(apply,execute)
        self.assertFalse(actor.offer(Input('foreign','market','Submit',account='another-account')))
        actor.start();await actor.finish()
        self.assertEqual(called,[])

    async def test_internal_fault_is_owned_by_the_configured_risk_domain(self):
        seen=[]
        async def apply(item):seen.append(item);return []
        async def execute(effect):pass
        identity=('fake_native','spot','BTCUSDT')
        actor=Coordinator(apply,execute,identity=identity)
        self.assertFalse(actor.offer(Input('private','private','Fill',venue=identity[0],
                                          account='foreign',instrument=identity[2])))
        actor.start();await actor.finish()
        self.assertEqual([item.payload for item in seen],['Disconnect'])
        self.assertEqual((seen[0].venue,seen[0].account,seen[0].instrument),identity)

    async def test_queued_order_expiry_keeps_intent_and_stops_dispatch(self):
        started,release,gated=asyncio.Event(),asyncio.Event(),asyncio.Event()
        logical=[0];calls=[]
        async def apply(item):
            if item.payload=='Disconnect': gated.set();return []
            return [{'SendOrder':dict(id=item.payload,valid_until=10)}]
        async def execute(effect):
            calls.append(effect['SendOrder']['id']);started.set();await release.wait()
        actor=Coordinator(apply,execute,clock_ms=lambda:logical[0]).start()
        actor.offer(Input('market','market',1));await asyncio.wait_for(started.wait(),1)
        actor.offer(Input('market','market',2))
        # Synchronize on writer progress rather than assuming a scheduling delay.
        while actor.counts['completed']<2:await asyncio.sleep(0)
        logical[0]=20;release.set()
        await asyncio.wait_for(gated.wait(),1);await actor.finish()
        self.assertEqual(calls,[1])
        self.assertEqual(actor.gate_reason,'queued_effect_expired')
        self.assertEqual(actor.effects_abandoned,1)

    async def test_confirmed_terminal_cancel_is_skipped_without_releasing_risk(self):
        calls=[]
        async def apply(item):return [{'SendCancel':{'id':1}}]
        async def execute(effect):calls.append(effect)
        async def guard(effect):return 'terminal_cancel'
        actor=Coordinator(apply,execute,dispatch_guard=guard).start()
        actor.offer(Input('control','control',{'Cancel':{'id':1}}))
        await actor.finish()
        self.assertEqual(calls,[])
        self.assertEqual(actor.effects_skipped_terminal,1)
        self.assertFalse(actor.gated)

    async def test_actual_intent_without_dispatch_clock_fails_closed(self):
        calls=[]
        async def apply(item):
            return [] if item.payload=='Disconnect' else [{'SendOrder':dict(id=1,valid_until=100)}]
        async def execute(effect):calls.append(effect)
        actor=Coordinator(apply,execute).start()
        actor.offer(Input('market','market','Submit'))
        await actor.finish()
        self.assertEqual(calls,[])
        self.assertEqual(actor.gate_reason,'dispatch_clock_missing')
        self.assertEqual(actor.effects_started,0)

    async def test_dispatch_guard_waits_for_already_admitted_private_reports(self):
        started,release,fill_started,fill_commit=(asyncio.Event() for _ in range(4))
        state=['Pending'];calls=[]
        async def apply(item):
            if item.payload=='Submit':return [{'SendOrder':{'id':1}}]
            if item.payload=='Cancel':return [{'SendCancel':{'id':1}}]
            if item.payload=='Accepted':state[0]='Accepted'
            if item.payload=='Fill':
                fill_started.set();await fill_commit.wait();state[0]='Filled'
            return []
        async def execute(effect):
            calls.append(effect)
            if 'SendOrder' in effect:
                started.set();await release.wait()
                actor.offer(Input('private','private','Accepted',source_seq=1))
                actor.offer(Input('private','private','Fill',source_seq=2))
        async def guard(effect):
            return 'terminal_cancel' if 'SendCancel' in effect and state[0]=='Filled' else 'send'
        actor=Coordinator(apply,execute,dispatch_guard=guard).start()
        actor.offer(Input('strategy','market','Submit'));await asyncio.wait_for(started.wait(),1)
        actor.offer(Input('strategy','market','Cancel'))
        while actor.counts['completed']<2:await asyncio.sleep(0)
        release.set();await asyncio.wait_for(fill_started.wait(),1)
        self.assertEqual(len(calls),1)
        fill_commit.set();await actor.finish()
        self.assertEqual(actor.effects_skipped_terminal,1)
        self.assertEqual(actor.pending_private,{})
