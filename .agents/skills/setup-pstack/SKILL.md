---
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
