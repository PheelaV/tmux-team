from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extensions.runner import HookRunner
from .store import Message, Store, normalize_notify_method, normalize_priority, role_notify_method


@dataclass(frozen=True)
class NotificationResult:
    ok: bool
    details: str
    method: str


@dataclass(frozen=True)
class SendMessageResult:
    message: Message
    blocked: dict[str, str] | None = None
    notification: NotificationResult | None = None
    duplicates: tuple[sqlite3.Row, ...] = ()


@dataclass(frozen=True)
class CompleteMessageResult:
    message: sqlite3.Row
    reply: SendMessageResult | None = None
    reply_skipped: str | None = None


class TeamService:
    def __init__(self, store: Store, hook_runner: HookRunner | None = None):
        self.store = store
        self.hook_runner = hook_runner or HookRunner(store.config)

    def send_message(
        self,
        conn: sqlite3.Connection,
        *,
        sender: str,
        recipient: str,
        priority: str,
        summary: str,
        body: str,
        force: bool = False,
        wake: bool = True,
        notify_method: str = "auto",
        correlation_key: str | None = None,
        related_to: str | None = None,
        supersedes: str | None = None,
        message_kind: str = "task",
        allow_duplicate: bool = False,
        actor: str | None = None,
    ) -> SendMessageResult:
        message_data: dict[str, Any] = {
            "sender": sender,
            "recipient": recipient,
            "priority": normalize_priority(priority),
            "summary": summary,
            "body": body,
            "force": force,
            "wake": wake,
            "notify_method": notify_method,
            "correlation_key": correlation_key,
            "related_to": related_to,
            "supersedes": supersedes,
            "message_kind": message_kind,
            "allow_duplicate": allow_duplicate,
        }
        hooked = self.hook_runner.run(
            self.store,
            conn,
            "message.before_create",
            {"message": message_data},
            actor=actor or sender,
        ).data
        message_data = dict(hooked.get("message") or {})

        sender = required_str(message_data, "sender")
        recipient = required_str(message_data, "recipient")
        priority = normalize_priority(required_str(message_data, "priority"))
        summary = required_str(message_data, "summary")
        body = str(message_data.get("body") or "")
        force = bool(message_data.get("force", force))
        wake = bool(message_data.get("wake", wake))
        notify_method = str(message_data.get("notify_method") or notify_method)
        correlation_key = optional_str(message_data.get("correlation_key"))
        related_to = optional_str(message_data.get("related_to"))
        supersedes = optional_str(message_data.get("supersedes"))
        message_kind = str(message_data.get("message_kind") or message_kind)
        allow_duplicate = bool(message_data.get("allow_duplicate", allow_duplicate))

        role = self.store.get_role(conn, recipient)
        if role is None:
            raise KeyError(f"Unknown recipient role: {recipient}")

        duplicates: tuple[sqlite3.Row, ...] = ()
        if not allow_duplicate:
            duplicates = tuple(
                self.store.find_duplicate_messages(
                    conn,
                    recipient=recipient,
                    summary=summary,
                    correlation_key=correlation_key,
                )
            )

        state = "queued"
        blocked = None
        if role["state"] != "active" and not force and priority != "urgent":
            state = f"blocked_by_role_{role['state']}"
            blocked = {"role": recipient, "state": role["state"]}

        message = self.store.create_message(
            conn,
            sender=sender,
            recipient=recipient,
            priority=priority,
            summary=summary,
            body=body,
            state=state,
            correlation_key=correlation_key,
            related_to=related_to,
            supersedes=supersedes,
            message_kind=message_kind,
        )
        self.hook_runner.run(
            self.store,
            conn,
            "message.created",
            {
                "message": message_to_dict(message),
                "blocked": blocked,
                "duplicates": [row_to_dict(row) for row in duplicates],
            },
            actor=actor or sender,
        )

        notification = None
        if state == "queued" and wake:
            notification = self.notify_role(conn, recipient, notify_method, actor=actor or sender)
        return SendMessageResult(message=message, blocked=blocked, notification=notification, duplicates=duplicates)

    def send_notice(
        self,
        conn: sqlite3.Connection,
        *,
        sender: str,
        recipient: str,
        summary: str,
        body: str,
        force: bool = False,
        wake: bool = True,
        notify_method: str = "auto",
        actor: str | None = None,
    ) -> SendMessageResult:
        role = self.store.get_role(conn, recipient)
        if role is None:
            raise KeyError(f"Unknown recipient role: {recipient}")

        state = "completed"
        blocked = None
        if role["state"] != "active" and not force:
            state = f"blocked_by_role_{role['state']}"
            blocked = {"role": recipient, "state": role["state"]}

        message = self.store.create_message(
            conn,
            sender=sender,
            recipient=recipient,
            priority="low",
            summary=summary,
            body=body,
            state=state,
            message_kind="notice",
        )
        self.hook_runner.run(
            self.store,
            conn,
            "message.created",
            {"message": message_to_dict(message), "blocked": blocked, "notice": True},
            actor=actor or sender,
        )

        notification = None
        if state == "completed" and wake:
            ok, details = self.store.notify_role(
                conn,
                recipient,
                notify_method,
                notice_message_id=message.id,
                notice_summary=summary,
            )
            notification = NotificationResult(ok=ok, details=details, method=notify_method)
        return SendMessageResult(message=message, blocked=blocked, notification=notification)

    def claim_next(
        self,
        conn: sqlite3.Connection,
        role: str,
        claim_seconds: int,
        *,
        actor: str | None = None,
    ) -> sqlite3.Row | None:
        self.hook_runner.run(
            self.store,
            conn,
            "message.before_claim",
            {"role": role, "claim_seconds": claim_seconds},
            actor=actor or role,
        )
        row = self.store.claim_next(conn, role, claim_seconds)
        if row is not None:
            self.hook_runner.run(
                self.store,
                conn,
                "message.claimed",
                {"message": row_to_dict(row)},
                actor=actor or role,
            )
        return row

    def ack_message(
        self,
        conn: sqlite3.Connection,
        role: str,
        message_id: str,
        *,
        actor: str | None = None,
    ) -> sqlite3.Row:
        row = self.store.ack_message(conn, role, message_id)
        self.hook_runner.run(
            self.store,
            conn,
            "message.acknowledged",
            {"message": row_to_dict(row)},
            actor=actor or role,
        )
        return row

    def complete_message(
        self,
        conn: sqlite3.Connection,
        role: str,
        message_id: str,
        status: str,
        summary: str,
        *,
        actor: str | None = None,
    ) -> sqlite3.Row:
        data = self.hook_runner.run(
            self.store,
            conn,
            "message.before_complete",
            {
                "message": {
                    "id": message_id,
                    "role": role,
                    "status": status,
                    "summary": summary,
                }
            },
            actor=actor or role,
        ).data
        message = data.get("message") or {}
        status = str(message.get("status") or status)
        summary = str(message.get("summary") if message.get("summary") is not None else summary)
        row = self.store.complete_message(conn, role, message_id, status, summary)
        self.hook_runner.run(
            self.store,
            conn,
            "message.completed",
            {"message": row_to_dict(row)},
            actor=actor or role,
        )
        return row

    def complete_message_with_optional_reply(
        self,
        conn: sqlite3.Connection,
        role: str,
        message_id: str,
        status: str,
        summary: str,
        *,
        reply_to_sender: bool = False,
        reply_wake: bool = True,
        actor: str | None = None,
    ) -> CompleteMessageResult:
        row = self.complete_message(conn, role, message_id, status, summary, actor=actor)
        if not reply_to_sender:
            return CompleteMessageResult(message=row)

        sender = str(row["sender"])
        if sender not in self.store.config.roles:
            return CompleteMessageResult(message=row, reply_skipped=f"sender {sender!r} is not a managed role")
        if is_completion_reply(row):
            return CompleteMessageResult(message=row, reply_skipped="message is already a completion reply")

        reply_summary = f"{role} completed: {row['summary']}"
        reply_body = (
            f"Completed message: {row['id']}\n"
            f"Original summary: {row['summary']}\n"
            f"Status: {row['result_status']}\n"
            f"Result: {row['result_summary']}"
        )
        reply = self.send_message(
            conn,
            sender=role,
            recipient=sender,
            priority="normal",
            summary=reply_summary,
            body=reply_body,
            wake=reply_wake,
            notify_method="auto",
            related_to=row["id"],
            message_kind="completion_notice",
            actor=actor or role,
        )
        return CompleteMessageResult(message=row, reply=reply)

    def notify_role(
        self,
        conn: sqlite3.Connection,
        role: str,
        method: str = "auto",
        *,
        actor: str | None = None,
    ) -> NotificationResult:
        data = self.hook_runner.run(
            self.store,
            conn,
            "notification.before",
            {"notification": {"role": role, "method": method}},
            actor=actor,
        ).data
        notification = data.get("notification") or {}
        role = str(notification.get("role") or role)
        method = normalize_notify_method(str(notification.get("method") or method))
        if method == "auto":
            role_row = self.store.get_role(conn, role)
            if role_row is None:
                raise KeyError(f"Unknown role: {role}")
            method = normalize_notify_method(role_notify_method(role_row))
        ok, details = self.store.notify_role(conn, role, method)
        event = "notification.after" if ok else "notification.failed"
        self.hook_runner.run(
            self.store,
            conn,
            event,
            {"notification": {"role": role, "method": method, "ok": ok, "details": details}},
            actor=actor,
        )
        return NotificationResult(ok=ok, details=details, method=method)


def message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "sender": message.sender,
        "recipient": message.recipient,
        "priority": message.priority,
        "summary": message.summary,
        "body_path": str(message.body_path),
        "state": message.state,
        "created_at": message.created_at,
        "updated_at": message.updated_at,
        "correlation_key": message.correlation_key,
        "related_to": message.related_to,
        "supersedes": message.supersedes,
        "message_kind": message.message_kind,
    }


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required message field: {key}")
    return str(value)


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_completion_reply(row: sqlite3.Row) -> bool:
    try:
        if row["message_kind"] == "completion_notice":
            return True
    except (IndexError, KeyError):
        pass
    try:
        body = Path(str(row["body_path"])).read_text(encoding="utf-8")
    except OSError:
        return " completed: " in str(row["summary"])
    return body.startswith("Completed message: ") and "\nOriginal summary: " in body
