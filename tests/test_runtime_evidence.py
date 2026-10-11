"""Runtime evidence must reject unverified builds and incomplete action traces."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from mininautilus.evidence import build_identity, percentile, validate_actions, verified_identity, compatible_reports


class RuntimeEvidenceTests(unittest.TestCase):
    def test_queue_samples_bound_categories_and_retention_without_hiding_bad_clocks(self):
        from mininautilus.evidence import QueueMeasurements
        from mininautilus.offline import Input
        metric=QueueMeasurements(retention=2,max_groups=1)
        item=Input('market','market',{'Quote':{}})
        for n in range(1,5):metric.record(item,10,10+n*1000000)
        metric.record(Input('timer','timer','Tick'),50,49)
        report=metric.report();self.assertEqual(len(report['rows']),2)
        normal=next(row for row in report['rows'] if row['source']=='market')
        unknown=next(row for row in report['rows'] if row['source']=='__other__')
        self.assertEqual(normal['events'],4)
        self.assertEqual(normal['retained_samples'],2)
        self.assertAlmostEqual(normal['p99_ms'],3.99)
        self.assertEqual(unknown['invalid_clock_samples'],1)
        self.assertIsNone(unknown['p99_ms'])

    def test_python_only_dirty_tree_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'python').mkdir()
            file = root / 'python/strategy.py'
            file.write_text('value = 1\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            binary = root / 'binary'
            binary.write_bytes(b'fixture')
            # Ignore generated binary, as a real target/ directory is ignored.
            (root / '.git/info/exclude').write_text('binary\n')
            self.assertFalse(build_identity(root, binary)['dirty'])
            file.write_text('value = 2\n')
            self.assertTrue(build_identity(root, binary)['dirty'])

    def test_exact_percentile_and_nonfinite_rejected(self):
        self.assertEqual(percentile([1, 2, 3], .99), 2.98)
        with self.assertRaises(ValueError):
            percentile([1, float('nan')], .99)

    def test_missing_duplicate_or_undurable_action_is_failure(self):
        effect = {'SendOrder': {'id': 7}}
        row = dict(request_id=2, seq=3, order_id=7, effect='SendOrder', durable_seq=3,
                   stages={'synced': True})
        validate_actions([effect], [row], request_id=2)
        for rows in ([], [row, row], [dict(row, durable_seq=2)], [dict(row, request_id=1)]):
            with self.assertRaises(ValueError):
                validate_actions([effect], rows, request_id=2)
        for invalid in (dict(row,seq=None,durable_seq=None),dict(row,seq=True,durable_seq=True),
                        dict(row,seq=0,durable_seq=0),dict(row,stages={'synced':1})):
            with self.assertRaises(ValueError):
                validate_actions([effect],[invalid],request_id=2)


class BuildReceiptTests(unittest.TestCase):
    def test_binary_tampering_and_profile_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root/'Cargo.toml').write_text('fixture')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            binary = root/'binary'; binary.write_bytes(b'release')
            identity = build_identity(root,binary)
            receipt = root/'receipt.json'
            receipt.write_text(json.dumps(dict(profile='release', binary_sha256=identity['binary_sha256'], rust_inputs=identity['source_hashes'])))
            verified_identity(root,binary,receipt)
            binary.write_bytes(b'tampered')
            with self.assertRaises(ValueError): verified_identity(root,binary,receipt)

    def test_comparison_rejects_dirty_candidates_and_workload_mismatch(self):
        def report():
            return dict(session_exit=0, experiment={'sync':'outbox'}, provenance=dict(dirty=False,
                binary_sha256='x',source_hashes={},build=dict(profile='release',binary_sha256='x',rust_inputs={})))
        rows=[report() for _ in range(10)]
        self.assertTrue(compatible_reports(rows))
        rows[-1]['provenance']['dirty']=True
        self.assertFalse(compatible_reports(rows))
        rows[-1]=report(); rows[-1]['experiment']['sync']='every'
        self.assertFalse(compatible_reports(rows))
        rows[-1]=report(); rows[-1]['provenance']['binary_sha256']='wrong'
        self.assertFalse(compatible_reports(rows))

    def test_comparison_rejects_changed_compiler_flags_python_or_affinity(self):
        import copy
        before=dict(session_exit=0,experiment={'sync':'outbox'},provenance=dict(dirty=False,
            binary_sha256='x',source_hashes={},python='3.12.0',platform='fixture',
            resources=dict(cpus=2,affinity=[0,1],cpu_model='fixture',filesystem_block_bytes=4096),
            build=dict(profile='release',binary_sha256='x',rust_inputs={},rustflags='',features=[])))
        self.assertTrue(compatible_reports([before,copy.deepcopy(before)]))
        for field,value in (('python','3.13.0'),('rustflags','-C debug-assertions=yes'),('affinity',[0])):
            after=copy.deepcopy(before)
            if field=='rustflags':after['provenance']['build'][field]=value
            elif field=='affinity':after['provenance']['resources'][field]=value
            else:after['provenance'][field]=value
            self.assertFalse(compatible_reports([before,after]))

class HistogramTests(unittest.TestCase):
    def test_histogram_is_labelled_estimate_not_exact_percentile(self):
        from mininautilus.evidence import histogram_estimate
        text='mini_request_seconds_bucket{le="0.001"} 90\nmini_request_seconds_bucket{le="0.002"} 100\nmini_request_seconds_bucket{le="+Inf"} 100\n'
        self.assertAlmostEqual(histogram_estimate(text,'mini_request_seconds'),.0019)
        self.assertIsNone(histogram_estimate('', 'missing'))

    def test_rss_guardrail_uses_raw_samples_and_missing_is_unknown(self):
        sys.path.insert(0,str(ROOT/'ops'))
        from runtime_compare import load
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            (path/'report.json').write_text(json.dumps({'metrics':{}}))
            self.assertIsNone(load(path)[0]['metrics']['python_sampled_peak_rss_kib'])
            samples=[{'python':{'status':{'VmRSS':'100 kB'}},'rust':None},
                     {'python':{'status':{'VmRSS':'120 kB'}},'rust':{'status':{'VmRSS':'10 kB'}}}]
            (path/'system.jsonl').write_text('\n'.join(map(json.dumps,samples)))
            metrics=load(path)[0]['metrics']
            self.assertEqual(metrics['python_sampled_peak_rss_kib'],120)
            self.assertEqual(metrics['rust_sampled_peak_rss_kib'],10)

    def test_bootstrap_failure_remains_a_failed_cohort_report(self):
        from unittest.mock import patch
        sys.path.insert(0,str(ROOT/'ops'))
        import runtime_bench
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)/'run'
            with patch.object(sys,'argv',['runtime_bench','--run-dir',str(run)]), \
                 patch.object(runtime_bench,'verified_identity',return_value={'dirty':False,'fixture':True}), \
                 patch.object(runtime_bench,'Engine',side_effect=OSError('fixture startup failure')):
                with self.assertRaises(OSError):runtime_bench.main()
            report=json.loads((run/'report.json').read_text())
            self.assertEqual(report['session_exit'],1)
            self.assertEqual(report['failure'],'OSError')
            self.assertIsNone(report['counts'])
            self.assertIsNone(report['metrics'])
            from runtime_compare import compare,load
            result=compare(load(run),load(run))
            self.assertFalse(result['compatible'])
            self.assertEqual(result['outcomes'][0][0]['session_exit'],1)
