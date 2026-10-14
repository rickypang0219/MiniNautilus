import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('native_evidence',Path(__file__).resolve().parents[1]/'ops/native_evidence.py')
evidence=importlib.util.module_from_spec(spec);spec.loader.exec_module(evidence)


class NativeEvidenceTest(unittest.TestCase):
    def fixture(self,root,group,index,p99,valid=True,host='same'):
        path=root/f'{group}-{index}';path.mkdir()
        report=dict(valid=valid,input_sha256='same',actions=[dict(id=1,qty=20)],metrics=dict(full_action=dict(p99_ms=p99)),cpu_ns_per_market=p99*1000,rss_peak_kib=p99*100)
        provenance=dict(host=host,manifest_sha256='same',cpu_affinity=[0,1],identity=dict(dirty=False),binary_sha256='native' if group=='ab-b' else 'baseline')
        (path/'report.json').write_text(json.dumps(report));(path/'provenance.json').write_text(json.dumps(provenance))

    def test_requires_stable_aa_complete_runs_and_nonoverlapping_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for group,value in [('aa0',2),('aa1',2.05),('ab-a',2),('ab-b',1)]:
                for index in range(5):self.fixture(root,group,index,value)
            result=evidence.analyze(root)
            self.assertEqual(result['aa']['verdict'],'PASS')
            self.assertEqual(result['comparison']['full_action_p99_ms']['verdict'],'IMPROVED')
            path=root/'aa1-0'/'report.json';record=json.loads(path.read_text());record['metrics']['full_action']['p99_ms']=4;path.write_text(json.dumps(record))
            result=evidence.analyze(root)
            self.assertEqual(result['aa']['verdict'],'INCONCLUSIVE')
            self.assertEqual(result['comparison']['full_action_p99_ms']['verdict'],'INCONCLUSIVE')

    def test_failures_and_wrong_host_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for group,value in [('aa0',2),('aa1',2),('ab-a',2),('ab-b',1)]:
                for index in range(5):self.fixture(root,group,index,value,host='wrong' if group=='ab-b' and index==0 else 'same')
            result=evidence.analyze(root)
            self.assertEqual(result['aa']['verdict'],'PASS')
            self.assertFalse(result['compatible_complete_runs'])
            self.assertEqual(result['comparison']['full_action_p99_ms']['verdict'],'INCONCLUSIVE')
            path=root/'ab-b-1'/'report.json';record=json.loads(path.read_text());record['valid']=False;path.write_text(json.dumps(record))
            self.assertEqual(evidence.analyze(root)['comparison']['full_action_p99_ms']['verdict'],'INSUFFICIENT')
