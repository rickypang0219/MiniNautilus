"""Cold-path, causal L2/trade replay over the existing Rust backtest Core.

Only public observations and bounded strategy inputs enter matching. The Core's
simulated executions determine inventory and subsequent order quantities. This
is an explicit model, not proof of exchange queue priority or market impact.
"""
from bisect import bisect_right
from fractions import Fraction as F
import heapq

from .bridge import next_order_id
from .sma import Candle, SmaStrategy


def integer_units(value, increment):
    units = F(value) / F(increment)
    if units.denominator != 1 or units <= 0:
        raise ValueError('nonpositive/off-grid market observation')
    return int(units)


def round_cash(value, quantum):
    """Signed truncation toward zero, frozen separately from exact Core economics."""
    return int(value / F(quantum)) * F(quantum)


def public_events(rows):
    """Venue facts use exchange time; agent observations retain availability time.

    Offline matching may know a past physical fact before its delayed public
    delivery. Only the separate quote/publication events can inform decisions.
    """
    for row in rows:
        kind = row['kind']
        if kind == 'ws':
            message = row['message']; channel = message.get('channel')
            if channel == 'l2Book':
                yield message['data']['time'], 'venue_book', message['data']
                yield row['received_ms'], 'quote', message['data']
            elif channel == 'activeAssetCtx':
                if message['data']['coin']!='BTC':raise ValueError('foreign oracle')
                yield row['received_ms'], 'oracle', dict(price=message['data']['ctx']['oraclePx'])
            elif channel == 'trades':
                for trade in message['data']:
                    yield trade['time'], 'trade', trade
        elif kind == 'runtime_boundary' and row['phase']=='start' and row['flatten']:
            yield row['planned_stop_ms'], 'stop', None
        elif kind == 'http' and row['path']=='/info' and row['request'].get('type')=='l2Book':
            yield row['response']['time'], 'venue_book', row['response']
            yield row['received_ms'], 'quote', row['response']
        elif kind == 'strategy_publication':
            yield row['available_ms'], 'publish', row['bar_open_ms']
        elif kind == 'strategy':
            yield row['received_ms'], 'bar', row['request']['bar']
        elif kind == 'asset_context':
            yield row['available_ms'], 'oracle', dict(price=row['context']['oraclePx'])
        elif kind == 'http' and row['path'] == '/info' and row['request'].get('type') == 'fundingHistory':
            for fact in row['response']:
                yield row['received_ms'], 'funding', fact


def walk_book(intent, book, consumed, manifest):
    """Cross only visible opposite depth; one snapshot's liquidity is used once."""
    fills = []
    remaining = intent['qty']
    levels = book['levels'][1 if intent['side'] == 'Buy' else 0]
    for index, level in enumerate(levels):
        price = integer_units(level['px'], manifest['tick'])
        if (price > intent['limit'] if intent['side'] == 'Buy' else price < intent['limit']):
            break
        key = (intent['side'], index)
        size = integer_units(level['sz'], manifest['lot']) - consumed.get(key, 0)
        quantity = min(remaining, max(0, size))
        if quantity:
            fills.append((quantity, price)); consumed[key] = consumed.get(key, 0) + quantity
            remaining -= quantity
        if not remaining:
            break
    return fills


def replay_depth(engine, events, manifest):
    """Run its own strategy, OMS and executions; never accepts actual execution data.

    Passive matching conservatively starts behind displayed same-price size and
    advances only on public opposing trades. That assumption remains unvalidated;
    every order using it is explicitly outside the immediate-cross coverage class.
    """
    if manifest['model'] != 'causal-depth-trade-v2' or manifest['arrival_delay_ms'] < 0 or manifest.get('report_delay_ms',0)<0:
        raise ValueError('unsupported model/latency')
    pending = []; serial = 0
    for at, kind, data in events:
        serial += 1; heapq.heappush(pending, (at, 0, serial, kind, data))
    if not pending:
        raise ValueError('empty public replay')
    strategy = SmaStrategy(fast=3, slow=8, lots=20, interval='1m')
    book = agent_book = None; book_at = agent_book_at = None
    consumed = {}; orders = {}; venue_seq = 0
    venue_position = delivered_position = 0; delivered_cash = F(0)
    execution_free_at = 0; published_bar = None
    report_delay = manifest.get('report_delay_ms',0)
    fills = []; positions = []; oracles = []; funding_seen = {}; trade_seen = {}
    cash = fees = funding = F(0); unknown_funding = []; signals = []; failures = []
    stopping = False
    now = pending[0][0]
    quantum = F(manifest['tick']) * F(manifest['lot'])

    def queue_report(body):
        nonlocal serial
        serial+=1
        identity=body.get('Fill',{}).get('order_id',body.get('Accepted',body.get('Canceled',{})).get('id'))
        due=max(now+report_delay,orders[identity].get('last_report_ms',0))
        orders[identity]['last_report_ms']=due
        heapq.heappush(pending,(due,2,serial,'report',body))

    def fill_order(order, quantity, price, maker):
        nonlocal cash, fees, venue_position
        if quantity <= 0 or order['filled'] + quantity > order['intent']['qty']:
            raise ValueError('model overfill')
        intent = order['intent']; sign = 1 if intent['side'] == 'Buy' else -1
        identity = len(fills) + 1
        queue_report({'Fill': dict(execution_id=identity, order_id=intent['id'], qty=quantity, price=price)})
        cash -= sign * quantity * price * quantum
        rate = manifest['maker_fee_rate'] if maker else manifest['taker_fee_rate']
        fee = round_cash(quantity * price * quantum * F(rate), manifest['cash_quantum'])
        fees += fee; order['filled'] += quantity
        fills.append(dict(at_ms=now,available_ms=now+report_delay, order_id=intent['id'], side=intent['side'], qty=quantity, price=price, fee=str(fee), maker=maker))
        venue_position+=sign*quantity
        positions.append((now,venue_position))

    def schedule_effects(effects):
        nonlocal serial, execution_free_at
        for effect in effects:
            if 'SendOrder' in effect:
                intent = effect['SendOrder']; serial += 1
                orders[intent['id']] = dict(intent=intent, submitted_ms=now, filled=0, immediate_filled=0, coverage='PENDING_ARRIVAL', queue_ahead=0, canceled=False, operational=stopping, signal_open_ms=published_bar)
                execution_free_at=now+manifest['arrival_delay_ms']+report_delay
                heapq.heappush(pending,(now+manifest['arrival_delay_ms'],1,serial,'arrival',intent['id']))
            elif 'SendCancel' in effect:
                serial += 1
                identity=effect['SendCancel']['id']
                # The live execution owner serializes submit before its cancel.
                due=max(now,execution_free_at)+manifest['cancel_delay_ms']
                execution_free_at=due+report_delay
                heapq.heappush(pending,(due,1,serial,'cancel',identity))
            else:
                failures.append(dict(at_ms=now,effect=effect))

    def dispatch_target():
        state = engine.state; target = state.get('target')
        if now<execution_free_at or not target or target['valid_until'] < now or state.open_orders or state['health'] != 'Healthy':
            return
        if agent_book is None or now-agent_book_at > manifest.get('max_agent_quote_age_ms',5000):
            return
        delta = target['position'] - state['position']
        if not delta:
            return
        side = 'Buy' if delta > 0 else 'Sell'
        touch = integer_units(agent_book['levels'][1 if delta>0 else 0][0]['px'],manifest['tick'])
        limit = touch
        if stopping:
            value=touch*F(manifest['tick'])*F(1001 if delta>0 else 999,1000)
            whole=-(-value.numerator//value.denominator) if delta>0 else value.numerator//value.denominator
            limit=integer_units(str(whole),manifest['tick'])
        intent = dict(id=next_order_id(state),side=side,qty=abs(delta),limit=limit,based_on_seq=state['seq'],valid_until=now+3000)
        effects = engine.send(now,{'SubmitTargeted':dict(intent=intent,revision=target['revision'],expected_position=state['position'])})
        schedule_effects(effects)

    while pending:
        now, _, _, kind, data = heapq.heappop(pending)
        client_event=kind in ('book','quote','bar','publish','target','stop','report','oracle','funding')
        if client_event:
            # Explicit ideal private heartbeat; network fault replay is a separate gate.
            engine.send(now, {'Heartbeat': {'epoch': engine.state['epoch']}})
        if kind in ('book','venue_book','quote'):
            for side,levels in enumerate(data['levels']):
                prices=[integer_units(v['px'],manifest['tick']) for v in levels]
                for level in levels:integer_units(level['sz'],manifest['lot'])
                if not prices or prices!=sorted(prices,reverse=side==0):raise ValueError('invalid depth ordering')
            if len(data['levels'])!=2 or data['coin']!='BTC' or data['time']>now:
                raise ValueError('foreign/future/invalid book')
            if kind in ('book','venue_book'):
                if book is not None and data['time']<book['time']:continue
                if book is None or data['time']!=book['time']:
                    book=data;book_at=now;consumed={}
                elif data!=book:raise ValueError('changed equal-time book')
            if kind in ('book','quote'):
                if agent_book is not None and data['time']<agent_book['time']:continue
                agent_book=data;agent_book_at=now
                bid=integer_units(data['levels'][0][0]['px'],manifest['tick'])
                ask=integer_units(data['levels'][1][0]['px'],manifest['tick'])
                schedule_effects(engine.send(now,{'Quote':dict(bid=bid,ask=ask)}))
        elif kind == 'bar':
            if not strategy.add(Candle(**data)):
                continue
            target = strategy.target if strategy.ready else None
            signals.append(dict(open_ms=data['open_ms'],target=target))
        elif kind == 'publish':
            if strategy.last is None or strategy.last.open_ms != data:
                raise ValueError('publication missing causal bounded inputs')
            if not stopping and strategy.ready and strategy.last.close_ms + 65000 >= now:
                published_bar=data
                schedule_effects(engine.send(now,{'SetTarget':dict(revision=engine.state['last_target_revision']+1,position=strategy.target,valid_until=strategy.last.close_ms+65000)}))
        elif kind == 'target':  # Explicit synthetic policy fixtures, never recovered live orders.
            schedule_effects(engine.send(now,{'SetTarget':dict(revision=engine.state['last_target_revision']+1,position=data,valid_until=now+65000)}))
        elif kind == 'arrival':
            order = orders[data]; intent = order['intent']; order['arrived_ms'] = now
            queue_report({'Accepted':dict(id=data)})
            if book is None or now-book_at > manifest['max_book_age_ms'] or now-book['time'] > manifest['max_book_age_ms']:
                order['coverage'] = 'MISSING_FRESH_BOOK'
            else:
                immediate = walk_book(intent,book,consumed,manifest)
                order['immediate_filled'] = sum(q for q,_ in immediate)
                order['coverage'] = 'IMMEDIATE_CROSS' if order['immediate_filled']==intent['qty'] else 'PASSIVE_QUEUE_UNVALIDATED'
                for quantity, price in immediate:
                    fill_order(order,quantity,price,False)
                same = book['levels'][0 if intent['side']=='Buy' else 1]
                order['queue_ahead'] = sum(integer_units(v['sz'],manifest['lot']) for v in same if integer_units(v['px'],manifest['tick'])==intent['limit'])
        elif kind == 'trade':
            if data['coin']!='BTC' or data['side'] not in ('B','A') or data['time'] > now:
                raise ValueError('foreign/invalid public trade')
            identity=data['tid']
            if identity in trade_seen:
                if trade_seen[identity]!=data:raise ValueError('changed public trade')
                continue
            trade_seen[identity]=data
            price=integer_units(data['px'],manifest['tick']);quantity=integer_units(data['sz'],manifest['lot'])
            for order in orders.values():
                i=order['intent'];remaining=i['qty']-order['filled']
                if order['canceled'] or not remaining or data['time']<order.get('arrived_ms',now+1) or (i['side']=='Buy')==(data['side']=='B'):
                    continue
                if price>i['limit'] if i['side']=='Buy' else price<i['limit']:
                    continue
                ahead=min(quantity,order['queue_ahead']);order['queue_ahead']-=ahead;quantity-=ahead
                take=min(quantity,remaining);quantity-=take
                if take:fill_order(order,take,i['limit'],True)
        elif kind == 'stop':
            stopping=True
            schedule_effects(engine.send(now,{'SetTarget':dict(revision=engine.state['last_target_revision']+1,position=0,valid_until=now+30000)}))
            for order in list(engine.state.open_orders.values()):
                schedule_effects(engine.send(now,{'Cancel':dict(id=order['intent']['id'])}))
        elif kind == 'cancel':
            order=orders[data];order['canceled']=True;order['cancel_effective_ms']=now
            queue_report({'Canceled':dict(id=data,cumulative_filled=order['filled'])})
        elif kind == 'report':
            venue_seq+=1
            effects=engine.send(now,{'Execution':dict(epoch=engine.state['epoch'],venue_seq=venue_seq,report=data)})
            if effects:raise ValueError('simulated execution gated: '+str(effects))
            if 'Fill' in data:
                fill=data['Fill'];side=orders[fill['order_id']]['intent']['side'];sign=1 if side=='Buy' else -1
                delivered_position+=sign*fill['qty'];delivered_cash-=sign*fill['qty']*fill['price']*quantum
            if engine.state['position']!=delivered_position or F(engine.state['cash'])*quantum!=delivered_cash:
                raise ValueError('independent/Core delivered ledger mismatch')
        elif kind == 'oracle':
            if F(data['price'])<=0:raise ValueError('nonpositive oracle')
            oracles.append((now,F(data['price'])))
        elif kind == 'funding':
            identity=(data['coin'],data['time'])
            if data['coin']!='BTC' or data['time']>now:raise ValueError('foreign/future funding')
            if identity in funding_seen:
                if funding_seen[identity]!=data:raise ValueError('changed public funding')
                continue
            funding_seen[identity]=data
            index=bisect_right([t for t,_ in positions],data['time'])-1
            lots=positions[index][1] if index>=0 else 0
            oracle_index=bisect_right([t for t,_ in oracles],data['time'])-1
            if lots and (oracle_index<0 or data['time']-oracles[oracle_index][0]>manifest['max_oracle_age_ms']):
                unknown_funding.append(data['time'])
            elif lots:
                funding+=round_cash(-lots*F(manifest['lot'])*oracles[oracle_index][1]*F(data['fundingRate']),manifest['cash_quantum'])
        else:
            raise ValueError('unknown replay input')
        if kind in ('book','quote','publish','target','stop','report'):
            dispatch_target()
    if engine.state['position']!=venue_position or delivered_cash!=cash:
        raise ValueError('undelivered modeled executions at replay end')
    mark = (F(book['levels'][0][0]['px'])+F(book['levels'][1][0]['px']))/2 if book else None
    equity = cash + engine.state['position']*F(manifest['lot'])*mark if mark is not None else None
    return dict(orders=list(orders.values()),fills=fills,signals=signals,failures=failures,
                position=engine.state['position'],venue_position=venue_position,gross_cash=str(cash),fees=str(fees),funding=str(funding),
                funding_settlements=len(funding_seen),unknown_funding=unknown_funding,
                gross_marked=str(equity) if equity is not None else None,
                net_marked=str(equity-fees+funding) if equity is not None and not unknown_funding else None,
                accounting='PASS',scope='own simulated executions drive Core inventory and future orders; queue/impact assumptions require holdout validation')
