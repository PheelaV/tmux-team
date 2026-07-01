from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .config import ConfigError, load_config
from .extensions.manifest import ExtensionError
from .extensions.runner import HookDenied, HookError
from .service import TeamService
from .store import CLAIMABLE_STATES, Store, normalize_notify_method, normalize_priority

JSONRPC_VERSION = "2.0"
SERVER_NAME = "tmux-team"
SERVER_VERSION = "0.1.0"


class ToolCallError(ValueError):
    pass


def list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "team_status",
            "description": "Show role state, queue counts, and app-server binding status.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "team_inbox_next",
            "description": "Claim the next durable inbox message for a role.",
            "inputSchema": {
                "type": "object",
                "required": ["role"],
                "properties": {
                    "role": {"type": "string"},
                    "claim_seconds": {"type": "integer", "default": 3600},
                    "include_body": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "team_ack",
            "description": "Acknowledge a claimed message addressed to a role.",
            "inputSchema": {
                "type": "object",
                "required": ["role", "message_id"],
                "properties": {"role": {"type": "string"}, "message_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "team_complete",
            "description": "Complete a message with result status and summary.",
            "inputSchema": {
                "type": "object",
                "required": ["role", "message_id"],
                "properties": {
                    "role": {"type": "string"},
                    "message_id": {"type": "string"},
                    "status": {"type": "string", "default": "done"},
                    "summary": {"type": "string", "default": ""},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "team_send",
            "description": "Queue a durable role message and optionally wake the recipient through app-server.",
            "inputSchema": {
                "type": "object",
                "required": ["to", "summary"],
                "properties": {
                    "to": {"type": "string"},
                    "from": {"type": "string", "default": "operator"},
                    "sender": {"type": "string"},
                    "priority": {"type": "string", "enum": ["urgent", "high", "normal", "low"], "default": "normal"},
                    "summary": {"type": "string"},
                    "body": {"type": "string", "default": ""},
                    "force": {"type": "boolean", "default": False},
                    "wake": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "team_notify",
            "description": "Wake a role through Codex app-server turn/start. Shell and tmux send-keys methods are not exposed.",
            "inputSchema": {
                "type": "object",
                "required": ["role"],
                "properties": {
                    "role": {"type": "string"},
                    "method": {"type": "string", "default": "app-server-turn"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "team_wake",
            "description": "Alias for team_notify with app-server-turn delivery.",
            "inputSchema": {
                "type": "object",
                "required": ["role"],
                "properties": {"role": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    ]


def call_tool(store: Store, conn: Any, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    args = arguments or {}
    if not isinstance(args, dict):
        raise ToolCallError("tool arguments must be an object")
    service = TeamService(store)

    if name == "team_status":
        return team_status(store, conn)
    if name == "team_inbox_next":
        return team_inbox_next(service, conn, args)
    if name == "team_ack":
        return team_ack(service, conn, args)
    if name == "team_complete":
        return team_complete(service, conn, args)
    if name == "team_send":
        return team_send(service, conn, args)
    if name in ("team_notify", "team_wake"):
        return team_notify(service, conn, args)
    raise ToolCallError(f"unknown tool: {name}")


def team_status(store: Store, conn: Any) -> dict[str, Any]:
    counts = store.active_counts(conn)
    roles = []
    for row in store.list_roles(conn):
        role_counts = counts.get(row["name"], {})
        roles.append(role_status_dict(store, conn, row, role_counts))
    return {
        "team": store.config.name,
        "runtime_dir": str(store.runtime_dir),
        "roles": roles,
    }


def team_inbox_next(service: TeamService, conn: Any, args: dict[str, Any]) -> dict[str, Any]:
    role = required_str(args, "role")
    claim_seconds = int_arg(args, "claim_seconds", 3600)
    include_body = bool_arg(args, "include_body", True)
    row = service.claim_next(conn, role, claim_seconds, actor=role)
    if row is None:
        return {"role": role, "message": None, "pending": service.store.pending_count(conn, role)}
    return {
        "role": role,
        "message": message_dict(row, include_body=include_body),
        "pending": service.store.pending_count(conn, role),
    }


def team_ack(service: TeamService, conn: Any, args: dict[str, Any]) -> dict[str, Any]:
    role = required_str(args, "role")
    message_id = required_str(args, "message_id")
    row = service.ack_message(conn, role, message_id, actor=role)
    return {"message": message_dict(row)}


def team_complete(service: TeamService, conn: Any, args: dict[str, Any]) -> dict[str, Any]:
    role = required_str(args, "role")
    message_id = required_str(args, "message_id")
    status = str_arg(args, "status", "done")
    summary = str_arg(args, "summary", "")
    row = service.complete_message(conn, role, message_id, status, summary, actor=role)
    return {"message": message_dict(row)}


def team_send(service: TeamService, conn: Any, args: dict[str, Any]) -> dict[str, Any]:
    recipient = required_str(args, "to")
    sender = str(args.get("from") or args.get("sender") or "operator")
    priority = str_arg(args, "priority", "normal")
    normalize_priority(priority)
    summary = required_str(args, "summary")
    body = str_arg(args, "body", "")
    force = bool_arg(args, "force", False)
    wake = bool_arg(args, "wake", True)

    sent = service.send_message(
        conn,
        sender=sender,
        recipient=recipient,
        priority=priority,
        summary=summary,
        body=body,
        force=force,
        wake=wake,
        notify_method="app-server-turn",
        actor=sender,
    )
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (sent.message.id,)).fetchone()
    result: dict[str, Any] = {"message": message_dict(row), "blocked": sent.blocked}

    if sent.notification is not None:
        result["notification"] = {
            "ok": sent.notification.ok,
            "method": sent.notification.method,
            "details": sent.notification.details,
        }
    return result


def team_notify(service: TeamService, conn: Any, args: dict[str, Any]) -> dict[str, Any]:
    role = required_str(args, "role")
    method = normalize_notify_method(str_arg(args, "method", "app-server-turn"))
    if method != "app-server-turn":
        raise ToolCallError("MCP notify only supports app-server-turn delivery")
    result = service.notify_role(conn, role, "app-server-turn", actor=role)
    return {"ok": result.ok, "method": result.method, "details": result.details}


def role_status_dict(store: Store, conn: Any, row: Any, counts: dict[str, int]) -> dict[str, Any]:
    pending = sum(counts.get(state, 0) for state in CLAIMABLE_STATES)
    result = {
        "name": row["name"],
        "state": row["state"],
        "mode": row["mode"],
        "pane": row["pane"],
        "worktree": row["worktree"],
        "counts": {
            "pending": pending,
            "claimed": counts.get("claimed", 0),
            "acknowledged": counts.get("acknowledged", 0),
            "completed": counts.get("completed", 0),
        },
    }
    binding = store.get_role_app_server(conn, row["name"])
    resolved = store.resolve_role_app_server(conn, row["name"], row)
    if binding is not None:
        result["app_server"] = {
            "endpoint": binding["endpoint"],
            "thread_id": binding["thread_id"],
            "source": "binding",
        }
    elif resolved is not None:
        endpoint, thread_id, timeout = resolved
        result["app_server"] = {
            "endpoint": endpoint,
            "thread_id": thread_id,
            "timeout": timeout,
            "source": "config",
        }
    else:
        result["app_server"] = None
    return result


def message_dict(row: Any, *, include_body: bool = False) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "sender": row["sender"],
        "recipient": row["recipient"],
        "priority": row["priority"],
        "summary": row["summary"],
        "body_path": row["body_path"],
        "state": row["state"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "claimed_by": row["claimed_by"],
        "claim_expires_at": row["claim_expires_at"],
        "acknowledged_at": row["acknowledged_at"],
        "completed_at": row["completed_at"],
        "result_status": row["result_status"],
        "result_summary": row["result_summary"],
    }
    if include_body:
        result["body"] = Path(row["body_path"]).read_text(encoding="utf-8")
    return result


def handle_json_rpc_request(store: Store, conn: Any, request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return json_rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    is_notification = "id" not in request
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(method, str):
        return None if is_notification else json_rpc_error(request_id, -32600, "Invalid Request")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "prototype-jsonrpc",
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {"tools": list_tools()}
        elif method == "tools/call":
            result = handle_tools_call(store, conn, params)
        elif method == "ping":
            result = {}
        elif method == "shutdown":
            result = {"ok": True}
        else:
            return None if is_notification else json_rpc_error(request_id, -32601, f"Method not found: {method}")
    except (ExtensionError, HookDenied, HookError, ToolCallError, ValueError, KeyError, PermissionError) as exc:
        return None if is_notification else json_rpc_error(request_id, -32000, str(exc))
    except OSError as exc:
        return None if is_notification else json_rpc_error(request_id, -32001, str(exc))

    return None if is_notification else {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def handle_tools_call(store: Store, conn: Any, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ToolCallError("tools/call params must be an object")
    name = required_str(params, "name")
    arguments = params.get("arguments") or {}
    structured = call_tool(store, conn, name, arguments)
    return {
        "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
        "structuredContent": structured,
    }


def serve_json_rpc(store: Store, conn: Any, input_stream: TextIO, output_stream: TextIO) -> int:
    for line in input_stream:
        if not line.strip():
            continue
        request = None
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = json_rpc_error(None, -32700, f"Parse error: {exc.msg}")
        else:
            response = handle_json_rpc_request(store, conn, request)
        if response is not None:
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()
        if isinstance(request, dict) and request.get("method") == "shutdown":
            break
    return 0


def serve_stdio(
    *,
    config_path: str | None = None,
    runtime_dir: str | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    config = load_config(config_path, runtime_dir)
    store = Store(config)
    with store.connect() as conn:
        return serve_json_rpc(store, conn, input_stream or sys.stdin, output_stream or sys.stdout)


def json_rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": {"code": code, "message": message}}


def required_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or value == "":
        raise ToolCallError(f"missing required argument: {key}")
    return str(value)


def str_arg(args: dict[str, Any], key: str, default: str) -> str:
    value = args.get(key, default)
    if value is None:
        return default
    return str(value)


def int_arg(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool):
        raise ToolCallError(f"{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ToolCallError(f"{key} must be an integer") from exc


def bool_arg(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise ToolCallError(f"{key} must be a boolean")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tmux_team.mcp_server")
    parser.add_argument("--config", help="Path to .tmux-team/team.toml")
    parser.add_argument("--runtime-dir", help="Override runtime directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return serve_stdio(config_path=args.config, runtime_dir=args.runtime_dir)
    except ConfigError as exc:
        print(f"tmux-team-mcp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
