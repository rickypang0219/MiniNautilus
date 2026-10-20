"""Regressions uncovered while comparing cross-platform accounting."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'python'), str(ROOT / 'examples')]
from replay import run


class ReplayMarkTests(unittest.TestCase):
    def test_open_short_is_marked_at_ask(self):
        # Use a patched strategy solely to exercise the generic replay report.
        from unittest.mock import patch
        class ShortOnce:
            def on_quote(self, state):
                if state['orders']:
                    return None
                return dict(id=1, side='Sell', qty=2, limit=100,
                            based_on_seq=state['seq'], valid_until=1000)
        events = [dict(at=0,event={'Quote':dict(bid=99,ask=101)}),
                  dict(at=1,event={'Trade':dict(taker='Buy',price=100,qty=2)}),
                  dict(at=2,event={'Quote':dict(bid=94,ask=97)})]
        with tempfile.TemporaryDirectory() as d, patch('replay.TargetPosition', return_value=ShortOnce()):
            result = run(Path(d)/'journal',events)
        self.assertEqual(result['position'],-2)
        self.assertEqual(result['cash_tick_lots'],200)
        self.assertEqual(result['gross_marked_pnl_tick_lots'],6)

    def test_flat_replay_without_quotes_has_known_zero_pnl(self):
        with tempfile.TemporaryDirectory() as d:
            result = run(Path(d)/'journal',[])
        self.assertEqual(result['gross_marked_pnl_tick_lots'],0)

    def test_open_position_without_quote_has_unknown_mark(self):
        events = [dict(at=0,event={'Quote':dict(bid=99,ask=101)}),
                  dict(at=1,event={'Trade':dict(taker='Sell',price=101,qty=1)}),
                  dict(at=2,event='MarketUnavailable')]
        with tempfile.TemporaryDirectory() as d:
            result = run(Path(d)/'journal',events)
        self.assertEqual(result['position'],1)
        self.assertIsNone(result['gross_marked_pnl_tick_lots'])


class AnalysisMarkTests(unittest.TestCase):
    def test_missing_mark_does_not_invent_zero_unrealized_pnl(self):
        from analyze import analyze
        events = [dict(at=0,event={'Quote':dict(bid=99,ask=101)}),
                  dict(at=1,event={'Trade':dict(taker='Sell',price=101,qty=1)}),
                  dict(at=2,event={'Quote':dict(bid=108,ask=110)}),
                  dict(at=3,event='MarketUnavailable')]
        with tempfile.TemporaryDirectory() as d:
            journal=Path(d)/'journal'
            run(journal,events)
            result=analyze(journal,Path(d)/'analysis.sqlite')
        self.assertEqual(result['position_lots'],1)
        self.assertEqual(result['realized_gross_tick_lots'],'0')
        self.assertIsNone(result['unrealized_gross_tick_lots'])
        self.assertIsNone(result['total_gross_tick_lots'])
        self.assertEqual(result['mark_basis'],'unavailable')

    def test_flat_account_without_mark_still_has_known_pnl(self):
        from analyze import analyze
        with tempfile.TemporaryDirectory() as d:
            journal=Path(d)/'journal'
            run(journal,[])
            result=analyze(journal,Path(d)/'analysis.sqlite')
        self.assertEqual(result['unrealized_gross_tick_lots'],'0')
        self.assertEqual(result['total_gross_tick_lots'],'0')
        self.assertEqual(result['mark_basis'],'flat_position')
