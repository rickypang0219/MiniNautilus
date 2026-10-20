#!/usr/bin/env python3
"""Validate archived evidence and documentation links without network access.

The default check works in a shallow CI checkout. --verify-blobs additionally
reads every original blob from the pinned Git commit and verifies its identity.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import quote, unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'docs/archive-manifest.json'
LINK = re.compile(r'\[[^\]\n]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
VERDICTS = {'PASS', 'FAIL', 'FAILED', 'INCONCLUSIVE', 'BLOCKED', 'DEFERRED',
            'NOT_MEASURED', 'NOT_RUN', 'NOT_IMPLEMENTED', 'REJECTED'}


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE)


def check(root=ROOT, *, verify_blobs=False):
    root = Path(root).resolve()
    manifest = json.loads((root / MANIFEST).read_text())
    if manifest.get('schema') != 1:
        raise ValueError('unsupported archive schema')
    source = manifest['archive_commit']
    if not re.fullmatch(r'[0-9a-f]{40}', source):
        raise ValueError('archive source must be a full immutable commit SHA')
    repository = manifest['repository']
    if not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+', repository):
        raise ValueError('invalid archive repository')
    tag = manifest['archive_tag']
    if not re.fullmatch(r'archive/[\w./-]+', tag) or '..' in tag:
        raise ValueError('invalid archive tag')
    if manifest['source_archive_url'] != f'{repository}/archive/{source}.tar.gz':
        raise ValueError('source download must use the immutable commit')
    entries = manifest['files']
    if not entries:
        raise ValueError('empty archive inventory')
    paths = set()
    for entry in entries:
        name = entry['path']
        path = PurePosixPath(name)
        if not name or path.is_absolute() or '..' in path.parts or str(path) != name:
            raise ValueError('unsafe archive path')
        if name in paths:
            raise ValueError(f'duplicate archive entry: {name}')
        paths.add(name)
        if (root / name).exists():
            raise ValueError(f'archived output returned to the working tree: {name}')
        if not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
            raise ValueError(f'invalid SHA256: {name}')
        if not re.fullmatch(r'[0-9a-f]{40}', entry['git_blob']):
            raise ValueError(f'invalid Git blob: {name}')
        if type(entry['size_bytes']) is not int or entry['size_bytes'] < 0:
            raise ValueError(f'invalid byte count: {name}')
        if not isinstance(entry['summary'], str) or not entry['summary'].strip():
            raise ValueError(f'missing historical summary: {name}')
        if (not isinstance(entry['recorded_verdicts'], list)
                or any(v not in VERDICTS for v in entry['recorded_verdicts'])):
            raise ValueError(f'invalid recorded verdicts: {name}')
        expected = f'{repository}/blob/{source}/{quote(name, safe="/")}'
        if entry['source_url'] != expected:
            raise ValueError(f'archive link must pin original path and commit: {name}')

    # Only checkout files are needed here; no historical fetch or HTTP request.
    names = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
    markdown_count = 0
    for name in set(names.decode().split('\0')):
        file = root / name
        if not name.endswith('.md') or not file.is_file():
            continue
        markdown_count += 1
        for match in LINK.finditer(file.read_text()):
            target = urlsplit(match.group(1).strip('<>'))
            if target.scheme or target.netloc or not target.path:
                continue
            local = unquote(target.path)
            destination = ((root / local.lstrip('/')) if local.startswith('/')
                           else (file.parent / local)).resolve()
            if not destination.exists():
                raise ValueError(f'broken local Markdown link: {name} -> {local}')

    if verify_blobs:
        try:
            resolved = git(root, 'rev-parse', f'refs/tags/{tag}^{{commit}}').decode().strip()
            if resolved != source:
                raise ValueError('archive tag no longer points to the pinned commit')
            for entry in entries:
                original = git(root, 'show', f'{source}:{entry["path"]}')
                blob = git(root, 'rev-parse', f'{source}:{entry["path"]}').decode().strip()
                if (blob != entry['git_blob'] or len(original) != entry['size_bytes']
                        or hashlib.sha256(original).hexdigest() != entry['sha256']):
                    raise ValueError(f'archived evidence identity mismatch: {entry["path"]}')
                markers = sorted(set(re.findall(r'\b[A-Z][A-Z_]+\b', original.decode())) & VERDICTS)
                if entry['recorded_verdicts'] != markers:
                    raise ValueError(f'recorded verdict markers differ from original: {entry["path"]}')
        except subprocess.CalledProcessError as error:
            raise ValueError(f'archive objects unavailable; fetch tag {tag} before --verify-blobs') from error
    return len(entries), markdown_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-blobs', action='store_true')
    args = parser.parse_args()
    try:
        entries, markdown = check(verify_blobs=args.verify_blobs)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'archive check failed: {error}\n')
    print(f'ARCHIVE OK: {entries} archived files, {markdown} Markdown files; '
          + ('original blobs verified' if args.verify_blobs else 'offline metadata/link check'))


if __name__ == '__main__':
    main()
