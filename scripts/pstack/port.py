#!/usr/bin/env python3
"""Regenerate the vendored pstack files from the pinned cursor/plugins checkout."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
from urllib.parse import urlsplit

COMMIT = 'd457567332aa6989e753ea289f30ea2bfbc80bc0'
ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(sys.argv[1]).resolve() / 'pstack'
BASE = f'https://github.com/cursor/plugins/blob/{COMMIT}/'
actual = subprocess.check_output(['git', '-C', str(SOURCE.parent), 'rev-parse', 'HEAD'], text=True).strip()
if actual != COMMIT:
    raise SystemExit(f'Expected upstream {COMMIT}, got {actual}')
subprocess.run(['git', '-C', str(SOURCE.parent), 'diff', '--exit-code', 'HEAD', '--', 'pstack/'], check=True)



def destination(path):
    p = path.as_posix()
    if p.startswith('skills/') and not p.startswith('skills/make-bot-ui/'):
        return ROOT / '.agents' / p
    if p.startswith('agents/'):
        return ROOT / '.agents/skills/poteto-mode/references' / p
    if p == 'LICENSE':
        return ROOT / 'docs/pstack/LICENSE'
    if p == 'README.md':
        return ROOT / 'docs/pstack/UPSTREAM-README.md'
    if p.startswith('docs/') and path.suffix == '.md':
        return ROOT / 'docs/pstack/upstream' / p[5:]
    return None


def relative(path, parent):
    return os.path.relpath(path, parent).replace(os.sep, '/')


def rewrite_links(text, source, target):
    def link(match):
        label, url = match.groups()
        parts = urlsplit(url)
        if parts.scheme or url.startswith(('#', '//')):
            return match.group(0)
        absolute = (source.parent / parts.path).resolve()
        try:
            local = absolute.relative_to(SOURCE)
        except ValueError:
            return match.group(0)
        mapped = destination(local)
        result = relative(mapped, target.parent) if mapped else BASE + 'pstack/' + local.as_posix()
        if parts.fragment:
            result += '#' + parts.fragment
        return f']({result})'
    return re.sub(r'(\])\(([^\s)]+)\)', link, text)


records = []
tracked = subprocess.check_output(['git', '-C', str(SOURCE.parent), 'ls-tree', '-r', '--name-only', COMMIT, '--', 'pstack/'], text=True).splitlines()
for source in sorted(SOURCE.parent / name for name in tracked):
    if not source.is_file():
        continue
    path = source.relative_to(SOURCE)
    target = destination(path)
    if target is None:
        continue
    data = source.read_bytes()
    text = data.decode()
    if source.suffix == '.md':
        text = rewrite_links(text, source, target)
        text = text.replace('[PR #123](url)', 'a real PR URL')
        if path.as_posix().startswith(('skills/', 'agents/')):
            text = text.replace('~/.cursor/rules/pstack-models.mdc', '.agents/pstack-models.md')
            text = text.replace('pstack-models.mdc', 'pstack-models.md')
            text = text.replace('~/.cursor/skills/', '.agents/skills/')
            text = text.replace('.cursor/skills/', '.agents/skills/')
            text = re.sub(r'(?:claude-opus-5-5|grok-4\.7)-(?:max|xhigh|high|medium|low)(?:-fast)?', 'inherit-parent', text)
            adapter = relative(ROOT / 'docs/pstack/CODEX-CLOUD.md', target.parent)
            note = f'\nRead the [Codex Cloud adapter]({adapter}) before following this file. Its tool, model, path, autonomy, and fallback rules replace the upstream harness instructions below.\n'
            if text.startswith('---\n'):
                end = text.index('\n---', 4) + 4
                header, body = text[:end], text[end:]
                header = re.sub(r'\n(?:disable-model-invocation|icon|color|paths):[^\n]*', '', header)
                text = header + '\n' + note + body
            else:
                text = note + '\n' + text
        else:
            adapter = relative(ROOT / 'docs/pstack/CODEX-CLOUD.md', target.parent)
            text = f'> Upstream historical documentation. For this repository follow the [Codex Cloud adapter]({adapter}). Cursor and Claude Code install instructions do not apply here.\n\n' + text
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
    records.append({'upstream': 'pstack/' + path.as_posix(), 'path': target.relative_to(ROOT).as_posix(), 'upstream_sha256': hashlib.sha256(data).hexdigest(), 'upstream_git_blob': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()})

mode = ROOT / '.agents/skills/poteto-mode/SKILL.md'
text = mode.read_text()
start = text.index('## Autonomy\n')
end = text.index('## Writing the reply\n')
text = text[:start] + '''## Autonomy

Work within the user's request and the session's permissions. Reversible local work may proceed. External messages require explicit authorization. Creating this skill does not grant permission to merge, deploy, delete data, alter credentials, or create automations. Follow the session's approval rules and existing user authorization.

## Subagents

Use the native Codex collaboration tools only when session instructions permit delegation. `poteto-agent` and `comment-sicko` are prompt roles, not registered agent types. Read `references/agents/poteto-agent.md` or `references/agents/comment-sicko.md` and include its absolute path and the adapter path in the delegate brief. The parent owns the playbook and reviews every delegate's evidence and diff.

Inherit the parent model by default. Never pass Cursor model slugs, `subagent_type`, `readonly`, `environment`, `run_in_background`, or `cloud_base_branch` to a Codex tool. Tool schemas and the adapter define the supported arguments. If delegation is unavailable or prohibited, execute the phases serially and label the missing independent review. Do not claim cross-model agreement from one model or a self-review.

Codex agents may share this filesystem. Allocate an explicit worktree or disjoint writable output per writer. A spawn does not create an isolated VM or worktree. Respect the actual concurrency limit. Use fresh agents for new tasks and provide the consolidated scope and evidence paths.

''' + text[end:]
mode.write_text(text)

agent = ROOT / '.agents/skills/poteto-mode/references/agents/poteto-agent.md'
text = agent.read_text()
text = re.sub(r'Use the absolute path your parent gave you\..*', 'Use the absolute path your parent gave you. Otherwise find `.agents/skills/poteto-mode/SKILL.md` in the repository. If absent, report the missing skill. Read the Codex Cloud adapter before working. Navigate to a leaf `principle-*` skill whenever you apply that principle.', text, flags=re.S)
text = text.replace('\nis_background: true', '')
agent.write_text(text)

setup = ROOT / '.agents/skills/setup-pstack/SKILL.md'
setup.write_text('''---
name: setup-pstack
description: Configure repository-local pstack role preferences for Codex Cloud. Use when asked to set up pstack or change its model preferences.
---

Read the [Codex Cloud adapter](../../../docs/pstack/CODEX-CLOUD.md) first.

# Set up pstack for Codex Cloud

1. Inspect the current session's collaboration tool schema and delegation policy. Do not guess model names or install another provider. If no delegation tool is available, explain that workflows run serially.
2. Read `.agents/pstack-models.md` if present. By default every role inherits the parent model and reasoning effort. No setup is required to use pstack.
3. If the user requests overrides, use only choices explicitly exposed by the current tool and allowed by session instructions. Keep unspecified roles on `inherit-parent`. Do not promise that a file can change the root chat model, reasoning budget, speed, or account entitlements.
4. Write the requested preferences to `.agents/pstack-models.md` as plain Markdown, one role per line. For example `swarm workers: inherit-parent`. This file is read explicitly by pstack, not automatically loaded as a Cursor rule. Read it again when roles are chosen. Never write home-directory rules or secrets.
5. Report the path and any unsupported preference. Before using a saved override in a later session, validate it against that session's tools again. An unavailable override falls back once to inheritance with a limitation note. Never retry guessed model names.
6. If the project lacks a verification harness and it is relevant to the user's goal, mention `../create-verification-skill/SKILL.md` and `../create-formal-verification-skill/SKILL.md`. Generate either only within the requested scope.
''')

help_file = ROOT / '.agents/skills/poteto-help/SKILL.md'
text = help_file.read_text()
start = text.index('## Get set up\n')
end = text.index('## Pick a skill\n')
text = text[:start] + '''## Get set up

The skills are already installed in this repository. Ask Codex to use pstack for the task. Read `../poteto-mode/SKILL.md` and choose its playbook. Optionally read `../setup-pstack/SKILL.md` to configure supported role preferences. No plugin installation, Custom Mode, or `/loop` command is needed or supported here. Repeat the pstack request in a new chat. Follow the adapter for delegation and bounded continuation.

''' + text[end:]
text = re.sub(r'^\| Build a page whose buttons wake.*\n', '', text, flags=re.M)
help_file.write_text(text)

# Remove obsolete discovery and fallback instructions rather than relying on a
# failed Cursor call to discover the host mismatch.
for document in (ROOT / '.agents/skills').rglob('*.md'):
    text = document.read_text()
    text = text.replace('under `.agents/skills/`, `.agents/skills/`, or `~/.cursor/plugins/`', 'under this repository\'s `.agents/skills/`')
    text = re.sub(r"If the Task tool rejects a configured entry, run that reviewer.*?Never treat an alias entry as a rejected slug or apply either fallback to it\.", 'Use only the adapter\'s validated model choices. If unavailable, inherit once and report the missing model diversity.', text)
    if document.parent.name == 'arena' and document.name == 'SKILL.md':
        text = re.sub(r"3\. Pick the runners\..*?(?=\n4\.)", '3. Pick at least two independent candidates. Read the optional `arena runners` role in `.agents/pstack-models.md`. Default both to `inherit-parent`. Use native delegation and model validation from the adapter. Report same-model review honestly. With no delegation, compare serial candidates and mark independent judgment unavailable.', text, flags=re.S)
    if document.parent.name == 'recall' and document.name == 'SKILL.md':
        text = re.sub(r'Transcripts live at .*?Every line is one chat message\.', 'Use authorized native chat tools, supplied exports, or a labeled digest of this chat. There is no assumed local transcript directory in Codex Cloud.', text)
    text = re.sub(r"Don't glob across `~/.cursor/projects/\*/`\. That crosses workspace boundaries and reads private chats from unrelated projects\.", 'Use only authorized history scoped to this repository.', text)
    text = re.sub(r"Do not glob across `~/.cursor/projects/\*/`\. That crosses workspace boundaries and reads private chats from unrelated projects\.", 'Use only authorized history scoped to this repository.', text)
    text = text.replace("Don't glob across `~/.cursor/projects/*/`. That reads unrelated private chats.", 'Use only authorized history scoped to this repository.')
    text = text.replace('Do not glob across `~/.cursor/projects/*/`, that crosses workspace boundaries and reads private chats from unrelated projects', 'Use only authorized history scoped to this repository')
    text = text.replace("the active workspace's `agent-transcripts/` directory (the system prompt names the path)", 'authorized session history or a user-provided export')
    text = text.replace("the active workspace's `agent-transcripts/` directory (the system prompt names this path)", 'authorized session history or a user-provided export')
    text = text.replace('the `agent-transcripts/` directory the system prompt names', 'authorized session history or a user-provided export')
    text = text.replace("The system prompt names the workspace's `agent-transcripts/` directory. Use only that path.", 'Use native tools only when exposed, or a labeled digest of the current chat.')
    if document.parent.name == 'reflect' and document.name == 'SKILL.md':
        start = text.index('```bash\n')
        end = text.index('## 2. Spawn', start)
        text = text[:start] + 'Use an authorized export if available. Otherwise pass a tight session digest, labeled as a digest rather than transcript evidence. Do not claim that the digest proves tool calls or file reads.\n\n' + text[end:]
    text = re.sub(r'If the Task tool rejects a slug, use the default and say so\. If it rejects the default, use the closest valid slug of the same family from its error message\.', 'Validate saved preferences against the native tool schema. Use inheritance once if unavailable, and report the limitation.', text)
    document.write_text(text)

cleanup = ROOT / '.agents/skills/poteto-mode/playbooks/worktree-cleanup.md'
text = cleanup.read_text()
text = re.sub(r'1\. Snapshot and audit\..*?(?=\n2\.)', '1. Snapshot and audit. Record `df -h /` and `git worktree list --porcelain`. Inspect each worktree with `git -C <path> status --short` and check its exact commit, merge ancestry, and GitHub PR state through available tools. The retained upstream `scripts/worktree-audit.sh` is unsupported on this Linux host. Missing transcript or forge evidence means unknown, never safe to delete.', text, flags=re.S)
text = re.sub(r'6\. Simulators and other reclaimers\..*?(?=\n7\.|\n\n|\Z)', '6. Simulator cleanup is unavailable on this Linux host. Do not run macOS commands or clear user caches. Restrict cleanup to explicitly authorized repository worktrees with verified ownership and preserved work.', text, flags=re.S)
cleanup.write_text(text)

for document in list((ROOT / '.agents/skills').rglob('*.md')) + list((ROOT / 'docs/pstack/upstream').rglob('*.md')) + [ROOT / 'docs/pstack/UPSTREAM-README.md']:
    document.write_text('\n'.join(line.rstrip() for line in document.read_text().splitlines()).rstrip() + '\n')

# Run the existing plan-checker behavior tests on Node without installing Bun.
node_test = ROOT / '.agents/skills/poteto-mode/scripts/check-plan.node.test.mjs'
original_test = (ROOT / '.agents/skills/poteto-mode/scripts/check-plan.test.ts').read_text()
node_text = original_test.replace('import { expect, test } from "bun:test";', 'import { test } from "node:test";\nimport assert from "node:assert/strict";')
node_text = node_text.replace('import.meta.dir', 'import.meta.dirname')
node_text = node_text.replace('(example: string, expectedExit: number)', '(example, expectedExit)')
node_text = re.sub(r'expect\(([^\n]+?)\)\.toBe\(([^\n]+?)\);', r'assert.equal(\1, \2);', node_text)
node_text = re.sub(r'expect\(([^\n]+?)\)\.toContain\(([^\n]+?)\);', r'assert.ok(\1.includes(\2));', node_text)
node_text = node_text.replace('assert.equal(result.status, expectedExit);', 'assert.ifError(result.error);\n\t\tassert.equal(result.status, expectedExit);')
node_test.write_text(node_text)

manifest = {'repository': 'https://github.com/cursor/plugins', 'commit': COMMIT, 'version': '0.15.28', 'target_base_commit': '2ae0f642eb1096e9d37f6f815076e0190abd041d', 'excluded': ['pstack/skills/make-bot-ui', 'pstack/automations/benny', 'Cursor plugin manifests and artwork'], 'files': records}
manifest['artifacts'] = [{'path': p.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'mode': '100755' if p.stat().st_mode & 0o111 else '100644'} for p in sorted({ROOT / item['path'] for item in records} | {node_test})]
(ROOT / 'docs/pstack/SOURCE.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Ported {len(records)} source files from {COMMIT}')
