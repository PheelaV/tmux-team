from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .extensions.runner import HookRunner
from .store import Message, Store, normalize_notify_method, normalize_priority


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

        role = self.store.get_role(conn, recipient)
        if role is None:
            raise KeyError(f"Unknown recipient role: {recipient}")

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
        )
        self.hook_runner.run(
            self.store,
            conn,
            "message.created",
            {"message": message_to_dict(message), "blocked": blocked},
            actor=actor or sender,
        )

        notification = None
        if state == "queued" and wake:
            notification = self.notify_role(conn, recipient, notify_method, actor=actor or sender)
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
    }


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required message field: {key}")
    return str(value)
