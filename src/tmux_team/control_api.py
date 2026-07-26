from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
from collections import deque
from contextlib import closing
from datetime import UTC, datetime
from typing import Any, TextIO

from . import __version__
from .config import ConfigError, TeamConfig, load_config
from .store import SCHEMA_VERSION, utc_now

API_VERSION = "tmux-team.control/v1"
JSONRPC_VERSION = "2.0"
SERVER_NAME = "tmux-team-control"
SERVER_VERSION = __version__

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_STATUS_ROLES = 64
MAX_FILTER_LENGTH = 128
MAX_IDENTIFIER_LENGTH = 128
MAX_SUMMARY_LENGTH = 512
MAX_MILESTONE_SCAN_BYTES = 2 * 1024 * 1024
MAX_MILESTONE_SUBJECTS = 16
MAX_MILESTONE_TAGS = 16
MAX_EVENT_CURSOR = (1 << 63) - 1
MAX_REQUEST_BYTES = 64 * 1024

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
_REQUIRED_TABLES = ("roles", "messages", "events")


class ControlAPIError(ValueError):
    pass


class ReadOnlyControl:
    """Bounded external observation surface over tmux-team-owned state."""

    def __init__(self, config: TeamConfig):
        self.config = config
        self.db_path = config.runtime_dir / "team.sqlite"
        self.milestones_path = config.runtime_dir / "milestones.jsonl"

    def connect(self) -> sqlite3.Connection:
        """Open the existing database without creating, migrating, or syncing it."""
        if self.db_path.is_symlink():
            raise ControlAPIError("refusing symlinked tmux-team runtime database")
        if not self.db_path.is_file():
            raise ControlAPIError("tmux-team runtime database does not exist")
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._validate_schema(conn)
        except Exception:
            conn.close()
            raise
        return conn

    def _validate_schema(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise ControlAPIError(
                f"runtime schema version {version} is not readable by this control API "
                f"(expected {SCHEMA_VERSION}); open it with the matching tmux-team CLI first"
            )
        placeholders = ", ".join("?" for _ in _REQUIRED_TABLES)
        rows = conn.execute(
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
            _REQUIRED_TABLES,
        ).fetchall()
        present = {str(row["name"]) for row in rows}
        missing = sorted(set(_REQUIRED_TABLES) - present)
        if missing:
            raise ControlAPIError(f"runtime database is missing required table: {missing[0]}")

    def status(self, conn: sqlite3.Connection, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = _arguments(arguments, allowed=())
        if args:
            raise AssertionError("unreachable")

        observed_at = utc_now()
        conn.execute("BEGIN")
        try:
            role_rows = conn.execute(
                """
                SELECT
                  CASE WHEN typeof(name) = 'text' AND length(name) <= ? THEN name END AS name,
                  CASE WHEN typeof(state) = 'text' AND length(state) <= ? THEN state END AS state,
                  CASE WHEN typeof(mode) = 'text' AND length(mode) <= ? THEN mode END AS mode
                FROM roles
                ORDER BY name
                LIMIT ?
                """,
                (
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_STATUS_ROLES + 1,
                ),
            ).fetchall()
            roles_truncated = len(role_rows) > MAX_STATUS_ROLES
            role_rows = role_rows[:MAX_STATUS_ROLES]
            role_names = [str(row["name"]) for row in role_rows if row["name"] is not None]
            counts = self._message_counts(conn, role_names, observed_at)
            roles = [
                {
                    "name": _safe_identifier_or_none(row["name"]),
                    "state": _safe_identifier_or_none(row["state"]),
                    "mode": _safe_identifier_or_none(row["mode"]),
                    "counts": _role_counts(counts.get(str(row["name"]), {})),
                }
                for row in role_rows
            ]
            cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
        finally:
            conn.rollback()
        return {
            "api_version": API_VERSION,
            "kind": "status",
            "team": {"name": _safe_identifier_or_none(self.config.name)},
            "snapshot": {"created_at": observed_at, "event_cursor": cursor},
            "roles": roles,
            "roles_truncated": roles_truncated,
        }

    def _message_counts(
        self,
        conn: sqlite3.Connection,
        roles: list[str],
        observed_at: str,
    ) -> dict[str, dict[str, int]]:
        if not roles:
            return {}
        placeholders = ", ".join("?" for _ in roles)
        rows = conn.execute(
            f"""
            SELECT
              recipient,
              priority,
              CASE
                WHEN state = 'claimed'
                  AND claim_expires_at IS NOT NULL
                  AND claim_expires_at <= ?
                THEN 'stale_claimed'
                ELSE state
              END AS display_state,
              COUNT(*) AS count
            FROM messages
            WHERE recipient IN ({placeholders})
            GROUP BY recipient, priority, display_state
            """,
            (observed_at, *roles),
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            role_counts = result.setdefault(str(row["recipient"]), {})
            display_state = str(row["display_state"])
            count = int(row["count"])
            role_counts[display_state] = role_counts.get(display_state, 0) + count
            if row["priority"] == "urgent" and display_state in {
                "queued",
                "notified",
                "retrying",
                "stale_claimed",
            }:
                role_counts["urgent"] = role_counts.get("urgent", 0) + count
        return result

    def events_since(self, conn: sqlite3.Connection, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = _arguments(arguments, allowed=("cursor", "limit"))
        cursor = _bounded_int(args, "cursor", default=0, minimum=0, maximum=MAX_EVENT_CURSOR)
        limit = _bounded_int(args, "limit", default=DEFAULT_PAGE_SIZE, minimum=1, maximum=MAX_PAGE_SIZE)
        conn.execute("BEGIN")
        try:
            upper_bound = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
            if cursor > upper_bound:
                return {
                    "api_version": API_VERSION,
                    "kind": "events",
                    "cursor": {
                        "requested": cursor,
                        "next": upper_bound,
                        "upper_bound": upper_bound,
                        "has_more": False,
                        "reset_required": True,
                    },
                    "events": [],
                }
            rows = conn.execute(
                """
                SELECT
                  id,
                  CASE
                    WHEN typeof(created_at) = 'text' AND length(created_at) <= ? THEN created_at
                  END AS created_at,
                  CASE WHEN typeof(type) = 'text' AND length(type) <= ? THEN type END AS type,
                  CASE WHEN typeof(actor) = 'text' AND length(actor) <= ? THEN actor END AS actor,
                  CASE WHEN typeof(ref_id) = 'text' AND length(ref_id) <= ? THEN ref_id END AS ref_id
                FROM events
                WHERE id > ?
                ORDER BY id
                LIMIT ?
                """,
                (
                    MAX_FILTER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    cursor,
                    limit + 1,
                ),
            ).fetchall()
        finally:
            conn.rollback()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        events = [_external_event(row) for row in page_rows]
        next_cursor = int(page_rows[-1]["id"]) if page_rows else cursor
        return {
            "api_version": API_VERSION,
            "kind": "events",
            "cursor": {
                "requested": cursor,
                "next": next_cursor,
                "upper_bound": upper_bound,
                "has_more": has_more,
                "reset_required": False,
            },
            "events": events,
        }

    def milestones(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = _arguments(
            arguments,
            allowed=("since", "until", "subject_role", "scope", "kind", "limit"),
        )
        limit = _bounded_int(args, "limit", default=DEFAULT_PAGE_SIZE, minimum=1, maximum=MAX_PAGE_SIZE)
        since = _optional_timestamp(args, "since")
        until = _optional_timestamp(args, "until")
        if since is not None and until is not None and since > until:
            raise ControlAPIError("since must not be after until")
        subject_role = _optional_filter(args, "subject_role")
        scope = _optional_filter(args, "scope")
        if scope is not None and scope not in {"role", "team"}:
            raise ControlAPIError("scope must be 'role' or 'team'")
        kind = _optional_filter(args, "kind")

        rows, scan_truncated, discarded_lines = self._read_milestone_tail()
        matched: deque[dict[str, Any]] = deque(maxlen=limit)
        matched_count = 0
        for row in rows:
            created_at = _row_timestamp(row)
            if created_at is None:
                continue
            if since is not None and created_at < since:
                continue
            if until is not None and created_at > until:
                continue
            subjects = _milestone_subjects(row)
            if subject_role is not None and subject_role not in subjects:
                continue
            if scope is not None and str(row.get("scope") or "") != scope:
                continue
            if kind is not None and str(row.get("kind") or "") != kind:
                continue
            matched_count += 1
            matched.append(_external_milestone(row, subjects))
        return {
            "api_version": API_VERSION,
            "kind": "milestones",
            "query": {
                "since": _format_timestamp(since),
                "until": _format_timestamp(until),
                "subject_role": subject_role,
                "scope": scope,
                "kind": kind,
                "limit": limit,
            },
            "milestones": list(matched),
            "has_earlier": scan_truncated or matched_count > limit,
            "scan_truncated": scan_truncated,
            "discarded_lines": discarded_lines,
        }

    def _read_milestone_tail(self) -> tuple[list[dict[str, Any]], bool, int]:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.milestones_path, flags)
        except FileNotFoundError:
            return [], False, 0
        except OSError as exc:
            raise ControlAPIError("could not safely open tmux-team milestone log") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ControlAPIError("tmux-team milestone log is not a regular file")
            start = max(0, info.st_size - MAX_MILESTONE_SCAN_BYTES)
            os.lseek(descriptor, start, os.SEEK_SET)
            data = os.read(descriptor, MAX_MILESTONE_SCAN_BYTES)
        finally:
            os.close(descriptor)
        lines = data.splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        rows: list[dict[str, Any]] = []
        discarded_lines = 0
        for line in lines:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                discarded_lines += 1
                continue
            if isinstance(value, dict):
                rows.append(value)
            else:
                discarded_lines += 1
        return rows, start > 0, discarded_lines


def list_operations() -> list[dict[str, Any]]:
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    return [
        {
            "name": "team_control_status",
            "description": "Read a bounded field-allowlisted team status snapshot.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": read_only,
        },
        {
            "name": "team_control_events_since",
            "description": "Read a bounded page of normalized events after an opaque integer cursor.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cursor": {"type": "integer", "minimum": 0, "maximum": MAX_EVENT_CURSOR, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE, "default": DEFAULT_PAGE_SIZE},
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
        {
            "name": "team_control_milestones",
            "description": "Query the latest bounded field-allowlisted operator milestones.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": {"type": "string", "maxLength": MAX_FILTER_LENGTH},
                    "until": {"type": "string", "maxLength": MAX_FILTER_LENGTH},
                    "subject_role": {"type": "string", "maxLength": MAX_FILTER_LENGTH},
                    "scope": {"type": "string", "enum": ["role", "team"]},
                    "kind": {"type": "string", "maxLength": MAX_FILTER_LENGTH},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE, "default": DEFAULT_PAGE_SIZE},
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
    ]


def call_operation(
    control: ReadOnlyControl,
    conn: sqlite3.Connection,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if name == "team_control_status":
        return control.status(conn, arguments)
    if name == "team_control_events_since":
        return control.events_since(conn, arguments)
    if name == "team_control_milestones":
        return control.milestones(arguments)
    raise ControlAPIError("unknown read-only control operation")


def handle_control_request(
    control: ReadOnlyControl,
    conn: sqlite3.Connection,
    request: Any,
) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return _json_rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    is_notification = "id" not in request
    if request.get("jsonrpc") != JSONRPC_VERSION or (
        not is_notification and not isinstance(request_id, (str, int, type(None)))
    ):
        return None if is_notification else _json_rpc_error(request_id, -32600, "Invalid Request")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return None if is_notification else _json_rpc_error(request_id, -32600, "Invalid Request")

    try:
        if method == "control/list":
            if params:
                raise ControlAPIError("control/list does not accept parameters")
            result = {
                "api_version": API_VERSION,
                "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "operations": list_operations(),
            }
        elif method == "control/call":
            result = _handle_control_call(control, conn, params)
        elif method == "ping":
            if params:
                raise ControlAPIError("ping does not accept parameters")
            result = {}
        else:
            return None if is_notification else _json_rpc_error(request_id, -32601, "Method not found")
    except (ControlAPIError, ValueError, KeyError) as exc:
        return None if is_notification else _json_rpc_error(request_id, -32602, _bounded_text(exc, MAX_SUMMARY_LENGTH))
    except (OSError, sqlite3.Error):
        return None if is_notification else _json_rpc_error(request_id, -32001, "control state read failed")
    return None if is_notification else {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _handle_control_call(
    control: ReadOnlyControl,
    conn: sqlite3.Connection,
    params: Any,
) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ControlAPIError("control/call params must be an object")
    unknown = set(params) - {"name", "arguments"}
    if unknown:
        raise ControlAPIError("unexpected control/call parameter")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ControlAPIError("missing required argument: name")
    arguments = params.get("arguments") or {}
    return call_operation(control, conn, name, arguments)


def serve_control_protocol(
    control: ReadOnlyControl,
    conn: sqlite3.Connection,
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    while True:
        line = input_stream.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            break
        if len(line.encode("utf-8")) > MAX_REQUEST_BYTES or not line.endswith("\n"):
            response = _json_rpc_error(None, -32700, "Request frame too large")
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()
            if not line.endswith("\n"):
                input_stream.readline()
            continue
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _json_rpc_error(None, -32700, f"Parse error: {exc.msg}")
        else:
            response = handle_control_request(control, conn, request)
        if response is not None:
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()
    return 0


def serve_stdio(
    *,
    config_path: str | None = None,
    runtime_dir: str | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    config = load_config(config_path, runtime_dir)
    control = ReadOnlyControl(config)
    with closing(control.connect()) as conn:
        return serve_control_protocol(control, conn, input_stream or sys.stdin, output_stream or sys.stdout)


def _arguments(arguments: dict[str, Any] | None, *, allowed: tuple[str, ...]) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise ControlAPIError("tool arguments must be an object")
    unknown = set(arguments) - set(allowed)
    if unknown:
        raise ControlAPIError(f"unexpected argument: {sorted(unknown)[0]}")
    return arguments


def _bounded_int(
    arguments: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlAPIError(f"{key} must be an integer")
    if value < minimum or value > maximum:
        raise ControlAPIError(f"{key} must be between {minimum} and {maximum}")
    return value


def _optional_filter(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControlAPIError(f"{key} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ControlAPIError(f"{key} must not be empty")
    if len(normalized) > MAX_FILTER_LENGTH:
        raise ControlAPIError(f"{key} exceeds maximum length {MAX_FILTER_LENGTH}")
    return normalized


def _optional_timestamp(arguments: dict[str, Any], key: str) -> datetime | None:
    value = _optional_filter(arguments, key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ControlAPIError(f"{key} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ControlAPIError(f"{key} must include a timezone")
    return parsed.astimezone(UTC)


def _row_timestamp(row: dict[str, Any]) -> datetime | None:
    value = row.get("created_at")
    if not isinstance(value, str) or len(value) > MAX_FILTER_LENGTH:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _external_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cursor": int(row["id"]),
        "created_at": _bounded_text(row["created_at"], MAX_FILTER_LENGTH),
        "type": _bounded_text(row["type"], MAX_IDENTIFIER_LENGTH),
        "actor": _safe_identifier_or_none(row["actor"]),
        "reference": _safe_identifier_or_none(row["ref_id"]),
    }


def _milestone_subjects(row: dict[str, Any]) -> tuple[str, ...]:
    values = row.get("subject_roles")
    subjects: list[str] = []
    if isinstance(values, list):
        for value in values[:MAX_MILESTONE_SUBJECTS]:
            safe = _safe_identifier_or_none(value)
            if safe is not None:
                subjects.append(safe)
    legacy = _safe_identifier_or_none(row.get("role"))
    if legacy is not None and legacy not in subjects:
        subjects.append(legacy)
    return tuple(subjects[:MAX_MILESTONE_SUBJECTS])


def _external_milestone(row: dict[str, Any], subjects: tuple[str, ...]) -> dict[str, Any]:
    tags: list[str] = []
    values = row.get("tags")
    if isinstance(values, list):
        for value in values[:MAX_MILESTONE_TAGS]:
            safe = _safe_identifier_or_none(value)
            if safe is not None:
                tags.append(safe)
    return {
        "created_at": _bounded_text(row.get("created_at"), MAX_FILTER_LENGTH),
        "kind": _bounded_text(row.get("kind") or "milestone", MAX_IDENTIFIER_LENGTH),
        "summary": _bounded_text(row.get("summary"), MAX_SUMMARY_LENGTH),
        "recorded_by": _safe_identifier_or_none(row.get("recorded_by") or row.get("actor")),
        "scope": _bounded_text(row.get("scope") or ("role" if subjects else "team"), MAX_IDENTIFIER_LENGTH),
        "subject_roles": list(subjects),
        "reference": _safe_identifier_or_none(row.get("ref_id")),
        "tags": tags,
    }


def _role_counts(counts: dict[str, int]) -> dict[str, int]:
    stale = counts.get("stale_claimed", 0)
    pending = sum(counts.get(state, 0) for state in ("queued", "notified", "retrying")) + stale
    return {
        "pending": pending,
        "urgent": counts.get("urgent", 0),
        "stale_claimed": stale,
        "claimed": counts.get("claimed", 0),
        "acknowledged": counts.get("acknowledged", 0),
        "completed": counts.get("completed", 0),
    }


def _bounded_text(value: Any, maximum: int) -> str:
    text = str(value or "")
    return text[:maximum]


def _safe_identifier_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
        return None
    return value if _SAFE_IDENTIFIER.fullmatch(value) else None


def _json_rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": {"code": code, "message": message}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tmux_team.control_api")
    parser.add_argument("--config", help="Path to .tmux-team/team.toml")
    parser.add_argument("--runtime-dir", help="Override runtime directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return serve_stdio(config_path=args.config, runtime_dir=args.runtime_dir)
    except (ConfigError, ControlAPIError, OSError, sqlite3.Error) as exc:
        print(f"tmux-team-control: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
