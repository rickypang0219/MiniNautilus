"""Venue/account/instrument contracts; v0 Core remains a single Spot instrument."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Capabilities:
    native_sequence: bool
    atomic_snapshot: bool
    history_retention_seconds: int | None
    absence_proof: bool
    amend: bool = False
    reduce_only: bool = False
    client_id_max_length: int = 32
    account_margin: bool = False
    client_id_scope: str = 'open-orders'
    submission_idempotency: str = 'query-only'
    snapshot_watermark: str = 'quiescent-local-delivery'
    absence_proof_contract: str = 'after-request-expiry-and-client-id-lookup'

    def __post_init__(self):
        if self.client_id_scope not in ('open-orders','retention-window','session'):
            raise ValueError('unknown client ID uniqueness scope')
        if self.submission_idempotency not in ('none','query-only','deduplicated-within-retention'):
            raise ValueError('unknown submission idempotency contract')
        if self.snapshot_watermark not in ('none','quiescent-local-delivery','native-atomic'):
            raise ValueError('unknown snapshot watermark contract')
        if self.snapshot_watermark=='native-atomic' and not (self.native_sequence and self.atomic_snapshot):
            raise ValueError('native atomic watermark requires both capabilities')
        if self.submission_idempotency=='deduplicated-within-retention' and self.history_retention_seconds is None:
            raise ValueError('deduplication needs a declared retention horizon')
        if self.absence_proof_contract not in ('unsupported','after-request-expiry-and-client-id-lookup','native-watermark-and-retention'):
            raise ValueError('unknown absence proof contract')
        if self.absence_proof != (self.absence_proof_contract!='unsupported'):
            raise ValueError('absence capability and proof contract disagree')
        if self.absence_proof_contract=='native-watermark-and-retention' and (
                self.snapshot_watermark!='native-atomic' or self.history_retention_seconds is None):
            raise ValueError('native absence proof requires watermark and history horizon')

    def require(self, operation, *, reduce_only=False):
        if operation not in ('limit','cancel','reconcile','amend'):
            raise ValueError('unsupported order operation')
        if operation=='amend' and not self.amend:
            raise ValueError('venue does not support amend')
        if reduce_only and not self.reduce_only:
            raise ValueError('venue does not support reduce-only')


@dataclass(frozen=True)
class Instrument:
    venue: str
    account: str
    symbol: str
    risk_domain: str
    tick: str
    lot: str
    quote_asset: str
    base_asset: str
    capabilities: Capabilities
    balance_authority: str = 'venue-snapshot'
    fee_authority: str = 'venue-execution-reports'
    core_accounting: str = 'gross-strategy-inventory'

    def __post_init__(self):
        if not all((self.venue,self.account,self.symbol,self.risk_domain,self.quote_asset,self.base_asset)):
            raise ValueError('explicit venue/account/instrument/risk identity required')
        if (self.balance_authority!='venue-snapshot' or self.fee_authority!='venue-execution-reports'
                or self.core_accounting!='gross-strategy-inventory'):
            raise ValueError('v0 Core does not implement wallet fees or margin accounting')
        for unit in (self.tick,self.lot):
            if not isinstance(unit,str): raise ValueError('tick/lot must be decimal strings')
            value=Decimal(unit)
            if not value.is_finite() or value<=0: raise ValueError('positive finite integer units required')
        if self.capabilities.client_id_max_length<1: raise ValueError('invalid client ID limit')
        if self.capabilities.history_retention_seconds is not None and self.capabilities.history_retention_seconds<=0:
            raise ValueError('unknown retention is None, never zero')

    @property
    def key(self): return self.venue,self.account,self.symbol

    def units(self,value,kind):
        if isinstance(value,float) or kind not in ('price','quantity'):
            raise ValueError('decimal strings and explicit unit kind required')
        value=Decimal(value)
        unit=Decimal(self.tick if kind=='price' else self.lot)
        if not value.is_finite():
            raise ValueError('finite instrument value required')
        # Decimal division rounds to the caller's context; rounding can hide an
        # off-grid value or change an integer beyond its precision. Ratios keep
        # both alignment and conversion exact, independent of that context.
        numerator,denominator=value.as_integer_ratio()
        unit_numerator,unit_denominator=unit.as_integer_ratio()
        result,remainder=divmod(numerator*unit_denominator,denominator*unit_numerator)
        if remainder:
            raise ValueError('value is not aligned to instrument units')
        return result


class Registry:
    def __init__(self,instruments):
        self.instruments={}
        accounts={}
        for instrument in instruments:
            if instrument.key in self.instruments: raise ValueError('duplicate instrument identity')
            account=instrument.venue,instrument.account
            if account in accounts and accounts[account]!=instrument.risk_domain:
                raise ValueError('shared account cannot have independent risk writers')
            accounts[account]=instrument.risk_domain
            self.instruments[instrument.key]=instrument

    def require_v0(self,key):
        instrument=self.instruments[key]
        if len(self.instruments)!=1 or instrument.capabilities.account_margin:
            raise ValueError('v0 Core is one Spot instrument; multi-instrument risk is not implemented')
        return instrument
