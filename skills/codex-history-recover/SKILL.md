---
name: codex-history-recover
description: Restore or resync local Codex conversation history when switching Codex API request URL, key, user, account, or model_provider makes existing local records disappear from the UI.
---

# Codex History Recover

Use this skill when the user says Codex history, local records, previous conversations, or threads disappeared after changing API URL, key, account, user, or `model_provider`.

## What It Does

- Scans `~/.codex/sessions/**/rollout-*.jsonl`.
- Rebuilds missing rows in `~/.codex/state_5.sqlite` `threads`.
- Updates `~/.codex/session_index.jsonl`.
- Aligns indexed rows to the current top-level `model_provider` in `~/.codex/config.toml` so they appear under the current user/provider.
- Creates backups under `~/.codex/backups/` before writing. If that directory is not writable, it falls back to the plugin's local `backups/` directory.

## Commands

Preview:

```bash
python3 /path/to/codex-history-recover/scripts/sync_history.py --dry-run
```

Apply:

```bash
python3 /path/to/codex-history-recover/scripts/sync_history.py
```

The plugin also exposes an MCP tool named `sync_codex_history` and runs the same sync once when the MCP server starts.
