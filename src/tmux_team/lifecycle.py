from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from .bootstrap import (
    DEFAULT_AGENTS_WINDOW,
    RoleBinding,
    RoleLaunchOptions,
    app_server_tmux_commands,
    configure_agent_window,
    label_role_pane,
    normalize_agent_layout,
    prepare_grouped_agent_window,
    role_resume_spawn_command,
    select_tiled_layout_commands,
    wait_for_app_server,
    write_role_env_files,
    write_team_config,
)
from .config import TeamConfig, load_config
from .store import Store, utc_now

APP_SERVER_WINDOW = "tt-app-server"
CONTROL_PLANE_WINDOW = "tt-control"
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


@dataclass(frozen=True)
class ResumeResult:
    snapshot_path: Path
    session: str
    endpoint: str
    commands: list[list[str]]
    role_count: int
    role_panes: dict[str, str]
    role_threads: dict[str, str]
    reactivated_roles: bool


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


def resume_team(
    config: TeamConfig,
    store: Store,
    conn: sqlite3.Connection,
    *,
    snapshot_path: Path | None = None,
    tmux_bin: str = "tmux",
    codex_bin: str = "codex",
    session: str | None = None,
    endpoint: str | None = None,
    agent_layout: str = "grouped",
    agents_window: str = DEFAULT_AGENTS_WINDOW,
    role_yolo: bool = False,
    role_profile: str | None = None,
    role_launch_options: dict[str, RoleLaunchOptions] | None = None,
    start_app_server: bool = True,
    reactivate_roles: bool = True,
    dry_run: bool = False,
) -> ResumeResult:
    if config.config_path is None:
        raise LifecycleError("resume requires a config file")
    if shutil.which(tmux_bin) is None and not dry_run:
        raise LifecycleError(f"tmux binary not found: {tmux_bin}")
    if shutil.which(codex_bin) is None and not dry_run:
        raise LifecycleError(f"codex binary not found: {codex_bin}")

    snapshot_path = (snapshot_path or store.runtime_dir / "sleeps" / "latest.toml").expanduser()
    if not snapshot_path.exists():
        raise LifecycleError(f"sleep snapshot does not exist: {snapshot_path}")
    snapshot = tomllib.loads(snapshot_path.read_text(encoding="utf-8"))
    validate_sleep_snapshot(snapshot)

    role_launch_options = role_launch_options or {}
    roles_data = snapshot["roles"]
    roles = tuple(str(role) for role in roles_data)
    if not roles:
        raise LifecycleError("sleep snapshot has no roles")
    unknown_launch_roles = set(role_launch_options or {}) - set(roles)
    if unknown_launch_roles:
        raise LifecycleError(
            f"Codex launch options specified for role(s) not in sleep snapshot: {', '.join(sorted(unknown_launch_roles))}"
        )
    agent_layout = normalize_agent_layout(agent_layout)
    session = session or snapshot.get("tmux", {}).get("session") or config.name
    endpoint = endpoint or first_snapshot_endpoint(snapshot)
    if not endpoint:
        raise LifecycleError("sleep snapshot has no app-server endpoint; pass --endpoint")

    role_bindings = snapshot_role_bindings(snapshot)
    commands: list[list[str]] = []
    if start_app_server:
        commands.extend(
            app_server_tmux_commands(tmux_bin, codex_bin, session, endpoint, config.project_root or Path.cwd())
        )
    commands.extend(
        resume_role_tmux_commands(
            tmux_bin,
            codex_bin,
            session,
            endpoint,
            config.config_path,
            roles,
            role_bindings,
            agent_layout,
            agents_window,
            role_yolo,
            role_profile,
            role_launch_options,
        )
    )

    if dry_run:
        return ResumeResult(
            snapshot_path=snapshot_path,
            session=session,
            endpoint=endpoint,
            commands=commands,
            role_count=len(roles),
            role_panes={role: binding.pane for role, binding in role_bindings.items()},
            role_threads={role: binding.thread_id for role, binding in role_bindings.items()},
            reactivated_roles=reactivate_roles,
        )

    if start_app_server:
        for command in app_server_tmux_commands(
            tmux_bin, codex_bin, session, endpoint, config.project_root or Path.cwd()
        ):
            subprocess_run_lifecycle(command)
    wait_for_app_server(endpoint, timeout=20.0)

    if agent_layout == "grouped":
        prepare_grouped_agent_window(tmux_bin, session, agents_window)
    resumed_bindings: dict[str, RoleBinding] = {}
    for index, role in enumerate(roles):
        binding = role_bindings[role]
        command = role_resume_spawn_command(
            tmux_bin,
            codex_bin,
            session,
            endpoint,
            binding.worktree,
            config.config_path,
            role,
            binding.thread_id,
            index,
            agent_layout,
            agents_window,
            role_yolo,
            role_profile,
            role_launch_options.get(role, RoleLaunchOptions()),
            print_pane=True,
        )
        result = subprocess_run_lifecycle(command)
        pane = result.stdout.strip() or binding.pane
        if agent_layout == "grouped":
            if index == 0:
                configure_agent_window(tmux_bin, session, agents_window)
            else:
                for layout_command in select_tiled_layout_commands(tmux_bin, session, agents_window):
                    subprocess_run_lifecycle(layout_command)
        label_role_pane(tmux_bin, pane, role)
        resumed_bindings[role] = RoleBinding(thread_id=binding.thread_id, pane=pane, worktree=binding.worktree)

    role_scratchpads = {
        role: config.roles[role].scratchpad or f".tmux-team/memory/{role}.md" for role in roles if role in config.roles
    }
    write_team_config(
        config.config_path,
        str(config.runtime_dir),
        endpoint,
        resumed_bindings,
        role_yolo,
        role_profile,
        role_launch_options,
        role_scratchpads,
        force=True,
    )
    write_role_env_files(config.config_path, resumed_bindings)

    resumed_config = load_resumed_config(config.config_path)
    store.sync_roles(conn, resumed_config.roles.values())
    for role, binding in resumed_bindings.items():
        store.bind_role_app_server(conn, role, endpoint, binding.thread_id)
        if reactivate_roles:
            store.set_role_state(conn, role, "active", actor="resume")
    store.record_event(
        conn,
        "team.resume",
        "operator",
        str(snapshot_path),
        {"session": session, "endpoint": endpoint, "roles": list(roles), "reactivated": reactivate_roles},
    )
    conn.commit()

    return ResumeResult(
        snapshot_path=snapshot_path,
        session=session,
        endpoint=endpoint,
        commands=commands,
        role_count=len(roles),
        role_panes={role: binding.pane for role, binding in resumed_bindings.items()},
        role_threads={role: binding.thread_id for role, binding in resumed_bindings.items()},
        reactivated_roles=reactivate_roles,
    )


def validate_sleep_snapshot(snapshot: dict[str, Any]) -> None:
    if int(snapshot.get("schema_version") or 0) != SLEEP_SCHEMA_VERSION:
        raise LifecycleError(f"unsupported sleep snapshot schema_version: {snapshot.get('schema_version')}")
    if not isinstance(snapshot.get("roles"), dict):
        raise LifecycleError("sleep snapshot is missing roles")


def first_snapshot_endpoint(snapshot: dict[str, Any]) -> str | None:
    for role_data in snapshot.get("roles", {}).values():
        app_server = role_data.get("app_server") if isinstance(role_data, dict) else None
        if isinstance(app_server, dict) and app_server.get("endpoint"):
            return str(app_server["endpoint"])
    return None


def snapshot_role_bindings(snapshot: dict[str, Any]) -> dict[str, RoleBinding]:
    bindings: dict[str, RoleBinding] = {}
    for role, role_data in snapshot["roles"].items():
        if not isinstance(role_data, dict):
            raise LifecycleError(f"invalid role entry in sleep snapshot: {role}")
        app_server = role_data.get("app_server")
        if not isinstance(app_server, dict) or not app_server.get("thread_id"):
            raise LifecycleError(f"sleep snapshot role {role!r} has no saved Codex thread id")
        worktree = role_data.get("worktree")
        if not worktree:
            raise LifecycleError(f"sleep snapshot role {role!r} has no worktree")
        bindings[str(role)] = RoleBinding(
            thread_id=str(app_server["thread_id"]),
            pane=str(role_data.get("pane") or ""),
            worktree=Path(str(worktree)).expanduser().resolve(),
        )
    return bindings


def resume_role_tmux_commands(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    config_path: Path,
    roles: tuple[str, ...],
    role_bindings: dict[str, RoleBinding],
    agent_layout: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    role_launch_options: dict[str, RoleLaunchOptions],
) -> list[list[str]]:
    commands: list[list[str]] = []
    for index, role in enumerate(roles):
        binding = role_bindings[role]
        commands.append(
            role_resume_spawn_command(
                tmux_bin,
                codex_bin,
                session,
                endpoint,
                binding.worktree,
                config_path,
                role,
                binding.thread_id,
                index,
                agent_layout,
                agents_window,
                role_yolo,
                role_profile,
                role_launch_options.get(role, RoleLaunchOptions()),
            )
        )
        if agent_layout == "grouped" and index == 0:
            commands.extend(
                [
                    [tmux_bin, "set-window-option", "-t", f"{session}:{agents_window}", "pane-border-status", "top"],
                    [
                        tmux_bin,
                        "set-window-option",
                        "-t",
                        f"{session}:{agents_window}",
                        "pane-border-format",
                        "#{pane_index}: #{@tmux-team-role}",
                    ],
                ]
            )
        if agent_layout == "grouped" and index > 0:
            commands.extend(select_tiled_layout_commands(tmux_bin, session, agents_window))
    return commands


def subprocess_run_lifecycle(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"command exited {result.returncode}").strip()
        raise LifecycleError(f"command failed: {' '.join(command)}\n{details}")
    return result


def load_resumed_config(config_path: Path) -> TeamConfig:
    return load_config(config_path)


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
                f"refusing to manage {CONTROL_PLANE_WINDOW} window for role {role}; rerun with --force if intended"
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
