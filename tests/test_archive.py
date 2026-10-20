"""Evidence cleanup must preserve recoverable bytes and safe offline references."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_archive', ROOT/'scripts/check_archive.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'repo'
        self.root.mkdir()
        self.run_git('init', '-q')
        self.run_git('config', 'user.name', 'Archive Test')
        self.run_git('config', 'user.email', 'archive@example.invalid')
        self.name = 'comparisons/results/failed.json'
        original = self.root/self.name
        original.parent.mkdir(parents=True)
        self.data = b'{"status":"FAIL","reason":"original failure retained"}\n'
        original.write_bytes(self.data)
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'original evidence')
        source = self.run_git('rev-parse', 'HEAD').strip()
        self.run_git('tag', 'archive/test', source)
        repository = 'https://github.com/example/archive-test'
        url = f'{repository}/blob/{source}/{self.name}'
        self.manifest = dict(schema=1, repository=repository, archive_commit=source,
            archive_tag='archive/test', source_archive_url=f'{repository}/archive/{source}.tar.gz',
            files=[dict(path=self.name, sha256=hashlib.sha256(self.data).hexdigest(),
                git_blob=self.run_git('rev-parse', f'{source}:{self.name}').strip(),
                size_bytes=len(self.data), source_url=url, summary='Original failed experiment.',
                recorded_verdicts=['FAIL'])])
        original.unlink()
        (self.root/'README.md').write_text(f'[Original failed result]({url})\n')
        (self.root/'docs').mkdir()
        self.save()

    def run_git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, text=True,
                                       stderr=subprocess.PIPE)

    def save(self):
        (self.root/archive.MANIFEST).write_text(json.dumps(self.manifest))

    def test_original_failed_evidence_can_be_restored_byte_for_byte(self):
        self.assertEqual(archive.check(self.root, verify_blobs=True), (1, 1))
        entry = self.manifest['files'][0]
        restored = archive.git(self.root, 'show', f'{self.manifest["archive_commit"]}:{entry["path"]}')
        self.assertEqual(restored, self.data)
        self.assertEqual(entry['recorded_verdicts'], ['FAIL'])

    def test_corrupt_digest_or_retargeted_tag_is_rejected(self):
        self.manifest['files'][0]['sha256'] = '0'*64
        self.save()
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            archive.check(self.root, verify_blobs=True)
        self.manifest['files'][0]['sha256'] = hashlib.sha256(self.data).hexdigest()
        self.save()
        self.run_git('add', '-A')
        self.run_git('commit', '-qm', 'cleanup')
        self.run_git('tag', '-f', 'archive/test', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'tag no longer points'):
            archive.check(self.root, verify_blobs=True)

    def test_floating_link_duplicate_entry_and_unsafe_path_are_rejected(self):
        entry = self.manifest['files'][0]
        original_url = entry['source_url']
        entry['source_url'] = original_url.replace(self.manifest['archive_commit'], 'main')
        self.save()
        with self.assertRaisesRegex(ValueError, 'pin original path'):
            archive.check(self.root)

        entry['source_url'] = original_url
        self.manifest['files'].append(entry.copy())
        self.save()
        with self.assertRaisesRegex(ValueError, 'duplicate archive'):
            archive.check(self.root)
        self.manifest['files'].pop()
        entry['path'] = '../outside.json'
        self.save()
        with self.assertRaisesRegex(ValueError, 'unsafe archive path'):
            archive.check(self.root)

    def test_failure_marker_cannot_be_relabelled_as_pass(self):
        self.manifest['files'][0]['recorded_verdicts'] = ['PASS']
        self.save()
        with self.assertRaisesRegex(ValueError, 'verdict markers differ from original'):
            archive.check(self.root, verify_blobs=True)

    def test_dangling_links_and_accidentally_reintroduced_outputs_are_rejected(self):
        (self.root/'README.md').write_text(f'[Broken old link]({self.name})\n')
        with self.assertRaisesRegex(ValueError, 'broken local Markdown'):
            archive.check(self.root)
        (self.root/self.name).write_bytes(self.data)
        with self.assertRaisesRegex(ValueError, 'returned to the working tree'):
            archive.check(self.root)

    def test_default_check_works_in_shallow_checkout_without_archive_objects(self):
        self.run_git('add', '-A')
        self.run_git('commit', '-qm', 'cleanup')
        shallow = Path(self.temp.name)/'shallow'
        subprocess.run(['git', 'clone', '-q', '--depth', '1', '--no-tags',
                        self.root.as_uri(), str(shallow)], check=True)
        self.assertEqual(archive.check(shallow), (1, 1))
        with self.assertRaisesRegex(ValueError, 'archive objects unavailable'):
            archive.check(shallow, verify_blobs=True)

    def test_repository_inventory_and_local_links_are_valid(self):
        count, markdown = archive.check(ROOT)
        self.assertGreater(count, 0)
        self.assertGreater(markdown, 0)


if __name__ == '__main__':
    unittest.main()
