
Read the [Codex Cloud adapter](../../../../docs/pstack/CODEX-CLOUD.md) before following this file. Its tool, model, path, autonomy, and fallback rules replace the upstream harness instructions below.

### Worktree and simulator cleanup

1. Snapshot and audit. Record `df -h /` and `git worktree list --porcelain`. Inspect each worktree with `git -C <path> status --short` and check its exact commit, merge ancestry, and GitHub PR state through available tools. The retained upstream `scripts/worktree-audit.sh` is unsupported on this Linux host. Missing transcript or forge evidence means unknown, never safe to delete.
2. The bucket is advice, not permission. The pinned and active chats are the real artifact (principle-prove-it-works). Get that set from the user or sidebar and cross-check every candidate. The lever has marked `safe` a worktree the user had pinned, so the pinned set wins.
3. Verify usage before deleting. For every `verify-recent-chat` row, or anything you doubt, fan subagents out to read the transcripts and report whether the chat is pinned or ongoing and which worktrees it touches (principle-guard-the-context-window, transcripts are bulk). A pinned chat spawns arena and repro trees into sibling worktrees via background subagents, and those are in use even when their names never hit the sidebar. Pick each subagent's reasoning level and fast or standard speed for the job, per the Subagents section.
4. Pause on irreversible loss. `wip:N` is N tracked uncommitted edits. Show the diff and get a decision first, since removing a clean worktree is recoverable from its branch but uncommitted work is gone. `scratch:N` is untracked throwaway, safe to drop, but name the files. Per Autonomy, clean and merged and not-in-use proceeds. `wip` and in-use pause.
5. Prune the confirmed set. Per path, `git worktree remove --force <path>`. If the dir survives on ignored build artifacts, `rm -rf` it, then `git worktree prune`. Branch refs survive, so no commits are lost. Confirm with `df -h /` and re-list.
6. Simulator cleanup is unavailable on this Linux host. Do not run macOS commands or clear user caches. Restrict cleanup to explicitly authorized repository worktrees with verified ownership and preserved work.

This is the one playbook that deletes user state with no code review to catch a slip, so the gates above are the review.

**Reply:** `df -h /` before and after with space reclaimed, the worktrees pruned, and a one-line reason for each held back (in-use by which chat, or uncommitted work).
