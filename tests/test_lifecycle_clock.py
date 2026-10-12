from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
from mininautilus.lifecycle import AdmissionClock


class AdmissionClockTests(unittest.TestCase):
    def test_downtime_is_included_and_shutdown_stops_denominator(self):
        now=[0];events=[]
        clock=AdmissionClock(lambda kind,**r:events.append(r),clock=lambda:now[0])
        clock.update(False,'startup')
        self.assertIsNone(clock.report()['trade_enabled_ratio'])
        now[0]=10;clock.update(True,'fresh_quote')
        now[0]=20;clock.update(False,'rotation_pending')
        now[0]=25;clock.update(False,'reconcile')
        now[0]=30;clock.update(True,'fresh_quote')
        now[0]=50;clock.finish()
        now[0]=100;clock.update(True,'ignored_after_shutdown')
        self.assertEqual(clock.report()['enabled_ns'],30)
        self.assertEqual(clock.report()['denominator_ns'],40)
        self.assertEqual(clock.report()['trade_enabled_ratio'],.75)
        self.assertEqual(len(events),3)
