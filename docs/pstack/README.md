# pstack for Codex Cloud

This repository vendors Lauren Tan's pstack 0.15.28 from [cursor/plugins](https://github.com/cursor/plugins/tree/d457567332aa6989e753ea289f30ea2bfbc80bc0/pstack). The source commit is `d457567332aa6989e753ea289f30ea2bfbc80bc0`. The target base is `2ae0f642eb1096e9d37f6f815076e0190abd041d` on `codex/cross-platform-backtest-audit`.

Ask Codex to use pstack for a task. [AGENTS.md](../../AGENTS.md) routes that request to [poteto-mode](../../.agents/skills/poteto-mode/SKILL.md). No plugin install is needed. All 55 applicable skills are under `.agents/skills/`, with their references, playbooks, and scripts. The [Codex Cloud adapter](CODEX-CLOUD.md) defines the supported tools and limitations. The [upstream README](UPSTREAM-README.md) and guide are historical context and do not override the adapter.

The port changes skill discovery paths, replaces default provider models with inheritance, rewrites setup and delegation rules, removes Cursor frontmatter, and supplies native tool mappings. It keeps the upstream workflow text where the adapter can map it. Delegation obeys session policy and does not create isolated machines. Existing GitHub reviews can be triaged, but Cursor Automations, webhook bot UI, Bugbot startup, Cursor transcript layouts, and desktop simulator control are unavailable. The bot UI skill and Benny automation pack are excluded.

The dependency-free plan checker runs on Node. Its existing five behavior tests have a generated Node test-runner adaptation. Bun-dependent orch and watch-pr sources, tests, `bun.lock`, and `package.json` remain available as optional upstream tools. Their bootstrap installs dependencies, so do not run them without Bun and permitted registry access. Their forge operations require authenticated `gh` or Origin. The upstream worktree audit script is retained for source completeness but is not supported on this Linux environment. See the adapter before using any helper.

## Validate

Run these commands from the repository root. Node 22 or later and Python 3.10 or later are sufficient. No package installation is needed.

```bash
python3 scripts/pstack/validate.py
node --test .agents/skills/poteto-mode/scripts/check-plan.node.test.mjs
bash -n .agents/skills/show-me-your-work/scripts/log.sh
bash -n .agents/skills/poteto-mode/scripts/worktree-audit.sh
```

Some execution sandboxes block Node child-process pipes. The test runner then reports an execution error rather than a product failure. Use the session's supported execution permission mechanism when available. Do not count such an error as a passing test.

The validator checks inventory hashes, frontmatter, local Markdown links, explicit prompt references, relative code imports, executable bits, the pstack entry point, and obsolete Cursor paths and model defaults. It does not prove that a language model follows every instruction. Live delegation, cross-model review, Bun tools, and external automations were not exercised for this port.

## Refresh from upstream

Read the new upstream README, skills, agent briefs, automation contracts, and dependency changes before changing the pin. Review `port.py` and the adapter together. Do not automatically add excluded integrations.

For the existing pin:

```bash
git clone https://github.com/cursor/plugins.git /tmp/cursor-plugins
git -C /tmp/cursor-plugins checkout d457567332aa6989e753ea289f30ea2bfbc80bc0
python3 scripts/pstack/port.py /tmp/cursor-plugins
python3 scripts/pstack/validate.py
node --test .agents/skills/poteto-mode/scripts/check-plan.node.test.mjs
```

The generator requires a clean source `pstack/` tree at the pinned commit. It updates only source-managed destinations plus the generated Node test and `SOURCE.json`. It never deletes destination-only files. Review removed upstream files manually when changing versions. Repeating generation at the same pin must leave the same output.

[SOURCE.json](SOURCE.json) maps every vendored source file to its original SHA-256 and records output hashes. The original [MIT LICENSE](LICENSE) applies to the vendored code and text. Cursor plugin manifests, artwork, the Claude Code installation tree, the webhook bot UI skill, and Benny are not installed as executable integrations. Historical guide images and excluded files link to the pinned upstream copy.
