"""Native backtesting adapters; optional volume-filler profiles are explicit."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2024, 1, 1)
CAPITAL = 1_000_000_000


def rounded_margin_pnl(fills, mark):
    """Independent average-cost ledger, rounding each realized increment to cents.

    The exact tick-lot ledger remains separate. This explains currency-account
    quantization without treating arbitrary discrepancies as a tolerance.
    """
    from fractions import Fraction
    from decimal import Decimal, ROUND_HALF_UP, localcontext
    position, average = 0, Fraction(0)
    realized = Decimal(0)
    closing_fills = 0
    def cents(value):
        with localcontext() as context:
            context.prec = 80
            return (Decimal(value.numerator)/Decimal(value.denominator)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP)
    for fill in fills:
        delta = int(fill['qty']) * (1 if fill['side']=='Buy' else -1)
        price = Fraction(str(fill['price']))
        if position == 0 or position*delta > 0:
            average = (average*abs(position)+price*abs(delta))/(abs(position)+abs(delta))
        else:
            closing_fills += 1
            realized += cents(min(abs(position),abs(delta))*(price-average)*(1 if position>0 else -1))
            if abs(delta) >= abs(position):
                average = price if abs(delta)>abs(position) else Fraction(0)
        position += delta
    return dict(rounded_pnl=float(realized+cents((Fraction(mark)-average)*position)),
                # Each realized increment and the final open PnL are stored as
                # USD cents. Native binary-float ties can choose a different cent
                # from the exact rational reference; retain the observed delta.
                error_bound=0.005*(closing_fills+bool(position)))


def ledger(fills, steps):
    """Independent exact integer cash/inventory oracle, marked at common last price."""
    position = cash = 0
    checkpoints = []
    by_step = {}
    for fill in fills:
        assert 0 <= fill['step'] < len(steps), fill
        by_step.setdefault(fill['step'], []).append(fill)
    for i, step in enumerate(steps):
        for f in by_step.get(i, []):
            delta = f['qty'] * (1 if f['side'] == 'Buy' else -1)
            position += delta
            cash -= delta * f['price']
        checkpoints.append(dict(step=i, position=position, cash=cash, equity=cash+position*step['price']))
    return dict(position=position, cash=cash, checkpoints=checkpoints)


def mini(workload):
    payload = json.dumps(workload)
    start = time.perf_counter()
    output = subprocess.check_output([str(ROOT/'target/release/examples/compare_engine')], input=payload, text=True)
    wall = time.perf_counter()-start
    result = json.loads(output)
    result['wall_seconds'] = wall
    return result


def mini_durable(workload):
    sys.path.insert(0, str(ROOT/'python'))
    from mininautilus.bridge import Engine
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        config = path/'config.json'
        config.write_text(json.dumps(dict(max_abs_position=1_000_000, max_order_qty=1_000_000,
            max_order_notional=2**63-1, request_timeout_ms=100, market_stale_ms=1000,
            private_stale_ms=2**64-1, max_signal_lag=100)))
        fills = []
        seen = set()
        checkpoints = []
        start = time.perf_counter()
        with Engine(path/'journal.jsonl', paper=True, config=config,
                    binary=ROOT/'target/release/mininautilus', time_mode='historical') as engine:
            for i, step in enumerate(workload['steps']):
                at = i * 60_000
                engine.send(at, {'Quote':dict(bid=step['price'],ask=step['price'])})
                if step['volume']:
                    engine.send(at, {'Trade':dict(taker=step['taker'],price=step['price'],qty=step['volume'])})
                for action in step['actions']:
                    if action['kind'] == 'Cancel':
                        event = {'Cancel': {'id':action['id']}}
                    else:
                        event = {'Submit':{k:v for k,v in action.items() if k != 'kind'}}
                        event['Submit'].update(based_on_seq=engine.state['seq'],valid_until=at+1000)
                    effects = engine.send(at, event)
                    if any('Refused' in e or 'Alert' in e for e in effects):
                        raise AssertionError(effects)
                state = engine.state
                assert state['health'] == 'Healthy'
                for key,f in state['fills'].items():
                    if key not in seen:
                        seen.add(key)
                        fills.append(dict(step=i,id=f['order_id'],side=state['orders'][str(f['order_id'])]['intent']['side'],qty=f['qty'],price=f['price']))
                checkpoints.append(dict(step=i,position=state['position'],cash=state['cash'],equity=state['cash']+state['position']*step['price']))
        elapsed = time.perf_counter()-start
        replay = json.loads(subprocess.check_output([str(ROOT/'target/release/mininautilus'),'inspect',str(path/'journal.jsonl')],text=True))
        assert replay == state
        return dict(fills=fills, checkpoints=checkpoints, position=state['position'],cash=state['cash'],elapsed_seconds=elapsed, journal_bytes=(path/'journal.jsonl').stat().st_size)


def backtrader(workload, volume_filler=False):
    import backtrader as bt
    import pandas as pd
    steps = workload['steps']
    frame = pd.DataFrame([dict(open=s['price'],high=s['price'],low=s['price'],close=s['price'],volume=s['volume']) for s in steps],
                         index=pd.date_range(START, periods=len(steps), freq='min'))
    fills, checkpoints = [], []

    class Strategy(bt.Strategy):
        def __init__(self):
            self.by_id = {}
            self.executions = {}

        def notify_order(self, order):
            # exbits is cumulative; notifications may share the same execution bits.
            bits = list(order.executed.exbits)
            seen = self.executions.get(order.ref, 0)
            for bit in bits[seen:]:
                dt = bt.num2date(bit.dt).replace(tzinfo=None)
                i = round((dt-START).total_seconds()/60)
                fills.append(dict(step=i,id=order.info.fixture_id,side='Buy' if bit.size>0 else 'Sell',qty=abs(bit.size),price=bit.price))
            self.executions[order.ref] = len(bits)

        def next(self):
            i = len(self.data)-1
            for a in steps[i]['actions']:
                if a['kind'] == 'Cancel':
                    self.cancel(self.by_id[a['id']])
                else:
                    method = self.buy if a['side'] == 'Buy' else self.sell
                    self.by_id[a['id']] = method(size=a['qty'],price=a['limit'],exectype=bt.Order.Limit,fixture_id=a['id'])
            checkpoints.append(dict(step=i,position=self.position.size,cash=self.broker.getcash()-CAPITAL,equity=self.broker.getvalue()-CAPITAL))

    engine = bt.Cerebro(stdstats=False)
    engine.broker.setcash(CAPITAL)
    engine.broker.setcommission(commission=0)
    if volume_filler == 'shared':
        class SharedVolume:
            """Explicit alternative: one liquidity budget per data feed/bar."""
            def __init__(self):self.budgets={}
            def __call__(self,order,price,ago):
                feed=id(order.data)
                stamp=order.data.datetime[ago]
                previous,available=self.budgets.get(feed,(None,0))
                if previous!=stamp:available=max(0,order.data.volume[ago])
                quantity=min(abs(order.executed.remsize),available)
                self.budgets[feed]=(stamp,available-quantity)
                return quantity
        engine.broker.set_filler(SharedVolume())
    elif volume_filler:
        engine.broker.set_filler(bt.fillers.FixedSize())
    engine.adddata(bt.feeds.PandasData(dataname=frame))
    engine.addstrategy(Strategy)
    start = time.perf_counter()
    engine.run(runonce=False, preload=False)
    elapsed = time.perf_counter()-start
    return dict(fills=fills,checkpoints=checkpoints,position=checkpoints[-1]['position'],cash=checkpoints[-1]['cash'],elapsed_seconds=elapsed)


def vnpy(workload):
    # vn.py otherwise creates/reads ~/.vntrader during import. Keep the audit
    # independent of any personal trading settings, without changing HOME.
    from contextlib import chdir
    sandbox = ROOT/'runs/comparison-vnpy'
    (sandbox/'.vntrader').mkdir(parents=True, exist_ok=True)
    with chdir(sandbox):
        from vnpy_ctastrategy.backtesting import BacktestingEngine
        from vnpy_ctastrategy.template import CtaTemplate
        from vnpy.trader.constant import Exchange, Interval, Direction, Offset
        from vnpy.trader.object import BarData
    steps = workload['steps']
    fills = []
    native_positions = []

    class Strategy(CtaTemplate):
        def on_init(self):
            pass

        def __init__(self, *args):
            super().__init__(*args)
            self.by_id = {}
            self.reverse_id = {}
            self.i = -1

        def on_bar(self, bar):
            self.i += 1
            native_positions.append(self.pos)
            for a in steps[self.i]['actions']:
                if a['kind'] == 'Cancel':
                    for oid in self.by_id[a['id']]:
                        self.cancel_order(oid)
                else:
                    # NET inventory contract. CTA Offset is OPEN; no futures close-today
                    # or hedge-book comparison is claimed here.
                    ids = self.send_order(Direction.LONG if a['side']=='Buy' else Direction.SHORT,
                                          Offset.OPEN,a['limit'],a['qty'])
                    self.by_id[a['id']] = ids
                    self.reverse_id.update({oid:a['id'] for oid in ids})

        def on_trade(self, trade):
            fills.append(dict(step=round((trade.datetime-START).total_seconds()/60),id=self.reverse_id[trade.vt_orderid],
                              side='Buy' if trade.direction==Direction.LONG else 'Sell',qty=trade.volume,price=trade.price))

    engine = BacktestingEngine()
    engine.output = lambda *args: None
    engine.set_parameters(vt_symbol='TEST.LOCAL',interval=Interval.MINUTE,start=START,end=START+timedelta(minutes=len(steps)),
                          rate=0,slippage=0,size=1,pricetick=1,capital=CAPITAL)
    engine.add_strategy(Strategy,{})
    engine.history_data = [BarData(symbol='TEST',exchange=Exchange.LOCAL,datetime=START+timedelta(minutes=i),
        gateway_name='fixture',interval=Interval.MINUTE,volume=s['volume'],open_price=s['price'],high_price=s['price'],
        low_price=s['price'],close_price=s['price']) for i,s in enumerate(steps)]
    start = time.perf_counter()
    engine.run_backtesting()
    elapsed = time.perf_counter()-start
    assert engine.strategy.i == len(steps)-1, 'vn.py stopped early'
    result = ledger(fills,steps)
    assert native_positions == [row['position'] for row in result['checkpoints']]
    assert engine.strategy.pos == result['position']
    daily = engine.calculate_result()
    native_pnl = float(daily['net_pnl'].sum())
    assert native_pnl == result['checkpoints'][-1]['equity']
    return dict(result,fills=fills,elapsed_seconds=elapsed,native_pnl=native_pnl)


def nautilus(workload, data_kind='bar', capture_cycles=False, liquidity_consumption=False):
    from decimal import Decimal
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, RiskEngineConfig
    from nautilus_trader.trading.strategy import Strategy
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue, TradeId
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Price, Quantity, Money
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import OmsType, AccountType, OrderSide, AggressorSide
    from nautilus_trader.model.data import Bar, BarType, TradeTick
    assert data_kind in ('bar','trade')
    venue = Venue('SIM')
    iid = InstrumentId.from_str('TEST.SIM')
    instrument = Equity(instrument_id=iid,raw_symbol=Symbol('TEST'),currency=USD,
        price_precision=0,price_increment=Price.from_int(1),lot_size=Quantity.from_int(1),ts_event=0,ts_init=0,
        margin_init=Decimal('0'),margin_maint=Decimal('0'),maker_fee=Decimal('0'),taker_fee=Decimal('0'))
    bar_type = BarType.from_str('TEST.SIM-1-MINUTE-LAST-EXTERNAL')
    steps=workload['steps']
    fills=[]
    native_positions=[]
    start_ns=1704067200*10**9

    class FixtureStrategy(Strategy):
        def __init__(self):
            super().__init__()
            self.by_id={}
            self.reverse_id={}
            self.i=-1
            self.observed_count=0

        def on_start(self):
            if data_kind=='bar':
                self.subscribe_bars(bar_type)
            else:
                self.subscribe_trade_ticks(iid)

        def on_bar(self, bar):
            self.observe((bar.ts_init-start_ns)//(60*10**9))

        def on_trade_tick(self, tick):
            self.observe((tick.ts_init-start_ns)//(60*10**9))

        def observe(self, index):
            self.i=index
            self.observed_count+=1
            native_positions.append((int(self.portfolio.net_position(iid)),len(fills)))
            for a in steps[self.i]['actions']:
                if a['kind']=='Cancel':
                    self.cancel_order(self.by_id[a['id']])
                else:
                    order=self.order_factory.limit(instrument_id=iid,
                        order_side=OrderSide.BUY if a['side']=='Buy' else OrderSide.SELL,
                        quantity=Quantity.from_int(a['qty']),price=Price.from_int(a['limit']))
                    self.by_id[a['id']]=order
                    self.reverse_id[order.client_order_id]=a['id']
                    self.submit_order(order)

        def on_order_filled(self, event):
            fills.append(dict(step=(event.ts_event-start_ns)//(60*10**9),id=self.reverse_id[event.client_order_id],
                side='Buy' if event.order_side==OrderSide.BUY else 'Sell',
                qty=float(event.last_qty),price=float(event.last_px)))
            assert float(event.commission) == 0

    engine=BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True),
        risk_engine=RiskEngineConfig(bypass=True),run_analysis=False))
    try:
        engine.add_venue(venue=venue,oms_type=OmsType.NETTING,account_type=AccountType.MARGIN,
            base_currency=USD,starting_balances=[Money(CAPITAL,USD)],
            bar_execution=data_kind=='bar',trade_execution=True,
            liquidity_consumption=liquidity_consumption)
        engine.add_instrument(instrument)
        data=[]
        for i,s in enumerate(steps):
            p=Price.from_int(s['price'])
            timestamp=start_ns+i*60*10**9
            if data_kind=='bar':
                data.append(Bar(bar_type=bar_type,open=p,high=p,low=p,close=p,volume=Quantity.from_int(s['volume']),
                                ts_event=timestamp,ts_init=timestamp))
            elif s['volume']:
                data.append(TradeTick(instrument_id=iid,price=p,size=Quantity.from_int(s['volume']),
                    aggressor_side=AggressorSide.BUYER if s['taker']=='Buy' else AggressorSide.SELLER,
                    trade_id=TradeId(str(i+1)),ts_event=timestamp,ts_init=timestamp))
            else:
                assert not s['actions'], 'TradeTick adapter cannot trigger actions without a trade'
        engine.add_data(data)
        strategy=FixtureStrategy()
        engine.add_strategy(strategy)
        start=time.perf_counter()
        engine.run()
        elapsed=time.perf_counter()-start
        assert strategy.observed_count==len(data)
        result=ledger(fills,steps)
        cumulative=[0]
        for fill in fills:
            cumulative.append(cumulative[-1]+fill['qty']*(1 if fill['side']=='Buy' else -1))
        assert all(position==cumulative[count] for position,count in native_positions)
        assert int(engine.portfolio.net_position(iid))==result['position']
        pnl=engine.portfolio.total_pnl(iid)
        account=engine.cache.account_for_venue(venue)
        fresh_pnl=engine.portfolio.total_pnl(iid,account_id=account.id,
                                           price=Price.from_int(steps[-1]['price']))
        unrealized=engine.portfolio.unrealized_pnl(iid,account_id=account.id,
                                                  price=Price.from_int(steps[-1]['price']))
        # Margin cash records realized PnL, not the trade's notional cash flow.
        # Independently reconcile account balance + open PnL to the fill ledger.
        account_pnl=float(account.balance_total(USD).as_decimal()-Decimal(CAPITAL)
                          +(unrealized.as_decimal() if unrealized else Decimal(0)))
        diagnostics=dict(cached_portfolio_pnl=float(pnl) if pnl is not None else None,
                         fresh_portfolio_pnl=float(fresh_pnl) if fresh_pnl is not None else None,
                         account_pnl=account_pnl)
        rounded_expected=rounded_margin_pnl(fills,steps[-1]['price'])
        diagnostics.update(exact_fill_pnl=result['checkpoints'][-1]['equity'],
                           rounded_margin_pnl=rounded_expected['rounded_pnl'],
                           currency_quantization_bound=rounded_expected['error_bound'],
                           account_rounding_delta=account_pnl-result['checkpoints'][-1]['equity'])
        assert abs(diagnostics['account_rounding_delta'])<=rounded_expected['error_bound']+1e-8,diagnostics
        if capture_cycles:
            from nautilus_cycles import inspect_cycles, DuplicateSnapshots
            cycles=inspect_cycles(engine.cache,iid,account.id)
            duplicate_cycles=inspect_cycles(DuplicateSnapshots(engine.cache),iid,account.id)
            assert duplicate_cycles==cycles
            diagnostics['cycle_identity_aggregation']=cycles
            diagnostics['duplicate_snapshot_invariance']=True
            # Reproducer workloads end flat, so realized account PnL is sufficient.
            if result['position']==0:
                assert abs(cycles['realized_pnl']-account_pnl)<1e-8,(cycles,diagnostics)
        return dict(result,fills=fills,elapsed_seconds=elapsed,native_pnl=account_pnl,
                    native_diagnostics=diagnostics,matching_input=data_kind)
    finally:
        engine.dispose()
