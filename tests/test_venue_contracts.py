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
