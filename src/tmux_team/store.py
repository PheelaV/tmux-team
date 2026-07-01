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

MESSAGE_ACTIVE_STATES = ("queued", "notified", "retrying")
CLAIMABLE_STATES = MESSAGE_ACTIVE_STATES
ROLE_STATES = ("active", "paused", "draining", "retired", "failed")
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


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
              result_summary TEXT
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
            """
        )
        conn.commit()

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
    ) -> Message:
        now = utc_now()
        message_id = new_message_id()
        body_path = self.messages_dir / f"{message_id}.md"
        body_path.write_text(body, encoding="utf-8")
        conn.execute(
            """
            INSERT INTO messages(id, sender, recipient, priority, summary, body_path, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, sender, recipient, normalize_priority(priority), summary, str(body_path), state, now, now),
        )
        self.record_event(
            conn,
            "message.created",
            sender,
            message_id,
            {"to": recipient, "priority": normalize_priority(priority), "summary": summary, "state": state},
        )
        conn.commit()
        return Message(message_id, sender, recipient, normalize_priority(priority), summary, body_path, state, now, now)

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
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            conn.execute(
                f"""
                SELECT * FROM messages
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
                tuple(params),
            )
        )

    def claim_next(self, conn: sqlite3.Connection, role: str, claim_seconds: int) -> sqlite3.Row | None:
        now = utc_now()
        row = conn.execute(
            """
            SELECT * FROM messages
            WHERE recipient = ?
              AND state IN ('queued', 'notified', 'retrying')
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
            (role,),
        ).fetchone()
        if row is None:
            return None
        claim_expires_at = (datetime.now(UTC) + timedelta(seconds=claim_seconds)).replace(microsecond=0).isoformat()
        conn.execute(
            """
            UPDATE messages
            SET state = 'claimed', claimed_by = ?, claim_expires_at = ?, updated_at = ?
            WHERE id = ? AND state IN ('queued', 'notified', 'retrying')
            """,
            (role, claim_expires_at, now, row["id"]),
        )
        self.record_event(conn, "message.claimed", role, row["id"], {"claim_expires_at": claim_expires_at})
        conn.commit()
        return conn.execute("SELECT * FROM messages WHERE id = ?", (row["id"],)).fetchone()

    def ack_message(self, conn: sqlite3.Connection, role: str, message_id: str) -> sqlite3.Row:
        now = utc_now()
        row = self._message_for_role(conn, role, message_id)
        conn.execute(
            """
            UPDATE messages
            SET state = 'acknowledged', acknowledged_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, message_id),
        )
        self.record_event(conn, "message.acknowledged", role, message_id, {})
        conn.commit()
        return conn.execute("SELECT * FROM messages WHERE id = ?", (row["id"],)).fetchone()

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
        conn.execute(
            """
            UPDATE messages
            SET state = 'completed',
                completed_at = ?,
                result_status = ?,
                result_summary = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, result_status, result_summary, now, message_id),
        )
        self.record_event(
            conn,
            "message.completed",
            role,
            message_id,
            {"status": result_status, "summary": result_summary, "previous_state": row["state"]},
        )
        conn.commit()
        return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()

    def notify_role(self, conn: sqlite3.Connection, role: str, method: str = "auto") -> tuple[bool, str]:
        role_row = self.get_role(conn, role)
        if role_row is None:
            return False, f"unknown role: {role}"
        if method == "auto":
            method = role_notify_method(role_row)
        method = normalize_notify_method(method)
        pending = self.pending_count(conn, role)
        if pending == 0:
            return True, "no pending messages"

        if method == "app-server-turn":
            return self.notify_role_app_server(conn, role, role_row, pending)

        pane = role_row["pane"]
        if not pane:
            self.record_notification(conn, None, role, method, "notify_failed", "role has no pane")
            conn.commit()
            return False, "role has no pane"

        text = f"[tmux-team] {pending} pending message(s). Run: tmux-team inbox next --role {role}"
        if method not in ("display-message", "send-keys"):
            self.record_notification(conn, None, role, method, "notify_failed", f"unsupported method: {method}")
            conn.commit()
            return False, f"unsupported method: {method}"

        tmux = shutil.which("tmux")
        if tmux is None:
            self.record_notification(conn, None, role, method, "notify_failed", "tmux not found")
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
                self.record_notification(conn, None, role, method, state, pane_details)
                event_type = "role.notification_deferred" if state == "notify_deferred" else "role.notification_failed"
                self.record_event(conn, event_type, "tmux", role, {"method": method, "details": pane_details})
                conn.commit()
                return False, pane_details
            wake_prompt = (
                f"You have {pending} pending tmux-team inbox message(s). "
                f"Run `tmux-team inbox next --role {role}`, acknowledge and complete the claimed message, "
                "then repeat `inbox next` until it reports no pending messages. "
                "Do not rely on pane text as task content."
            )
            command = [tmux, "send-keys", "-t", pane, wake_prompt, "Enter"]

        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            now = utc_now()
            conn.execute(
                """
                UPDATE messages
                SET state = 'notified', attempts = attempts + 1, updated_at = ?
                WHERE recipient = ? AND state = 'queued'
                """,
                (now, role),
            )
            details = text if method == "display-message" else wake_prompt
            self.record_notification(conn, None, role, method, "notified", details)
            self.record_event(conn, "role.notified", "tmux", role, {"pending": pending, "method": method})
            conn.commit()
            return True, details

        details = (result.stderr or result.stdout or f"tmux exited {result.returncode}").strip()
        self.record_notification(conn, None, role, method, "notify_failed", details)
        conn.commit()
        return False, details

    def notify_role_app_server(
        self,
        conn: sqlite3.Connection,
        role: str,
        role_row: sqlite3.Row,
        pending: int,
    ) -> tuple[bool, str]:
        settings = self.resolve_role_app_server(conn, role, role_row)
        if settings is None:
            details = "role has no app-server endpoint/thread binding"
            self.record_notification(conn, None, role, "app-server-turn", "notify_failed", details)
            self.record_event(
                conn, "role.notification_failed", "app-server", role, {"method": "app-server-turn", "details": details}
            )
            conn.commit()
            return False, details

        endpoint, thread_id, timeout = settings
        prompt = self.app_server_wake_prompt(role, pending)
        try:
            turn = submit_app_server_wake(
                endpoint=endpoint,
                thread_id=thread_id,
                prompt=prompt,
                client_user_message_id=f"tmux-team-{role}-{utc_now()}",
                timeout=timeout,
            )
        except (AppServerError, OSError, TimeoutError) as exc:
            details = f"app-server turn submission failed: {exc}"
            self.record_notification(conn, None, role, "app-server-turn", "notify_failed", details)
            self.record_event(
                conn, "role.notification_failed", "app-server", role, {"method": "app-server-turn", "details": details}
            )
            conn.commit()
            return False, details

        now = utc_now()
        conn.execute(
            """
            UPDATE messages
            SET state = 'notified', attempts = attempts + 1, updated_at = ?
            WHERE recipient = ? AND state = 'queued'
            """,
            (now, role),
        )
        details = json.dumps(
            {
                "endpoint": endpoint,
                "thread_id": turn.thread_id,
                "turn_id": turn.turn_id,
                "turn_status": turn.status,
                "pending": pending,
            },
            sort_keys=True,
        )
        self.record_notification(conn, None, role, "app-server-turn", "submitted", details)
        self.record_event(
            conn,
            "role.notified",
            "app-server",
            role,
            {"pending": pending, "method": "app-server-turn", "thread_id": turn.thread_id, "turn_id": turn.turn_id},
        )
        conn.commit()
        return True, f"app-server turn submitted thread={turn.thread_id} turn={turn.turn_id}"

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

    def app_server_wake_prompt(self, role: str, pending: int) -> str:
        config = self.cli_config_arg()
        return (
            f"You have {pending} pending tmux-team inbox message(s) for role `{role}`.\n\n"
            "Claim durable work now. Do not treat this wake-up prompt as the task body.\n\n"
            "Process messages one at a time from the project worktree. After completing one message, "
            "repeat `inbox next` until it reports no pending messages.\n\n"
            "For each claimed message:\n\n"
            f"1. `tmux-team{config} inbox next --role {role}`\n"
            f"2. `tmux-team{config} inbox ack <message-id> --role {role}`\n"
            f"3. Complete the work described in the claimed message body.\n"
            f'4. `tmux-team{config} inbox complete <message-id> --role {role} --status <status> --summary "<summary>"`\n'
        )

    def cli_config_arg(self) -> str:
        if self.config.config_path is None:
            return ""
        config_path = self.config.config_path
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
                "SELECT COUNT(*) FROM messages WHERE recipient = ? AND state IN ('queued', 'notified', 'retrying')",
                (role,),
            ).fetchone()[0]
        )

    def active_counts(self, conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
        rows = conn.execute(
            """
            SELECT recipient, state, COUNT(*) AS count
            FROM messages
            GROUP BY recipient, state
            """
        ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["recipient"], {})[row["state"]] = int(row["count"])
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


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_message_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(3)
    return f"msg_{stamp}_{suffix}"


def normalize_priority(priority: str) -> str:
    value = priority.lower()
    if value not in PRIORITY_ORDER:
        raise ValueError(f"Invalid priority: {priority}")
    return value


def normalize_notify_method(method: str) -> str:
    value = method.strip().lower().replace("_", "-")
    if value == "display":
        return "display-message"
    if value in ("app-server", "appserver", "codex", "codex-app-server"):
        return "app-server-turn"
    return value


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
