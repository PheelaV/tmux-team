from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import deque
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from typing import Any, TextIO

from . import __version__
from .config import ConfigError, TeamConfig, load_config
from .service import TeamService
from .store import SCHEMA_VERSION, Store, utc_now

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
MAX_GOAL_LENGTH = 4096
MAX_GOAL_ITEMS = 16
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_MILESTONE_SCAN_BYTES = 2 * 1024 * 1024
MAX_MILESTONE_SUBJECTS = 16
MAX_MILESTONE_TAGS = 16
MAX_EVENT_CURSOR = (1 << 63) - 1
MAX_REQUEST_BYTES = 64 * 1024
OBLIGATION_STATES = ("active", "blocked", "paused", "done", "failed", "cancelled")

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
_REQUIRED_TABLES = (
    "roles",
    "messages",
    "events",
    "obligations",
    "watchdog_runners",
    "stable_commits",
    "control_requests",
)


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

    def obligations(
        self,
        conn: sqlite3.Connection,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = _arguments(arguments, allowed=("role", "status", "limit"))
        role = _optional_filter(args, "role")
        status_filter = _optional_filter(args, "status")
        if status_filter is not None and status_filter not in OBLIGATION_STATES:
            raise ControlAPIError("status is not a supported obligation state")
        limit = _bounded_int(args, "limit", default=DEFAULT_PAGE_SIZE, minimum=1, maximum=MAX_PAGE_SIZE)
        clauses: list[str] = []
        filter_params: list[Any] = []
        if role is not None:
            clauses.append("role = ?")
            filter_params.append(role)
        if status_filter is not None:
            clauses.append("status = ?")
            filter_params.append(status_filter)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params = (
            MAX_IDENTIFIER_LENGTH,
            MAX_IDENTIFIER_LENGTH,
            MAX_IDENTIFIER_LENGTH,
            MAX_SUMMARY_LENGTH,
            MAX_FILTER_LENGTH,
            MAX_FILTER_LENGTH,
            MAX_SUMMARY_LENGTH,
            MAX_FILTER_LENGTH,
            *filter_params,
            limit + 1,
        )
        rows = conn.execute(
            f"""
            SELECT
              CASE WHEN typeof(id) = 'text' AND length(id) <= ? THEN id END AS id,
              CASE WHEN typeof(role) = 'text' AND length(role) <= ? THEN role END AS role,
              CASE WHEN typeof(status) = 'text' AND length(status) <= ? THEN status END AS status,
              CASE
                WHEN typeof(current_summary) = 'text' THEN substr(current_summary, 1, ?)
              END AS current_summary,
              CASE
                WHEN typeof(updated_at) = 'text' AND length(updated_at) <= ? THEN updated_at
              END AS updated_at,
              CASE
                WHEN typeof(next_update_at) = 'text' AND length(next_update_at) <= ? THEN next_update_at
              END AS next_update_at,
              CASE
                WHEN typeof(paused_reason) = 'text' THEN substr(paused_reason, 1, ?)
              END AS paused_reason,
              CASE
                WHEN typeof(review_at) = 'text' AND length(review_at) <= ? THEN review_at
              END AS review_at
            FROM obligations
            {where}
            ORDER BY
              CASE status
                WHEN 'blocked' THEN 0
                WHEN 'paused' THEN 1
                WHEN 'active' THEN 2
                WHEN 'failed' THEN 3
                WHEN 'done' THEN 4
                ELSE 5
              END,
              updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        truncated = len(rows) > limit
        obligations = [_external_obligation(row) for row in rows[:limit]]
        return {
            "api_version": API_VERSION,
            "kind": "obligations",
            "query": {"role": role, "status": status_filter, "limit": limit},
            "obligations": obligations,
            "truncated": truncated,
        }

    def watchdog_status(
        self,
        conn: sqlite3.Connection,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = _arguments(arguments, allowed=("name", "limit"))
        name = _optional_filter(args, "name")
        limit = _bounded_int(
            args,
            "limit",
            default=DEFAULT_PAGE_SIZE,
            minimum=1,
            maximum=MAX_PAGE_SIZE,
        )
        where = "WHERE name = ?" if name is not None else ""
        filter_params: tuple[Any, ...] = (name,) if name is not None else ()
        rows = conn.execute(
            f"""
            SELECT
              CASE WHEN typeof(name) = 'text' AND length(name) <= ? THEN name END AS name,
              CASE WHEN typeof(state) = 'text' AND length(state) <= ? THEN state END AS state,
              interval_seconds,
              CASE
                WHEN typeof(scope_role) = 'text' AND length(scope_role) <= ? THEN scope_role
              END AS scope_role,
              CASE
                WHEN typeof(notify_role) = 'text' AND length(notify_role) <= ? THEN notify_role
              END AS notify_role,
              CASE
                WHEN typeof(delivery_method) = 'text' AND length(delivery_method) <= ?
                THEN delivery_method
              END AS delivery_method,
              CASE
                WHEN typeof(updated_at) = 'text' AND length(updated_at) <= ? THEN updated_at
              END AS updated_at,
              CASE
                WHEN typeof(last_run_at) = 'text' AND length(last_run_at) <= ? THEN last_run_at
              END AS last_run_at,
              CASE
                WHEN typeof(next_run_at) = 'text' AND length(next_run_at) <= ? THEN next_run_at
              END AS next_run_at,
              last_finding_count,
              CASE
                WHEN typeof(last_finding_summary) = 'text'
                THEN substr(last_finding_summary, 1, ?)
              END AS last_finding_summary,
              CASE WHEN typeof(last_error) = 'text' THEN substr(last_error, 1, ?) END AS last_error
            FROM watchdog_runners
            {where}
            ORDER BY updated_at DESC, name
            LIMIT ?
            """,
            (
                MAX_IDENTIFIER_LENGTH,
                MAX_IDENTIFIER_LENGTH,
                MAX_IDENTIFIER_LENGTH,
                MAX_IDENTIFIER_LENGTH,
                MAX_IDENTIFIER_LENGTH,
                MAX_FILTER_LENGTH,
                MAX_FILTER_LENGTH,
                MAX_FILTER_LENGTH,
                MAX_SUMMARY_LENGTH,
                MAX_SUMMARY_LENGTH,
                *filter_params,
                limit + 1,
            ),
        ).fetchall()
        return {
            "api_version": API_VERSION,
            "kind": "watchdog-status",
            "query": {"name": name, "limit": limit},
            "watchdogs": [_external_watchdog(row) for row in rows[:limit]],
            "truncated": len(rows) > limit,
        }

    def evidence(
        self,
        conn: sqlite3.Connection,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = _arguments(arguments, allowed=("limit",))
        limit = _bounded_int(
            args,
            "limit",
            default=DEFAULT_PAGE_SIZE,
            minimum=1,
            maximum=MAX_PAGE_SIZE,
        )
        conn.execute("BEGIN")
        try:
            stable_rows = conn.execute(
                """
                SELECT
                  CASE WHEN typeof(scope) = 'text' AND length(scope) <= ? THEN scope END AS scope,
                  CASE
                    WHEN typeof(commit_sha) = 'text' AND length(commit_sha) <= ? THEN commit_sha
                  END AS commit_sha,
                  CASE
                    WHEN typeof(approved_by) = 'text' AND length(approved_by) <= ? THEN approved_by
                  END AS approved_by,
                  CASE
                    WHEN typeof(approved_at) = 'text' AND length(approved_at) <= ? THEN approved_at
                  END AS approved_at,
                  CASE WHEN typeof(note) = 'text' THEN substr(note, 1, ?) END AS note
                FROM stable_commits
                ORDER BY approved_at DESC, scope
                LIMIT ?
                """,
                (
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_FILTER_LENGTH,
                    MAX_SUMMARY_LENGTH,
                    limit + 1,
                ),
            ).fetchall()
            completion_rows = conn.execute(
                """
                SELECT
                  CASE WHEN typeof(id) = 'text' AND length(id) <= ? THEN id END AS id,
                  CASE
                    WHEN typeof(recipient) = 'text' AND length(recipient) <= ? THEN recipient
                  END AS recipient,
                  CASE
                    WHEN typeof(result_status) = 'text' AND length(result_status) <= ?
                    THEN result_status
                  END AS result_status,
                  CASE
                    WHEN typeof(result_summary) = 'text' THEN substr(result_summary, 1, ?)
                  END AS result_summary,
                  CASE
                    WHEN typeof(completed_at) = 'text' AND length(completed_at) <= ?
                    THEN completed_at
                  END AS completed_at
                FROM messages
                WHERE state = 'completed' AND result_summary IS NOT NULL
                ORDER BY completed_at DESC, id
                LIMIT ?
                """,
                (
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_IDENTIFIER_LENGTH,
                    MAX_SUMMARY_LENGTH,
                    MAX_FILTER_LENGTH,
                    limit + 1,
                ),
            ).fetchall()
            cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
        finally:
            conn.rollback()
        return {
            "api_version": API_VERSION,
            "kind": "evidence",
            "snapshot": {"created_at": utc_now(), "event_cursor": cursor},
            "stable_commits": [_external_stable_commit(row) for row in stable_rows[:limit]],
            "completed_work": [_external_completion(row) for row in completion_rows[:limit]],
            "truncated": {
                "stable_commits": len(stable_rows) > limit,
                "completed_work": len(completion_rows) > limit,
            },
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


class MutationControl(ReadOnlyControl):
    """Explicitly enabled, idempotent mutation surface for a registered team."""

    def __init__(self, config: TeamConfig, *, project_id: str, actor: str):
        super().__init__(config)
        self.project_id = _validated_identifier(
            project_id,
            "project_id",
            maximum=MAX_IDENTIFIER_LENGTH,
        )
        self.actor = _validated_identifier(
            actor,
            "actor",
            maximum=MAX_IDENTIFIER_LENGTH,
        )
        self.store = Store(config)
        self.service = TeamService(self.store)

    def connect(self) -> sqlite3.Connection:
        if self.db_path.is_symlink():
            raise ControlAPIError("refusing symlinked tmux-team runtime database")
        if not self.db_path.is_file():
            raise ControlAPIError("tmux-team runtime database does not exist")
        conn = self.store.connect()
        try:
            self._validate_schema(conn)
        except Exception:
            conn.close()
            raise
        return conn

    def submit_goal(
        self,
        conn: sqlite3.Connection,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = _arguments(
            arguments,
            allowed=(
                "idempotency_key",
                "objective",
                "constraints",
                "acceptance_criteria",
            ),
        )
        idempotency_key = _required_identifier(
            args,
            "idempotency_key",
            maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        objective = _required_text(args, "objective", maximum=MAX_GOAL_LENGTH)
        constraints = _bounded_text_list(args, "constraints")
        acceptance = _bounded_text_list(args, "acceptance_criteria")
        normalized = {
            "objective": objective,
            "constraints": constraints,
            "acceptance_criteria": acceptance,
        }

        def action() -> dict[str, Any]:
            role = self.store.get_role(conn, "orchestrator")
            if role is None:
                raise ControlAPIError("team has no orchestrator role")
            if role["state"] != "active":
                raise ControlAPIError("orchestrator is not active")
            result = self.service.send_message(
                conn,
                sender="external-control",
                recipient="orchestrator",
                priority="high",
                summary=_goal_summary(objective),
                body=_goal_body(objective, constraints, acceptance),
                wake=True,
                correlation_key=f"control-goal:{idempotency_key}",
                actor=self.actor,
            )
            if result.blocked is not None:
                raise ControlAPIError("orchestrator rejected the goal")
            notification = result.notification
            return {
                "api_version": API_VERSION,
                "kind": "action",
                "action": "submit-goal",
                "state": "accepted",
                "message": {
                    "id": _safe_identifier_or_none(result.message.id),
                    "state": _safe_identifier_or_none(result.message.state),
                    "recipient": "orchestrator",
                },
                "notification": {
                    "attempted": notification is not None,
                    "accepted": bool(notification and notification.ok),
                },
            }

        return self._idempotent_mutation(
            conn,
            operation="team_control_submit_goal",
            idempotency_key=idempotency_key,
            normalized_arguments=normalized,
            action=action,
        )

    def _idempotent_mutation(
        self,
        conn: sqlite3.Connection,
        *,
        operation: str,
        idempotency_key: str,
        normalized_arguments: dict[str, Any],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "operation": operation,
                    "project_id": self.project_id,
                    "actor": self.actor,
                    "arguments": normalized_arguments,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                """
                SELECT operation, request_hash, state, result_json
                FROM control_requests
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["operation"] != operation or existing["request_hash"] != request_hash:
                    raise ControlAPIError("idempotency_key was already used for a different request")
                state = str(existing["state"])
                if state == "running":
                    conn.rollback()
                    return {
                        "api_version": API_VERSION,
                        "kind": "action",
                        "action": operation.removeprefix("team_control_").replace("_", "-"),
                        "state": "conflict",
                        "idempotency": {
                            "key": idempotency_key,
                            "state": "running",
                            "replayed": True,
                        },
                    }
                result = json.loads(str(existing["result_json"]))
                conn.rollback()
                result["idempotency"] = {
                    "key": idempotency_key,
                    "state": state,
                    "replayed": True,
                }
                return result
            conn.execute(
                """
                INSERT INTO control_requests(
                  idempotency_key, operation, project_id, actor,
                  request_hash, state, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    idempotency_key,
                    operation,
                    self.project_id,
                    self.actor,
                    request_hash,
                    utc_now(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        try:
            result = action()
        except ControlAPIError as exc:
            rejected = {
                "api_version": API_VERSION,
                "kind": "action",
                "action": operation.removeprefix("team_control_").replace("_", "-"),
                "state": "rejected",
                "error": _bounded_text(exc, MAX_SUMMARY_LENGTH),
            }
            self._complete_control_request(
                conn,
                operation=operation,
                idempotency_key=idempotency_key,
                state="rejected",
                result=rejected,
            )
            rejected["idempotency"] = {
                "key": idempotency_key,
                "state": "rejected",
                "replayed": False,
            }
            return rejected
        except (KeyError, ValueError):
            rejected = {
                "api_version": API_VERSION,
                "kind": "action",
                "action": operation.removeprefix("team_control_").replace("_", "-"),
                "state": "rejected",
                "error": "control mutation rejected",
            }
            self._complete_control_request(
                conn,
                operation=operation,
                idempotency_key=idempotency_key,
                state="rejected",
                result=rejected,
            )
            rejected["idempotency"] = {
                "key": idempotency_key,
                "state": "rejected",
                "replayed": False,
            }
            return rejected
        except Exception:
            conn.rollback()
            raise

        self._complete_control_request(
            conn,
            operation=operation,
            idempotency_key=idempotency_key,
            state="completed",
            result=result,
        )
        result["idempotency"] = {
            "key": idempotency_key,
            "state": "completed",
            "replayed": False,
        }
        return result

    def _complete_control_request(
        self,
        conn: sqlite3.Connection,
        *,
        operation: str,
        idempotency_key: str,
        state: str,
        result: dict[str, Any],
    ) -> None:
        completed_at = utc_now()
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            updated = conn.execute(
                """
                UPDATE control_requests
                SET state = ?, result_json = ?, completed_at = ?
                WHERE idempotency_key = ? AND state = 'running'
                """,
                (state, serialized, completed_at, idempotency_key),
            )
            if updated.rowcount != 1:
                raise ControlAPIError("control request is no longer running")
            self.store.record_event(
                conn,
                "control.request_completed",
                self.actor,
                idempotency_key,
                {
                    "action": operation,
                    "project_id": self.project_id,
                    "request_id": idempotency_key,
                    "result": state,
                    "completed_at": completed_at,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def list_operations(*, include_mutations: bool = False) -> list[dict[str, Any]]:
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    operations = [
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
        {
            "name": "team_control_obligations",
            "description": "List bounded field-allowlisted durable obligations.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "maxLength": MAX_FILTER_LENGTH},
                    "status": {"type": "string", "enum": list(OBLIGATION_STATES)},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_PAGE_SIZE,
                        "default": DEFAULT_PAGE_SIZE,
                    },
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
        {
            "name": "team_control_watchdog_status",
            "description": "Read bounded watchdog health without goals or process bindings.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": MAX_FILTER_LENGTH},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_PAGE_SIZE,
                        "default": DEFAULT_PAGE_SIZE,
                    },
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
        {
            "name": "team_control_evidence",
            "description": "Read bounded approved commits and completed-work summaries.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_PAGE_SIZE,
                        "default": DEFAULT_PAGE_SIZE,
                    },
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
    ]
    if include_mutations:
        operations.append(
            {
                "name": "team_control_submit_goal",
                "description": ("Submit one bounded goal to the configured orchestrator using an idempotency key."),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "idempotency_key": {
                            "type": "string",
                            "maxLength": MAX_IDEMPOTENCY_KEY_LENGTH,
                        },
                        "objective": {
                            "type": "string",
                            "maxLength": MAX_GOAL_LENGTH,
                        },
                        "constraints": {
                            "type": "array",
                            "maxItems": MAX_GOAL_ITEMS,
                            "items": {
                                "type": "string",
                                "maxLength": MAX_SUMMARY_LENGTH,
                            },
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "maxItems": MAX_GOAL_ITEMS,
                            "items": {
                                "type": "string",
                                "maxLength": MAX_SUMMARY_LENGTH,
                            },
                        },
                    },
                    "required": ["idempotency_key", "objective"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
        )
    return operations


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
    if name == "team_control_obligations":
        return control.obligations(conn, arguments)
    if name == "team_control_watchdog_status":
        return control.watchdog_status(conn, arguments)
    if name == "team_control_evidence":
        return control.evidence(conn, arguments)
    if name == "team_control_submit_goal" and isinstance(control, MutationControl):
        return control.submit_goal(conn, arguments)
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
                "operations": list_operations(include_mutations=isinstance(control, MutationControl)),
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
    allow_mutations: bool = False,
    project_id: str | None = None,
    actor: str | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    config = load_config(config_path, runtime_dir)
    if allow_mutations:
        if project_id is None:
            raise ControlAPIError("--project-id is required with --allow-mutations")
        if actor is None:
            raise ControlAPIError("--actor is required with --allow-mutations")
        control: ReadOnlyControl = MutationControl(
            config,
            project_id=project_id,
            actor=actor,
        )
    else:
        if project_id is not None or actor is not None:
            raise ControlAPIError("--project-id and --actor require --allow-mutations")
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


def _validated_identifier(value: Any, key: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ControlAPIError(f"{key} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ControlAPIError(f"{key} must not be empty")
    if len(normalized) > maximum:
        raise ControlAPIError(f"{key} exceeds maximum length {maximum}")
    if _SAFE_IDENTIFIER.fullmatch(normalized) is None:
        raise ControlAPIError(f"{key} contains unsupported characters")
    return normalized


def _required_identifier(
    arguments: dict[str, Any],
    key: str,
    *,
    maximum: int,
) -> str:
    if key not in arguments:
        raise ControlAPIError(f"missing required argument: {key}")
    return _validated_identifier(arguments[key], key, maximum=maximum)


def _required_text(arguments: dict[str, Any], key: str, *, maximum: int) -> str:
    if key not in arguments:
        raise ControlAPIError(f"missing required argument: {key}")
    value = arguments[key]
    if not isinstance(value, str):
        raise ControlAPIError(f"{key} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ControlAPIError(f"{key} must not be empty")
    if len(normalized) > maximum:
        raise ControlAPIError(f"{key} exceeds maximum length {maximum}")
    return normalized


def _bounded_text_list(arguments: dict[str, Any], key: str) -> list[str]:
    value = arguments.get(key, [])
    if not isinstance(value, list):
        raise ControlAPIError(f"{key} must be an array")
    if len(value) > MAX_GOAL_ITEMS:
        raise ControlAPIError(f"{key} exceeds maximum items {MAX_GOAL_ITEMS}")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ControlAPIError(f"{key} items must be strings")
        normalized = item.strip()
        if not normalized:
            raise ControlAPIError(f"{key} items must not be empty")
        if len(normalized) > MAX_SUMMARY_LENGTH:
            raise ControlAPIError(f"{key} item exceeds maximum length {MAX_SUMMARY_LENGTH}")
        items.append(normalized)
    return items


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


def _external_obligation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": _safe_identifier_or_none(row["id"]),
        "role": _safe_identifier_or_none(row["role"]),
        "status": _safe_identifier_or_none(row["status"]),
        "current_summary": _bounded_text(row["current_summary"], MAX_SUMMARY_LENGTH),
        "updated_at": _bounded_text(row["updated_at"], MAX_FILTER_LENGTH),
        "next_update_at": _bounded_text(row["next_update_at"], MAX_FILTER_LENGTH) or None,
        "paused_reason": _bounded_text(row["paused_reason"], MAX_SUMMARY_LENGTH) or None,
        "review_at": _bounded_text(row["review_at"], MAX_FILTER_LENGTH) or None,
    }


def _external_watchdog(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": _safe_identifier_or_none(row["name"]),
        "state": _safe_identifier_or_none(row["state"]),
        "interval_seconds": int(row["interval_seconds"]),
        "scope_role": _safe_identifier_or_none(row["scope_role"]),
        "notify_role": _safe_identifier_or_none(row["notify_role"]),
        "delivery_method": _safe_identifier_or_none(row["delivery_method"]),
        "updated_at": _bounded_text(row["updated_at"], MAX_FILTER_LENGTH),
        "last_run_at": _bounded_text(row["last_run_at"], MAX_FILTER_LENGTH) or None,
        "next_run_at": _bounded_text(row["next_run_at"], MAX_FILTER_LENGTH) or None,
        "last_finding_count": int(row["last_finding_count"]),
        "last_finding_summary": (_bounded_text(row["last_finding_summary"], MAX_SUMMARY_LENGTH) or None),
        "last_error": _bounded_text(row["last_error"], MAX_SUMMARY_LENGTH) or None,
    }


def _external_stable_commit(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "scope": _safe_identifier_or_none(row["scope"]),
        "commit": _safe_identifier_or_none(row["commit_sha"]),
        "approved_by": _safe_identifier_or_none(row["approved_by"]),
        "approved_at": _bounded_text(row["approved_at"], MAX_FILTER_LENGTH),
        "note": _bounded_text(row["note"], MAX_SUMMARY_LENGTH) or None,
    }


def _external_completion(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": _safe_identifier_or_none(row["id"]),
        "role": _safe_identifier_or_none(row["recipient"]),
        "status": _safe_identifier_or_none(row["result_status"]),
        "summary": _bounded_text(row["result_summary"], MAX_SUMMARY_LENGTH),
        "completed_at": _bounded_text(row["completed_at"], MAX_FILTER_LENGTH),
    }


def _goal_summary(objective: str) -> str:
    one_line = " ".join(objective.split())
    return _bounded_text(f"External goal: {one_line}", MAX_SUMMARY_LENGTH)


def _goal_body(
    objective: str,
    constraints: list[str],
    acceptance_criteria: list[str],
) -> str:
    sections = ["Objective", objective]
    if constraints:
        sections.extend(
            [
                "",
                "Constraints",
                *(f"- {item}" for item in constraints),
            ]
        )
    if acceptance_criteria:
        sections.extend(
            [
                "",
                "Acceptance criteria",
                *(f"- {item}" for item in acceptance_criteria),
            ]
        )
    return "\n".join(sections).rstrip() + "\n"


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
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="Explicitly enable the bounded mutation catalog.",
    )
    parser.add_argument(
        "--project-id",
        help="Opaque project identifier recorded with mutation audit events.",
    )
    parser.add_argument(
        "--actor",
        help="Bounded external actor identifier recorded with mutation audit events.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return serve_stdio(
            config_path=args.config,
            runtime_dir=args.runtime_dir,
            allow_mutations=args.allow_mutations,
            project_id=args.project_id,
            actor=args.actor,
        )
    except (ConfigError, ControlAPIError, OSError, sqlite3.Error) as exc:
        print(f"tmux-team-control: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
