"""Short version of the exact long-soak driver, included in verify/CI."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class RuntimeSoakTests(unittest.TestCase):
    def test_all_faults_flat_shutdown_and_kill_latch(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)/'run'
            process=subprocess.run([sys.executable,str(ROOT/'ops/runtime_soak.py'),
                '--run-dir',str(run),'--minutes','0.3','--rate','20','--fault-interval','2','--allow-dirty'],
                cwd=ROOT,text=True,capture_output=True,timeout=45)
            self.assertEqual(process.returncode,0,process.stderr[-2000:]+'\n'+process.stdout[-2000:])
            report=json.loads((run/'report.json').read_text())
            self.assertEqual(report['unexpected'],[])
            self.assertGreaterEqual(len(report['faults']),6)
            self.assertTrue(all(row['recovered'] for row in report['faults']))
            self.assertEqual(report['final']['position'],0)
            self.assertTrue(report['final']['killed'])
            self.assertEqual(report['final']['fills'],report['fake_venue']['fills'])
            self.assertLessEqual(report['window_length'],8)
            for session in report['sessions']:
                counts=session['counts']
                self.assertEqual(counts['offered'],counts['admitted']+counts['rejected'])
                self.assertEqual(counts['admitted'],counts['completed']+counts['pending'])
                self.assertLessEqual(session['input_high_water'],128)
                self.assertLessEqual(session['effect_high_water'],32)
