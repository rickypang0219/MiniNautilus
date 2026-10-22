#!/usr/bin/env python3
"""Check the vendored pstack inventory, skill metadata, and local references."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
manifest = json.loads((ROOT / 'docs/pstack/SOURCE.json').read_text())
errors = []


def require(condition, message):
    if not condition:
        errors.append(message)


skills = sorted((ROOT / '.agents/skills').glob('*/SKILL.md'))
require(len(skills) == 55, f'Expected 55 skills, found {len(skills)}')
for path in skills:
    text = path.read_text()
    require(text.startswith('---\n'), f'{path}: missing frontmatter')
    frontmatter = text.split('---', 2)[1]
    name = re.search(r'^name: (.+)$', frontmatter, re.M)
    require(name is not None and name[1] == path.parent.name, f'{path}: invalid skill name')
    require(bool(re.search(r'^description: \S', frontmatter, re.M)), f'{path}: missing description')
    require('Codex Cloud adapter' in text, f'{path}: missing adapter entry')
    require(not re.search(r'^(icon|color|paths|is_background|disable-model-invocation):', frontmatter, re.M), f'{path}: Cursor metadata remains')

for item in manifest['files']:
    require((ROOT / item['path']).is_file(), f"Missing source file {item['path']}")
    require(bool(re.fullmatch('[0-9a-f]{64}', item['upstream_sha256'])), f"Invalid source hash {item['path']}")

# A release inventory covers generated files as well as adapted upstream files.
for item in manifest.get('artifacts', []):
    path = ROOT / item['path']
    require(path.is_file(), f'Missing artifact {path}')
    if path.is_file():
        require(hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256'], f'Artifact drift {path}')
        actual_mode = '100755' if path.stat().st_mode & 0o111 else '100644'
        require(actual_mode == item['mode'], f'Artifact mode drift {path}')

markdown = sorted((ROOT / '.agents/skills').rglob('*.md')) + sorted((ROOT / 'docs/pstack').rglob('*.md'))
for path in markdown:
    text = path.read_text()
    for url in re.findall(r'\]\(([^\s)]+)\)', text):
        parts = urlsplit(url.strip('<>'))
        if parts.scheme or url.startswith(('#', '//')):
            continue
        target = (path.parent / unquote(parts.path)).resolve()
        require(target.is_relative_to(ROOT), f'{path}: reference escapes repository {url}')
        require(target.exists(), f'{path.relative_to(ROOT)}: broken link {url}')
    if path.is_relative_to(ROOT / '.agents/skills'):
        require('Codex Cloud adapter' in text, f'{path}: missing adapter on agent-facing document')
        require(not re.search(r'~?/\.cursor/|(?:claude-opus|grok-4\.7|composer)-', text), f'{path}: obsolete Cursor path or model')
        # Backtick paths in prompts may be relative to the owning skill, rather
        # than the nested playbook or reference containing the prompt.
        owner = ROOT / '.agents/skills' / path.relative_to(ROOT / '.agents/skills').parts[0]
        for value in re.findall(r'`((?:references|scripts|playbooks)/[^`\n]+)`', text):
            if any(char in value for char in '<>* []|') or value.endswith('/'):
                continue
            require((path.parent / value).exists() or (owner / value).exists(), f'{path}: broken prompt path {value}')

for path in (ROOT / '.agents/skills').rglob('*'):
    if path.suffix in {'.ts', '.mjs'}:
        for target in re.findall(r'(?:from\s+|import\s*\()\s*["\'](\.[^"\']+)["\']', path.read_text()):
            require((path.parent / target).is_file(), f'{path}: missing module {target}')
    if path.suffix == '.sh' or path.name == 'watch-pr':
        require(os.access(path, os.X_OK), f'{path}: missing executable bit')

require('.agents/skills/poteto-mode/SKILL.md' in (ROOT / 'AGENTS.md').read_text(), 'AGENTS.md: missing pstack entry point')
require(not (ROOT / '.agents/skills/make-bot-ui').exists(), 'Unsupported bot UI must not register as a skill')
require((ROOT / 'docs/pstack/LICENSE').read_text().startswith('MIT License'), 'Missing MIT license')
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
print(f'PASS: {len(skills)} skills, {len(markdown)} Markdown documents, {len(manifest["files"])} source records; local links, prompt paths, imports, metadata, and executable modes checked.')
