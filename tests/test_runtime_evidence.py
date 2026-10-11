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
