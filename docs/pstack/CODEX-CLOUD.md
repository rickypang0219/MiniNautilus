# pstack on Codex Cloud

Read this adapter before any vendored pstack skill, playbook, or delegate prompt. It replaces upstream harness instructions in those files. System instructions, developer instructions, repository instructions, and the user's authorization still take precedence. A skill grants no new permissions.

The entry point is [poteto-mode](../../.agents/skills/poteto-mode/SKILL.md). Resolve another pstack skill by reading `.agents/skills/<name>/SKILL.md`, then follow it. Slash names in the upstream text are skill names, not commands to execute. Resolve relative references from the file that contains them. Give delegates absolute paths to the skill, this adapter, and their grounding artifacts.

## Tools and execution

| Upstream vocabulary | Codex Cloud behavior |
| --- | --- |
| `Read`, `Grep`, `Glob`, shell | Use the session's file and execution tools. Prefer `rg` and `rg --files`. |
| Todo list | Use `update_plan` when available. Otherwise maintain a concise checklist. Record skipped steps and the reason. |
| `AskQuestion`, `AskUserQuestion` | Use the session's supported question tool and its actual schema, or ask in chat when information is required. Do not require Cursor-specific options or wait for confirmation already given. |
| `Task`, `Agent` | Use native collaboration tools, such as `collaboration.spawn_agent`, only if available and permitted by session instructions. Otherwise execute phases serially and state the loss of independent coverage. |
| `subagent_type: poteto-agent` | A prompt role. Read [poteto-agent](../../.agents/skills/poteto-mode/references/agents/poteto-agent.md) and put its path, the mode path, and adapter path in the native delegate brief. |
| `comment-sicko`, `generalPurpose`, `readonly` | Prompt roles and scopes. For Comment Sicko read [its brief](../../.agents/skills/poteto-mode/references/agents/comment-sicko.md). Request read-only behavior in the prompt and inspect the resulting diff. This is not tool-enforced isolation. |
| `environment: cloud`, `isolation: worktree`, `cloud_base_branch` | No automatic VM or worktree. Create an explicit git worktree from the intended commit for each writer, or allocate disjoint outputs. Verify the starting SHA. Agents in this environment can share files. |
| `run_in_background`, resume, messaging | Use only native tool arguments. Spawns are asynchronous. Wait for completion through native coordination, respect actual concurrency limits, and collect results before synthesis. Do not pass Cursor fields. |
| `gh`, Origin, built-in PR tools | Prefer the available GitHub connector when CLI auth, network, or binaries are absent. Use CLI only when installed and authorized. Connector mutations must preserve the intended parent and base. Attach created PRs to the chat when the app provides an attachment tool. |
| MCP discovery, `mcps/` | Inspect the current callable tools and their descriptions. Do not assume a Cursor `mcps/` filesystem directory. Unavailable sources are evidence gaps. |

## Models and role preferences

Every role defaults to `inherit-parent`, including code, judgment, exploration, synthesis, verification, and panels. Read `.agents/pstack-models.md` explicitly if it exists. It is an optional repository file, not an automatically applied rule. [setup-pstack](../../.agents/skills/setup-pstack/SKILL.md) describes it.

An override is usable only when the current tool schema exposes the model and session instructions permit the override. Never pass Claude, Grok, Composer, or Cursor slugs. Do not translate names into guessed Codex models. Do not adjust model families, fast-mode suffixes, or reasoning budgets automatically. If an override is unavailable, fall back once to inheritance and report the limitation. This replaces all upstream family fallback and retry rules.

A panel can use separate same-model runs when delegation is available. Describe it as same-model review. It does not establish cross-model agreement. If no independent reviewer can run, mark independent review unavailable. Do not silently convert an independent-verification merge gate into self-approval. Ten verification lanes are ten scenarios, not ten simultaneous agents or ten VMs. Queue them within the actual limit.

## Paths, history, and authoring

Skills and generated skills belong in `.agents/skills/<name>/`. Never install into `.cursor/`, scan a plugin cache, select the highest cached plugin version, or write home-directory rules. Resolve this repository's copy first. Cursor metadata such as `icon`, `color`, `paths`, `is_background`, and `disable-model-invocation` is not used to activate these skills. Activation is the user's pstack request and the repository's `AGENTS.md`.

Cursor transcript paths and JSONL layouts in upstream recall, reflect, automate-me, eval, and audit instructions are unsupported here. Use the current chat context, user-provided exports, or authorized native chat tools if exposed. Restrict searches to this repository and the requested chats. If transcript access is unavailable, use a clearly labeled session digest. Do not search private home directories or invent transcript citations. A digest cannot prove which files a worker opened. Mark transcript-dependent eval or audit checks unverified.

`create-skill` is not a Cursor built-in here. Author the skill directly with a `SKILL.md` containing `name` and `description` frontmatter, concise instructions, and real relative references. Follow any available Codex skill-authoring guidance. Validate paths and exercise the resulting workflow on a suitable task. Preserve the user's requested scope.

`deslop` is not bundled. Review the actual diff for unnecessary complexity and dead code, use the repository's lint and format commands, and apply the bundled unslop and technical-writing skills to prose. Report that this is a local review, not execution of Cursor's deslop skill.

`control-ui` and `control-cli` from cursor-team-kit are not installed. Use an existing project harness or available browser, PTY, HTTP, or CLI execution tool. Verify its capabilities before promising screenshots, recordings, or UI control. A compile or unit test does not substitute for a missing live proof. Record a blocked live-verification gate when the necessary surface cannot run.

## Continuation and external actions

Cursor Custom Modes, `/loop`, routine wakeups, durable cloud-agent ownership, and Cursor dashboard status are unavailable. Work through the current session with bounded polling. Do not start persistent jobs or create schedules merely because a playbook mentions them. If a session must stop, save a concise handoff with exact commits, remaining gates, and evidence paths. Do not promise overnight completion after the session ends. Native chat or automation tools are used only when available and explicitly requested.

PR creation is not merge authorization. A request for a review PR stops at the review PR. Preserve existing work; never apply upstream suggestions to reset a dirty checkout or delete worktrees without checking ownership and authorization. Do not post to team chat, create tracker tickets, or send messages based only on a skill's autonomy wording. Carry existing user authorization forward, and follow actual session approval requirements.

## Unsupported and conditional features

| Feature | Status and alternative |
| --- | --- |
| Benny Slack triage and repro automations | Not installed. Requires Cursor Automations, Slack routing, secret handoff, and app-control integration. No equivalent backend is configured here. |
| make-bot-ui, Grok Bot webhook routines, `update_state`, `SendToUser` | Skill excluded. Cursor webhook and secret-request contracts have no direct Codex Cloud equivalent. Do not invent URLs or read secrets. |
| Starting or configuring Bugbot and agentic security review | Not provided by this port. Existing GitHub review threads and check results can be read and triaged through GitHub. Do not start or wait for a check that does not exist. |
| Cursor cloud VMs, cross-family models, native desktop and iOS simulator control | Unsupported as automatic pstack features. Use available tools and explicit worktrees. Record unavailable platforms and model diversity. |
| Bun orch and watch-pr CLIs | Sources, tests, lockfile, and dependencies retained. Require Bun, registry access, and an authenticated `gh` or Origin CLI for forge operations. Current environment lacks Bun and Origin. `gh` is installed, but its authenticated forge access must be checked before use. Use native coordination and GitHub connector operations. These scripts are not automatically executed. |
| Cursor worktree audit script | Retained as upstream source only. Its transcript search and BSD `stat` and `date` commands are not supported on this Linux environment. Do not execute it here. Use `git worktree list --porcelain`, `git status`, and GitHub state, and treat missing history as unknown. Never infer safe deletion from missing evidence. |
| `check-plan.mjs` and decision log helper | Run with Node and Bash respectively. No registry dependency. Commands are below. |

For an unsupported step, record one line naming the step, the missing capability, and the substitute or remaining gate. Continue independent work. An unverified prerequisite never counts as a pass.

## Local checks

Run from the repository root.

```bash
python3 scripts/pstack/validate.py
node --test .agents/skills/poteto-mode/scripts/check-plan.node.test.mjs
node .agents/skills/poteto-mode/scripts/check-plan.mjs path/to/plan.md
bash .agents/skills/show-me-your-work/scripts/log.sh /tmp/pstack-decisions.tsv phase decision reason evidence result
```

The plan checker validates the upstream plan format, including its ten scenario lanes and textual `/loop 1h` marker. That marker is template text, not permission to run a Cursor loop. It does not prove the plan's live or perf claims.

[Source record](SOURCE.json), [MIT license](LICENSE), and [port maintenance instructions](README.md) accompany the skills.
