"""Conservative native USDC perp adapter, one dedicated Testnet account/instrument.

Rust owns signed gross inventory and orders. This boundary independently checks
venue collateral, USDC fees/funding and position. Unknown delivery NEVER proves
absence. Mainnet is public-data-only. No generic arbitrary exchange actions.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request

URLS = {'mainnet': 'https://api.hyperliquid.xyz', 'testnet': 'https://api.hyperliquid-testnet.xyz'}
Q = Decimal('0.000001')
TERMINAL = {'filled': 'Filled', 'canceled': 'Canceled', 'rejected': 'Rejected'}
# Explicit documented cancel reasons; new venue statuses are not silently accepted.
for _s in ('marginCanceled', 'vaultWithdrawalCanceled', 'openInterestCapCanceled',
           'selfTradeCanceled', 'reduceOnlyCanceled', 'siblingFilledCanceled', 'delistedCanceled',
           'scheduledCancel'):
    TERMINAL[_s] = 'Canceled'


class HyperliquidError(RuntimeError):
    pass


def decimal(value):
    if isinstance(value, (float, bool)):
        raise HyperliquidError('decimal strings required')
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise HyperliquidError('invalid decimal') from None
    if not result.is_finite():
        raise HyperliquidError('nonfinite decimal')
    return result


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63-1:
        raise HyperliquidError('invalid integer')
    return value


def units(value, increment, *, signed=False):
    value, increment = decimal(value), decimal(increment)
    a, b = value.as_integer_ratio(); c, d = increment.as_integer_ratio()
    n, rem = divmod(a*d, b*c)
    if rem or abs(n) > 2**63-1 or (not signed and n < 0):
        raise HyperliquidError('off-grid or unsupported integer units')
    return n


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def wire_decimal(value):
    # Match official float_to_wire without introducing binary floating point.
    result = format(decimal(value), 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset: int
    sz_decimals: int

    def __post_init__(self):
        if not re.fullmatch(r'[A-Z0-9]{1,20}', self.symbol):
            raise HyperliquidError('only native perps supported')
        integer(self.asset)
        if type(self.sz_decimals) is not int or not 0 <= self.sz_decimals <= 6:
            raise HyperliquidError('unsupported size precision')

    @property
    def tick(self): return Decimal(10) ** -(6-self.sz_decimals)
    @property
    def lot(self): return Decimal(10) ** -self.sz_decimals

    def price(self, value):
        p = decimal(value)
        if p <= 0:
            raise HyperliquidError('positive price required')
        if p != p.to_integral_value() and len(p.normalize().as_tuple().digits) > 5:
            raise HyperliquidError('price exceeds five significant figures')
        return units(p, self.tick)

    def size(self, value):
        n = units(value, self.lot)
        if n <= 0: raise HyperliquidError('positive size required')
        return n


class ActionJournal:
    """Bounded side journal for signing nonces/attempts; never stores keys/signatures.

    Exclusive lifetime lock. Complete/torn corruption fails closed: it may contain
    an action that escaped. No guessed truncation or action retry after restart.
    Main Rust intent must already be durable when prepare is called.
    """
    def __init__(self, path):
        self.lock = threading.RLock()
        self.failed = False
        self.attempts = {}
        self.last_nonce = 0
        self.chain = '0'*64
        self.file = open(path, 'a+b', buffering=0)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            for line in self.file:
                if len(line) > 65536 or len(self.attempts) >= 20000 or not line.endswith(b'\n'):
                    raise HyperliquidError('action journal incomplete or capacity exceeded')
                row = json.loads(line)
                digest = hashlib.sha256(self.chain.encode()+canonical(row['payload'])).hexdigest()
                p = row['payload']
                if row['hash'] != digest or p['key'] in self.attempts or p['nonce'] <= self.last_nonce:
                    raise HyperliquidError('action journal corrupt')
                self.chain, self.last_nonce = digest, integer(p['nonce'], 1)
                self.attempts[p['key']] = p
            os.fsync(self.file.fileno())
            fd = os.open(Path(path).parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except BaseException:
            self.file.close()
            raise

    def prepare(self, key, action, wall_ms):
        with self.lock:
            if self.failed or key in self.attempts or len(self.attempts) >= 20000:
                raise HyperliquidError('action already attempted or journal unavailable; reconcile, never resend')
            nonce = max(integer(wall_ms, 1), self.last_nonce+1)
            if nonce-wall_ms > 1000:
                raise HyperliquidError('clock rollback or nonce budget exceeded')
            p = dict(key=key, action=action, nonce=nonce, expires_after=wall_ms+5000)
            digest = hashlib.sha256(self.chain.encode()+canonical(p)).hexdigest()
            row = canonical(dict(payload=p, hash=digest))+b'\n'
            try:
                if len(row) > 65536 or self.file.write(row) != len(row):
                    raise OSError('short action journal write')
                os.fsync(self.file.fileno())
            except BaseException:
                self.failed = True
                raise
            self.chain, self.last_nonce = digest, nonce
            self.attempts[key] = p
            return nonce

    def close(self): self.file.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def unique(rows, key):
    result = {}
    for row in rows:
        identity = key(row)
        if identity in result and result[identity] != row:
            raise HyperliquidError('changed duplicate venue fact')
        result[identity] = row
    return list(result.values())


def account_ledger(symbol, raw_fills, raw_funding):
    """Flat-genesis average-cost oracle, exact Decimal; exchange rounding <=1 USDC micro-unit/fill."""
    fills = sorted(unique(raw_fills, lambda f: integer(f['tid'])), key=lambda f: (f['time'], f['tid']))
    funding = unique(raw_funding, lambda f: (f['time'], f['hash'], f['delta']['coin']))
    position = entry = realized = fees = payments = Decimal(0)
    for f in fills:
        if f['coin'] != symbol or f['feeToken'] != 'USDC' or f['side'] not in ('B', 'A'):
            raise HyperliquidError('foreign fill or unsupported fee token')
        if decimal(f['startPosition']) != position:
            raise HyperliquidError('fill history position gap')
        size, price = decimal(f['sz']), decimal(f['px'])
        if size <= 0 or price <= 0: raise HyperliquidError('invalid fill amount')
        delta = size if f['side'] == 'B' else -size
        closes = position and (position > 0) != (delta > 0)
        pnl = min(abs(position), size)*(price-entry)*(1 if position > 0 else -1) if closes else Decimal(0)
        if abs(pnl-decimal(f['closedPnl'])) > Q:
            raise HyperliquidError('realized PnL disagrees with independent fills')
        after = position+delta
        if not position or (position > 0) == (delta > 0):
            entry = (abs(position)*entry+size*price)/abs(after)
        elif not after: entry = Decimal(0)
        elif (after > 0) != (position > 0): entry = price
        position = after
        # Venue rounding is independently bounded per fill above.
        realized += decimal(f['closedPnl']); fees += decimal(f['fee'])
    for f in funding:
        delta = f['delta']
        if delta['coin'] != symbol or delta['type'] != 'funding':
            raise HyperliquidError('foreign funding event')
        payments += decimal(delta['usdc'])
    return dict(position=position, entry=entry, realized=realized, fees=fees,
                funding=payments, cash_change=realized-fees+payments)


class Hyperliquid:
    def __init__(self, symbol='BTC', session='0000000000000000', *, network='testnet', account=None,
                 execute=False, actions=None, signer=None, max_notional='25', max_position_lots=100,
                 baseline_cash=None, started_ms=None, max_loss='10'):
        if network not in URLS: raise ValueError('unsupported network')
        if execute and network != 'testnet': raise HyperliquidError('mainnet mutations are disabled')
        if not re.fullmatch('[0-9a-f]{16}', session): raise ValueError('session must be 64-bit hex')
        if account is not None and not re.fullmatch('0x[0-9a-fA-F]{40}', account):
            raise ValueError('invalid account address')
        self.network, self.base_url, self.symbol, self.session = network, URLS[network], symbol, session
        self.account, self.execute, self.actions, self.signer = account, execute, actions, signer
        self.max_notional, self.max_loss = decimal(max_notional), decimal(max_loss)
        if self.max_notional <= 0 or self.max_loss <= 0: raise ValueError('positive risk limits required')
        self.max_position_lots = integer(max_position_lots, 1)
        self.baseline_cash = None if baseline_cash is None else decimal(baseline_cash)
        self.started_ms = int(time.time()*1000) if started_ms is None else integer(started_ms)
        self.offset_ms = 0
        self.instrument = None
        self.execution_times = {}
        self.owned_oids = set()
        self.last_reconciliation = {}
        self.lock = threading.RLock()
        self.next_request = 0.0

    def post(self, path, payload):
        if path not in ('/info', '/exchange') or (path == '/exchange' and (not self.execute or self.network != 'testnet')):
            raise HyperliquidError('mutation disabled')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs): return None
        # Bounded serialized pacing; no automatic mutation retry.
        with self.lock:
            time.sleep(max(0, self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+0.1
            # Preserve official action/wire insertion order through signing and transport.
            req = urllib.request.Request(self.base_url+path,
                                         data=json.dumps(payload, separators=(',', ':'), allow_nan=False).encode(),
                                         headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=8) as response:
                    raw = response.read(4*1024*1024+1)
                if len(raw) > 4*1024*1024: raise HyperliquidError('response exceeds bound')
                return json.loads(raw)
            except (OSError, ValueError, urllib.error.URLError):
                raise HyperliquidError('transport/schema failure; mutation outcome may be unknown') from None

    def info(self, kind, **params): return self.post('/info', dict(type=kind, **params))

    def initialize(self):
        meta = self.info('meta')
        rows = [(n, row) for n, row in enumerate(meta['universe']) if row['name'] == self.symbol]
        if len(rows) != 1 or rows[0][1].get('isDelisted', False):
            raise HyperliquidError('unknown or delisted native perp')
        asset, row = rows[0]
        self.instrument = Instrument(self.symbol, asset, row['szDecimals'])
        self.tick, self.lot = self.instrument.tick, self.instrument.lot
        return row

    def client_id(self, order_id):
        integer(order_id, 1)
        return '0x'+self.session+f'{order_id:016x}'

    def quote(self):
        book = self.info('l2Book', coin=self.symbol)
        now = int(time.time()*1000)
        if book['coin'] != self.symbol or not 0 <= now-integer(book['time']) <= 5000:
            raise HyperliquidError('wrong/stale book')
        bid, ask = (self.instrument.price(level[0]['px']) for level in book['levels'])
        if bid > ask: raise HyperliquidError('crossed book')
        return dict(bid=bid, ask=ask)

    def validate(self, intent):
        if intent['side'] not in ('Buy', 'Sell'): raise HyperliquidError('invalid side')
        q, p = integer(intent['qty'], 1)*self.instrument.lot, integer(intent['limit'], 1)*self.instrument.tick
        self.instrument.size(q); self.instrument.price(p)
        integer(intent['id'], 1)
        if not Decimal('10') <= q*p <= self.max_notional:
            raise HyperliquidError('notional outside $10 venue minimum / session cap')

    def account_state(self):
        if not self.account: raise HyperliquidError('Testnet account address required')
        state = self.info('clearinghouseState', user=self.account)
        if not 0 <= int(time.time()*1000)-integer(state['time']) <= 5000:
            raise HyperliquidError('stale account snapshot')
        for item in state['assetPositions']:
            p = item['position']
            if item['type'] != 'oneWay' or p['coin'] != self.symbol:
                raise HyperliquidError('foreign position/account mode')
            if p['leverage']['type'] != 'cross' or p['leverage']['value'] != 1:
                raise HyperliquidError('requires configured cross leverage 1; adapter never changes leverage')
        return state

    def open_orders(self): return self.info('openOrders', user=self.account)

    def check_account_mode(self):
        if self.info('userAbstraction', user=self.account) not in ('default', 'disabled'):
            raise HyperliquidError('unsupported account abstraction')

    def configure_testnet_leverage(self):
        """Explicit setup only: a flat dedicated Testnet account, cross 1x."""
        self.check_account_mode()
        if self.open_orders() or self.account_state()['assetPositions']:
            raise HyperliquidError('leverage setup requires no positions or open orders')
        action = dict(type='updateLeverage', asset=self.instrument.asset, isCross=True, leverage=1)
        self.mutate(action, 'configure-cross-1')
        self.check_leverage()

    def check_leverage(self):
        active = self.info('activeAssetData', user=self.account, coin=self.symbol)
        if active.get('leverage') != {'type': 'cross', 'value': 1}:
            raise HyperliquidError('requires configured cross leverage 1 even while flat')

    def admission(self, intent):
        self.validate(intent)
        self.check_account_mode()
        self.check_leverage()
        state = self.account_state()
        positions = state['assetPositions']
        if len(positions) > 1: raise HyperliquidError('multiple positions unsupported')
        position = units(positions[0]['position']['szi'], self.instrument.lot, signed=True) if positions else 0
        change = intent['qty']*(1 if intent['side'] == 'Buy' else -1)
        if max(abs(position), abs(position+change)) > self.max_position_lots:
            raise HyperliquidError('position budget exceeded')
        summary = state['marginSummary']
        nav, used = decimal(summary['accountValue']), decimal(summary['totalMarginUsed'])
        if self.baseline_cash is None or nav < self.baseline_cash-self.max_loss or nav < 100 or used < 0:
            raise HyperliquidError('collateral/loss limit')
        if self.open_orders():
            raise HyperliquidError('account already has open orders; no concurrent reservation domains')
        # Reserve full order notional plus fixed safety buffer, independent of leverage.
        if nav-used < self.max_notional*2:
            raise HyperliquidError('insufficient conservative collateral buffer')
        return position != 0 and (position > 0) != (change > 0) and abs(change) <= abs(position)

    def mutate(self, action, key):
        if not self.execute or self.network != 'testnet' or self.actions is None or self.signer is None:
            raise HyperliquidError('Testnet signing is disabled')
        setup = action == dict(type='updateLeverage', asset=self.instrument.asset, isCross=True, leverage=1)
        if action['type'] not in ('order', 'cancelByCloid') and not setup:
            raise HyperliquidError('unsupported mutation')
        with self.lock:
            now = int(time.time()*1000)
            nonce = self.actions.prepare(key, action, now)
            expires = self.actions.attempts[key]['expires_after']
            signature = self.signer(action, nonce, expires)
            response = self.post('/exchange', dict(action=action, nonce=nonce, signature=signature,
                                                    vaultAddress=None, expiresAfter=expires))
            if response.get('status') != 'ok':
                raise HyperliquidError('exchange rejected action; reconcile without retry')
            if setup:
                if response.get('response', {}).get('type') != 'default':
                    raise HyperliquidError('unknown leverage configuration acknowledgement')
                return response['response']
            statuses = response['response']['data']['statuses']
            if len(statuses) != 1 or isinstance(statuses[0], dict) and 'error' in statuses[0]:
                raise HyperliquidError('order action rejected; reconcile without retry')
            return statuses[0]

    def submit(self, intent):
        with self.lock:
            reduce_only = self.admission(intent)
            action = dict(type='order', orders=[dict(a=self.instrument.asset, b=intent['side']=='Buy',
                          p=wire_decimal(self.instrument.tick*intent['limit']), s=wire_decimal(self.instrument.lot*intent['qty']),
                          r=reduce_only, t={'limit': {'tif': 'Gtc'}}, c=self.client_id(intent['id']))], grouping='na')
            status = self.mutate(action, 'order-'+str(intent['id']))
            if not isinstance(status, dict) or not ('resting' in status or 'filled' in status):
                raise HyperliquidError('unknown order acknowledgement')
            ack = status.get('resting', status.get('filled'))
            self.owned_oids.add(integer(ack['oid']))
            return status

    def query(self, intent):
        response = self.info('orderStatus', user=self.account, oid=self.client_id(intent['id']))
        if response.get('status') != 'order':
            raise HyperliquidError('unknown order: absence is unresolved, never resend')
        result = response['order']; order = result['order']
        if (order.get('cloid') != self.client_id(intent['id']) or order['coin'] != self.symbol
                or order['side'] != ('B' if intent['side']=='Buy' else 'A')
                or self.instrument.size(order['origSz']) != intent['qty']
                or self.instrument.price(order['limitPx']) != intent['limit']):
            raise HyperliquidError('remote order identity/terms mismatch')
        self.owned_oids.add(integer(order['oid']))
        if result['status'] not in TERMINAL and result['status'] != 'open':
            raise HyperliquidError('unsupported remote lifecycle')
        return result

    def cancel(self, intent):
        with self.lock:
            order = self.query(intent)
            if order['status'] in TERMINAL: return order
            status = self.mutate(dict(type='cancelByCloid', cancels=[dict(asset=self.instrument.asset,
                                 cloid=self.client_id(intent['id']))]), 'cancel-'+str(intent['id']))
            if status != 'success': raise HyperliquidError('unknown cancellation acknowledgement')
            return status

    def history(self, kind, *, end_ms=None):
        """Recursive inclusive time splits preserve equal-timestamp pages; saturation fails closed.

        Maximum 10000 facts /64 requests. History retention is additionally proven
        by each order's cumulative fills and account cash; never infer completeness
        merely from a short response.
        """
        end = int(time.time()*1000) if end_ms is None else end_ms
        todo, result, count = [(self.started_ms, end)], [], 0
        while todo:
            lo, hi = todo.pop(); count += 1
            if count > 64: raise HyperliquidError('history request budget')
            args = dict(user=self.account, startTime=lo, endTime=hi)
            if kind == 'userFillsByTime': args['aggregateByTime'] = False
            page = self.info(kind, **args)
            if not isinstance(page, list) or any(not lo <= integer(r['time']) <= hi for r in page):
                raise HyperliquidError('invalid history window')
            # Funding endpoint has smaller response limits than fills; use 500 conservatively.
            limit = 2000 if kind == 'userFillsByTime' else 500
            if len(page) >= limit:
                if lo == hi: raise HyperliquidError('equal-timestamp history saturation; completeness unproven')
                mid = (lo+hi)//2
                todo.extend([(mid+1, hi), (lo, mid)])
                continue
            result.extend(page)
            if len(result) > 10000: raise HyperliquidError('hot history cap')
        return unique(result, (lambda r: integer(r['tid'])) if kind == 'userFillsByTime' else
                      (lambda r: (r['time'], r['hash'], r['delta']['coin'])))

    def collect(self, intent, all_fills=None):
        remote = self.query(intent)
        rows = self.history('userFillsByTime') if all_fills is None else all_fills
        selected = unique([f for f in rows if f['oid']==remote['order']['oid']], lambda f: integer(f['tid']))
        fills = []
        for f in selected:
            if f['coin'] != self.symbol or f['side'] != ('B' if intent['side']=='Buy' else 'A'):
                raise HyperliquidError('fill identity mismatch')
            execution_id = integer(f['tid'])+1
            integer(execution_id, 1)
            fills.append(dict(execution_id=execution_id, order_id=intent['id'],
                              qty=self.instrument.size(f['sz']), price=self.instrument.price(f['px'])))
            self.execution_times[execution_id] = integer(f['time'])
        remaining = units(remote['order']['sz'], self.instrument.lot)
        filled = sum(f['qty'] for f in fills)
        if filled > intent['qty'] or remaining > intent['qty']:
            raise HyperliquidError('fill quantity exceeds intent')
        # sz is remaining size also on canceled orders. Never treat canceled as zero-filled.
        if filled != intent['qty']-remaining:
            raise HyperliquidError('incomplete fill history / cumulative quantity mismatch')
        status = remote['status']
        lifecycle = TERMINAL.get(status, 'Partial' if filled else 'Accepted')
        if lifecycle == 'Filled' and filled != intent['qty']:
            raise HyperliquidError('filled order lacks complete history')
        if lifecycle == 'Rejected' and filled:
            raise HyperliquidError('rejected order has fills')
        return dict(intent=intent, filled=filled, lifecycle=lifecycle), fills, selected

    def poll(self, state):
        if not state['orders']: return []
        rows = self.history('userFillsByTime')
        reports = []
        for o in state['orders'].values():
            remote, fills, _ = self.collect(o['intent'], rows)
            reports.extend({'Fill': f} for f in fills if str(f['execution_id']) not in state['fills'])
            oid, status = o['intent']['id'], remote['lifecycle']
            if status in ('Accepted', 'Partial'): reports.append({'Accepted': {'id': oid}})
            elif status == 'Canceled': reports.append({'Canceled': {'id': oid, 'cumulative_filled': remote['filled']}})
            elif status == 'Rejected': reports.append({'Rejected': {'id': oid}})
        return reports

    def reconcile(self, state):
        with self.lock:
            if len(state['orders']) > 1000: raise HyperliquidError('session order cap; offline audit required')
            known = {self.client_id(int(i)) for i in state['orders']}
            if any(o.get('cloid') not in known or o['coin'] != self.symbol for o in self.open_orders()):
                raise HyperliquidError('foreign open orders; never cancel another writer')
            # Quiescent, all own orders terminal. Mutation uncertainty is deliberately NOT resolved by absence.
            for o in state['orders'].values(): self.cancel(o['intent'])
            all_fills = self.history('userFillsByTime')
            orders, fills, owned_ids = [], [], set()
            for o in state['orders'].values():
                remote, fs, raw = self.collect(o['intent'], all_fills)
                if remote['lifecycle'] not in ('Filled', 'Canceled', 'Rejected'):
                    raise HyperliquidError('account not quiescent')
                orders.append(remote); fills.extend(fs); owned_ids.update(f['tid'] for f in raw)
            if owned_ids != {f['tid'] for f in all_fills}:
                raise HyperliquidError('foreign or unowned historical fills')
            ledger = account_ledger(self.symbol, all_fills, self.history('userFunding'))
            account = self.account_state()
            positions = account['assetPositions']
            if len(positions)>1: raise HyperliquidError('multiple positions')
            position = decimal(positions[0]['position']['szi']) if positions else Decimal(0)
            unrealized = decimal(positions[0]['position']['unrealizedPnl']) if positions else Decimal(0)
            actual = decimal(account['marginSummary']['accountValue'])-unrealized
            if self.baseline_cash is None or abs(actual-(self.baseline_cash+ledger['cash_change'])) > Q:
                raise HyperliquidError('USDC cash/funding/fee mismatch or external transfer')
            if position != ledger['position'] or self.open_orders():
                raise HyperliquidError('position mismatch or nonquiescent account')
            self.last_reconciliation = {k: str(v) for k,v in ledger.items()}
            return dict(epoch=state['epoch'], watermark=state['venue_seq'], orders=orders, fills=fills,
                        position=units(position, self.instrument.lot, signed=True), absent=[])

    def timing(self, event):
        if not isinstance(event, dict): return {}
        if 'Reconcile' in event: return dict(fill_event_times=self.execution_times.copy())
        f = event.get('Execution', {}).get('report', {}).get('Fill')
        if f and f['execution_id'] in self.execution_times:
            return dict(event_time_ms=self.execution_times[f['execution_id']], time_source='hyperliquid fill')
        return {}

    def audit_account(self):
        """Private heartbeat also checks funding/fees/external balance changes.

        Non-atomic REST races deliberately close the gate; quiescent reconciliation
        is required to resume. No claim of a native sequence or atomic snapshot.
        """
        with self.lock:
            fills = self.history('userFillsByTime')
            if any(f['oid'] not in self.owned_oids for f in fills):
                raise HyperliquidError('unowned historical fill; reconcile before continuing')
            ledger = account_ledger(self.symbol, fills, self.history('userFunding'))
            state = self.account_state()
            positions = state['assetPositions']
            if len(positions) > 1: raise HyperliquidError('multiple positions')
            position = decimal(positions[0]['position']['szi']) if positions else Decimal(0)
            upnl = decimal(positions[0]['position']['unrealizedPnl']) if positions else Decimal(0)
            cash = decimal(state['marginSummary']['accountValue'])-upnl
            if self.baseline_cash is None or abs(cash-self.baseline_cash-ledger['cash_change']) > Q or position != ledger['position']:
                raise HyperliquidError('live account/funding/fee discrepancy')
            return ledger


@contextmanager
def account_writer_lock(account, directory='/tmp/mininautilus-hyperliquid-locks'):
    """Prevent two local sessions sharing an API nonce/account reservation domain.

    Operators must also keep the dedicated account exclusive across machines.
    Never unlink the inode: another process may already be waiting on it.
    """
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(account.lower().encode()).hexdigest()
    with (directory/name).open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def testnet_credentials(environ=None):
    """Read existing HL bindings; validate the destination before constructing a signer."""
    env = os.environ if environ is None else environ
    if env.get('HL_NETWORK') != 'testnet':
        raise HyperliquidError('HL_NETWORK must explicitly be testnet')
    account, agent, key = (env.get(k, '') for k in
                          ('HL_MAIN_ADDRESS', 'HL_API_ADDRESS', 'HL_API_PRIVATE_KEY'))
    if not all(re.fullmatch('0x[0-9a-fA-F]{40}', a) for a in (account, agent)):
        raise HyperliquidError('valid HL_MAIN_ADDRESS and HL_API_ADDRESS required')
    if not re.fullmatch('(0x)?[0-9a-fA-F]{64}', key):
        raise HyperliquidError('raw HL_API_PRIVATE_KEY required for local signing')
    return account, agent, key


def testnet_signer(private_key, agent_address=None):
    """Official SDK signing only, no SDK HTTP client or mainnet boolean exposed."""
    from eth_account import Account
    from hyperliquid.utils.signing import sign_l1_action
    try: wallet = Account.from_key(private_key)
    except Exception: raise HyperliquidError('invalid securely injected Testnet signing key') from None
    if agent_address is not None and wallet.address.lower() != agent_address.lower():
        raise HyperliquidError('signing key does not match HL_API_ADDRESS')
    return lambda action, nonce, expires: sign_l1_action(wallet, action, None, nonce, expires, False)
