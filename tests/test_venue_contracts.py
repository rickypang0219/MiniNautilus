import json
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from mininautilus.venue import Capabilities,Instrument,Registry

class VenueContractTests(unittest.TestCase):
    def instruments(self):
        rows=json.loads((ROOT/'comparisons/runtime-v2/venues.json').read_text())
        return [Instrument(**dict(row,capabilities=Capabilities(**row['capabilities']))) for row in rows]

    def test_identity_units_and_unsupported_features(self):
        instruments=self.instruments();registry=Registry(instruments)
        self.assertEqual(len(registry.instruments),3)
        for instrument in instruments:
            self.assertEqual(instrument.units(instrument.tick,'price'),1)
            self.assertEqual(instrument.units(instrument.lot,'quantity'),1)
            with self.assertRaises(ValueError): instrument.units(.1,'price')
            with self.assertRaises(ValueError): instrument.capabilities.require('limit',reduce_only=True)
        self.assertNotEqual(instruments[0].key,instruments[1].key)
        with self.assertRaises(ValueError): registry.require_v0(instruments[0].key)
        with self.assertRaises(ValueError): instruments[0].capabilities.require('amend')
        instruments[1].capabilities.require('amend')

    def test_shared_account_cannot_split_its_risk_domain(self):
        first=self.instruments()[0]
        from dataclasses import replace
        with self.assertRaises(ValueError):
            Registry([first,replace(first,symbol='ETHUSDT',risk_domain='another-writer')])

    def test_unit_alignment_does_not_depend_on_decimal_context_precision(self):
        from dataclasses import replace
        from decimal import localcontext
        instrument=replace(self.instruments()[0],tick='0.0001')
        with localcontext() as context:
            context.prec=6
            self.assertEqual(instrument.units('123456789012345678901234567890.0001','price'),
                             1234567890123456789012345678900001)
            with self.assertRaises(ValueError):
                instrument.units('123456789012345678901234567890.00001','price')

    def test_idempotency_absence_watermark_and_accounting_are_explicit(self):
        from dataclasses import replace
        spot,native,weak=self.instruments()
        self.assertEqual(spot.capabilities.submission_idempotency,'query-only')
        self.assertEqual(native.capabilities.snapshot_watermark,'native-atomic')
        self.assertEqual(weak.capabilities.absence_proof_contract,'unsupported')
        for instrument in (spot,native,weak):
            self.assertEqual(instrument.balance_authority,'venue-snapshot')
            self.assertEqual(instrument.fee_authority,'venue-execution-reports')
            self.assertEqual(instrument.core_accounting,'gross-strategy-inventory')
            with self.assertRaises(ValueError):replace(instrument,core_accounting='account-margin')
        with self.assertRaises(ValueError):replace(native.capabilities,history_retention_seconds=None)
        with self.assertRaises(ValueError):replace(spot.capabilities,snapshot_watermark='native-atomic')
        with self.assertRaises(ValueError):replace(weak.capabilities,absence_proof=True)
