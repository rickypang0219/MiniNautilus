"""Fault contracts for market backfill and restart artifacts (no network)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import Mock
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'python'), str(Path(__file__).resolve().parents[1] / 'examples')]
from mininautilus.market import missing_candles
from mininautilus.retry import ReconnectBudget
from mininautilus.sma import Candle, CandleGap, SmaStrategy
from sma_spot import load_env, write_summary, audit


def row(t): return [t, '1', '1', '1', '1', '0', t+999]

class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.venue = Mock(symbol='BTCUSDT', tick=Decimal('.01'))
        self.sleep = Mock()
    def bridge(self):
        return missing_candles(self.venue, Candle(0,999,100), Candle(3000,3999,100), '1s',1000,sleep=self.sleep)
    def test_rest_lag_retries_exact_range(self):
        self.venue.request.side_effect = [[row(1000)], [row(1000),row(2000)]]
        self.assertEqual([b.open_ms for b in self.bridge()],[1000,2000])
        self.sleep.assert_called_once_with(1)
        self.assertEqual(self.venue.request.call_args.args[2],dict(symbol='BTCUSDT',interval='1s',startTime=1000,endTime=2999,limit=2))
    def test_same_count_wrong_times_duplicates_and_future_bars_rejected(self):
        for rows in ([row(1000),row(1000)], [row(2000),row(3000)], [row(2000),row(1000)]):
            self.venue.request.reset_mock(); self.venue.request.return_value=rows
            with self.assertRaises(CandleGap): self.bridge()
            self.assertEqual(self.venue.request.call_count,4)
    def test_no_partial_strategy_mutation_on_missing_history(self):
        strategy=SmaStrategy(1,2,1,'1s',long_only=True);strategy.add(Candle(0,999,100))
        self.venue.request.return_value=[row(1000)]
        with self.assertRaises(CandleGap): self.bridge()
        self.assertEqual(strategy.last,Candle(0,999,100))
    def test_excessive_gap_does_not_issue_request(self):
        with self.assertRaises(CandleGap):
            missing_candles(self.venue,Candle(0,999,1),Candle(1002000,1002999,1),'1s',1000)
        self.venue.request.assert_not_called()
    def test_no_gap_or_old_bar_needs_no_backfill(self):
        self.assertEqual(missing_candles(self.venue,Candle(0,999,1),Candle(1000,1999,1),'1s',1000),[])
        self.venue.request.assert_not_called()

class ArtifactTests(unittest.TestCase):
    def test_resume_summary_preserves_previous_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            write_summary(path,'first',{'submissions':2})
            write_summary(path,'resume',{'submissions':0})
            self.assertEqual(json.loads((path/'summary-first.json').read_text()),{'submissions':2})
            self.assertEqual(json.loads((path/'summary.json').read_text()),{'submissions':0})
    def test_env_rejects_shell_commands_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'env'; path.write_text('export BAD=1\n')
            with self.assertRaises(ValueError):load_env(path)
    def test_audit_detects_position_drift(self):
        with self.assertRaises(RuntimeError):audit(dict(orders={},fills={},position=1),10)

class RetryTests(unittest.TestCase):
    def test_persistent_failure_stops(self):
        r=ReconnectBudget()
        self.assertEqual([r.failed(),r.failed(),r.failed()],[1,2,4])
        with self.assertRaises(RuntimeError):r.failed()
    def test_short_connection_does_not_reset_budget(self):
        r=ReconnectBudget();r.failed();r.connected(10);r.progress(20)
        self.assertEqual(r.failed(),2)
    def test_stable_period_forgives_earlier_failure(self):
        r=ReconnectBudget();r.failed();r.connected(10);r.progress(40)
        self.assertEqual(r.failed(),1)

if __name__ == '__main__':unittest.main()
