# Codex History Recover

Codex History Recover is a local Codex plugin that restores conversation history
visibility after switching API endpoints, API keys, accounts, or
`model_provider` values.

Codex keeps the full local session rollouts under `~/.codex/sessions`, but the
app sidebar is driven by local index files. When those indexes are tied to an
old provider/account state, conversations can look like they disappeared even
though the underlying files are still on disk. This plugin scans the local
rollout files and rebuilds the visible thread index.

## What It Does

- Scans active rollouts in `~/.codex/sessions/**/rollout-*.jsonl`.
- Scans archived rollouts in `~/.codex/archived_sessions/**/rollout-*.jsonl`.
- Rebuilds missing or stale rows in `~/.codex/state_5.sqlite`.
- Updates `~/.codex/session_index.jsonl`.
- Aligns thread index rows to the current top-level `model_provider` in
  `~/.codex/config.toml`.
- Also aligns any existing database rows still tied to an old provider, even if
  their rollout file is not found.
- Creates backups before writing local Codex state.
- Exposes an MCP tool named `sync_codex_history`.
- Runs one sync automatically when the plugin MCP server starts.

## Safety Notes

This plugin is local-only. It does not upload your sessions, API keys, config, or
history anywhere.

It does read local Codex session files and writes local Codex index files. Before
writing, it creates backups under `~/.codex/backups/`. If that directory is not
writable, it falls back to a local `backups/` directory inside the plugin.

Never commit generated backup files. They can contain private history titles,
workspace paths, and message previews. This repository's `.gitignore` excludes
`backups/`, SQLite databases, JSONL files, `.env` files, and Python caches.

## Usage

Preview the changes first:

```bash
python3 scripts/sync_history.py --dry-run
```

Apply the sync:

```bash
python3 scripts/sync_history.py
```

Print a machine-readable summary:

```bash
python3 scripts/sync_history.py --json
```

Important summary fields:

- `provider_counts`: how many threads are indexed under each provider.
- `remaining_provider_mismatches`: should be `0` after provider alignment.
- `active_rollouts` and `archived_rollouts`: how many active and archived
  rollout files were scanned.
- `orphan_provider_aligned`: existing database rows fixed even when no matching
  rollout was scanned.

Use a custom Codex home:

```bash
python3 scripts/sync_history.py --codex-home /path/to/.codex
```

Skip provider alignment and only rebuild missing/stale index rows:

```bash
python3 scripts/sync_history.py --no-align-provider
```

## Plugin Layout

```text
.codex-plugin/plugin.json
.mcp.json
scripts/history_recover_mcp.py
scripts/sync_history.py
skills/codex-history-recover/SKILL.md
```

## Publishing Checklist

Before pushing your own copy, verify the repository contains only source files:

```bash
git ls-tree --name-only -r HEAD
git status --short --ignored
```

Do not publish:

- `backups/`
- `*.sqlite`
- `*.jsonl`
- `.env` or `.env.*`
- `__pycache__/`
- `.DS_Store`
