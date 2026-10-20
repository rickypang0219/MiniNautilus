"""The scheduler changes delivery timing, never the committed Core transition."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from mininautilus.bridge import Engine
from mininautilus.offline import Input, SerialRuntime
from mininautilus.runtime import Coordinator


class RuntimeParityTests(unittest.IsolatedAsyncioTestCase):
    async def drive(self, kind, journal):
        committed, dispatched = [], []
        pool = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        with Engine(journal, time_mode='historical') as engine:
            def transition(item):
                payload = item.payload
                if isinstance(payload, dict) and 'New' in payload:
                    oid, qty = payload['New']
                    payload = {'Submit': dict(id=oid, side='Buy', qty=qty, limit=100,
                                             based_on_seq=engine.state['seq'], valid_until=10000)}
                effects = engine.send(item.at, payload)
                committed.append((json.loads(json.dumps(engine.state)), effects))
                return effects
            async def apply(item):
                return await loop.run_in_executor(pool, transition, item)
            async def execute(effect):
                if any(name in effect for name in ('SendOrder','SendCancel')):
                    dispatched.append(effect)
            actor = kind(apply, execute, clock_ms=lambda: 0).start()
            events = [
                {'Quote': {'bid': 99, 'ask': 101}}, {'New': [1, 3]},
                {'Execution': dict(epoch=0, venue_seq=1, report={'Accepted': {'id': 1}})},
                {'Execution': dict(epoch=0, venue_seq=2, report={'Fill': dict(execution_id=1, order_id=1, qty=1, price=100)})},
                {'Cancel': {'id': 1}},
                {'Execution': dict(epoch=0, venue_seq=3, report={'Canceled': dict(id=1, cumulative_filled=1)})},
                {'Execution': dict(epoch=0, venue_seq=4, report={'Fill': dict(execution_id=1, order_id=1, qty=1, price=100)})},
                {'New': [2, 1]},
                {'Execution': dict(epoch=0, venue_seq=5, report={'Rejected': {'id': 2}})},
                {'New': [3, 50]},  # Deterministic risk refusal, not an adapter call.
                {'Heartbeat': {'epoch': 0}}, 'Tick',
            ]
            for n, event in enumerate(events):
                self.assertTrue(actor.offer(Input('ordered', 'control', event, at=n, source_seq=n+1)))
                # A fixed serial fixture waits for each commit and dispatch. No
                # timing-based assertions and no cross-source reorder assumption.
                async def drain():
                    while actor.counts['completed'] <= n or getattr(actor, 'inflight', False) or getattr(actor, 'effects', ()):
                        if actor.failure: raise RuntimeError(actor.failure)
                        await asyncio.sleep(0)
                await asyncio.wait_for(drain(), 3)
            await actor.finish()
            self.assertEqual(actor.counts['pending'], 0)
            if isinstance(actor, Coordinator): self.assertFalse(actor.gated)
        pool.shutdown()
        replay = json.loads(subprocess.check_output([str(ROOT/'target/debug/mininautilus'), 'inspect', str(journal)]))
        self.assertEqual(replay, committed[-1][0])
        return committed, dispatched

    async def test_every_committed_transition_matches_serial_reference_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            a = await self.drive(SerialRuntime, Path(directory)/'serial.jsonl')
            b = await self.drive(Coordinator, Path(directory)/'actor.jsonl')
            self.assertEqual(a, b)
            self.assertEqual(a[0][-1][0]['position'], 1)
            self.assertEqual(len(a[0][-1][0]['fills']), 1)
            self.assertFalse(a[0][-1][0]['killed'])
            self.assertNotIn('3',a[0][-1][0]['orders'])
            self.assertTrue(any('Refused' in effect for _,effects in a[0] for effect in effects))
