import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'ops'))
spec = importlib.util.spec_from_file_location('fast_report', ROOT/'ops/lifecycle_fast_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class FastReportTests(unittest.TestCase):
    def fixture(self, directory):
        d=Path(directory);collector=d/'collector';collector.mkdir();session=d/'session';session.mkdir()
        manifest=dict(run_id='fixture',duration_seconds=2,sample_interval_seconds=1,
                      start_monotonic_ns=0,end_monotonic_ns=2*10**9,exit_code=0)
        (collector/'manifest.json').write_text(json.dumps(manifest))
        rows=[dict(monotonic_ns=n*10**9,processes=[],rss_bytes=100,
                   cgroup=dict(peak_bytes=100),host=dict(mem_available_bytes=2**30,
                   pswpin=10,pswpout=20,oom_kill=0,cpu_total_ticks=n*100,cpu_steal_ticks=0))
              for n in (0,1,2)]
        (collector/'resources.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        (collector/'stdout.log').write_text('{}')
        (collector/'artifact-index.json').write_text(json.dumps(dict(files=[
            dict(path=p.name,sha256=report.sha(p)) for p in collector.iterdir()])))
        return collector,session

    def test_durations_recomputed_and_duplicate_events_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            collector,session=self.fixture(d)
            rows=[dict(kind='internal',order_id=n,received_ns=0,ready_ns=6*10**6,
                       duration_ns=6*10**6) for n in range(1000)]
            path=session/'durations.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            (session/'summary.json').write_text(json.dumps(dict(start_perf_counter_ns=0,passed=True)))
            result=report.report(collector,session)
            c=next(c for c in result['checks'] if c['metric']=='internal_p99')
            self.assertEqual((c['observed'],c['status']),(6,'FAIL'))
            with path.open('a') as f:f.write(json.dumps(rows[0])+'\n')
            with self.assertRaisesRegex(ValueError,'duplicate latency'):
                report.report(collector,session)

    def test_endpoint_mismatch_and_nonfinite_data_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            collector,session=self.fixture(d)
            path=session/'durations.jsonl'
            path.write_text(json.dumps(dict(kind='internal',order_id=1,received_ns=0,
                                          ready_ns=2,duration_ns=1))+'\n')
            with self.assertRaisesRegex(ValueError,'endpoint mismatch'):
                report.report(collector,session)
            path.write_text('{"duration_ns": NaN}\n')
            with self.assertRaisesRegex(ValueError,'nonfinite'):
                report.lines(path)

    def test_historical_swap_totals_are_not_current_swap_io(self):
        with tempfile.TemporaryDirectory() as d:
            collector,_=self.fixture(d)
            result=report.report(collector)
            swap=next(c for c in result['checks'] if c['metric']=='pswpout_delta')
            self.assertEqual((swap['observed'],swap['status']),(0,'PASS'))

    def test_storage_denominator_and_cleanup_writes_are_not_omitted(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)
            raw=dict(schema='lc_pages_component_probe_v2',history=1000,cycles=20,
                     rotation_durations_ms=[1]*20,new_payload_bytes=196000,
                     cold_logical_write_bytes=35514432,post_measure_cleanup_write_bytes=1,
                     cold_steady_allocated_bytes=1000,cold_peak_allocated_bytes=1000,
                     path_first_lookup_ms=1,warm_lookup_ms=[1]*1000)
            (path/'stdout.log').write_text(json.dumps(raw))
            result=report.native_checks(path)
            c=next(c for c in result if c['metric']=='all_maintenance_logical_writes')
            self.assertEqual(c['status'],'FAIL')
            raw['new_payload_bytes']=0
            (path/'stdout.log').write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError,'payload denominator mismatch'):
                report.native_checks(path)
