import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
from mininautilus.evidence import committed_rust_inputs, digest, verified_identity


class HistoricalReceiptTests(unittest.TestCase):
    def test_old_engine_requires_explicit_immutable_source_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'repo';root.mkdir();(root/'src').mkdir()
            def git(*args):return subprocess.check_output(['git',*args],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
            git('init');git('config','user.name','Receipt Test');git('config','user.email','test@example.invalid')
            (root/'src/lib.rs').write_text('original');git('add','.');git('commit','-m','baseline');old=git('rev-parse','HEAD')
            binary=Path(d)/'binary';binary.write_bytes(b'exact baseline artifact')
            receipt=dict(source_commit=old,source_dirty=False,profile='release',binary_sha256=digest(binary),rust_inputs=committed_rust_inputs(root,old))
            path=Path(d)/'receipt.json';path.write_text(json.dumps(receipt))
            (root/'src/lib.rs').write_text('candidate');git('add','.');git('commit','-m','candidate')
            with self.assertRaisesRegex(ValueError,'inputs changed'):verified_identity(root,binary,path)
            identity=verified_identity(root,binary,path,allow_historical=True)
            self.assertEqual(identity['historical_engine_source_commit'],old)
            self.assertNotEqual(identity['commit'],old)
            receipt['rust_inputs']['src/lib.rs']='0'*64;path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError,'historical build source receipt mismatch'):
                verified_identity(root,binary,path,allow_historical=True)
