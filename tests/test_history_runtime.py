"""Exercise the exact large-history driver at small scale in the shared CI gate."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class HistoryRuntimeTests(unittest.TestCase):
    def test_retained_history_late_duplicate_and_lost_ack_keep_exact_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)/'run'
            subprocess.run([sys.executable,str(ROOT/'ops/history_runtime_probe.py'),'--allow-dirty',
                            '--history','0','20','--run-dir',str(run)],check=True,capture_output=True,timeout=30)
            report=json.loads((run/'report.json').read_text())
            self.assertEqual(report['session_exit'],0)
            for row in report['rows']:
                self.assertEqual(row['final']['position'],1)
                self.assertEqual(row['final']['fills'],row['history']+3)
                self.assertEqual(row['final']['health'],'Healthy')
                self.assertFalse(row['final']['uncertain'])
                self.assertEqual(row['venue_calls'],3,'each of three intents is sent once')
                self.assertTrue(row['late_duplicate_dedup'])
                self.assertEqual(row['blind_retries'],0)
                for counts in (row['healthy_counts'],row['uncertain_counts']):
                    self.assertEqual(counts['admitted'],counts['completed'])
                    self.assertEqual(counts['pending'],0)
                traces=run/str(row['history'])/'orders.jsonl'
                for line in traces.read_text().splitlines():
                    action=json.loads(line)
                    self.assertEqual(action['durable_seq'],action['seq'])
                    self.assertTrue(action['stages']['synced'])
