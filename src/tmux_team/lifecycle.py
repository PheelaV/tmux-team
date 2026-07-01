from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from .config import TeamConfig
from .store import Store, utc_now

APP_SERVER_WINDOW = "app-server"
CONTROL_PLANE_WINDOW = "control-plane"
SLEEP_SCHEMA_VERSION = 1


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class TmuxTarget:
    target: str
    session: str | None
    window_id: str | None
    window_name: str | None
    pane_id: str | None
    pane_title: str | None
    pane_dead: bool | None
    current_command: str | None
    live: bool
    details: str | None = None


@dataclass(frozen=True)
class SleepResult:
    sleep_id: str
    snapshot_path: Path | None
    latest_path: Path | None
    session: str | None
    commands: list[list[str]]
    role_count: int
    managed_windows: list[dict[str, Any]]


def sleep_team(
    config: TeamConfig,
    store: Store,
    conn: sqlite3.Connection,
    *,
    tmux_bin: str = "tmux",
    session: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    kill_session: bool = False,
    pause_roles: bool = True,
) -> SleepResult:
    if not config.roles:
        raise LifecycleError("no roles configured")

    if shutil.which(tmux_bin) is None and not dry_run:
        raise LifecycleError(f"tmux binary not found: {tmux_bin}")

    sleep_id = f"sleep_{utc_now().replace(':', '').replace('+', 'Z')}"
    role_targets = inspect_role_targets(config, tmux_bin, dry_run=dry_run)
    resolved_session = session or first_session(role_targets) or infer_session_from_config(config)
    managed_windows = managed_window_targets(
        role_targets,
        tmux_bin,
        resolved_session,
        dry_run=dry_run,
        force=force,
    )
    commands = teardown_commands(tmux_bin, resolved_session, managed_windows, kill_session=kill_session)
    snapshot = build_sleep_snapshot(
        sleep_id=sleep_id,
        config=config,
        store=store,
        conn=conn,
        session=resolved_session,
        role_targets=role_targets,
        managed_windows=managed_windows,
        commands=commands,
        kill_session=kill_session,
        pause_roles=pause_roles,
        dry_run=dry_run,
    )

    snapshot_path: Path | None = None
    latest_path: Path | None = None
    if not dry_run:
        snapshot_path, latest_path = write_sleep_snapshot(store.runtime_dir, sleep_id, snapshot)
        store.record_event(
            conn,
            "team.sleep.snapshot",
            "operator",
            sleep_id,
            {"snapshot_path": str(snapshot_path), "session": resolved_session, "role_count": len(config.roles)},
        )
        if pause_roles:
            pause_active_roles(store, conn)

    for command in commands:
        if dry_run:
            continue
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or f"tmux exited {result.returncode}").strip()
            raise LifecycleError(f"teardown command failed: {' '.join(command)}\n{details}")

    if not dry_run:
        store.record_event(
            conn,
            "team.sleep.teardown",
            "operator",
            sleep_id,
            {"commands": commands, "session": resolved_session, "kill_session": kill_session},
        )
        conn.commit()

    return SleepResult(
        sleep_id=sleep_id,
        snapshot_path=snapshot_path,
        latest_path=latest_path,
        session=resolved_session,
        commands=commands,
        role_count=len(config.roles),
        managed_windows=managed_windows,
    )


def inspect_role_targets(config: TeamConfig, tmux_bin: str, *, dry_run: bool) -> dict[str, TmuxTarget]:
    targets: dict[str, TmuxTarget] = {}
    for role_name, role in config.roles.items():
        pane = role.pane or ""
        if pane and not dry_run:
            target = inspect_tmux_target(tmux_bin, pane)
        else:
            target = infer_tmux_target(pane)
        targets[role_name] = target
    return targets


def inspect_tmux_target(tmux_bin: str, target: str) -> TmuxTarget:
    fmt = "#{session_name}\t#{window_id}\t#{window_name}\t#{pane_id}\t#{pane_title}\t#{pane_dead}\t#{pane_current_command}"
    result = subprocess.run(
        [tmux_bin, "display-message", "-p", "-t", target, fmt],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"tmux exited {result.returncode}").strip()
        inferred = infer_tmux_target(target)
        return TmuxTarget(
            target=target,
            session=inferred.session,
            window_id=None,
            window_name=inferred.window_name,
            pane_id=None,
            pane_title=None,
            pane_dead=None,
            current_command=None,
            live=False,
            details=details,
        )

    parts = result.stdout.rstrip("\n").split("\t", 6)
    if len(parts) != 7:
        inferred = infer_tmux_target(target)
        return TmuxTarget(
            target=target,
            session=inferred.session,
            window_id=None,
            window_name=inferred.window_name,
            pane_id=None,
            pane_title=None,
            pane_dead=None,
            current_command=None,
            live=False,
            details=f"unexpected tmux output: {result.stdout.strip()}",
        )
    return TmuxTarget(
        target=target,
        session=parts[0] or None,
        window_id=parts[1] or None,
        window_name=parts[2] or None,
        pane_id=parts[3] or None,
        pane_title=parts[4] or None,
        pane_dead=parts[5] == "1",
        current_command=parts[6] or None,
        live=True,
    )


def infer_tmux_target(target: str) -> TmuxTarget:
    session: str | None = None
    window_name: str | None = None
    if target and not target.startswith("%"):
        match = re.match(r"(?P<session>[^:]+):(?P<window>[^.]+)(?:\..*)?$", target)
        if match:
            session = match.group("session")
            window_name = match.group("window")
    return TmuxTarget(
        target=target,
        session=session,
        window_id=None,
        window_name=window_name,
        pane_id=target if target.startswith("%") else None,
        pane_title=None,
        pane_dead=None,
        current_command=None,
        live=False,
    )


def first_session(role_targets: dict[str, TmuxTarget]) -> str | None:
    for target in role_targets.values():
        if target.session:
            return target.session
    return None


def infer_session_from_config(config: TeamConfig) -> str | None:
    for role in config.roles.values():
        target = infer_tmux_target(role.pane or "")
        if target.session:
            return target.session
    return None


def managed_window_targets(
    role_targets: dict[str, TmuxTarget],
    tmux_bin: str,
    session: str | None,
    *,
    dry_run: bool,
    force: bool,
) -> list[dict[str, Any]]:
    windows: dict[str, dict[str, Any]] = {}
    for role, target in role_targets.items():
        key, value = target_window_key(target, session)
        if not key or not value:
            continue
        if target.window_name == CONTROL_PLANE_WINDOW and not force:
            raise LifecycleError(
                f"refusing to manage control-plane window for role {role}; rerun with --force if intended"
            )
        windows.setdefault(
            key,
            {
                "target": value,
                "window_id": target.window_id,
                "window_name": target.window_name,
                "roles": [],
                "kind": "roles",
            },
        )["roles"].append(role)

    app_server = app_server_window_target(tmux_bin, session, dry_run=dry_run)
    if app_server is not None:
        key, value = app_server
        windows.setdefault(
            key,
            {
                "target": value,
                "window_id": value if value.startswith("@") else None,
                "window_name": APP_SERVER_WINDOW,
                "roles": [],
                "kind": "app-server",
            },
        )

    return sorted(windows.values(), key=lambda item: (item["kind"], item["target"]))


def target_window_key(target: TmuxTarget, session: str | None) -> tuple[str | None, str | None]:
    if target.window_id:
        return f"id:{target.window_id}", target.window_id
    if target.window_name and (target.session or session):
        value = f"{target.session or session}:{target.window_name}"
        return f"name:{value}", value
    return None, None


def app_server_window_target(tmux_bin: str, session: str | None, *, dry_run: bool) -> tuple[str, str] | None:
    if not session:
        return None
    if dry_run:
        value = f"{session}:{APP_SERVER_WINDOW}"
        return f"name:{value}", value

    result = subprocess.run(
        [tmux_bin, "list-windows", "-t", session, "-F", "#{window_id}\t#{window_name}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1] == APP_SERVER_WINDOW:
            return f"id:{parts[0]}", parts[0]
    return None


def teardown_commands(
    tmux_bin: str,
    session: str | None,
    managed_windows: list[dict[str, Any]],
    *,
    kill_session: bool,
) -> list[list[str]]:
    if kill_session:
        if not session:
            raise LifecycleError("--kill-session requires a tmux session")
        return [[tmux_bin, "kill-session", "-t", session]]
    return [[tmux_bin, "kill-window", "-t", str(window["target"])] for window in managed_windows]


def build_sleep_snapshot(
    *,
    sleep_id: str,
    config: TeamConfig,
    store: Store,
    conn: sqlite3.Connection,
    session: str | None,
    role_targets: dict[str, TmuxTarget],
    managed_windows: list[dict[str, Any]],
    commands: list[list[str]],
    kill_session: bool,
    pause_roles: bool,
    dry_run: bool,
) -> dict[str, Any]:
    roles: dict[str, Any] = {}
    for role_name, role_config in config.roles.items():
        role_row = store.get_role(conn, role_name)
        resolved = store.resolve_role_app_server(conn, role_name, role_row) if role_row is not None else None
        target = role_targets[role_name]
        roles[role_name] = {
            "mode": role_config.mode,
            "state": role_row["state"] if role_row is not None else role_config.state,
            "pane": role_config.pane,
            "worktree": role_config.worktree,
            "capabilities": role_config.capabilities,
            "app_server": (
                {"endpoint": resolved[0], "thread_id": resolved[1], "timeout": resolved[2]}
                if resolved is not None
                else None
            ),
            "tmux": {
                "target": target.target,
                "session": target.session,
                "window_id": target.window_id,
                "window_name": target.window_name,
                "pane_id": target.pane_id,
                "pane_title": target.pane_title,
                "pane_dead": target.pane_dead,
                "current_command": target.current_command,
                "live": target.live,
                "details": target.details,
            },
        }

    return {
        "schema_version": SLEEP_SCHEMA_VERSION,
        "sleep_id": sleep_id,
        "created_at": utc_now(),
        "dry_run": dry_run,
        "team": {
            "name": config.name,
            "project_root": str(config.project_root) if config.project_root else None,
            "config_path": str(config.config_path) if config.config_path else None,
            "runtime_dir": str(config.runtime_dir),
        },
        "tmux": {
            "session": session,
            "kill_session": kill_session,
            "managed_windows": managed_windows,
            "teardown_commands": commands,
        },
        "roles": roles,
        "pause_roles": pause_roles,
    }


def write_sleep_snapshot(runtime_dir: Path, sleep_id: str, snapshot: dict[str, Any]) -> tuple[Path, Path]:
    sleep_dir = runtime_dir / "sleeps"
    sleep_dir.mkdir(parents=True, exist_ok=True)
    text = tomli_w.dumps(drop_none(snapshot))
    snapshot_path = sleep_dir / f"{sleep_id}.toml"
    latest_path = sleep_dir / "latest.toml"
    snapshot_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")
    return snapshot_path, latest_path


def pause_active_roles(store: Store, conn: sqlite3.Connection) -> None:
    for role in store.list_roles(conn):
        if role["state"] in ("active", "draining"):
            store.set_role_state(conn, role["name"], "paused", actor="sleep")


def drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [drop_none(item) for item in value if item is not None]
    return value
