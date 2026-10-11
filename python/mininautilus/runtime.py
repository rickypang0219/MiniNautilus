"""Bounded single-writer coordinator with an independent effect executor.

The apply callback owns Engine and MUST finish its durable barrier before
returning effects. The coordinator never retries effects. A delivery failure
latches this coordinator; reconcile quiescently and create a new coordinator.
Only one event loop may call offer/start/finish. Blocking callbacks must be
isolated using a bounded worker (see BlockingExecutor), never asyncio.to_thread
per input without an admission bound.
"""
import asyncio
from collections import deque
from dataclasses import replace
import json
import math
import threading
import time
from .offline import Input

MUTATIONS = {'Submit', 'SubmitTargeted', 'SetTarget'}


class Coordinator:
    def __init__(self, apply, execute, *, capacity=128, max_bytes=1<<20,
                 max_message=65536, reserve=32, effect_capacity=32,
                 effect_bytes=65536, source_limit=8, max_market_burst=8,
                 execution_timeout=2, observer=None, identity=('offline','dedicated','TEST')):
        if (capacity<3 or not 1<=reserve<capacity or max_message<=0
                or max_bytes<max_message+1024 or effect_capacity<1 or effect_bytes<1
                or source_limit<1 or max_market_burst<1
                or not math.isfinite(execution_timeout) or execution_timeout<=0):
            raise ValueError('positive bounded capacities and finite execution timeout required')
        self.apply,self.execute,self.observer=apply,execute,observer
        self.capacity,self.max_bytes,self.max_message=capacity,max_bytes,max_message
        self.reserve,self.effect_capacity,self.effect_bytes=reserve,effect_capacity,effect_bytes
        self.source_limit,self.max_market_burst=source_limit,max_market_burst
        self.execution_timeout=execution_timeout
        self.identity=tuple(identity)
        if len(self.identity)!=3 or not all(self.identity): raise ValueError('explicit writer identity required')
        self.sources={}
        self.last_sequence={}
        self.bytes,self.queued=0,0
        self.effects=deque()
        self.pending_effect_bytes=0
        self.wake,self.effect_wake=asyncio.Event(),asyncio.Event()
        self.counts=dict(offered=0,admitted=0,completed=0,rejected=0,coalesced=0,pending=0)
        self.high_water,self.effect_high_water=0,0
        self.effects_started,self.effects_abandoned,self.execution_failures=0,0,0
        self.mutations_refused=0
        self.inflight=False
        self.gated,self.closed,self.finishing=False,False,False
        self.gate_reason,self.failure=None,None
        self.fault_pending=None
        self.task,self.effect_task=None,None
        self.critical_burst,self.market_burst,self.cursor=0,0,0

    def start(self):
        if self.task is not None: raise RuntimeError('coordinator already started')
        self.task=asyncio.create_task(self._writer(),name='single-writer')
        self.effect_task=asyncio.create_task(self._executor(),name='effect-executor')
        return self

    def _observe(self,kind,**record):
        if self.observer: self.observer(kind,**record)

    def gate(self,reason):
        if not self.gated:
            self.gated=True
            # Reasons are fixed local labels, never transport exception strings.
            self.gate_reason=reason
            now=time.perf_counter_ns()
            self.fault_pending=Input('runtime-fault','control','Disconnect',received_ns=now,scheduled_ns=now)
            self.counts['offered']+=1;self.counts['admitted']+=1;self.counts['pending']+=1
            self.high_water=max(self.high_water,self.queued+1)
            self._observe('gate',reason=reason)
        self.effects_abandoned+=len(self.effects)
        self.effects.clear();self.pending_effect_bytes=0
        self.wake.set();self.effect_wake.set()

    def offer(self,item):
        self.counts['offered']+=1
        critical=item.lane in ('control','private','timer')
        def refuse(reason):
            self.counts['rejected']+=1
            if critical: self.gate(reason)
            return False
        if self.closed or (self.finishing and not critical): return refuse('closed_admission')
        if (item.venue,item.account,item.instrument)!=self.identity:
            return refuse('identity_mismatch')
        if not isinstance(item.source,str) or not item.source or type(item.epoch) is not int or item.epoch<0:
            return refuse('invalid_source')
        if item.source_seq is not None and type(item.source_seq) is not int:
            return refuse('invalid_sequence')
        if item.schema_version!=1 or item.lane not in ('market','private','timer','control'):
            return refuse('invalid_envelope')
        if not item.received_ns:
            now=time.perf_counter_ns()
            item=replace(item,received_ns=now,scheduled_ns=item.scheduled_ns or now)
        try:
            encoded=json.dumps(item.__dict__,separators=(',',':'),allow_nan=False).encode()
        except (ValueError,TypeError):
            return refuse('invalid_envelope')
        size=len(encoded)
        if size>self.max_message: return refuse('oversized_message')
        # One count slot and 1024 bytes are always reserved for the fault latch.
        limit=self.capacity-1 if critical else self.capacity-self.reserve-1
        if self.queued>=limit or self.bytes+size>self.max_bytes-1024:
            return refuse('input_overflow')
        if item.source not in self.sources and len(self.sources)>=self.source_limit:
            return refuse('source_limit')
        previous=self.last_sequence.get(item.source)
        if previous and item.epoch<previous[0]: return refuse('old_source_epoch')
        if previous and item.epoch!=previous[0]:
            self.counts['rejected']+=1;self.gate('source_epoch_changed');return False
        if item.source_seq is not None:
            if item.source_seq<0: return refuse('invalid_sequence')
            if previous and previous[1] is not None:
                if item.source_seq<=previous[1]: return refuse('duplicate_source_sequence')
                if item.source_seq!=previous[1]+1:
                    self.counts['rejected']+=1;self.gate('source_sequence_gap');return False
        # Snapshot mutable payloads at admission so producers cannot alter replay.
        item=Input(**json.loads(encoded))
        self.sources.setdefault(item.source,deque()).append((item,size))
        self.last_sequence[item.source]=(item.epoch,item.source_seq)
        self.queued+=1;self.bytes+=size
        self.counts['admitted']+=1;self.counts['pending']+=1
        self.high_water=max(self.high_water,self.queued+(self.fault_pending is not None))
        self.wake.set()
        return True

    def _next(self):
        if self.fault_pending is not None:
            item,self.fault_pending=self.fault_pending,None
            return item
        if not self.queued: return None
        names=list(self.sources)
        heads=[(name,self.sources[name][0][0]) for name in names if self.sources[name]]
        critical=[name for name,item in heads if item.lane!='market']
        market=[name for name,item in heads if item.lane=='market']
        # Four critical heads then at least one market head, when both are ready.
        # Never bypass a source's head, even when a later item has higher priority.
        choices=critical if critical and (self.critical_burst<4 or not market) else market
        if not choices: choices=critical
        index=next((i for i in range(len(names)) if names[(self.cursor+i)%len(names)] in choices),0)
        self.cursor=(self.cursor+index)%len(names)
        name=names[self.cursor];self.cursor=(self.cursor+1)%len(names)
        item,size=self.sources[name].popleft()
        self.queued-=1;self.bytes-=size
        if item.lane=='market': self.critical_burst=0;self.market_burst=min(self.max_market_burst,self.market_burst+1)
        else: self.critical_burst=min(4,self.critical_burst+1);self.market_burst=0
        return item

    def _enqueue_effects(self,effects):
        for effect in effects:
            if 'QueryState' in effect or 'Alert' in effect: self.gate('core_requires_recovery')
            if not any(name in effect for name in ('SendOrder','SendCancel')): continue
            if self.gated:
                self.effects_abandoned+=1;continue
            encoded=json.dumps(effect,separators=(',',':')).encode()
            if len(self.effects)>=self.effect_capacity or self.pending_effect_bytes+len(encoded)>self.effect_bytes:
                self.effects_abandoned+=1;self.gate('effect_overflow');continue
            self.effects.append((json.loads(encoded),len(encoded)))
            self.pending_effect_bytes+=len(encoded)
            self.effect_high_water=max(self.effect_high_water,len(self.effects))
            self.effect_wake.set()

    async def _writer(self):
        try:
            while True:
                self.wake.clear()
                item=self._next()
                if item is None:
                    if self.closed or (self.finishing and not self.effects and not self.inflight):
                        self.closed=True;self.effect_wake.set();return
                    await self.wake.wait();continue
                began=time.perf_counter_ns()
                mutating=(isinstance(item.payload,dict) and bool(MUTATIONS.intersection(item.payload))) or item.payload=='Submit'
                if self.gated and mutating:
                    self.mutations_refused+=1;effects=[]
                else:
                    effects=await self.apply(item)
                self._enqueue_effects(effects)
                self.counts['pending']-=1;self.counts['completed']+=1
                self._observe('input',item=item,started_ns=began,finished_ns=time.perf_counter_ns())
        except BaseException as error:
            self.failure=type(error).__name__;self.gate('writer_failed');self.closed=True
            self.effect_wake.set()
            raise

    async def _executor(self):
        try:
            await self._executor_loop()
        except BaseException:
            self.gate('executor_stopped')
            raise

    async def _executor_loop(self):
        while True:
            self.effect_wake.clear()
            if not self.effects:
                if self.closed: return
                await self.effect_wake.wait();continue
            effect,size=self.effects.popleft();self.pending_effect_bytes-=size
            if self.gated:
                self.effects_abandoned+=1;continue
            self.inflight=True;self.effects_started+=1
            try:
                await asyncio.wait_for(self.execute(effect),timeout=self.execution_timeout)
            except asyncio.CancelledError:
                self.gate('executor_cancelled');raise
            except Exception:
                self.execution_failures+=1;self.gate('delivery_uncertain')
            finally:
                self.inflight=False;self.wake.set()

    async def finish(self):
        self.finishing=True;self.wake.set();self.effect_wake.set()
        try:
            await asyncio.gather(self.task,self.effect_task)
        except BaseException:
            for task in (self.task,self.effect_task):
                if task and not task.done(): task.cancel()
            await asyncio.gather(self.task,self.effect_task,return_exceptions=True)
            raise

    async def abort(self):
        self.gate('hard_stop');self.closed=True
        for task in (self.task,self.effect_task):
            if task and not task.done(): task.cancel()
        await asyncio.gather(self.task,self.effect_task,return_exceptions=True)


class BlockingExecutor:
    """At most one daemon worker; timeout cannot pretend to cancel a socket send.

    After cancellation this shim remains busy until the underlying call returns.
    Discard its result and reconcile externally. Never create a replacement shim
    while the old call could still mutate the venue. Native async is preferred.
    """
    def __init__(self,function):
        self.function=function
        self.busy=False

    async def __call__(self,effect):
        if self.busy: raise RuntimeError('blocking executor is still unresolved')
        self.busy=True
        loop=asyncio.get_running_loop();result=loop.create_future()
        def deliver(value,error):
            self.busy=False
            if result.done(): return
            if error is None: result.set_result(value)
            else: result.set_exception(error)
        def worker():
            try: value,error=self.function(effect),None
            except Exception as exception: value,error=None,exception
            try: loop.call_soon_threadsafe(deliver,value,error)
            except RuntimeError: pass  # closed loop; outcome remains unresolved
        threading.Thread(target=worker,name='bounded-venue-worker',daemon=True).start()
        return await result
