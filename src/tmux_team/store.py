from __future__ import annotations

import json
import secrets
import shlex
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .app_server import AppServerError, submit_app_server_wake
from .config import RoleConfig, TeamConfig

ROLE_STATES = ("active", "paused", "draining", "retired", "failed")
MESSAGE_ACTIVE_STATES = ("queued", "notified", "retrying")
STALE_CLAIMED_STATE = "stale_claimed"
PENDING_MESSAGE_STATE = "pending"
CLAIMABLE_STATES = MESSAGE_ACTIVE_STATES
MESSAGE_STORED_STATES = (
    MESSAGE_ACTIVE_STATES
    + (
        "claimed",
        "acknowledged",
        "completed",
    )
    + tuple(f"blocked_by_role_{state}" for state in ROLE_STATES)
)
MESSAGE_STATE_FILTERS = (PENDING_MESSAGE_STATE, STALE_CLAIMED_STATE) + MESSAGE_STORED_STATES
OBLIGATION_ACTIVE_STATES = ("active", "blocked")
OBLIGATION_PAUSED_STATE = "paused"
OBLIGATION_VISIBLE_STATES = OBLIGATION_ACTIVE_STATES + (OBLIGATION_PAUSED_STATE,)
OBLIGATION_STATES = OBLIGATION_VISIBLE_STATES + ("done", "failed", "cancelled")
TODO_STATES = ("open", "done", "superseded")
WATCHDOG_RUNNER_STATES = ("running", "paused", "stopped", "failed")
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
SCHEMA_VERSION = 9


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    recipient: str
    priority: str
    summary: str
    body_path: Path
    state: str
    created_at: str
    updated_at: str
    correlation_key: str | None = None
    related_to: str | None = None
    supersedes: str | None = None
    message_kind: str = "task"


@dataclass(frozen=True)
class TmuxPaneState:
    dead: bool
    in_mode: bool
    current_command: str


class Store:
    def __init__(self, config: TeamConfig):
        self.config = config
        self.runtime_dir = config.runtime_dir
        self.db_path = self.runtime_dir / "team.sqlite"
        self.events_path = self.runtime_dir / "events.jsonl"
        self.milestones_path = self.runtime_dir / "milestones.jsonl"
        self.messages_dir = self.runtime_dir / "messages"

    def connect(self) -> sqlite3.Connection:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        self.init_schema(conn)
        self.sync_roles(conn, self.config.roles.values())
        return conn

    def init_schema(self, conn: sqlite3.Connection) -> None:
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise ValueError(f"Database schema version {current_version} is newer than supported {SCHEMA_VERSION}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS roles (
              name TEXT PRIMARY KEY,
              mode TEXT NOT NULL,
              state TEXT NOT NULL,
              pane TEXT,
              worktree TEXT,
              capabilities_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY,
              sender TEXT NOT NULL,
              recipient TEXT NOT NULL,
              priority TEXT NOT NULL,
              summary TEXT NOT NULL,
              body_path TEXT NOT NULL,
              state TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              claimed_by TEXT,
              claim_expires_at TEXT,
              acknowledged_at TEXT,
              completed_at TEXT,
              result_status TEXT,
              result_summary TEXT,
              correlation_key TEXT,
              related_to TEXT,
              supersedes TEXT,
              message_kind TEXT NOT NULL DEFAULT 'task'
            );

            CREATE INDEX IF NOT EXISTS idx_messages_recipient_state_created
              ON messages(recipient, state, created_at);

            CREATE TABLE IF NOT EXISTS notifications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              message_id TEXT,
              role TEXT NOT NULL,
              method TEXT NOT NULL,
              state TEXT NOT NULL,
              details TEXT,
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_notifications_role_created
              ON notifications(role, created_at);

            CREATE TABLE IF NOT EXISTS role_app_servers (
              role TEXT PRIMARY KEY,
              endpoint TEXT NOT NULL,
              thread_id TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stable_commits (
              scope TEXT PRIMARY KEY,
              commit_sha TEXT NOT NULL,
              approved_by TEXT NOT NULL,
              approved_at TEXT NOT NULL,
              note TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              type TEXT NOT NULL,
              actor TEXT,
              ref_id TEXT,
              payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS obligations (
              id TEXT PRIMARY KEY,
              role TEXT NOT NULL,
              status TEXT NOT NULL,
              summary TEXT NOT NULL,
              current_summary TEXT NOT NULL,
              goal TEXT,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_update_at TEXT NOT NULL,
              next_update_at TEXT,
              completed_at TEXT,
              paused_reason TEXT,
              paused_at TEXT,
              paused_by TEXT,
              review_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_obligations_role_status_updated
              ON obligations(role, status, updated_at);

            CREATE TABLE IF NOT EXISTS todos (
              id TEXT PRIMARY KEY,
              role TEXT NOT NULL,
              message_id TEXT NOT NULL,
              text TEXT NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              superseded_by TEXT,
              FOREIGN KEY(role) REFERENCES roles(name),
              FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_todos_role_message_state_created
              ON todos(role, message_id, state, created_at);

            CREATE TABLE IF NOT EXISTS watchdog_runners (
              name TEXT PRIMARY KEY,
              state TEXT NOT NULL,
              interval_seconds INTEGER NOT NULL,
              scope_role TEXT,
              description TEXT,
              goal TEXT,
              notify_role TEXT,
              delivery_method TEXT NOT NULL,
              pane TEXT,
              window TEXT,
              process_id INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_run_at TEXT,
              next_run_at TEXT,
              last_finding_count INTEGER NOT NULL DEFAULT 0,
              last_finding_summary TEXT,
              last_error TEXT,
              paused_reason TEXT,
              paused_at TEXT,
              paused_by TEXT,
              review_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_watchdog_runners_state_updated
              ON watchdog_runners(state, updated_at);
            """
        )
        self._ensure_column(conn, "messages", "correlation_key", "TEXT")
        self._ensure_column(conn, "messages", "related_to", "TEXT")
        self._ensure_column(conn, "messages", "supersedes", "TEXT")
        self._ensure_column(conn, "messages", "message_kind", "TEXT NOT NULL DEFAULT 'task'")
        self._ensure_column(conn, "obligations", "goal", "TEXT")
        for column in ("paused_reason", "paused_at", "paused_by", "review_at"):
            self._ensure_column(conn, "obligations", column, "TEXT")
        self._migrate_watches_to_obligations(conn)
        for column in ("description", "goal", "notify_role"):
            self._ensure_column(conn, "watchdog_runners", column, "TEXT")
        for column in ("paused_reason", "paused_at", "paused_by", "review_at"):
            self._ensure_column(conn, "watchdog_runners", column, "TEXT")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    def _migrate_watches_to_obligations(self, conn: sqlite3.Connection) -> None:
        if not self._table_exists(conn, "watches"):
            return
        for column in ("paused_reason", "paused_at", "paused_by", "review_at"):
            self._ensure_column(conn, "watches", column, "TEXT")
        conn.execute(
            """
            INSERT OR IGNORE INTO obligations(
              id, role, status, summary, current_summary, goal, created_by, created_at,
              updated_at, last_update_at, next_update_at, completed_at, paused_reason,
              paused_at, paused_by, review_at
            )
            SELECT
              id, role, status, summary, current_summary, NULL, created_by, created_at,
              updated_at, last_update_at, next_update_at, completed_at, paused_reason,
              paused_at, paused_by, review_at
            FROM watches
            """
        )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def sync_roles(self, conn: sqlite3.Connection, roles: Iterable[RoleConfig]) -> None:
        now = utc_now()
        for role in roles:
            existing = conn.execute("SELECT name FROM roles WHERE name = ?", (role.name,)).fetchone()
            capabilities_json = json.dumps(role.capabilities, sort_keys=True)
            if existing:
                conn.execute(
                    """
                    UPDATE roles
                    SET mode = ?, pane = ?, worktree = ?, capabilities_json = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    (role.mode, role.pane, role.worktree, capabilities_json, now, role.name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO roles(name, mode, state, pane, worktree, capabilities_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (role.name, role.mode, role.state, role.pane, role.worktree, capabilities_json, now, now),
                )
                self.record_event(conn, "role.created", "config", role.name, {"state": role.state, "mode": role.mode})
        conn.commit()

    def create_message(
        self,
        conn: sqlite3.Connection,
        *,
        sender: str,
        recipient: str,
        priority: str,
        summary: str,
        body: str,
        state: str = "queued",
        correlation_key: str | None = None,
        related_to: str | None = None,
        supersedes: str | None = None,
        message_kind: str = "task",
    ) -> Message:
        now = utc_now()
        message_id = new_message_id()
        body_path = self.messages_dir / f"{message_id}.md"
        body_path.write_text(body, encoding="utf-8")
        conn.execute(
            """
            INSERT INTO messages(
              id, sender, recipient, priority, summary, body_path, state, created_at, updated_at,
              correlation_key, related_to, supersedes, message_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                sender,
                recipient,
                normalize_priority(priority),
                summary,
                str(body_path),
                state,
                now,
                now,
                empty_to_none(correlation_key),
                empty_to_none(related_to),
                empty_to_none(supersedes),
                normalize_message_kind(message_kind),
            ),
        )
        self.record_event(
            conn,
            "message.created",
            sender,
            message_id,
            {
                "to": recipient,
                "priority": normalize_priority(priority),
                "summary": summary,
                "state": state,
                "correlation_key": empty_to_none(correlation_key),
                "related_to": empty_to_none(related_to),
                "supersedes": empty_to_none(supersedes),
                "message_kind": normalize_message_kind(message_kind),
            },
        )
        conn.commit()
        return Message(
            message_id,
            sender,
            recipient,
            normalize_priority(priority),
            summary,
            body_path,
            state,
            now,
            now,
            empty_to_none(correlation_key),
            empty_to_none(related_to),
            empty_to_none(supersedes),
            normalize_message_kind(message_kind),
        )

    def get_role(self, conn: sqlite3.Connection, role: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM roles WHERE name = ?", (role,)).fetchone()

    def set_role_state(self, conn: sqlite3.Connection, role: str, state: str, actor: str = "operator") -> None:
        if state not in ROLE_STATES:
            raise ValueError(f"Invalid role state: {state}")
        now = utc_now()
        result = conn.execute("UPDATE roles SET state = ?, updated_at = ? WHERE name = ?", (state, now, role))
        if result.rowcount == 0:
            raise KeyError(f"Unknown role: {role}")
        self.record_event(conn, "role.state_changed", actor, role, {"state": state})
        conn.commit()

    def list_roles(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM roles ORDER BY name"))

    def bind_role_app_server(self, conn: sqlite3.Connection, role: str, endpoint: str, thread_id: str) -> None:
        if self.get_role(conn, role) is None:
            raise KeyError(f"Unknown role: {role}")
        if not endpoint:
            raise ValueError("app-server endpoint is required")
        if not thread_id:
            raise ValueError("Codex thread id is required")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO role_app_servers(role, endpoint, thread_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(role) DO UPDATE SET
              endpoint = excluded.endpoint,
              thread_id = excluded.thread_id,
              updated_at = excluded.updated_at
            """,
            (role, endpoint, thread_id, now),
        )
        self.record_event(
            conn, "role.app_server_bound", "operator", role, {"endpoint": endpoint, "thread_id": thread_id}
        )
        conn.commit()

    def get_role_app_server(self, conn: sqlite3.Connection, role: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM role_app_servers WHERE role = ?", (role,)).fetchone()

    def list_messages(
        self,
        conn: sqlite3.Connection,
        *,
        role: str | None = None,
        states: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if role:
            clauses.append("recipient = ?")
            params.append(role)
        now = utc_now()
        if states:
            state_clause, state_params = message_state_filter_clause(states, now=now)
            clauses.append(state_clause)
            params.extend(state_params)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            conn.execute(
                f"""
                SELECT
                  *,
                  CASE
                    WHEN state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                    THEN '{STALE_CLAIMED_STATE}'
                    ELSE state
                  END AS display_state
                FROM messages
                {where}
                ORDER BY
                  CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2
                    ELSE 3
                  END,
                  created_at
                LIMIT ?
                """,
                (now, *params),
            )
        )

    def list_reclaimable_messages(
        self,
        conn: sqlite3.Connection,
        *,
        role: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses = ["state = 'claimed'", "claim_expires_at IS NOT NULL", "claim_expires_at <= ?"]
        params: list[Any] = [utc_now()]
        if role:
            clauses.append("recipient = ?")
            params.append(role)
        params.append(limit)
        return list(
            conn.execute(
                f"""
                SELECT *, '{STALE_CLAIMED_STATE}' AS display_state
                FROM messages
                WHERE {" AND ".join(clauses)}
                ORDER BY
                  CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2
                    ELSE 3
                  END,
                  claim_expires_at,
                  created_at
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def list_active_messages(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        limit: int = 3,
    ) -> list[sqlite3.Row]:
        now = utc_now()
        return list(
            conn.execute(
                f"""
                SELECT
                  *,
                  CASE
                    WHEN state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                    THEN '{STALE_CLAIMED_STATE}'
                    ELSE state
                  END AS display_state
                FROM messages
                WHERE recipient = ?
                  AND state IN ('queued', 'notified', 'retrying', 'claimed', 'acknowledged')
                ORDER BY
                  CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2
                    ELSE 3
                  END,
                  created_at
                LIMIT ?
                """,
                (now, role, limit),
            )
        )

    def list_in_progress_messages(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        limit: int = 10,
    ) -> list[sqlite3.Row]:
        now = utc_now()
        return list(
            conn.execute(
                f"""
                SELECT
                  *,
                  CASE
                    WHEN state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                    THEN '{STALE_CLAIMED_STATE}'
                    ELSE state
                  END AS display_state
                FROM messages
                WHERE recipient = ?
                  AND state IN ('claimed', 'acknowledged')
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (now, role, limit),
            )
        )

    def add_todo(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        message_id: str,
        text: str,
        actor: str = "operator",
    ) -> sqlite3.Row:
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("todo text is required")
        message = self._message_for_role(conn, role, message_id)
        self._require_message_state(message, "add todo for", ("claimed", "acknowledged"))
        now = utc_now()
        todo_id = new_todo_id()
        conn.execute(
            """
            INSERT INTO todos(id, role, message_id, text, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?)
            """,
            (todo_id, role, message_id, normalized_text, now, now),
        )
        self.record_event(
            conn,
            "todo.created",
            actor,
            todo_id,
            {"role": role, "message_id": message_id, "text": normalized_text},
        )
        conn.commit()
        return self.get_todo(conn, todo_id)

    def list_todos(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        message_id: str | None = None,
        states: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        if states:
            invalid = tuple(state for state in states if state not in TODO_STATES)
            if invalid:
                raise ValueError(f"Invalid todo state: {invalid[0]}")
        clauses = ["role = ?"]
        params: list[Any] = [role]
        if message_id is not None:
            clauses.append("message_id = ?")
            params.append(message_id)
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        params.append(limit)
        return list(
            conn.execute(
                f"""
                SELECT * FROM todos
                WHERE {" AND ".join(clauses)}
                ORDER BY
                  CASE state
                    WHEN 'open' THEN 0
                    WHEN 'done' THEN 1
                    ELSE 2
                  END,
                  created_at
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def get_todo(self, conn: sqlite3.Connection, todo_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown todo: {todo_id}")
        return row

    def complete_todo(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        todo_id: str,
        actor: str = "operator",
    ) -> sqlite3.Row:
        row = self._todo_for_role(conn, role, todo_id)
        if row["state"] == "superseded":
            raise ValueError(f"Cannot complete todo {todo_id} in state superseded")
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE todos
            SET state = 'done', updated_at = ?, completed_at = COALESCE(completed_at, ?)
            WHERE id = ? AND role = ? AND state IN ('open', 'done')
            RETURNING *
            """,
            (now, now, todo_id, role),
        ).fetchone()
        if updated is None:
            current = self._todo_for_role(conn, role, todo_id)
            raise ValueError(f"Cannot complete todo {todo_id} in state {current['state']}")
        self.record_event(
            conn,
            "todo.completed",
            actor,
            todo_id,
            {"role": role, "message_id": updated["message_id"], "text": updated["text"]},
        )
        conn.commit()
        return updated

    def reopen_todo(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        todo_id: str,
        actor: str = "operator",
    ) -> sqlite3.Row:
        row = self._todo_for_role(conn, role, todo_id)
        if row["state"] == "superseded":
            raise ValueError(f"Cannot reopen todo {todo_id} in state superseded")
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE todos
            SET state = 'open', updated_at = ?, completed_at = NULL
            WHERE id = ? AND role = ? AND state IN ('open', 'done')
            RETURNING *
            """,
            (now, todo_id, role),
        ).fetchone()
        if updated is None:
            current = self._todo_for_role(conn, role, todo_id)
            raise ValueError(f"Cannot reopen todo {todo_id} in state {current['state']}")
        self.record_event(
            conn,
            "todo.reopened",
            actor,
            todo_id,
            {"role": role, "message_id": updated["message_id"], "text": updated["text"]},
        )
        conn.commit()
        return updated

    def supersede_todo(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        todo_id: str,
        replacement_text: str,
        actor: str = "operator",
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        row = self._todo_for_role(conn, role, todo_id)
        if row["state"] != "open":
            raise ValueError(f"Cannot supersede todo {todo_id} in state {row['state']}; expected open")
        normalized_text = replacement_text.strip()
        if not normalized_text:
            raise ValueError("replacement todo text is required")
        now = utc_now()
        replacement_id = new_todo_id()
        conn.execute(
            """
            INSERT INTO todos(id, role, message_id, text, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?)
            """,
            (replacement_id, role, row["message_id"], normalized_text, now, now),
        )
        updated = conn.execute(
            """
            UPDATE todos
            SET state = 'superseded',
                updated_at = ?,
                completed_at = ?,
                superseded_by = ?
            WHERE id = ? AND role = ? AND state = 'open'
            RETURNING *
            """,
            (now, now, replacement_id, todo_id, role),
        ).fetchone()
        if updated is None:
            current = self._todo_for_role(conn, role, todo_id)
            raise ValueError(f"Cannot supersede todo {todo_id} in state {current['state']}")
        self.record_event(
            conn,
            "todo.superseded",
            actor,
            todo_id,
            {
                "role": role,
                "message_id": row["message_id"],
                "text": row["text"],
                "superseded_by": replacement_id,
                "replacement_text": normalized_text,
            },
        )
        self.record_event(
            conn,
            "todo.created",
            actor,
            replacement_id,
            {
                "role": role,
                "message_id": row["message_id"],
                "text": normalized_text,
                "supersedes": todo_id,
            },
        )
        conn.commit()
        return updated, self.get_todo(conn, replacement_id)

    def clear_todos(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        message_id: str,
        actor: str = "operator",
    ) -> int:
        self._message_for_role(conn, role, message_id)
        deleted = conn.execute("DELETE FROM todos WHERE role = ? AND message_id = ?", (role, message_id)).rowcount
        self.record_event(conn, "todo.cleared", actor, message_id, {"role": role, "deleted": deleted})
        conn.commit()
        return int(deleted)

    def open_todo_count(self, conn: sqlite3.Connection, *, role: str, message_id: str) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM todos WHERE role = ? AND message_id = ? AND state IN ('open')",
                (role, message_id),
            ).fetchone()[0]
        )

    def open_todo_counts(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        message_ids: Iterable[str],
    ) -> dict[str, int]:
        ids = tuple(dict.fromkeys(message_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT message_id, COUNT(*) AS count
            FROM todos
            WHERE role = ?
              AND state = 'open'
              AND message_id IN ({placeholders})
            GROUP BY message_id
            """,
            (role, *ids),
        ).fetchall()
        return {row["message_id"]: int(row["count"]) for row in rows}

    def find_duplicate_messages(
        self,
        conn: sqlite3.Connection,
        *,
        recipient: str,
        summary: str,
        correlation_key: str | None = None,
        limit: int = 5,
    ) -> list[sqlite3.Row]:
        normalized_summary = normalize_summary(summary)
        clauses = [
            "recipient = ?",
            "message_kind = 'task'",
            "state IN ('queued', 'notified', 'retrying', 'claimed', 'acknowledged')",
        ]
        params: list[Any] = [recipient]
        match_clauses: list[str] = []
        if correlation_key:
            match_clauses.append("correlation_key = ?")
            params.append(correlation_key)
        if normalized_summary:
            match_clauses.append("LOWER(TRIM(summary)) = ?")
            params.append(normalized_summary)
        if not match_clauses:
            return []
        clauses.append(f"({' OR '.join(match_clauses)})")
        params.append(limit)
        return list(
            conn.execute(
                f"""
                SELECT * FROM messages
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def start_obligation(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        summary: str,
        goal: str | None,
        created_by: str,
        next_update_at: str | None = None,
    ) -> sqlite3.Row:
        if self.get_role(conn, role) is None:
            raise KeyError(f"Unknown role: {role}")
        now = utc_now()
        obligation_id = new_obligation_id()
        conn.execute(
            """
            INSERT INTO obligations(
              id, role, status, summary, current_summary, goal,
              created_by, created_at, updated_at, last_update_at, next_update_at
            )
            VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (obligation_id, role, summary, summary, empty_to_none(goal), created_by, now, now, now, next_update_at),
        )
        self.record_event(
            conn,
            "obligation.started",
            created_by,
            obligation_id,
            {"role": role, "summary": summary, "goal": empty_to_none(goal), "next_update_at": next_update_at},
        )
        conn.commit()
        return self.get_obligation(conn, obligation_id)

    def update_obligation(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        obligation_id: str,
        summary: str,
        status: str = "active",
        next_update_at: str | None = None,
        actor: str = "operator",
    ) -> sqlite3.Row:
        if status not in OBLIGATION_ACTIVE_STATES:
            raise ValueError(f"Invalid active obligation status: {status}")
        row = self._obligation_for_role(conn, role, obligation_id)
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE obligations
            SET status = ?,
                current_summary = ?,
                updated_at = ?,
                last_update_at = ?,
                next_update_at = ?
            WHERE id = ? AND role = ? AND status IN ('active', 'blocked')
            RETURNING *
            """,
            (status, summary, now, now, next_update_at, obligation_id, role),
        ).fetchone()
        if updated is None:
            raise ValueError(f"Cannot update obligation {obligation_id} in state {row['status']}")
        self.record_event(
            conn,
            "obligation.updated",
            actor,
            obligation_id,
            {"role": role, "status": status, "summary": summary, "next_update_at": next_update_at},
        )
        conn.commit()
        return updated

    def pause_obligation(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        obligation_id: str,
        reason: str,
        review_at: str | None = None,
        actor: str = "operator",
    ) -> sqlite3.Row:
        row = self._obligation_for_role(conn, role, obligation_id)
        if row["status"] not in OBLIGATION_VISIBLE_STATES:
            raise ValueError(f"Cannot pause obligation {obligation_id} in state {row['status']}")
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE obligations
            SET status = 'paused',
                updated_at = ?,
                next_update_at = NULL,
                paused_reason = ?,
                paused_at = ?,
                paused_by = ?,
                review_at = ?
            WHERE id = ? AND role = ?
            RETURNING *
            """,
            (now, reason, now, actor, review_at, obligation_id, role),
        ).fetchone()
        if updated is None:
            raise KeyError(f"Unknown obligation: {obligation_id}")
        self.record_event(
            conn,
            "obligation.paused",
            actor,
            obligation_id,
            {
                "role": role,
                "reason": reason,
                "review_at": review_at,
                "previous_status": row["status"],
                "previous_summary": row["current_summary"],
            },
        )
        conn.commit()
        return updated

    def resume_obligation(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        obligation_id: str,
        summary: str,
        next_update_at: str | None = None,
        actor: str = "operator",
    ) -> sqlite3.Row:
        row = self._obligation_for_role(conn, role, obligation_id)
        if row["status"] != OBLIGATION_PAUSED_STATE:
            raise ValueError(f"Cannot resume obligation {obligation_id} in state {row['status']}")
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE obligations
            SET status = 'active',
                current_summary = ?,
                updated_at = ?,
                last_update_at = ?,
                next_update_at = ?,
                completed_at = NULL,
                paused_reason = NULL,
                paused_at = NULL,
                paused_by = NULL,
                review_at = NULL
            WHERE id = ? AND role = ? AND status = 'paused'
            RETURNING *
            """,
            (summary, now, now, next_update_at, obligation_id, role),
        ).fetchone()
        if updated is None:
            raise ValueError(f"Cannot resume obligation {obligation_id} in state {row['status']}")
        self.record_event(
            conn,
            "obligation.resumed",
            actor,
            obligation_id,
            {
                "role": role,
                "summary": summary,
                "next_update_at": next_update_at,
                "paused_reason": row["paused_reason"],
                "paused_at": row["paused_at"],
            },
        )
        conn.commit()
        return updated

    def complete_obligation(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        obligation_id: str,
        status: str,
        summary: str,
        actor: str = "operator",
    ) -> sqlite3.Row:
        if status not in ("done", "failed", "cancelled"):
            raise ValueError(f"Invalid terminal obligation status: {status}")
        row = self._obligation_for_role(conn, role, obligation_id)
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE obligations
            SET status = ?,
                current_summary = ?,
                updated_at = ?,
                last_update_at = ?,
                completed_at = ?,
                next_update_at = NULL,
                paused_reason = NULL,
                paused_at = NULL,
                paused_by = NULL,
                review_at = NULL
            WHERE id = ? AND role = ? AND status IN ('active', 'blocked', 'paused')
            RETURNING *
            """,
            (status, summary, now, now, now, obligation_id, role),
        ).fetchone()
        if updated is None:
            raise ValueError(f"Cannot complete obligation {obligation_id} in state {row['status']}")
        self.record_event(
            conn,
            "obligation.completed",
            actor,
            obligation_id,
            {"role": role, "status": status, "summary": summary},
        )
        conn.commit()
        return updated

    def list_obligations(
        self,
        conn: sqlite3.Connection,
        *,
        role: str | None = None,
        states: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if role:
            clauses.append("role = ?")
            params.append(role)
        if states:
            invalid = tuple(state for state in states if state not in OBLIGATION_STATES)
            if invalid:
                raise ValueError(f"Invalid obligation state: {invalid[0]}")
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"status IN ({placeholders})")
            params.extend(states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            conn.execute(
                f"""
                SELECT * FROM obligations
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
                tuple(params),
            )
        )

    def upsert_watchdog_runner(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        state: str,
        interval_seconds: int,
        scope_role: str | None = None,
        description: str | None = None,
        goal: str | None = None,
        notify_role: str | None = None,
        delivery_method: str = "report-only",
        pane: str | None = None,
        window: str | None = None,
        process_id: int | None = None,
        next_run_at: str | None = None,
        actor: str = "operator",
    ) -> sqlite3.Row:
        normalized_name = normalize_watchdog_runner_name(name)
        if state not in WATCHDOG_RUNNER_STATES:
            raise ValueError(f"Invalid watchdog runner state: {state}")
        if interval_seconds <= 0:
            raise ValueError("watchdog runner interval_seconds must be positive")
        if scope_role and self.get_role(conn, scope_role) is None:
            raise KeyError(f"Unknown role: {scope_role}")
        if notify_role and self.get_role(conn, notify_role) is None:
            raise KeyError(f"Unknown notify role: {notify_role}")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO watchdog_runners(
              name, state, interval_seconds, scope_role, description, goal, notify_role,
              delivery_method, pane, window, process_id,
              created_at, updated_at, next_run_at, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(name) DO UPDATE SET
              state = excluded.state,
              interval_seconds = excluded.interval_seconds,
              scope_role = excluded.scope_role,
              description = excluded.description,
              goal = excluded.goal,
              notify_role = excluded.notify_role,
              delivery_method = excluded.delivery_method,
              pane = COALESCE(excluded.pane, watchdog_runners.pane),
              window = COALESCE(excluded.window, watchdog_runners.window),
              process_id = COALESCE(excluded.process_id, watchdog_runners.process_id),
              updated_at = excluded.updated_at,
              next_run_at = excluded.next_run_at,
              last_error = NULL,
              paused_reason = CASE WHEN excluded.state = 'paused' THEN watchdog_runners.paused_reason ELSE NULL END,
              paused_at = CASE WHEN excluded.state = 'paused' THEN watchdog_runners.paused_at ELSE NULL END,
              paused_by = CASE WHEN excluded.state = 'paused' THEN watchdog_runners.paused_by ELSE NULL END,
              review_at = CASE WHEN excluded.state = 'paused' THEN watchdog_runners.review_at ELSE NULL END
            """,
            (
                normalized_name,
                state,
                interval_seconds,
                empty_to_none(scope_role),
                empty_to_none(description),
                empty_to_none(goal),
                empty_to_none(notify_role),
                delivery_method,
                empty_to_none(pane),
                empty_to_none(window),
                process_id,
                now,
                now,
                next_run_at,
            ),
        )
        self.record_event(
            conn,
            "watchdog.runner_upserted",
            actor,
            normalized_name,
            {
                "state": state,
                "interval_seconds": interval_seconds,
                "scope_role": empty_to_none(scope_role),
                "description": empty_to_none(description),
                "goal": empty_to_none(goal),
                "notify_role": empty_to_none(notify_role),
                "delivery_method": delivery_method,
                "pane": empty_to_none(pane),
                "window": empty_to_none(window),
                "process_id": process_id,
                "next_run_at": next_run_at,
            },
        )
        conn.commit()
        return self.get_watchdog_runner(conn, normalized_name)

    def record_watchdog_runner_run(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        interval_seconds: int,
        scope_role: str | None,
        description: str | None,
        goal: str | None,
        notify_role: str | None,
        delivery_method: str,
        pane: str | None,
        window: str | None,
        process_id: int | None,
        last_run_at: str,
        next_run_at: str | None,
        finding_count: int,
        finding_summary: str,
        actor: str = "watchdog",
    ) -> sqlite3.Row:
        normalized_name = normalize_watchdog_runner_name(name)
        if interval_seconds <= 0:
            raise ValueError("watchdog runner interval_seconds must be positive")
        existing = conn.execute("SELECT * FROM watchdog_runners WHERE name = ?", (normalized_name,)).fetchone()
        if existing is not None and existing["state"] == "paused":
            return existing
        if scope_role and self.get_role(conn, scope_role) is None:
            raise KeyError(f"Unknown role: {scope_role}")
        if notify_role and self.get_role(conn, notify_role) is None:
            raise KeyError(f"Unknown notify role: {notify_role}")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO watchdog_runners(
              name, state, interval_seconds, scope_role, description, goal, notify_role,
              delivery_method, pane, window, process_id,
              created_at, updated_at, last_run_at, next_run_at, last_finding_count,
              last_finding_summary, last_error
            )
            VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(name) DO UPDATE SET
              state = 'running',
              interval_seconds = excluded.interval_seconds,
              scope_role = excluded.scope_role,
              description = excluded.description,
              goal = excluded.goal,
              notify_role = excluded.notify_role,
              delivery_method = excluded.delivery_method,
              pane = COALESCE(excluded.pane, watchdog_runners.pane),
              window = COALESCE(excluded.window, watchdog_runners.window),
              process_id = COALESCE(excluded.process_id, watchdog_runners.process_id),
              updated_at = excluded.updated_at,
              last_run_at = excluded.last_run_at,
              next_run_at = excluded.next_run_at,
              last_finding_count = excluded.last_finding_count,
              last_finding_summary = excluded.last_finding_summary,
              last_error = NULL,
              paused_reason = NULL,
              paused_at = NULL,
              paused_by = NULL,
              review_at = NULL
            """,
            (
                normalized_name,
                interval_seconds,
                empty_to_none(scope_role),
                empty_to_none(description),
                empty_to_none(goal),
                empty_to_none(notify_role),
                delivery_method,
                empty_to_none(pane),
                empty_to_none(window),
                process_id,
                now,
                now,
                last_run_at,
                next_run_at,
                finding_count,
                finding_summary,
            ),
        )
        self.record_event(
            conn,
            "watchdog.runner_ran",
            actor,
            normalized_name,
            {
                "scope_role": empty_to_none(scope_role),
                "description": empty_to_none(description),
                "goal": empty_to_none(goal),
                "notify_role": empty_to_none(notify_role),
                "finding_count": finding_count,
                "finding_summary": finding_summary,
                "last_run_at": last_run_at,
                "next_run_at": next_run_at,
            },
        )
        conn.commit()
        return self.get_watchdog_runner(conn, normalized_name)

    def update_watchdog_runner(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        interval_seconds: int | None = None,
        scope_role: str | None = None,
        clear_scope_role: bool = False,
        description: str | None = None,
        goal: str | None = None,
        notify_role: str | None = None,
        clear_notify_role: bool = False,
        delivery_method: str | None = None,
        actor: str = "operator",
    ) -> sqlite3.Row:
        normalized_name = normalize_watchdog_runner_name(name)
        row = self.get_watchdog_runner(conn, normalized_name)
        if interval_seconds is not None and interval_seconds <= 0:
            raise ValueError("watchdog runner interval_seconds must be positive")
        if scope_role and self.get_role(conn, scope_role) is None:
            raise KeyError(f"Unknown role: {scope_role}")
        if notify_role and self.get_role(conn, notify_role) is None:
            raise KeyError(f"Unknown notify role: {notify_role}")

        next_scope_role = (
            None if clear_scope_role else empty_to_none(scope_role) if scope_role is not None else row["scope_role"]
        )
        next_notify_role = (
            None if clear_notify_role else empty_to_none(notify_role) if notify_role is not None else row["notify_role"]
        )
        next_interval = int(interval_seconds if interval_seconds is not None else row["interval_seconds"])
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE watchdog_runners
            SET interval_seconds = ?,
                scope_role = ?,
                description = COALESCE(?, description),
                goal = COALESCE(?, goal),
                notify_role = ?,
                delivery_method = COALESCE(?, delivery_method),
                updated_at = ?
            WHERE name = ? AND state NOT IN ('stopped', 'failed')
            RETURNING *
            """,
            (
                next_interval,
                next_scope_role,
                empty_to_none(description),
                empty_to_none(goal),
                next_notify_role,
                empty_to_none(delivery_method),
                now,
                normalized_name,
            ),
        ).fetchone()
        if updated is None:
            raise ValueError(f"Cannot update watchdog runner {normalized_name} in state {row['state']}")
        self.record_event(
            conn,
            "watchdog.runner_updated",
            actor,
            normalized_name,
            {
                "interval_seconds": next_interval,
                "scope_role": next_scope_role,
                "description": empty_to_none(description),
                "goal": empty_to_none(goal),
                "notify_role": next_notify_role,
                "delivery_method": empty_to_none(delivery_method),
            },
        )
        conn.commit()
        return updated

    def stop_watchdog_runner(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        state: str = "stopped",
        error: str | None = None,
        actor: str = "operator",
    ) -> sqlite3.Row:
        normalized_name = normalize_watchdog_runner_name(name)
        if state not in ("stopped", "failed"):
            raise ValueError("watchdog runner stop state must be stopped or failed")
        row = self.get_watchdog_runner(conn, normalized_name)
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE watchdog_runners
            SET state = ?,
                updated_at = ?,
                next_run_at = NULL,
                last_error = ?,
                paused_reason = NULL,
                paused_at = NULL,
                paused_by = NULL,
                review_at = NULL
            WHERE name = ?
            RETURNING *
            """,
            (state, now, empty_to_none(error), normalized_name),
        ).fetchone()
        if updated is None:
            raise KeyError(f"Unknown watchdog runner: {normalized_name}")
        self.record_event(
            conn,
            "watchdog.runner_stopped",
            actor,
            normalized_name,
            {"previous_state": row["state"], "state": state, "error": empty_to_none(error)},
        )
        conn.commit()
        return updated

    def pause_watchdog_runner(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        reason: str,
        review_at: str | None = None,
        actor: str = "operator",
    ) -> sqlite3.Row:
        normalized_name = normalize_watchdog_runner_name(name)
        row = self.get_watchdog_runner(conn, normalized_name)
        if row["state"] in ("stopped", "failed"):
            raise ValueError(f"Cannot pause watchdog runner {normalized_name} in state {row['state']}")
        now = utc_now()
        updated = conn.execute(
            """
            UPDATE watchdog_runners
            SET state = 'paused',
                updated_at = ?,
                next_run_at = ?,
                paused_reason = ?,
                paused_at = ?,
                paused_by = ?,
                review_at = ?,
                last_error = NULL
            WHERE name = ?
            RETURNING *
            """,
            (now, review_at, reason, now, actor, review_at, normalized_name),
        ).fetchone()
        if updated is None:
            raise KeyError(f"Unknown watchdog runner: {normalized_name}")
        self.record_event(
            conn,
            "watchdog.runner_paused",
            actor,
            normalized_name,
            {
                "previous_state": row["state"],
                "reason": reason,
                "review_at": review_at,
                "last_finding_summary": row["last_finding_summary"],
            },
        )
        conn.commit()
        return updated

    def resume_watchdog_runner(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        actor: str = "operator",
    ) -> sqlite3.Row:
        normalized_name = normalize_watchdog_runner_name(name)
        row = self.get_watchdog_runner(conn, normalized_name)
        if row["state"] != "paused":
            raise ValueError(f"Cannot resume watchdog runner {normalized_name} in state {row['state']}")
        now_dt = datetime.now(UTC).replace(microsecond=0)
        next_run_at = (now_dt + timedelta(seconds=int(row["interval_seconds"]))).isoformat()
        updated = conn.execute(
            """
            UPDATE watchdog_runners
            SET state = 'running',
                updated_at = ?,
                next_run_at = ?,
                paused_reason = NULL,
                paused_at = NULL,
                paused_by = NULL,
                review_at = NULL,
                last_error = NULL
            WHERE name = ? AND state = 'paused'
            RETURNING *
            """,
            (now_dt.isoformat(), next_run_at, normalized_name),
        ).fetchone()
        if updated is None:
            raise ValueError(f"Cannot resume watchdog runner {normalized_name} in state {row['state']}")
        self.record_event(
            conn,
            "watchdog.runner_resumed",
            actor,
            normalized_name,
            {
                "previous_state": row["state"],
                "paused_reason": row["paused_reason"],
                "paused_at": row["paused_at"],
                "next_run_at": next_run_at,
            },
        )
        conn.commit()
        return updated

    def get_watchdog_runner(self, conn: sqlite3.Connection, name: str) -> sqlite3.Row:
        normalized_name = normalize_watchdog_runner_name(name)
        row = conn.execute("SELECT * FROM watchdog_runners WHERE name = ?", (normalized_name,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown watchdog runner: {normalized_name}")
        return row

    def list_watchdog_runners(
        self,
        conn: sqlite3.Connection,
        *,
        name: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        if name:
            try:
                return [self.get_watchdog_runner(conn, name)]
            except KeyError:
                return []
        return list(
            conn.execute(
                """
                SELECT * FROM watchdog_runners
                ORDER BY
                  CASE state
                    WHEN 'running' THEN 0
                    WHEN 'paused' THEN 1
                    WHEN 'failed' THEN 2
                    ELSE 3
                  END,
                  updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def claim_next(self, conn: sqlite3.Connection, role: str, claim_seconds: int) -> sqlite3.Row | None:
        now = utc_now()
        claim_expires_at = (datetime.now(UTC) + timedelta(seconds=claim_seconds)).replace(microsecond=0).isoformat()
        row = conn.execute(
            """
            UPDATE messages
            SET state = 'claimed', claimed_by = ?, claim_expires_at = ?, updated_at = ?
            WHERE id = (
                SELECT id FROM messages
                WHERE recipient = ?
                  AND (
                    state IN ('queued', 'notified', 'retrying')
                    OR (state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
                  )
                ORDER BY
                  CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2
                    ELSE 3
                  END,
                  created_at
                LIMIT 1
            )
              AND recipient = ?
              AND (
                state IN ('queued', 'notified', 'retrying')
                OR (state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
              )
            RETURNING *
            """,
            (role, claim_expires_at, now, role, now, role, now),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        self.record_event(conn, "message.claimed", role, row["id"], {"claim_expires_at": claim_expires_at})
        conn.commit()
        return row

    def ack_message(self, conn: sqlite3.Connection, role: str, message_id: str) -> sqlite3.Row:
        now = utc_now()
        row = self._message_for_role(conn, role, message_id)
        self._require_message_state(row, "acknowledge", ("claimed", "acknowledged"))
        updated = conn.execute(
            """
            UPDATE messages
            SET state = 'acknowledged', acknowledged_at = ?, updated_at = ?
            WHERE id = ? AND recipient = ? AND state IN ('claimed', 'acknowledged')
            RETURNING *
            """,
            (now, now, message_id, role),
        ).fetchone()
        if updated is None:
            current = self._message_for_role(conn, role, message_id)
            self._require_message_state(current, "acknowledge", ("claimed", "acknowledged"))
            raise ValueError(f"Message {message_id} could not be acknowledged")
        self.record_event(conn, "message.acknowledged", role, message_id, {})
        conn.commit()
        return updated

    def complete_message(
        self,
        conn: sqlite3.Connection,
        role: str,
        message_id: str,
        result_status: str,
        result_summary: str,
    ) -> sqlite3.Row:
        now = utc_now()
        row = self._message_for_role(conn, role, message_id)
        self._require_message_state(row, "complete", ("claimed", "acknowledged"))
        updated = conn.execute(
            """
            UPDATE messages
            SET state = 'completed',
                completed_at = ?,
                result_status = ?,
                result_summary = ?,
                updated_at = ?
            WHERE id = ? AND recipient = ? AND state IN ('claimed', 'acknowledged')
            RETURNING *
            """,
            (now, result_status, result_summary, now, message_id, role),
        ).fetchone()
        if updated is None:
            current = self._message_for_role(conn, role, message_id)
            self._require_message_state(current, "complete", ("claimed", "acknowledged"))
            raise ValueError(f"Message {message_id} could not be completed")
        self.record_event(
            conn,
            "message.completed",
            role,
            message_id,
            {"status": result_status, "summary": result_summary, "previous_state": row["state"]},
        )
        conn.commit()
        return updated

    def complete_completion_notices(
        self,
        conn: sqlite3.Connection,
        *,
        role: str,
        result_status: str = "done",
        result_summary: str = "completion notice recorded",
        limit: int = 50,
        actor: str = "operator",
    ) -> list[sqlite3.Row]:
        now = utc_now()
        rows = list(
            conn.execute(
                """
                SELECT * FROM messages
                WHERE recipient = ?
                  AND message_kind = 'completion_notice'
                  AND state IN ('claimed', 'acknowledged')
                ORDER BY updated_at
                LIMIT ?
                """,
                (role, limit),
            )
        )
        completed: list[sqlite3.Row] = []
        for row in rows:
            updated = conn.execute(
                """
                UPDATE messages
                SET state = 'completed',
                    completed_at = ?,
                    result_status = ?,
                    result_summary = ?,
                    updated_at = ?
                WHERE id = ? AND recipient = ? AND state IN ('claimed', 'acknowledged')
                RETURNING *
                """,
                (now, result_status, result_summary, now, row["id"], role),
            ).fetchone()
            if updated is not None:
                completed.append(updated)
                self.record_event(
                    conn,
                    "message.completed",
                    actor,
                    row["id"],
                    {"status": result_status, "summary": result_summary, "previous_state": row["state"]},
                )
        conn.commit()
        return completed

    def notify_role(
        self,
        conn: sqlite3.Connection,
        role: str,
        method: str = "auto",
        *,
        notice_message_id: str | None = None,
        notice_summary: str | None = None,
    ) -> tuple[bool, str]:
        role_row = self.get_role(conn, role)
        if role_row is None:
            return False, f"unknown role: {role}"
        if method == "auto":
            method = role_notify_method(role_row)
        method = normalize_notify_method(method)
        is_notice = notice_summary is not None
        pending = self.pending_count(conn, role)
        if pending == 0 and not is_notice:
            return True, "no pending messages"

        if method == "app-server-turn":
            if is_notice:
                prompt = self.app_server_notice_prompt(role, str(notice_summary))
                return self.notify_role_app_server_prompt(
                    conn,
                    role,
                    role_row,
                    prompt=prompt,
                    message_id=notice_message_id,
                    update_queued=False,
                    event_type="role.notice_sent",
                    event_payload={"message_id": notice_message_id},
                    success_label="app-server notice",
                    failure_label="app-server notice submission failed",
                )
            return self.notify_role_app_server(conn, role, role_row, self.pending_wake_context(conn, role))

        pane = role_row["pane"]
        if not pane:
            self.record_notification(conn, notice_message_id, role, method, "notify_failed", "role has no pane")
            conn.commit()
            return False, "role has no pane"

        text = (
            f"[tmux-team notice] {notice_summary}"
            if is_notice
            else f"[tmux-team] {pending} pending message(s). Run: tmux-team inbox next --role {role}"
        )
        if method not in ("display-message", "send-keys"):
            self.record_notification(
                conn, notice_message_id, role, method, "notify_failed", f"unsupported method: {method}"
            )
            conn.commit()
            return False, f"unsupported method: {method}"
        if is_notice and method == "send-keys":
            details = "notice notification does not support method: send-keys"
            self.record_notification(conn, notice_message_id, role, method, "notify_failed", details)
            conn.commit()
            return False, details

        tmux = shutil.which("tmux")
        if tmux is None:
            self.record_notification(conn, notice_message_id, role, method, "notify_failed", "tmux not found")
            conn.commit()
            return False, "tmux not found"

        if method == "display-message":
            command = [tmux, "display-message", "-t", pane, text]
        else:
            pane_ok, pane_details = self.check_send_keys_target(tmux, pane)
            if not pane_ok:
                state = "notify_failed"
                if pane_details.startswith("notify_deferred:"):
                    state = "notify_deferred"
                self.record_notification(conn, notice_message_id, role, method, state, pane_details)
                event_type = "role.notification_deferred" if state == "notify_deferred" else "role.notification_failed"
                self.record_event(conn, event_type, "tmux", role, {"method": method, "details": pane_details})
                conn.commit()
                return False, pane_details
            wake_prompt = (
                f"You have {pending} pending tmux-team inbox message(s). "
                "Wake notice only. Claim durable work with `tmux-team inbox next`. "
                "Follow the loaded tmux-team role loop and drain until empty."
            )
            command = [tmux, "send-keys", "-t", pane, wake_prompt, "Enter"]

        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            details = text if method == "display-message" else wake_prompt
            if is_notice:
                self.record_notification(conn, notice_message_id, role, method, "notified", details)
                self.record_event(
                    conn, "role.notice_sent", "tmux", role, {"message_id": notice_message_id, "method": method}
                )
            else:
                now = utc_now()
                conn.execute(
                    """
                    UPDATE messages
                    SET state = 'notified', attempts = attempts + 1, updated_at = ?
                    WHERE recipient = ? AND state = 'queued'
                    """,
                    (now, role),
                )
                self.record_notification(conn, None, role, method, "notified", details)
                self.record_event(conn, "role.notified", "tmux", role, {"pending": pending, "method": method})
            conn.commit()
            return True, details

        details = (result.stderr or result.stdout or f"tmux exited {result.returncode}").strip()
        self.record_notification(conn, notice_message_id, role, method, "notify_failed", details)
        conn.commit()
        return False, details

    def notify_role_app_server(
        self,
        conn: sqlite3.Connection,
        role: str,
        role_row: sqlite3.Row,
        wake_context: dict[str, Any],
    ) -> tuple[bool, str]:
        pending = int(wake_context["pending"])
        prompt = self.app_server_wake_prompt(
            role,
            pending,
            top_message=wake_context.get("top_message"),
            urgent_count=int(wake_context.get("urgent_count") or 0),
        )
        return self.notify_role_app_server_prompt(
            conn,
            role,
            role_row,
            prompt=prompt,
            update_queued=True,
            event_payload={"pending": pending},
            success_label="app-server turn",
            failure_label="app-server turn submission failed",
        )

    def notify_role_app_server_prompt(
        self,
        conn: sqlite3.Connection,
        role: str,
        role_row: sqlite3.Row,
        *,
        prompt: str,
        message_id: str | None = None,
        update_queued: bool,
        event_type: str = "role.notified",
        event_payload: dict[str, Any] | None = None,
        success_label: str,
        failure_label: str,
    ) -> tuple[bool, str]:
        settings = self.resolve_role_app_server(conn, role, role_row)
        if settings is None:
            details = "role has no app-server endpoint/thread binding"
            self.record_notification(conn, message_id, role, "app-server-turn", "notify_failed", details)
            self.record_event(
                conn,
                "role.notification_failed",
                "app-server",
                role,
                {"method": "app-server-turn", "details": details, "message_id": message_id},
            )
            conn.commit()
            return False, details

        endpoint, thread_id, timeout = settings
        try:
            turn = submit_app_server_wake(
                endpoint=endpoint,
                thread_id=thread_id,
                prompt=prompt,
                client_user_message_id=f"tmux-team-{role}-{utc_now()}",
                timeout=timeout,
            )
        except (AppServerError, OSError, TimeoutError) as exc:
            details = f"{failure_label}: {exc}"
            self.record_notification(conn, message_id, role, "app-server-turn", "notify_failed", details)
            self.record_event(
                conn,
                "role.notification_failed",
                "app-server",
                role,
                {"method": "app-server-turn", "details": details, "message_id": message_id},
            )
            conn.commit()
            return False, details

        if update_queued:
            now = utc_now()
            conn.execute(
                """
                UPDATE messages
                SET state = 'notified', attempts = attempts + 1, updated_at = ?
                WHERE recipient = ? AND state = 'queued'
                """,
                (now, role),
            )
        payload = {
            "method": "app-server-turn",
            "thread_id": turn.thread_id,
            "turn_id": turn.turn_id,
        }
        if event_payload:
            payload.update(event_payload)
        details = json.dumps(
            {
                "endpoint": endpoint,
                "thread_id": turn.thread_id,
                "turn_id": turn.turn_id,
                "turn_status": turn.status,
                **(event_payload or {}),
            },
            sort_keys=True,
        )
        self.record_notification(conn, message_id, role, "app-server-turn", "submitted", details)
        self.record_event(conn, event_type, "app-server", role, payload)
        conn.commit()
        return True, f"{success_label} submitted thread={turn.thread_id} turn={turn.turn_id}"

    def resolve_role_app_server(
        self,
        conn: sqlite3.Connection,
        role: str,
        role_row: sqlite3.Row,
    ) -> tuple[str, str, float] | None:
        binding = self.get_role_app_server(conn, role)
        endpoint: str | None = None
        thread_id: str | None = None
        timeout = 10.0
        if binding is not None:
            endpoint = str(binding["endpoint"])
            thread_id = str(binding["thread_id"])

        try:
            capabilities = json.loads(role_row["capabilities_json"] or "{}")
        except json.JSONDecodeError:
            capabilities = {}
        endpoint = endpoint or _optional_capability(
            capabilities, "app_server_endpoint", "codex_app_server", "app_server"
        )
        thread_id = thread_id or _optional_capability(capabilities, "codex_thread_id", "thread_id")
        raw_timeout = capabilities.get("app_server_timeout")
        if raw_timeout is not None:
            timeout = float(raw_timeout)
        if not endpoint or not thread_id:
            return None
        return endpoint, thread_id, timeout

    def app_server_wake_prompt(
        self,
        role: str,
        pending: int,
        *,
        top_message: Any | None = None,
        urgent_count: int = 0,
    ) -> str:
        lines = [f"Inbox wake: {pending} pending message(s) for role `{role}`."]
        if top_message is not None:
            priority = str(top_message["priority"]).upper()
            display_state = str(_row_value(top_message, "display_state", top_message["state"]))
            header = (
                "URGENT tmux-team inbox message pending" if priority == "URGENT" else "tmux-team inbox message pending"
            )
            lines.extend(
                [
                    header,
                    f"From: {top_message['sender']}",
                    f"Priority: {priority}",
                    f"Summary: {truncate_wake_line(str(top_message['summary']))}",
                    f"Pending: {pending} total, {urgent_count} urgent",
                ]
            )
            if display_state == STALE_CLAIMED_STATE:
                lines.append(
                    "State: stale claimed message; reclaim it with `tmux-team inbox next` if the previous turn did not finish."
                )
            if priority == "URGENT":
                lines.append(
                    "Action: stop at the current safe point, claim this urgent message before continuing other work, then drain by priority."
                )
            else:
                lines.append("Action: claim durable inbox work now and follow the loaded role loop.")
        else:
            lines.append("Wake notice only. Claim durable inbox work now.")
            lines.append("Follow the loaded role loop and drain until empty.")
        return "\n".join(lines).rstrip() + "\n"

    def app_server_notice_prompt(self, role: str, summary: str) -> str:
        return (
            f"tmux-team notice for role `{role}`.\n"
            f"Summary: {truncate_wake_line(summary)}\n"
            "No inbox task was queued; no claim, ack, or completion is required.\n"
        )

    def cli_config_arg(self, role: str | None = None) -> str:
        if self.config.config_path is None:
            return ""
        config_path = self.config.config_path
        if role is not None:
            role_config = self.config.roles.get(role)
            if role_config is not None and role_config.worktree:
                worktree = Path(role_config.worktree).expanduser().resolve()
                try:
                    return f" --config {shlex.quote(str(config_path.relative_to(worktree)))}"
                except ValueError:
                    return f" --config {shlex.quote(str(config_path))}"
        if self.config.project_root is not None:
            try:
                config_path = config_path.relative_to(self.config.project_root)
            except ValueError:
                pass
        return f" --config {shlex.quote(str(config_path))}"

    def check_send_keys_target(self, tmux: str, pane: str) -> tuple[bool, str]:
        ok, state_or_details = inspect_tmux_pane(tmux, pane)
        if not ok:
            return False, str(state_or_details)
        state = state_or_details
        if not isinstance(state, TmuxPaneState):
            return False, "notify_failed: invalid tmux pane inspection result"
        if state.dead:
            return False, "notify_failed: pane is dead; not sending keys"
        if state.in_mode:
            return False, "notify_deferred: pane is in tmux copy/mode; not sending keys"
        return True, f"pane command={state.current_command or '-'}"

    def pending_count(self, conn: sqlite3.Connection, role: str) -> int:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE recipient = ?
                  AND (
                    state IN ('queued', 'notified', 'retrying')
                    OR (state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
                  )
                """,
                (role, utc_now()),
            ).fetchone()[0]
        )

    def pending_wake_context(self, conn: sqlite3.Connection, role: str) -> dict[str, Any]:
        now = utc_now()
        pending = self.pending_count(conn, role)
        urgent_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE recipient = ?
                  AND priority = 'urgent'
                  AND (
                    state IN ('queued', 'notified', 'retrying')
                    OR (state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
                  )
                """,
                (role, now),
            ).fetchone()[0]
        )
        top_message = conn.execute(
            """
            SELECT
              id,
              sender,
              recipient,
              priority,
              summary,
              state,
              CASE
                WHEN state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                THEN 'stale_claimed'
                ELSE state
              END AS display_state,
              created_at,
              claim_expires_at,
              claimed_by
            FROM messages
            WHERE recipient = ?
              AND (
                state IN ('queued', 'notified', 'retrying')
                OR (state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)
              )
            ORDER BY
              CASE priority
                WHEN 'urgent' THEN 0
                WHEN 'high' THEN 1
                WHEN 'normal' THEN 2
                ELSE 3
              END,
              created_at
            LIMIT 1
            """,
            (now, role, now),
        ).fetchone()
        return {"pending": pending, "urgent_count": urgent_count, "top_message": top_message}

    def active_counts(self, conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
        now = utc_now()
        rows = conn.execute(
            """
            SELECT
              recipient,
              CASE
                WHEN state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                THEN 'stale_claimed'
                ELSE state
              END AS display_state,
              COUNT(*) AS count
            FROM messages
            GROUP BY recipient, display_state
            """,
            (now,),
        ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["recipient"], {})[row["display_state"]] = int(row["count"])
        return counts

    def approve_stable_commit(
        self,
        conn: sqlite3.Connection,
        *,
        scope: str,
        commit_sha: str,
        approved_by: str,
        note: str | None = None,
    ) -> None:
        now = utc_now()
        conn.execute(
            """
            INSERT INTO stable_commits(scope, commit_sha, approved_by, approved_at, note)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET
              commit_sha = excluded.commit_sha,
              approved_by = excluded.approved_by,
              approved_at = excluded.approved_at,
              note = excluded.note
            """,
            (scope, commit_sha, approved_by, now, note),
        )
        self.record_event(conn, "stable.approved", approved_by, scope, {"commit": commit_sha, "note": note})
        conn.commit()

    def current_stable_commit(self, conn: sqlite3.Connection, scope: str) -> sqlite3.Row | None:
        row = conn.execute("SELECT * FROM stable_commits WHERE scope = ?", (scope,)).fetchone()
        if row is not None:
            return row
        if scope != "global":
            return conn.execute("SELECT * FROM stable_commits WHERE scope = 'global'").fetchone()
        return None

    def list_stable_commits(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM stable_commits ORDER BY scope"))

    def record_milestone(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        summary: str,
        body: str = "",
        role: str | None = None,
        subject_roles: tuple[str, ...] = (),
        scope: str | None = None,
        kind: str = "milestone",
        ref_id: str | None = None,
        tags: tuple[str, ...] = (),
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        normalized_subjects = tuple(dict.fromkeys(role.strip() for role in subject_roles if role.strip()))
        if role and not normalized_subjects:
            normalized_subjects = (role,)
        normalized_scope = scope or ("team" if not normalized_subjects else "role")
        milestone = {
            "created_at": utc_now(),
            "actor": actor,
            "recorded_by": actor,
            "role": role,
            "scope": normalized_scope,
            "subject_roles": list(normalized_subjects),
            "kind": kind,
            "summary": summary,
            "body": body,
            "ref_id": ref_id,
            "tags": list(tags),
            "metadata": metadata or {},
        }
        self.milestones_path.parent.mkdir(parents=True, exist_ok=True)
        with self.milestones_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(milestone, sort_keys=True) + "\n")
        self.record_event(
            conn,
            "milestone.recorded",
            actor,
            ref_id,
            {
                "summary": summary,
                "role": role,
                "recorded_by": actor,
                "scope": normalized_scope,
                "subject_roles": list(normalized_subjects),
                "kind": kind,
                "tags": list(tags),
            },
        )
        conn.commit()
        return milestone

    def list_milestones(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        role: str | None = None,
        subject_role: str | None = None,
        scope: str | None = None,
        kind: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self.milestones_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        required_tags = set(tags)
        for line in self.milestones_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            created_at = parse_utc_datetime(str(row.get("created_at") or ""))
            if since is not None and created_at < since:
                continue
            if until is not None and created_at > until:
                continue
            subject_roles = tuple(str(item) for item in row.get("subject_roles") or ())
            if role is not None and row.get("role") != role and role not in subject_roles:
                continue
            if subject_role is not None and subject_role not in subject_roles and row.get("role") != subject_role:
                continue
            if scope is not None and row.get("scope") != scope:
                continue
            if kind is not None and row.get("kind") != kind:
                continue
            if required_tags and not required_tags <= set(row.get("tags") or []):
                continue
            rows.append(row)
        if limit > 0:
            rows = rows[-limit:]
        return rows

    def record_notification(
        self,
        conn: sqlite3.Connection,
        message_id: str | None,
        role: str,
        method: str,
        state: str,
        details: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO notifications(message_id, role, method, state, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, role, method, state, details, utc_now()),
        )

    def record_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        actor: str | None,
        ref_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now()
        payload_json = json.dumps(payload, sort_keys=True)
        conn.execute(
            "INSERT INTO events(created_at, type, actor, ref_id, payload_json) VALUES (?, ?, ?, ?, ?)",
            (now, event_type, actor, ref_id, payload_json),
        )
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "created_at": now,
                        "type": event_type,
                        "actor": actor,
                        "ref_id": ref_id,
                        "payload": payload,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    def _message_for_role(self, conn: sqlite3.Connection, role: str, message_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown message: {message_id}")
        if row["recipient"] != role:
            raise PermissionError(f"Message {message_id} is addressed to {row['recipient']}, not {role}")
        return row

    def _require_message_state(self, row: sqlite3.Row, action: str, allowed_states: tuple[str, ...]) -> None:
        if row["state"] not in allowed_states:
            allowed = ", ".join(allowed_states)
            raise ValueError(f"Cannot {action} message {row['id']} in state {row['state']}; expected one of: {allowed}")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def get_obligation(self, conn: sqlite3.Connection, obligation_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM obligations WHERE id = ?", (obligation_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown obligation: {obligation_id}")
        return row

    def _obligation_for_role(self, conn: sqlite3.Connection, role: str, obligation_id: str) -> sqlite3.Row:
        row = self.get_obligation(conn, obligation_id)
        if row["role"] != role:
            raise PermissionError(f"Obligation {obligation_id} belongs to {row['role']}, not {role}")
        return row

    def _todo_for_role(self, conn: sqlite3.Connection, role: str, todo_id: str) -> sqlite3.Row:
        row = self.get_todo(conn, todo_id)
        if row["role"] != role:
            raise PermissionError(f"Todo {todo_id} belongs to {row['role']}, not {role}")
        return row


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def parse_utc_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def new_message_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(3)
    return f"msg_{stamp}_{suffix}"


def new_obligation_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(3)
    return f"obligation_{stamp}_{suffix}"


def new_todo_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(3)
    return f"todo_{stamp}_{suffix}"


def normalize_watchdog_runner_name(name: str) -> str:
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("watchdog runner name is required")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_.-")
    if any(char not in allowed for char in normalized):
        raise ValueError("watchdog runner name may contain only letters, numbers, '.', '_', and '-'")
    return normalized


def normalize_priority(priority: str) -> str:
    value = priority.lower()
    if value not in PRIORITY_ORDER:
        raise ValueError(f"Invalid priority: {priority}")
    return value


def message_state_filter_clause(states: Iterable[str], *, now: str) -> tuple[str, list[str]]:
    selectors = tuple(dict.fromkeys(state.lower() for state in states))
    invalid = tuple(state for state in selectors if state not in MESSAGE_STATE_FILTERS)
    if invalid:
        raise ValueError(f"Invalid message state filter: {', '.join(invalid)}")

    conditions: list[str] = []
    params: list[str] = []
    concrete_states = tuple(state for state in selectors if state not in (PENDING_MESSAGE_STATE, STALE_CLAIMED_STATE))
    if concrete_states:
        placeholders = ", ".join("?" for _ in concrete_states)
        conditions.append(f"state IN ({placeholders})")
        params.extend(concrete_states)
    if PENDING_MESSAGE_STATE in selectors:
        placeholders = ", ".join("?" for _ in CLAIMABLE_STATES)
        conditions.append(
            f"(state IN ({placeholders}) "
            "OR (state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?))"
        )
        params.extend((*CLAIMABLE_STATES, now))
    elif STALE_CLAIMED_STATE in selectors:
        conditions.append("(state = 'claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?)")
        params.append(now)
    return f"({' OR '.join(conditions)})", params


def pending_count_from_state_counts(counts: dict[str, int]) -> int:
    return sum(counts.get(state, 0) for state in CLAIMABLE_STATES) + counts.get(STALE_CLAIMED_STATE, 0)


def normalize_summary(summary: str) -> str:
    return " ".join(summary.strip().lower().split())


def normalize_message_kind(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in ("task", "completion_notice", "notice"):
        raise ValueError(f"Invalid message kind: {value}")
    return normalized


def empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize_notify_method(method: str) -> str:
    value = method.strip().lower().replace("_", "-")
    if value == "display":
        return "display-message"
    if value in ("app-server", "appserver", "codex", "codex-app-server"):
        return "app-server-turn"
    return value


def truncate_wake_line(value: str, limit: int = 200) -> str:
    single_line = " ".join(value.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 3].rstrip() + "..."


def role_notify_method(role_row: sqlite3.Row) -> str:
    try:
        capabilities = json.loads(role_row["capabilities_json"] or "{}")
    except json.JSONDecodeError:
        return "display-message"
    method = capabilities.get("notify_method") or capabilities.get("notify")
    if method is None:
        return "display-message"
    return str(method)


def _optional_capability(capabilities: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = capabilities.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def inspect_tmux_pane(tmux: str, pane: str) -> tuple[bool, TmuxPaneState | str]:
    result = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane, "#{pane_dead}\t#{pane_in_mode}\t#{pane_current_command}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"tmux exited {result.returncode}").strip()
        return False, f"notify_failed: could not inspect pane: {details}"
    parts = result.stdout.rstrip("\n").split("\t", 2)
    if len(parts) != 3:
        return False, f"notify_failed: unexpected pane inspection output: {result.stdout.strip()}"
    return True, TmuxPaneState(
        dead=parts[0] == "1",
        in_mode=parts[1] == "1",
        current_command=parts[2],
    )
