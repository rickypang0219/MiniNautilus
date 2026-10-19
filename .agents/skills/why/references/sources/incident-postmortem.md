
Read the [Codex Cloud adapter](../../../../../docs/pstack/CODEX-CLOUD.md) before following this file. Its tool, model, path, autonomy, and fallback rules replace the upstream harness instructions below.

# Incident & Postmortem Context

- **Linear**: look for tickets labeled `incident`, `sev-*`, `postmortem-action-item`, `reliability`
- **Slack**: search `#sev-*` and `#incident-*` channels around the dates the target code was added
- **Git**: commits with messages like "fix for incident", "add defensive check", "revert" followed by "re-apply with..." are strong signals
