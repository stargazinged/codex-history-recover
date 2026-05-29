#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = PLUGIN_ROOT / "scripts" / "sync_history.py"
SERVER_INFO = {"name": "codex-history-recover", "version": "0.1.1"}


def run_sync(*, dry_run: bool = False, align_provider: bool = True) -> tuple[bool, str]:
    args = [sys.executable, str(SYNC_SCRIPT), "--json"]
    if dry_run:
        args.append("--dry-run")
    if not align_provider:
        args.append("--no-align-provider")
    result = subprocess.run(args, text=True, capture_output=True, timeout=30)
    output = (result.stdout or result.stderr or "sync failed").strip()
    if result.returncode != 0:
        return False, output
    return True, output


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    raw = sys.stdin.buffer.read(length)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def send_message(payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def result(message_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": value}


def error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")

    if message_id is None:
        return None

    if method == "initialize":
        return result(
            message_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method == "tools/list":
        return result(
            message_id,
            {
                "tools": [
                    {
                        "name": "sync_codex_history",
                        "description": "Scan local Codex rollout files and restore the visible thread index.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "dry_run": {
                                    "type": "boolean",
                                    "description": "Preview changes without writing the database or index.",
                                },
                                "align_provider": {
                                    "type": "boolean",
                                    "description": "Align imported threads to the current model_provider.",
                                    "default": True,
                                },
                            },
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )

    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "sync_codex_history":
            return error(message_id, -32602, "unknown tool")
        arguments = params.get("arguments") or {}
        dry_run = bool(arguments.get("dry_run", False))
        align_provider = bool(arguments.get("align_provider", True))
        ok, output = run_sync(dry_run=dry_run, align_provider=align_provider)
        return result(
            message_id,
            {"content": [{"type": "text", "text": output}], "isError": not ok},
        )

    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method == "resources/list" else "prompts"
        return result(message_id, {key: []})

    return error(message_id, -32601, f"method not found: {method}")


def main() -> int:
    # Plugin startup is the automatic part: when Codex starts this MCP server, the
    # local history index is repaired before the server begins handling requests.
    ok, output = run_sync(dry_run=False, align_provider=True)
    if not ok:
        print(f"codex-history-recover startup sync failed: {output}", file=sys.stderr)
    while True:
        message = read_message()
        if message is None:
            return 0
        response = handle_request(message)
        if response is not None:
            send_message(response)


if __name__ == "__main__":
    raise SystemExit(main())
