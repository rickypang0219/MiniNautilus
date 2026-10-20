import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('lc', Path(__file__).resolve().parents[1]/'ops/lifecycle_evidence.py')
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)


class LifecycleEvidenceTests(unittest.TestCase):
    def test_nearest_rank_and_missing_values(self):
        self.assertEqual(lc.percentile(list(range(1000))), 989)
        self.assertIsNone(lc.percentile([]))
        verdict = lc.ac('LC1-2', 'rss', None, 10, 'bytes', [])
        self.assertEqual(verdict['status'], 'NOT_MEASURED')
        self.assertEqual(lc.ac('x', 'rss', 11, 10, 'bytes', [])['status'], 'FAIL')

    def fixture(self, directory):
        d = Path(directory)
        lc.dump(d/'manifest.json', dict(run_id='test', duration_seconds=1, sample_interval_seconds=.5,
                                       start_monotonic_ns=0, end_monotonic_ns=10**9, exit_code=0))
        p = dict(pid=1, start_ticks=1, cpu_seconds=.1, hwm_bytes=1024)
        rows = [dict(monotonic_ns=t, processes=[p], rss_bytes=1024,
                     cgroup=dict(peak_bytes=None), host=dict(mem_available_bytes=2**30, steal_pct=0))
                for t in (1, 500000001)]
        (d/'resources.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        files = [dict(path=f.name, sha256=lc.sha(f)) for f in d.iterdir()]
        lc.dump(d/'artifact-index.json',dict(files=files))
        return d

    def test_raw_recompute_rejects_tampering_and_reports_missing_cgroup(self):
        with tempfile.TemporaryDirectory() as directory:
            d = self.fixture(directory)
            report = lc.analyse(d)
            check = next(c for c in report['checks'] if c['metric'] == 'cgroup_memory_peak')
            self.assertEqual(check['status'], 'NOT_MEASURED')
            (d/'resources.jsonl').write_text('{}\n')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                lc.analyse(d)

    def test_duplicate_timestamps_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            d = self.fixture(directory)
            path = d/'resources.jsonl'
            row = path.read_text().splitlines()[0]
            path.write_text(row+'\n'+row+'\n')
            index = json.loads((d/'artifact-index.json').read_text())
            for f in index['files']:
                if f['path'] == path.name:
                    f['sha256'] = lc.sha(path)
            (d/'artifact-index.json').write_text(json.dumps(index))
            with self.assertRaisesRegex(ValueError, 'unordered'):
                lc.analyse(d)

    def test_index_cannot_read_outside_the_run_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            d = self.fixture(directory)
            index = json.loads((d/'artifact-index.json').read_text())
            index['files'].append(dict(path='../outside',sha256='ignored'))
            (d/'artifact-index.json').write_text(json.dumps(index))
            with self.assertRaisesRegex(ValueError,'unsafe'):
                lc.analyse(d)
