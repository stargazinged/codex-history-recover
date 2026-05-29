#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SANDBOX_POLICY = {
    "type": "workspace-write",
    "network_access": False,
    "exclude_tmpdir_env_var": False,
    "exclude_slash_tmp": False,
}

THREAD_COLUMNS = [
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "source",
    "model_provider",
    "cwd",
    "title",
    "sandbox_policy",
    "approval_mode",
    "tokens_used",
    "has_user_event",
    "archived",
    "archived_at",
    "git_sha",
    "git_branch",
    "git_origin_url",
    "cli_version",
    "first_user_message",
    "agent_nickname",
    "agent_role",
    "memory_mode",
    "model",
    "reasoning_effort",
    "agent_path",
    "created_at_ms",
    "updated_at_ms",
    "thread_source",
    "preview",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild Codex local history indexes from ~/.codex/sessions."
    )
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--no-align-provider",
        action="store_true",
        help="Do not rewrite thread rows to the current model_provider.",
    )
    parser.add_argument(
        "--no-align-rollouts",
        action="store_true",
        help="Do not rewrite rollout session_meta model_provider values.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report changes without writing the sqlite database or index.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON summary.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only a compact summary.",
    )
    return parser.parse_args()


def parse_timestamp(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def format_timestamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_config(codex_home: Path) -> dict[str, str]:
    config_path = codex_home / "config.toml"
    values: dict[str, str] = {}
    if not config_path.exists():
        return values

    wanted = {"model_provider", "model", "model_reasoning_effort"}
    for raw_line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in wanted:
            continue
        value = raw_value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values[key] = value
    return values


def read_title_index(codex_home: Path) -> dict[str, str]:
    index_path = codex_home / "session_index.jsonl"
    titles: dict[str, str] = {}
    if not index_path.exists():
        return titles
    with index_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = item.get("id")
            title = item.get("thread_name")
            if isinstance(thread_id, str) and isinstance(title, str) and title.strip():
                titles[thread_id] = title.strip()
    return titles


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(part for part in parts if part).strip()


def is_real_user_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    skipped_prefixes = (
        "<environment_context>",
        "<permissions instructions>",
        "<app-context>",
        "<collaboration_mode>",
        "<skills_instructions>",
        "<plugins_instructions>",
    )
    return not stripped.startswith(skipped_prefixes)


def title_from_text(text: str, fallback: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        clean = fallback
    return clean[:120]


def rollout_id_from_path(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
    )
    return match.group(1) if match else None


def scan_rollout(
    path: Path,
    archived: bool,
    title_index: dict[str, str],
    config: dict[str, str],
    align_provider: bool,
) -> dict[str, Any] | None:
    meta: dict[str, Any] = {}
    turn_context: dict[str, Any] = {}
    first_user_message = ""
    max_timestamp_ms = 0
    tokens_used = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp_ms = parse_timestamp(item.get("timestamp"))
            if timestamp_ms:
                max_timestamp_ms = max(max_timestamp_ms, timestamp_ms)

            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue

            item_type = item.get("type")
            if item_type == "session_meta":
                meta.update(payload)
                meta_timestamp_ms = parse_timestamp(payload.get("timestamp"))
                if meta_timestamp_ms:
                    max_timestamp_ms = max(max_timestamp_ms, meta_timestamp_ms)
                continue

            if item_type == "turn_context":
                turn_context.update(payload)
                continue

            if not first_user_message:
                text = ""
                if item_type == "response_item" and payload.get("role") == "user":
                    text = extract_content_text(payload.get("content"))
                elif item_type == "event_msg" and payload.get("type") == "user_message":
                    text = text_value(payload.get("message"))
                if is_real_user_text(text):
                    first_user_message = text.strip()

            if item_type == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    total = info.get("total_token_usage")
                    if isinstance(total, dict) and isinstance(total.get("total_tokens"), int):
                        tokens_used = max(tokens_used, total["total_tokens"])

    thread_id = text_value(meta.get("id")) or rollout_id_from_path(path)
    if not thread_id:
        return None

    stat = path.stat()
    fallback_created_ms = int(stat.st_ctime * 1000)
    fallback_updated_ms = int(stat.st_mtime * 1000)
    created_ms = parse_timestamp(meta.get("timestamp")) or fallback_created_ms
    updated_ms = max(max_timestamp_ms, created_ms) if max_timestamp_ms else max(fallback_updated_ms, created_ms)

    current_provider = config.get("model_provider") or text_value(meta.get("model_provider"))
    original_provider = text_value(meta.get("model_provider")) or current_provider or "openai"
    provider = current_provider if align_provider and current_provider else original_provider
    title = title_index.get(thread_id) or title_from_text(first_user_message, path.stem)
    preview = title_from_text(first_user_message, title)[:500]

    sandbox_policy = turn_context.get("sandbox_policy") or DEFAULT_SANDBOX_POLICY
    approval_mode = text_value(turn_context.get("approval_policy")) or "on-request"
    model = text_value(turn_context.get("model")) or config.get("model") or None
    reasoning_effort = (
        text_value(turn_context.get("effort"))
        or config.get("model_reasoning_effort")
        or None
    )

    return {
        "id": thread_id,
        "rollout_path": str(path),
        "created_at": created_ms // 1000,
        "updated_at": updated_ms // 1000,
        "source": text_value(meta.get("source")) or "vscode",
        "model_provider": provider,
        "cwd": text_value(meta.get("cwd")) or text_value(turn_context.get("cwd")) or str(Path.home()),
        "title": title,
        "sandbox_policy": compact_json(sandbox_policy),
        "approval_mode": approval_mode,
        "tokens_used": tokens_used,
        "has_user_event": 1 if first_user_message else 0,
        "archived": 1 if archived else 0,
        "archived_at": updated_ms // 1000 if archived else None,
        "git_sha": None,
        "git_branch": None,
        "git_origin_url": None,
        "cli_version": text_value(meta.get("cli_version")),
        "first_user_message": first_user_message,
        "agent_nickname": None,
        "agent_role": None,
        "memory_mode": "enabled",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "agent_path": None,
        "created_at_ms": created_ms,
        "updated_at_ms": updated_ms,
        "thread_source": text_value(meta.get("thread_source")) or text_value(turn_context.get("thread_source")) or None,
        "preview": preview,
    }


def discover_rollouts(codex_home: Path) -> list[tuple[Path, bool]]:
    roots = [
        (codex_home / "sessions", False),
        (codex_home / "archived_sessions", True),
    ]
    rollouts: list[tuple[Path, bool]] = []
    for root, archived in roots:
        if root.exists():
            rollouts.extend((path, archived) for path in root.rglob("rollout-*.jsonl"))
    return sorted(rollouts, key=lambda item: str(item[0]))


def writable_backup_dir(codex_home: Path) -> Path:
    plugin_backup_dir = Path(__file__).resolve().parents[1] / "backups"
    candidates = [codex_home / "backups", plugin_backup_dir]
    errors: list[str] = []

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".history-recover-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")

    raise RuntimeError("no writable backup directory found; " + "; ".join(errors))


def backup_state(
    codex_home: Path,
    dry_run: bool,
    rollout_paths: list[Path] | None = None,
) -> list[str]:
    if dry_run:
        return []
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = writable_backup_dir(codex_home)
    backups: list[str] = []

    db_path = codex_home / "state_5.sqlite"
    if db_path.exists():
        backup_path = backup_dir / f"state_5.before-history-recover-{timestamp}.sqlite"
        source = sqlite3.connect(str(db_path), timeout=10)
        try:
            target = sqlite3.connect(str(backup_path))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        backups.append(str(backup_path))

    index_path = codex_home / "session_index.jsonl"
    if index_path.exists():
        backup_path = backup_dir / f"session_index.before-history-recover-{timestamp}.jsonl"
        shutil.copy2(index_path, backup_path)
        backups.append(str(backup_path))

    if rollout_paths:
        rollout_backup_root = backup_dir / f"rollouts.before-history-recover-{timestamp}"
        for rollout_path in rollout_paths:
            try:
                relative_path = rollout_path.relative_to(codex_home)
            except ValueError:
                relative_path = Path(rollout_path.name)
            backup_path = rollout_backup_root / relative_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rollout_path, backup_path)
        backups.append(str(rollout_backup_root))

    return backups


def rollout_provider_change_paths(
    rollouts: list[tuple[Path, bool]],
    current_provider: str | None,
    align_provider: bool,
    align_rollouts: bool,
) -> list[Path]:
    if not align_provider or not align_rollouts or not current_provider:
        return []

    paths: list[Path] = []
    for path, _ in rollouts:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") != "session_meta":
                    continue
                payload = item.get("payload")
                if isinstance(payload, dict) and payload.get("model_provider") != current_provider:
                    paths.append(path)
                break
    return paths


def align_rollout_metadata(
    paths: list[Path],
    current_provider: str | None,
    dry_run: bool,
) -> dict[str, int]:
    if not current_provider:
        return {"rollout_provider_aligned": 0}

    changed = 0
    for path in paths:
        if dry_run:
            changed += 1
            continue

        tmp_path = path.with_name(path.name + ".history-recover.tmp")
        file_changed = False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                with tmp_path.open("w", encoding="utf-8") as target:
                    for line in source:
                        output_line = line
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            target.write(output_line)
                            continue

                        if item.get("type") == "session_meta":
                            payload = item.get("payload")
                            if (
                                isinstance(payload, dict)
                                and payload.get("model_provider") != current_provider
                            ):
                                payload["model_provider"] = current_provider
                                output_line = (
                                    json.dumps(
                                        item,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                    + "\n"
                                )
                                file_changed = True
                        target.write(output_line)

            if file_changed:
                os.replace(tmp_path, path)
                changed += 1
            else:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    return {"rollout_provider_aligned": changed}


def ensure_threads_schema(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "select name from sqlite_master where type='table' and name='threads'"
    ).fetchone()
    if not table:
        raise RuntimeError("state_5.sqlite does not contain a threads table")
    columns = {row[1] for row in conn.execute("pragma table_info(threads)").fetchall()}
    missing = [name for name in THREAD_COLUMNS if name not in columns]
    if missing:
        raise RuntimeError("threads table is missing columns: " + ", ".join(missing))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def merge_existing(
    existing: dict[str, Any],
    record: dict[str, Any],
    align_provider: bool,
) -> dict[str, Any]:
    merged = dict(existing)
    always_update = {
        "rollout_path",
        "sandbox_policy",
        "approval_mode",
        "cli_version",
        "thread_source",
    }
    for key in always_update:
        if record.get(key) not in (None, ""):
            merged[key] = record[key]

    merged["created_at"] = min(existing.get("created_at") or record["created_at"], record["created_at"])
    merged["updated_at"] = max(existing.get("updated_at") or 0, record["updated_at"])
    merged["created_at_ms"] = min(
        existing.get("created_at_ms") or record["created_at_ms"],
        record["created_at_ms"],
    )
    merged["updated_at_ms"] = max(existing.get("updated_at_ms") or 0, record["updated_at_ms"])
    merged["tokens_used"] = max(existing.get("tokens_used") or 0, record.get("tokens_used") or 0)
    merged["has_user_event"] = max(existing.get("has_user_event") or 0, record.get("has_user_event") or 0)
    record_archived = 1 if record.get("archived") else 0
    merged["archived"] = record_archived
    merged["archived_at"] = (
        existing.get("archived_at") or record.get("archived_at")
        if record_archived
        else None
    )

    if align_provider and record.get("model_provider"):
        merged["model_provider"] = record["model_provider"]
    for key in ("source", "cwd", "title", "first_user_message", "memory_mode", "model", "reasoning_effort", "preview"):
        if not existing.get(key) and record.get(key) not in (None, ""):
            merged[key] = record[key]
    return merged


def update_threads(
    codex_home: Path,
    records: list[dict[str, Any]],
    align_provider: bool,
    current_provider: str | None,
    dry_run: bool,
) -> dict[str, int]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        raise RuntimeError(f"Codex state database not found: {db_path}")

    stats = {
        "inserted": 0,
        "updated": 0,
        "provider_aligned": 0,
        "orphan_provider_aligned": 0,
    }
    provider_aligned_ids: set[str] = set()
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        ensure_threads_schema(conn)
        for record in records:
            existing = row_to_dict(
                conn.execute("select * from threads where id = ?", (record["id"],)).fetchone()
            )
            if existing is None:
                stats["inserted"] += 1
                if not dry_run:
                    placeholders = ", ".join("?" for _ in THREAD_COLUMNS)
                    columns = ", ".join(THREAD_COLUMNS)
                    conn.execute(
                        f"insert into threads ({columns}) values ({placeholders})",
                        [record.get(column) for column in THREAD_COLUMNS],
                    )
                continue

            desired = merge_existing(existing, record, align_provider)
            if existing.get("model_provider") != desired.get("model_provider"):
                stats["provider_aligned"] += 1
                provider_aligned_ids.add(record["id"])
            changed_columns = [
                column
                for column in THREAD_COLUMNS
                if column != "id" and existing.get(column) != desired.get(column)
            ]
            if not changed_columns:
                continue
            stats["updated"] += 1
            if not dry_run:
                set_clause = ", ".join(f"{column} = ?" for column in changed_columns)
                values = [desired.get(column) for column in changed_columns]
                values.append(record["id"])
                conn.execute(f"update threads set {set_clause} where id = ?", values)

        if align_provider and current_provider:
            remaining_ids = [
                row[0]
                for row in conn.execute(
                    "select id from threads where model_provider != ?",
                    (current_provider,),
                ).fetchall()
                if row[0] not in provider_aligned_ids
            ]
            stats["orphan_provider_aligned"] = len(remaining_ids)
            stats["provider_aligned"] += len(remaining_ids)
            stats["updated"] += len(remaining_ids)
            if not dry_run:
                for thread_id in remaining_ids:
                    conn.execute(
                        "update threads set model_provider = ? where id = ?",
                        (current_provider, thread_id),
                    )

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return stats


def update_session_index(
    codex_home: Path,
    records: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, int]:
    index_path = codex_home / "session_index.jsonl"
    existing: dict[str, dict[str, Any]] = {}
    if index_path.exists():
        with index_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                thread_id = item.get("id")
                if isinstance(thread_id, str):
                    existing[thread_id] = item

    active_records = [record for record in records if not record.get("archived")]
    changed = 0
    for record in active_records:
        thread_id = record["id"]
        item = {
            "id": thread_id,
            "thread_name": existing.get(thread_id, {}).get("thread_name") or record["title"],
            "updated_at": format_timestamp(record["updated_at_ms"]),
        }
        if existing.get(thread_id) != item:
            changed += 1
        existing[thread_id] = item

    if not dry_run:
        ordered = sorted(existing.values(), key=lambda item: item.get("updated_at", ""), reverse=True)
        with index_path.open("w", encoding="utf-8") as handle:
            for item in ordered:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"session_index_changed": changed}


def inspect_threads(
    codex_home: Path,
    current_provider: str | None,
) -> dict[str, Any]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return {
            "thread_count": 0,
            "provider_counts": {},
            "remaining_provider_mismatches": 0,
        }

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        provider_counts = {
            row[0]: row[1]
            for row in conn.execute(
                "select model_provider, count(*) from threads group by model_provider"
            ).fetchall()
        }
        thread_count = sum(provider_counts.values())
        remaining = 0
        if current_provider:
            remaining = conn.execute(
                "select count(*) from threads where model_provider != ?",
                (current_provider,),
            ).fetchone()[0]
    finally:
        conn.close()

    return {
        "thread_count": thread_count,
        "provider_counts": provider_counts,
        "remaining_provider_mismatches": remaining,
    }


def run_sync(args: argparse.Namespace) -> dict[str, Any]:
    codex_home = Path(args.codex_home).expanduser().resolve()
    config = parse_config(codex_home)
    current_provider = config.get("model_provider")
    align_provider = not args.no_align_provider
    align_rollouts = align_provider and not args.no_align_rollouts
    title_index = read_title_index(codex_home)
    rollouts = discover_rollouts(codex_home)
    rollout_paths_to_align = rollout_provider_change_paths(
        rollouts,
        current_provider,
        align_provider,
        align_rollouts,
    )

    records: list[dict[str, Any]] = []
    skipped = 0
    active_rollouts = sum(1 for _, archived in rollouts if not archived)
    archived_rollouts = sum(1 for _, archived in rollouts if archived)

    for path, archived in rollouts:
        record = scan_rollout(path, archived, title_index, config, align_provider)
        if record is None:
            skipped += 1
        else:
            records.append(record)

    rollout_stats = align_rollout_metadata(rollout_paths_to_align, current_provider, True)
    thread_stats = update_threads(codex_home, records, align_provider, current_provider, True)
    index_stats = update_session_index(codex_home, records, True)
    needs_write = any(
        thread_stats[key] > 0
        for key in ("inserted", "updated", "provider_aligned", "orphan_provider_aligned")
    ) or rollout_stats["rollout_provider_aligned"] > 0 or index_stats["session_index_changed"] > 0

    backups: list[str] = []
    if not args.dry_run and needs_write:
        backups = backup_state(codex_home, False, rollout_paths_to_align)
        rollout_stats = align_rollout_metadata(rollout_paths_to_align, current_provider, False)
        thread_stats = update_threads(codex_home, records, align_provider, current_provider, False)
        index_stats = update_session_index(codex_home, records, False)

    inspection = inspect_threads(codex_home, current_provider)

    return {
        "codex_home": str(codex_home),
        "current_provider": current_provider,
        "align_provider": align_provider,
        "align_rollouts": align_rollouts,
        "dry_run": args.dry_run,
        "scanned_rollouts": len(rollouts),
        "active_rollouts": active_rollouts,
        "archived_rollouts": archived_rollouts,
        "valid_threads": len(records),
        "skipped_rollouts": skipped,
        **rollout_stats,
        **thread_stats,
        **index_stats,
        **inspection,
        "backups": backups,
    }


def print_summary(summary: dict[str, Any], as_json: bool, quiet: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    provider = summary.get("current_provider") or "(unknown)"
    line = (
        f"Codex history sync: {summary['valid_threads']} local threads scanned, "
        f"{summary['inserted']} inserted, {summary['updated']} updated, "
        f"{summary['rollout_provider_aligned']} rollout files aligned, "
        f"{summary['provider_aligned']} aligned to provider {provider}, "
        f"{summary['remaining_provider_mismatches']} provider mismatches remain."
    )
    print(line)
    if quiet:
        return
    if summary["backups"]:
        print("Backups:")
        for backup in summary["backups"]:
            print(f"  {backup}")
    if summary["dry_run"]:
        print("Dry run only; no files were changed.")


def main() -> int:
    args = parse_args()
    try:
        summary = run_sync(args)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"Codex history sync failed: {exc}", file=sys.stderr)
        return 1
    print_summary(summary, args.json, args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
