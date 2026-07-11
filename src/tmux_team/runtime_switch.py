from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .acp_tui import send_control_request, wait_for_acp_tui
from .bootstrap import acp_role_shell_command
from .config import load_config, role_scratchpad_path, update_role_capabilities
from .display import role_capabilities
from .store import Store

ACTIVE_TURN_STATES = {"busy", "asking"}
SCRATCHPAD_EXCERPT_CHARS = 4_000
GIT_OUTPUT_CHARS = 4_000
CANCEL_TIMEOUT_SECONDS = 15.0
HANDOFF_BODY_CHARS = 16_000


class RuntimeSwitchError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeSwitchResult:
    tmux_command: tuple[str, ...]
    old_session_id: str | None
    new_session_id: str | None
    handoff_file: Path
    dry_run: bool = False


def runtime_show(store: Store, conn: sqlite3.Connection, role: str) -> str:
    role_row = _role(store, conn, role)
    capabilities = role_capabilities(role_row)
    values = (
        ("provider", capabilities.get("acp_provider")),
        ("model", capabilities.get("acp_model")),
        ("effort", capabilities.get("acp_effort")),
        ("session_id", capabilities.get("runtime_session_id")),
        ("previous_session_id", capabilities.get("previous_runtime_session_id")),
        ("acp_agent_command", capabilities.get("acp_agent_command")),
        ("last_handoff_file", capabilities.get("last_handoff_file")),
    )
    lines = [
        f"{role} state={role_row['state']} mode={role_row['mode']} "
        f"pane={role_row['pane'] or '-'} worktree={role_row['worktree'] or '-'}"
    ]
    lines.extend(f"{key}: {value or '-'}" for key, value in values)
    return "\n".join(lines) + "\n"


def read_handoff_body(path: Path) -> str:
    expanded = path.expanduser()
    try:
        with expanded.open(encoding="utf-8") as handle:
            body = handle.read(HANDOFF_BODY_CHARS + 1)
    except OSError as exc:
        raise RuntimeSwitchError(f"could not read handoff body file {path}: {exc}") from exc
    if len(body) > HANDOFF_BODY_CHARS:
        raise RuntimeSwitchError(f"handoff body exceeds {HANDOFF_BODY_CHARS} characters")
    return body


def prepare_runtime_handoff(
    store: Store,
    conn: sqlite3.Connection,
    role: str,
    *,
    summary: str,
    body: str | None = None,
    actor: str = "operator",
) -> Path:
    role_row = _acp_role(store, conn, role)
    if not summary.strip():
        raise RuntimeSwitchError("handoff summary is required")
    if body is not None and len(body) > HANDOFF_BODY_CHARS:
        raise RuntimeSwitchError(f"handoff body exceeds {HANDOFF_BODY_CHARS} characters")
    socket_path = _control_socket(store, role_row)
    previous_state = str(role_row["state"])
    if previous_state != "draining":
        store.set_role_state(conn, role, "draining", actor=actor)
    try:
        status = send_control_request(socket_path, {"action": "status"})
        _require_idle(role, status, "prepare a runtime handoff")
        source_session_id = _optional_string(status.get("sessionId"))
        if source_session_id is None:
            raise RuntimeSwitchError(f"cannot prepare a runtime handoff for role {role!r}: session ID is unknown")
    except Exception:
        if previous_state != "draining":
            store.set_role_state(conn, role, previous_state, actor=actor)
        raise

    timestamp = datetime.now(UTC)
    handoff_dir = store.runtime_dir / "handoffs" / role
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / timestamp.strftime("%Y%m%dT%H%M%S.%fZ.md")
    role_row = _acp_role(store, conn, role)
    capsule = render_handoff_capsule(
        store,
        conn,
        role_row,
        summary=summary,
        body=body,
        status=status,
        created_at=timestamp,
    )
    _write_new_private_file(handoff_path, capsule)
    try:
        store.record_event(
            conn,
            "role.runtime_handoff_prepared",
            actor,
            role,
            {
                "handoff_file": str(handoff_path.resolve()),
                "sha256": _handoff_digest(handoff_path),
                "source_session_id": source_session_id,
            },
        )
        conn.commit()
    except Exception:
        handoff_path.unlink(missing_ok=True)
        if previous_state != "draining":
            store.set_role_state(conn, role, previous_state, actor=actor)
        raise
    return handoff_path


def render_handoff_capsule(
    store: Store,
    conn: sqlite3.Connection,
    role_row: sqlite3.Row,
    *,
    summary: str,
    body: str | None,
    status: dict[str, Any],
    created_at: datetime,
) -> str:
    role = str(role_row["name"])
    capabilities = role_capabilities(role_row)
    worktree = Path(str(role_row["worktree"])) if role_row["worktree"] else None
    scratchpad_path = role_scratchpad_path(store.config, role)
    scratchpad = _bounded_file_excerpt(scratchpad_path, SCRATCHPAD_EXCERPT_CHARS)
    git_status, git_diff_stat = _git_snapshot(worktree)
    messages = store.list_active_messages(conn, role=role, limit=20)
    todos = store.list_todos(conn, role=role, states=("open",), limit=100)
    session_id = status.get("sessionId") or capabilities.get("runtime_session_id")

    lines = [
        "# tmux-team Runtime Handoff",
        "",
        f"- Created: {created_at.isoformat()}",
        f"- Role: {role}",
        f"- Role state: {role_row['state']}",
        f"- Provider: {capabilities.get('acp_provider') or 'unknown'}",
        f"- Model: {capabilities.get('acp_model') or 'unknown'}",
        f"- Effort: {capabilities.get('acp_effort') or 'unknown'}",
        f"- ACP session: {session_id or 'unknown'}",
        f"- ACP command: `{capabilities.get('acp_agent_command') or 'unknown'}`",
        f"- Worktree: {worktree or 'unknown'}",
        f"- Pane: {role_row['pane'] or 'unknown'}",
        f"- Scratchpad: {scratchpad_path}",
        "",
        "## Operator Handoff",
        "",
        f"Summary: {summary.strip()}",
    ]
    if body:
        lines.extend(["", body.rstrip()])
    lines.extend(["", "## Active Inbox Metadata", ""])
    if messages:
        for message in messages:
            lines.append(
                f"- `{message['id']}` state={message['display_state']} priority={message['priority']} "
                f"from={message['sender']} summary={_one_line(message['summary'])}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Open Todos", ""])
    if todos:
        for todo in todos:
            lines.append(
                f"- `{todo['id']}` message=`{todo['message_id']}` state={todo['state']} text={_one_line(todo['text'])}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Scratchpad Excerpt",
            "",
            scratchpad or "(missing or empty)",
            "",
            "## Git Status",
            "",
            "```text",
            git_status,
            "```",
            "",
            "## Git Diff Stat",
            "",
            "```text",
            git_diff_stat,
            "```",
            "",
            "## Next Action",
            "",
            "Load the start-tmux-team skill, read the scratchpad and this handoff, inspect Git state, "
            "then recover open todos and active inbox work without repeating completed work.",
            "",
        ]
    )
    return "\n".join(lines)


def switch_runtime(
    store: Store,
    conn: sqlite3.Connection,
    role: str,
    *,
    acp_agent_command: str,
    handoff_file: Path,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    cancel_active: bool = False,
    tmux_bin: str = "tmux",
    dry_run: bool = False,
    actor: str = "operator",
) -> RuntimeSwitchResult:
    role_row = _acp_role(store, conn, role)
    command_value = acp_agent_command.strip()
    if not command_value:
        raise RuntimeSwitchError("ACP agent command is required")
    handoff_path = handoff_file.expanduser().resolve()
    handoff_metadata = validate_prepared_handoff(store, conn, role, handoff_path)
    if store.config.config_path is None:
        raise RuntimeSwitchError("runtime switch requires a config file")
    if not role_row["pane"]:
        raise RuntimeSwitchError(f"role {role!r} has no tmux pane")
    if not role_row["worktree"]:
        raise RuntimeSwitchError(f"role {role!r} has no worktree")

    capabilities = role_capabilities(role_row)
    acp_tui_bin = str(capabilities.get("acp_tui_bin") or "")
    if not acp_tui_bin:
        raise RuntimeSwitchError(f"role {role!r} has no acp_tui_bin capability")
    socket_path = _control_socket(store, role_row)
    worktree = Path(str(role_row["worktree"])).expanduser().resolve()
    pane = str(role_row["pane"])
    shell_command = acp_role_shell_command(
        acp_tui_bin,
        command_value,
        worktree,
        store.config.config_path,
        role,
        str(socket_path),
    )
    tmux_command = (tmux_bin, "respawn-pane", "-k", "-t", pane, "-c", str(worktree), shell_command)
    old_session_id = _optional_string(capabilities.get("runtime_session_id"))
    prepared_session_id = str(handoff_metadata["source_session_id"])
    if old_session_id and prepared_session_id != old_session_id:
        raise RuntimeSwitchError(
            f"handoff source session {prepared_session_id!r} does not match configured session {old_session_id!r}"
        )
    if dry_run:
        return RuntimeSwitchResult(tmux_command, old_session_id, None, handoff_path, dry_run=True)

    status = send_control_request(socket_path, {"action": "status"})
    live_session_id = _optional_string(status.get("sessionId"))
    if live_session_id:
        old_session_id = live_session_id
    if old_session_id != prepared_session_id:
        raise RuntimeSwitchError(
            f"handoff source session {prepared_session_id!r} does not match live session {old_session_id or 'unknown'!r}"
        )
    state = str(status.get("state") or "unknown")
    if state in ACTIVE_TURN_STATES:
        if not cancel_active:
            raise RuntimeSwitchError(
                f"role {role!r} has an active ACP turn (state={state}); use --cancel-active to cancel it"
            )
        send_control_request(socket_path, {"action": "cancel"})
        wait_for_idle(socket_path, timeout=CANCEL_TIMEOUT_SECONDS)
    else:
        _require_idle(role, status, "switch runtime")

    quiesce_runtime_session(socket_path, role=role, expected_session_id=prepared_session_id)
    try:
        result = subprocess.run(
            tmux_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise RuntimeSwitchError(f"could not run {tmux_bin}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"{tmux_bin} exited {result.returncode}").strip()
        raise RuntimeSwitchError(f"tmux respawn-pane failed: {details}")

    ready = wait_for_replacement_session(socket_path, role=role, previous_session_id=old_session_id, timeout=30.0)
    new_session_id = str(ready["sessionId"])

    old_provider = _optional_string(capabilities.get("acp_provider"))
    old_model = _optional_string(capabilities.get("acp_model"))
    old_effort = _optional_string(capabilities.get("acp_effort"))
    old_command = _optional_string(capabilities.get("acp_agent_command"))
    new_provider = provider if provider is not None else old_provider
    new_model = model if model is not None else old_model
    new_effort = effort if effort is not None else old_effort
    updates: dict[str, Any | None] = {
        "acp_agent_command": command_value,
        "runtime_session_id": new_session_id,
        "previous_runtime_session_id": old_session_id,
        "last_handoff_file": str(handoff_path),
    }
    if provider is not None:
        updates["acp_provider"] = provider
    if model is not None:
        updates["acp_model"] = model
    if effort is not None:
        updates["acp_effort"] = effort
    update_role_capabilities(store.config.config_path, role, updates)
    updated_config = load_config(store.config.config_path, store.runtime_dir)
    store.config = updated_config
    store.sync_roles(conn, updated_config.roles.values())

    prompt = recovery_prompt(updated_config, role, handoff_path)
    send_control_request(
        socket_path,
        {
            "action": "prompt",
            "text": prompt,
            "priority": "normal",
            "coalesceKey": "runtime-handoff",
        },
    )

    lineage = {
        "created_at": datetime.now(UTC).isoformat(),
        "actor": actor,
        "role": role,
        "handoff_file": str(handoff_path),
        "old": {
            "provider": old_provider,
            "model": old_model,
            "effort": old_effort,
            "command": old_command,
            "session_id": old_session_id,
        },
        "new": {
            "provider": new_provider,
            "model": new_model,
            "effort": new_effort,
            "command": command_value,
            "session_id": new_session_id,
        },
    }
    _append_lineage(store.runtime_dir / "handoffs" / role / "lineage.jsonl", lineage)
    store.record_event(conn, "role.runtime_switched", actor, role, lineage)
    store.set_role_state(conn, role, "active", actor=actor)
    return RuntimeSwitchResult(tmux_command, old_session_id, new_session_id, handoff_path)


def wait_for_idle(socket_path: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        status = send_control_request(socket_path, {"action": "status"}, timeout=1.0)
        last_state = str(status.get("state") or "unknown")
        if last_state == "idle" and int(status.get("queueDepth") or 0) == 0:
            return status
        if last_state == "failed":
            raise RuntimeSwitchError("ACP TUI reported failed state while waiting for cancellation")
        time.sleep(0.2)
    raise RuntimeSwitchError(f"ACP TUI did not become idle after cancellation (last state={last_state})")


def quiesce_runtime_session(socket_path: Path, *, role: str, expected_session_id: str) -> None:
    status = send_control_request(socket_path, {"action": "quiesce", "sessionId": expected_session_id})
    _require_idle(role, status, "switch runtime")
    if status.get("acceptingPrompts") is not False:
        raise RuntimeSwitchError(f"ACP TUI did not confirm prompt quiescence for role {role!r}")
    session_id = _optional_string(status.get("sessionId"))
    if session_id != expected_session_id:
        raise RuntimeSwitchError(
            f"runtime session changed while quiescing role {role!r}: expected {expected_session_id!r}, "
            f"got {session_id or 'unknown'!r}"
        )


def wait_for_replacement_session(
    socket_path: Path,
    *,
    role: str,
    previous_session_id: str | None,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = wait_for_acp_tui(socket_path, timeout=max(0.1, deadline - time.monotonic()))
        _require_idle(role, ready, "complete runtime switch")
        session_id = _optional_string(ready.get("sessionId"))
        if session_id and session_id != previous_session_id:
            return ready
        time.sleep(0.2)
    raise RuntimeSwitchError("replacement ACP TUI did not report a new session ID")


def recovery_prompt(config, role: str, handoff_path: Path) -> str:
    scratchpad_path = role_scratchpad_path(config, role)
    return (
        f"Runtime handoff recovery for tmux-team role `{role}`. Load the `start-tmux-team` skill. "
        f"Read scratchpad `{scratchpad_path}` and handoff `{handoff_path}`. "
        "Inspect `git status --short` and `git diff --stat`. "
        f"Recover open todos with `tmux-team todo recover --role {role}` and active inbox work with "
        f"`tmux-team inbox next --role {role}`. Verify continuity before changing files and continue "
        "without repeating completed work."
    )


def _role(store: Store, conn: sqlite3.Connection, role: str) -> sqlite3.Row:
    role_row = store.get_role(conn, role)
    if role_row is None:
        raise KeyError(f"Unknown role: {role}")
    return role_row


def _acp_role(store: Store, conn: sqlite3.Connection, role: str) -> sqlite3.Row:
    role_row = _role(store, conn, role)
    if role_row["mode"] != "acp_tui":
        raise RuntimeSwitchError(f"runtime prepare/switch requires an acp_tui role: {role}")
    return role_row


def _control_socket(store: Store, role_row: sqlite3.Row) -> Path:
    value = store.resolve_role_control_socket(role_row)
    if not value:
        raise RuntimeSwitchError(f"role {role_row['name']!r} has no ACP TUI control socket")
    return Path(str(value)).expanduser().resolve()


def _require_idle(role: str, status: dict[str, Any], operation: str) -> None:
    state = str(status.get("state") or "unknown")
    if state in ACTIVE_TURN_STATES:
        raise RuntimeSwitchError(f"cannot {operation} for role {role!r}: ACP TUI state={state}")
    if state != "idle":
        raise RuntimeSwitchError(f"cannot {operation} for role {role!r}: ACP TUI is not idle (state={state})")
    queue_depth = int(status.get("queueDepth") or 0)
    if queue_depth:
        raise RuntimeSwitchError(f"cannot {operation} for role {role!r}: ACP TUI has {queue_depth} queued prompt(s)")


def validate_prepared_handoff(
    store: Store,
    conn: sqlite3.Connection,
    role: str,
    handoff_path: Path,
) -> dict[str, Any]:
    role_row = _role(store, conn, role)
    if role_row["state"] != "draining":
        raise RuntimeSwitchError(f"role {role!r} must be draining; run runtime prepare before runtime switch")
    expected_dir = (store.runtime_dir / "handoffs" / role).resolve()
    if not handoff_path.is_file() or handoff_path.parent != expected_dir:
        raise RuntimeSwitchError(f"handoff file is not a prepared capsule for role {role!r}: {handoff_path}")
    event = conn.execute(
        """
        SELECT payload_json
        FROM events
        WHERE type = 'role.runtime_handoff_prepared' AND ref_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (role,),
    ).fetchone()
    if event is None:
        raise RuntimeSwitchError(f"role {role!r} has no prepared runtime handoff")
    try:
        payload = json.loads(event["payload_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeSwitchError(f"role {role!r} has invalid prepared handoff metadata") from exc
    if payload.get("handoff_file") != str(handoff_path):
        raise RuntimeSwitchError(f"handoff is stale; use the latest runtime prepare result for role {role!r}")
    if payload.get("sha256") != _handoff_digest(handoff_path):
        raise RuntimeSwitchError(f"handoff file changed after preparation: {handoff_path}")
    if not _optional_string(payload.get("source_session_id")):
        raise RuntimeSwitchError(f"prepared handoff for role {role!r} has no source session ID")
    return payload


def _handoff_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeSwitchError(f"could not read prepared handoff {path}: {exc}") from exc
    return digest.hexdigest()


def _git_snapshot(worktree: Path | None) -> tuple[str, str]:
    if worktree is None or not worktree.is_dir():
        return "unavailable: worktree is missing", "unavailable: worktree is missing"
    return _git_output(worktree, ("status", "--short")), _git_output(worktree, ("diff", "--stat"))


def _git_output(worktree: Path, args: tuple[str, ...]) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(worktree), *args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"
    if result.returncode != 0:
        return f"unavailable: {(result.stderr or result.stdout).strip() or 'git command failed'}"
    return _bounded(result.stdout.strip() or "clean", GIT_OUTPUT_CHARS)


def _bounded_file_excerpt(path: Path, max_chars: int) -> str:
    try:
        return _bounded(path.read_text(encoding="utf-8").strip(), max_chars)
    except OSError:
        return ""


def _bounded(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "\n...[truncated]"


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _write_new_private_file(path: Path, text: str) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _append_lineage(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
