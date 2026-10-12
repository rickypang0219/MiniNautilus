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


class SidecarPublicationTests(unittest.TestCase):
    def test_every_boundary_three_times_is_recoverable_or_fail_closed(self):
        import tempfile
        import json
        from mininautilus.lifecycle import publish_sidecar
        record=dict(successor='events-1',position=7,venue=dict(baseline_base='1.0'))
        for boundary in ('sidecar_before','sidecar_written','sidecar_fsync_after','sidecar_dirsync_after'):
            for repetition in range(3):
                with tempfile.TemporaryDirectory() as directory:
                    path=Path(directory)/'rotation-events-1.json'
                    def fault(stage):
                        if stage==boundary:raise OSError('simulated publication failure')
                    with self.assertRaises(OSError):publish_sidecar(path,record,fault)
                    publish_sidecar(path,record)
                    self.assertEqual(json.loads(path.read_text()),record)
                    original=path.read_bytes()
                    publish_sidecar(path,record)
                    self.assertEqual(path.read_bytes(),original)
                    changed=dict(record,position=8)
                    with self.assertRaisesRegex(RuntimeError,'conflicting'):publish_sidecar(path,changed)
                    self.assertEqual(path.read_bytes(),original)

    def test_partial_sidecar_is_not_overwritten(self):
        import tempfile
        from mininautilus.lifecycle import publish_sidecar
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'rotation.json'
            path.write_bytes(b'{"partial":')
            with self.assertRaisesRegex(RuntimeError,'partial'):publish_sidecar(path,dict(position=1))
            self.assertEqual(path.read_bytes(),b'{"partial":')


class HistoryCountTests(unittest.TestCase):
    def test_current_and_retired_counts_are_not_confused(self):
        from mininautilus.lifecycle import history_counts
        state=dict(orders={'1':{},'2':{}},fills={'a':{}},cold=dict(orders=100,fills=90),id_floor=100)
        result=history_counts(state)
        self.assertEqual(result['current_segment_orders'],2)
        self.assertEqual(result['total_session_orders'],102)
        self.assertEqual(result['total_session_unique_fills'],91)
        state['cold']=None
        self.assertIsNone(history_counts(state)['total_session_orders'])
        self.assertFalse(history_counts(state)['history_counts_complete'])
