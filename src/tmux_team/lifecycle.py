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

from .acp_tui import ACPControlError, control_socket_path, send_control_request, wait_for_acp_tui
from .bootstrap import (
    DEFAULT_AGENTS_WINDOW,
    RoleBinding,
    RoleLaunchOptions,
    acp_role_spawn_command,
    app_server_tmux_commands,
    configure_agent_window,
    configure_session_truecolor,
    label_role_pane,
    normalize_agent_layout,
    prepare_grouped_agent_window,
    resolve_tool_executable,
    role_resume_spawn_command,
    select_tiled_layout_commands,
    session_truecolor_tmux_commands,
    wait_for_app_server,
    write_role_env_files,
    write_team_config,
)
from .config import OperatorConfig, TeamConfig, load_config, update_role_runtime_binding
from .display import format_seconds_duration
from .runtime_switch import prepare_runtime_handoff, recovery_prompt
from .store import Store, utc_now
from .watchdog_runner import (
    watchdog_layout_command,
    watchdog_pane_setup_commands,
    watchdog_spawn_command,
    watchdog_window_name,
)

APP_SERVER_WINDOW = "tt-app-server"
CONTROL_PLANE_WINDOW = "tt-control"
SLEEP_SCHEMA_VERSION = 1
ACP_RESUME_POLICIES = ("exact", "handoff")


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
    watchdog_count: int
    managed_windows: list[dict[str, Any]]


@dataclass(frozen=True)
class ResumeResult:
    snapshot_path: Path
    session: str
    endpoint: str | None
    agent_runtime: str
    commands: list[list[str]]
    role_count: int
    role_panes: dict[str, str]
    role_threads: dict[str, str]
    watchdog_panes: dict[str, str]
    reactivated_roles: bool
    restored_launch_roles: tuple[str, ...]


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
    acp_resume_policy: str = "exact",
) -> SleepResult:
    if not config.roles:
        raise LifecycleError("no roles configured")
    agent_runtime = configured_agent_runtime(config)

    if shutil.which(tmux_bin) is None and not dry_run:
        raise LifecycleError(f"tmux binary not found: {tmux_bin}")

    sleep_id = f"sleep_{utc_now().replace(':', '').replace('+', 'Z')}"
    role_targets = inspect_role_targets(config, tmux_bin, dry_run=dry_run)
    watchdog_targets = inspect_watchdog_targets(store, conn, tmux_bin, dry_run=dry_run)
    resolved_session = session or first_session(role_targets) or infer_session_from_config(config)
    managed_windows = managed_window_targets(
        role_targets,
        watchdog_targets,
        tmux_bin,
        resolved_session,
        dry_run=dry_run,
        force=force,
        include_app_server=agent_runtime == "codex",
    )
    commands = teardown_commands(tmux_bin, resolved_session, managed_windows, kill_session=kill_session)
    acp_sleep_metadata: dict[str, dict[str, Any]] = {}
    if agent_runtime == "acp":
        acp_sleep_metadata = prepare_acp_roles_for_sleep(
            config,
            store,
            conn,
            policy=normalize_acp_resume_policy(acp_resume_policy),
            dry_run=dry_run,
        )
    snapshot_path: Path | None = None
    latest_path: Path | None = None
    try:
        snapshot = build_sleep_snapshot(
            sleep_id=sleep_id,
            config=config,
            store=store,
            conn=conn,
            session=resolved_session,
            role_targets=role_targets,
            watchdog_targets=watchdog_targets,
            managed_windows=managed_windows,
            commands=commands,
            kill_session=kill_session,
            pause_roles=pause_roles,
            dry_run=dry_run,
            tmux_bin=tmux_bin,
            acp_sleep_metadata=acp_sleep_metadata,
        )
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
    except Exception:
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        if latest_path is not None:
            latest_path.unlink(missing_ok=True)
        if agent_runtime == "acp" and not dry_run:
            rollback_acp_sleep_preparation(store, conn, acp_sleep_metadata)
        raise

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
        watchdog_count=len(watchdog_targets),
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
    acp_resume_policy: str | None = None,
    start_app_server: bool = True,
    reactivate_roles: bool = True,
    dry_run: bool = False,
    enable_truecolor: bool = True,
) -> ResumeResult:
    if config.config_path is None:
        raise LifecycleError("resume requires a config file")
    if shutil.which(tmux_bin) is None and not dry_run:
        raise LifecycleError(f"tmux binary not found: {tmux_bin}")

    requested_snapshot = (snapshot_path or store.runtime_dir / "sleeps" / "latest.toml").expanduser()
    snapshot_path, snapshot = load_resume_snapshot(
        config,
        store,
        conn,
        requested_snapshot=requested_snapshot,
        explicit_snapshot=snapshot_path is not None,
        session=session,
        tmux_bin=tmux_bin,
        dry_run=dry_run,
    )
    validate_sleep_snapshot(snapshot)

    snapshot_runtime = snapshot_agent_runtime(snapshot)
    if snapshot_runtime == "acp":
        return resume_acp_team(
            config,
            store,
            conn,
            snapshot_path=snapshot_path,
            snapshot=snapshot,
            tmux_bin=tmux_bin,
            session=session,
            agent_layout=agent_layout,
            agents_window=agents_window,
            reactivate_roles=reactivate_roles,
            dry_run=dry_run,
            enable_truecolor=enable_truecolor,
            requested_policy=acp_resume_policy,
        )
    if shutil.which(codex_bin) is None and not dry_run:
        raise LifecycleError(f"codex binary not found: {codex_bin}")

    explicit_role_launch_options = role_launch_options or {}
    roles_data = snapshot["roles"]
    roles = tuple(str(role) for role in roles_data)
    if not roles:
        raise LifecycleError("sleep snapshot has no roles")
    unknown_launch_roles = set(explicit_role_launch_options) - set(roles)
    if unknown_launch_roles:
        raise LifecycleError(
            f"Codex launch options specified for role(s) not in sleep snapshot: {', '.join(sorted(unknown_launch_roles))}"
        )
    role_launch_options = merge_role_launch_options(
        saved_role_launch_options(snapshot, config, roles),
        explicit_role_launch_options,
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
    if enable_truecolor:
        commands.extend(session_truecolor_tmux_commands(tmux_bin, session))
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
    watchdogs_data = snapshot_watchdog_runners(snapshot)
    commands.extend(
        resume_watchdog_tmux_commands(
            tmux_bin,
            session,
            config.config_path,
            config.project_root or Path.cwd(),
            watchdogs_data,
        )
    )

    if dry_run:
        return ResumeResult(
            snapshot_path=snapshot_path,
            session=session,
            endpoint=endpoint,
            agent_runtime="codex",
            commands=commands,
            role_count=len(roles),
            role_panes={role: binding.pane for role, binding in role_bindings.items()},
            role_threads={role: binding.thread_id for role, binding in role_bindings.items()},
            watchdog_panes={name: str(data.get("pane") or "") for name, data in watchdogs_data.items()},
            reactivated_roles=reactivate_roles,
            restored_launch_roles=tuple(sorted(role for role, options in role_launch_options.items() if options)),
        )

    if start_app_server:
        for command in app_server_tmux_commands(
            tmux_bin, codex_bin, session, endpoint, config.project_root or Path.cwd()
        ):
            subprocess_run_lifecycle(command)
    if enable_truecolor:
        configure_session_truecolor(tmux_bin, session)
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
        operator=operator_config_from_snapshot(snapshot, fallback=config.operator),
    )
    write_role_env_files(config.config_path, resumed_bindings)

    resumed_config = load_resumed_config(config.config_path)
    store.sync_roles(conn, resumed_config.roles.values())
    for role, binding in resumed_bindings.items():
        store.bind_role_app_server(conn, role, endpoint, binding.thread_id)
        if reactivate_roles:
            store.set_role_state(conn, role, "active", actor="resume")
    resumed_watchdog_panes: dict[str, str] = {}
    watchdog_window_present = bool(watchdogs_data) and tmux_window_exists(tmux_bin, session, watchdog_window_name())
    for index, (name, data) in enumerate(watchdogs_data.items()):
        pane = start_resumed_watchdog_runner(
            tmux_bin,
            session,
            config.config_path,
            config.project_root or Path.cwd(),
            name,
            data,
            use_existing_window=watchdog_window_present or index > 0,
        )
        watchdog_window_present = True
        resumed_watchdog_panes[name] = pane
        store.upsert_watchdog_runner(
            conn,
            name=name,
            state=str(data.get("state") or "running"),
            interval_seconds=int(data["interval_seconds"]),
            scope_role=optional_string(data.get("scope_role")),
            description=optional_string(data.get("description")),
            goal=optional_string(data.get("goal")),
            notify_role=optional_string(data.get("notify_role")),
            delivery_method=str(data.get("delivery_method") or "report-only"),
            pane=pane,
            window=f"{session}:{watchdog_window_name()}",
            actor="resume",
        )
    store.record_event(
        conn,
        "team.resume",
        "operator",
        str(snapshot_path),
        {
            "session": session,
            "endpoint": endpoint,
            "roles": list(roles),
            "watchdogs": list(watchdogs_data),
            "reactivated": reactivate_roles,
        },
    )
    conn.commit()

    return ResumeResult(
        snapshot_path=snapshot_path,
        session=session,
        endpoint=endpoint,
        agent_runtime="codex",
        commands=commands,
        role_count=len(roles),
        role_panes={role: binding.pane for role, binding in resumed_bindings.items()},
        role_threads={role: binding.thread_id for role, binding in resumed_bindings.items()},
        watchdog_panes=resumed_watchdog_panes,
        reactivated_roles=reactivate_roles,
        restored_launch_roles=tuple(sorted(role for role, options in role_launch_options.items() if options)),
    )


def resume_acp_team(
    config: TeamConfig,
    store: Store,
    conn: sqlite3.Connection,
    *,
    snapshot_path: Path,
    snapshot: dict[str, Any],
    tmux_bin: str,
    session: str | None,
    agent_layout: str,
    agents_window: str,
    reactivate_roles: bool,
    dry_run: bool,
    enable_truecolor: bool,
    requested_policy: str | None,
) -> ResumeResult:
    assert config.config_path is not None
    roles = tuple(str(role) for role in snapshot["roles"])
    if not roles:
        raise LifecycleError("sleep snapshot has no roles")
    policy_override = normalize_acp_resume_policy(requested_policy) if requested_policy else None
    role_metadata: dict[str, dict[str, Any]] = {}
    role_bindings: dict[str, RoleBinding] = {}
    for role in roles:
        role_data = snapshot["roles"][role]
        if not isinstance(role_data, dict):
            raise LifecycleError(f"invalid role entry in sleep snapshot: {role}")
        worktree = role_data.get("worktree")
        if not worktree:
            raise LifecycleError(f"sleep snapshot role {role!r} has no worktree")
        metadata = snapshot_acp_metadata(snapshot, config, role)
        policy = policy_override or str(metadata.get("resume_policy") or "exact")
        policy = normalize_acp_resume_policy(policy)
        session_id = optional_string(metadata.get("session_id"))
        if policy == "exact":
            if not session_id:
                raise LifecycleError(f"ACP exact resume for role {role!r} has no saved session ID")
            if metadata.get("resume_supported") is not True:
                raise LifecycleError(f"ACP exact resume for role {role!r} was not capability-verified")
        elif not metadata.get("handoff_file"):
            raise LifecycleError(f"ACP handoff resume for role {role!r} has no saved handoff capsule")
        tui_bin = optional_string(metadata.get("acp_tui_bin"))
        agent_command = optional_string(metadata.get("acp_agent_command"))
        socket_value = optional_string(metadata.get("control_socket")) or str(
            control_socket_path(config.runtime_dir, role)
        )
        if not tui_bin or not agent_command:
            raise LifecycleError(f"ACP resume role {role!r} is missing Toad or agent-command metadata")
        if not dry_run:
            resolved_tui = resolve_tool_executable(tui_bin)
            if resolved_tui is None:
                raise LifecycleError(f"ACP TUI binary not found for role {role!r}: {tui_bin}")
            tui_bin = resolved_tui
        metadata.update(
            {
                "resume_policy": policy,
                "session_id": session_id,
                "control_socket": socket_value,
                "acp_tui_bin": tui_bin,
                "acp_agent_command": agent_command,
            }
        )
        role_metadata[role] = metadata
        role_bindings[role] = RoleBinding(
            thread_id="",
            pane=str(role_data.get("pane") or ""),
            worktree=Path(str(worktree)).expanduser().resolve(),
            session_id=session_id or "",
            control_socket=socket_value,
            resume_supported=bool(metadata.get("resume_supported")),
        )

    session = session or snapshot.get("tmux", {}).get("session") or config.name
    agent_layout = normalize_agent_layout(agent_layout)
    commands: list[list[str]] = []
    if enable_truecolor:
        commands.extend(session_truecolor_tmux_commands(tmux_bin, session))
    commands.extend(
        resume_acp_role_tmux_commands(
            tmux_bin,
            session,
            config.config_path,
            roles,
            role_bindings,
            role_metadata,
            agent_layout,
            agents_window,
        )
    )
    watchdogs_data = snapshot_watchdog_runners(snapshot)
    commands.extend(
        resume_watchdog_tmux_commands(
            tmux_bin,
            session,
            config.config_path,
            config.project_root or Path.cwd(),
            watchdogs_data,
        )
    )
    if dry_run:
        return ResumeResult(
            snapshot_path=snapshot_path,
            session=session,
            endpoint=None,
            agent_runtime="acp",
            commands=commands,
            role_count=len(roles),
            role_panes={role: binding.pane for role, binding in role_bindings.items()},
            role_threads={
                role: str(role_metadata[role].get("session_id") or "")
                if role_metadata[role]["resume_policy"] == "exact"
                else ""
                for role in roles
            },
            watchdog_panes={name: str(data.get("pane") or "") for name, data in watchdogs_data.items()},
            reactivated_roles=reactivate_roles,
            restored_launch_roles=(),
        )

    if enable_truecolor:
        configure_session_truecolor(tmux_bin, session)
    if agent_layout == "grouped":
        prepare_grouped_agent_window(tmux_bin, session, agents_window)
    resumed_bindings: dict[str, RoleBinding] = {}
    for index, role in enumerate(roles):
        binding = role_bindings[role]
        metadata = role_metadata[role]
        exact_session = binding.session_id if metadata["resume_policy"] == "exact" else None
        command = acp_role_spawn_command(
            tmux_bin,
            str(metadata["acp_tui_bin"]),
            str(metadata["acp_agent_command"]),
            session,
            binding.worktree,
            config.config_path,
            role,
            binding.control_socket,
            index,
            agent_layout,
            agents_window,
            exact_session,
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
        try:
            status = wait_for_acp_tui(Path(binding.control_socket), timeout=30.0)
        except ACPControlError as exc:
            raise LifecycleError(f"ACP role {role!r} did not resume: {exc}") from exc
        resumed_session = optional_string(status.get("sessionId"))
        if metadata["resume_policy"] == "exact":
            if resumed_session != binding.session_id:
                raise LifecycleError(
                    f"ACP exact resume session mismatch for role {role!r}: expected {binding.session_id!r}, "
                    f"got {resumed_session or 'unknown'!r}"
                )
            if status.get("resumeSupported") is not True:
                raise LifecycleError(f"ACP exact resume capability disappeared for role {role!r}")
        elif not resumed_session:
            raise LifecycleError(f"ACP handoff resume did not create a session for role {role!r}")
        if metadata["resume_policy"] == "handoff":
            handoff_path = Path(str(metadata["handoff_file"]))
            send_control_request(
                Path(binding.control_socket),
                {
                    "action": "prompt",
                    "text": recovery_prompt(config, role, handoff_path),
                    "priority": "normal",
                    "coalesceKey": "sleep-handoff",
                },
            )
        resumed_bindings[role] = RoleBinding(
            thread_id="",
            pane=pane,
            worktree=binding.worktree,
            session_id=resumed_session or "",
            control_socket=binding.control_socket,
            resume_supported=bool(status.get("resumeSupported")),
        )
        update_role_runtime_binding(
            config.config_path,
            role,
            pane=pane,
            state="active" if reactivate_roles else "paused",
            capabilities={
                "control_socket": binding.control_socket,
                "runtime_session_id": resumed_session,
                "acp_resume_supported": status.get("resumeSupported")
                if isinstance(status.get("resumeSupported"), bool)
                else None,
                "last_handoff_file": metadata.get("handoff_file"),
            },
        )

    write_role_env_files(config.config_path, resumed_bindings)
    resumed_config = load_resumed_config(config.config_path)
    store.config = resumed_config
    store.sync_roles(conn, resumed_config.roles.values())
    resumed_watchdog_panes = resume_watchdogs_from_snapshot(
        store,
        conn,
        tmux_bin=tmux_bin,
        session=session,
        config_path=config.config_path,
        project_root=config.project_root or Path.cwd(),
        watchdogs_data=watchdogs_data,
    )
    if reactivate_roles:
        for role in roles:
            if store.pending_count(conn, role):
                store.notify_role(conn, role, "control-socket")
    store.record_event(
        conn,
        "team.resume",
        "operator",
        str(snapshot_path),
        {
            "session": session,
            "agent_runtime": "acp",
            "roles": list(roles),
            "resume_policies": {role: role_metadata[role]["resume_policy"] for role in roles},
            "watchdogs": list(watchdogs_data),
            "reactivated": reactivate_roles,
        },
    )
    conn.commit()
    return ResumeResult(
        snapshot_path=snapshot_path,
        session=session,
        endpoint=None,
        agent_runtime="acp",
        commands=commands,
        role_count=len(roles),
        role_panes={role: binding.pane for role, binding in resumed_bindings.items()},
        role_threads={role: binding.session_id for role, binding in resumed_bindings.items()},
        watchdog_panes=resumed_watchdog_panes,
        reactivated_roles=reactivate_roles,
        restored_launch_roles=(),
    )


def validate_sleep_snapshot(snapshot: dict[str, Any]) -> None:
    if int(snapshot.get("schema_version") or 0) != SLEEP_SCHEMA_VERSION:
        raise LifecycleError(f"unsupported sleep snapshot schema_version: {snapshot.get('schema_version')}")
    if not isinstance(snapshot.get("roles"), dict):
        raise LifecycleError("sleep snapshot is missing roles")


def load_resume_snapshot(
    config: TeamConfig,
    store: Store,
    conn: sqlite3.Connection,
    *,
    requested_snapshot: Path,
    explicit_snapshot: bool,
    session: str | None,
    tmux_bin: str,
    dry_run: bool,
) -> tuple[Path, dict[str, Any]]:
    if requested_snapshot.exists():
        return requested_snapshot, tomllib.loads(requested_snapshot.read_text(encoding="utf-8"))
    if explicit_snapshot:
        raise LifecycleError(f"sleep snapshot does not exist: {requested_snapshot}")

    snapshot = build_recovery_snapshot(
        config,
        store,
        conn,
        session=session,
        tmux_bin=tmux_bin,
    )
    recovery_path = store.runtime_dir / "sleeps" / "recovery_latest.toml"
    if not dry_run:
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        recovery_path.write_text(tomli_w.dumps(drop_none(snapshot)), encoding="utf-8")
        store.record_event(
            conn,
            "team.resume.recovery_snapshot",
            "operator",
            snapshot["sleep_id"],
            {"snapshot_path": str(recovery_path), "role_count": len(snapshot["roles"])},
        )
    return recovery_path, snapshot


def build_recovery_snapshot(
    config: TeamConfig,
    store: Store,
    conn: sqlite3.Connection,
    *,
    session: str | None,
    tmux_bin: str,
) -> dict[str, Any]:
    role_targets = inspect_role_targets(config, tmux_bin, dry_run=True)
    watchdog_targets = inspect_watchdog_targets(store, conn, tmux_bin, dry_run=True)
    resolved_session = session or first_session(role_targets) or infer_session_from_config(config)
    snapshot = build_sleep_snapshot(
        sleep_id=f"recovery_{utc_now().replace(':', '').replace('+', 'Z')}",
        config=config,
        store=store,
        conn=conn,
        session=resolved_session,
        role_targets=role_targets,
        watchdog_targets=watchdog_targets,
        managed_windows=[],
        commands=[],
        kill_session=False,
        pause_roles=False,
        dry_run=True,
        tmux_bin=tmux_bin,
    )
    snapshot["recovery"] = {
        "source": "runtime-state",
        "reason": "no sleep snapshot found",
    }
    return snapshot


def saved_role_launch_options(
    snapshot: dict[str, Any],
    config: TeamConfig,
    roles: tuple[str, ...],
) -> dict[str, RoleLaunchOptions]:
    options: dict[str, RoleLaunchOptions] = {}
    for role in roles:
        snapshot_capabilities = snapshot_role_capabilities(snapshot, role)
        config_role = config.roles.get(role)
        config_capabilities = config_role.capabilities if config_role is not None else {}
        launch_options = role_launch_options_from_capabilities(config_capabilities | snapshot_capabilities)
        if launch_options != RoleLaunchOptions():
            options[role] = launch_options
    return options


def snapshot_role_capabilities(snapshot: dict[str, Any], role: str) -> dict[str, Any]:
    role_data = snapshot.get("roles", {}).get(role)
    if not isinstance(role_data, dict):
        return {}
    capabilities = role_data.get("capabilities")
    return dict(capabilities) if isinstance(capabilities, dict) else {}


def role_launch_options_from_capabilities(capabilities: dict[str, Any]) -> RoleLaunchOptions:
    codex_config = capabilities.get("codex_config")
    if isinstance(codex_config, (list, tuple)):
        config_overrides = tuple(str(item) for item in codex_config if str(item))
    elif codex_config:
        config_overrides = (str(codex_config),)
    else:
        config_overrides = ()
    return RoleLaunchOptions(
        model=optional_capability(capabilities, "codex_model"),
        reasoning_effort=optional_capability(capabilities, "codex_reasoning_effort"),
        profile=optional_capability(capabilities, "codex_profile"),
        config_overrides=config_overrides,
        yolo=optional_bool_capability(capabilities, "codex_yolo"),
    )


def merge_role_launch_options(
    saved: dict[str, RoleLaunchOptions],
    explicit: dict[str, RoleLaunchOptions],
) -> dict[str, RoleLaunchOptions]:
    merged = dict(saved)
    for role, override in explicit.items():
        base = merged.get(role, RoleLaunchOptions())
        merged[role] = RoleLaunchOptions(
            model=override.model if override.model is not None else base.model,
            reasoning_effort=override.reasoning_effort
            if override.reasoning_effort is not None
            else base.reasoning_effort,
            profile=override.profile if override.profile is not None else base.profile,
            config_overrides=override.config_overrides or base.config_overrides,
            yolo=override.yolo if override.yolo is not None else base.yolo,
        )
    return {role: options for role, options in merged.items() if options != RoleLaunchOptions()}


def optional_capability(capabilities: dict[str, Any], key: str) -> str | None:
    value = capabilities.get(key)
    if value is None or value == "":
        return None
    return str(value)


def optional_bool_capability(capabilities: dict[str, Any], key: str) -> bool | None:
    value = capabilities.get(key)
    if isinstance(value, bool):
        return value
    return None


def normalize_acp_resume_policy(value: str) -> str:
    policy = str(value).strip().lower()
    if policy not in ACP_RESUME_POLICIES:
        raise LifecycleError(f"invalid ACP resume policy {value!r}; expected one of: {', '.join(ACP_RESUME_POLICIES)}")
    return policy


def configured_agent_runtime(config: TeamConfig) -> str:
    runtimes = {"acp" if role.mode == "acp_tui" else "codex" for role in config.roles.values()}
    if len(runtimes) != 1:
        raise LifecycleError("sleep/resume requires a homogeneous Codex or ACP role runtime")
    return next(iter(runtimes))


def prepare_acp_roles_for_sleep(
    config: TeamConfig,
    store: Store,
    conn: sqlite3.Connection,
    *,
    policy: str,
    dry_run: bool,
) -> dict[str, dict[str, Any]]:
    prepared: dict[str, dict[str, Any]] = {}
    previous_states: dict[str, str] = {}
    quiesced: list[tuple[str, Path, str]] = []

    for role, role_config in config.roles.items():
        capabilities = role_config.capabilities
        session_id = optional_capability(capabilities, "runtime_session_id")
        socket_value = optional_capability(capabilities, "control_socket")
        tui_bin = optional_capability(capabilities, "acp_tui_bin")
        agent_command = optional_capability(capabilities, "acp_agent_command")
        if not session_id or not socket_value or not tui_bin or not agent_command:
            raise LifecycleError(f"ACP role {role!r} is missing session, socket, TUI, or agent-command metadata")
        if dry_run:
            status = {
                "state": "idle",
                "sessionId": session_id,
                "queueDepth": 0,
                "resumeSupported": capabilities.get("acp_resume_supported"),
            }
        else:
            try:
                status = send_control_request(Path(socket_value), {"action": "status", "sessionId": session_id})
            except ACPControlError as exc:
                raise LifecycleError(f"could not inspect ACP role {role!r} before sleep: {exc}") from exc
        _validate_acp_sleep_status(role, status, session_id=session_id, policy=policy)
        role_row = store.get_role(conn, role)
        previous_states[role] = str(role_row["state"] if role_row is not None else role_config.state)
        prepared[role] = {
            "resume_policy": policy,
            "session_id": session_id,
            "resume_supported": bool(status.get("resumeSupported")),
            "control_socket": socket_value,
            "acp_tui_bin": tui_bin,
            "acp_agent_command": agent_command,
            "provider": optional_capability(capabilities, "acp_provider"),
            "model": optional_capability(capabilities, "acp_model"),
            "effort": optional_capability(capabilities, "acp_effort"),
            "original_state": previous_states[role],
        }

    if dry_run:
        return prepared

    try:
        for role in config.roles:
            if previous_states[role] != "draining":
                store.set_role_state(conn, role, "draining", actor="sleep")
        for role, metadata in prepared.items():
            socket_path = Path(str(metadata["control_socket"]))
            session_id = str(metadata["session_id"])
            response = send_control_request(
                socket_path,
                {"action": "quiesce", "sessionId": session_id},
            )
            if response.get("acceptingPrompts") is False:
                quiesced.append((role, socket_path, session_id))
            _validate_acp_sleep_status(
                role,
                response,
                session_id=session_id,
                policy=policy,
                require_resume_capability=False,
            )
            if response.get("acceptingPrompts") is not False:
                raise LifecycleError(f"ACP role {role!r} did not confirm prompt quiescence")
        for role, metadata in prepared.items():
            handoff = prepare_runtime_handoff(
                store,
                conn,
                role,
                summary=f"Sleep checkpoint for exact or handoff ACP resume ({policy}).",
                actor="sleep",
            )
            metadata["handoff_file"] = str(handoff)
    except Exception:
        rollback_acp_sleep_preparation(store, conn, prepared, quiesced=quiesced)
        raise
    return prepared


def rollback_acp_sleep_preparation(
    store: Store,
    conn: sqlite3.Connection,
    metadata: dict[str, dict[str, Any]],
    *,
    quiesced: list[tuple[str, Path, str]] | None = None,
) -> None:
    targets = quiesced or [
        (role, Path(str(data["control_socket"])), str(data["session_id"])) for role, data in metadata.items()
    ]
    for _role, socket_path, session_id in reversed(targets):
        try:
            send_control_request(socket_path, {"action": "unquiesce", "sessionId": session_id})
        except ACPControlError:
            pass
    for role, data in metadata.items():
        state = str(data.get("original_state") or "active")
        current = store.get_role(conn, role)
        if current is not None and current["state"] != state:
            store.set_role_state(conn, role, state, actor="sleep-rollback")


def _validate_acp_sleep_status(
    role: str,
    status: dict[str, Any],
    *,
    session_id: str,
    policy: str,
    require_resume_capability: bool = True,
) -> None:
    state = str(status.get("state") or "unknown")
    if state != "idle":
        raise LifecycleError(f"ACP role {role!r} is not idle for sleep (state={state})")
    queue_depth = int(status.get("queueDepth") or 0)
    if queue_depth:
        raise LifecycleError(f"ACP role {role!r} has {queue_depth} queued prompt(s)")
    reported_session = optional_string(status.get("sessionId"))
    if reported_session != session_id:
        raise LifecycleError(
            f"ACP role {role!r} session mismatch before sleep: expected {session_id!r}, "
            f"got {reported_session or 'unknown'!r}"
        )
    if require_resume_capability and policy == "exact" and status.get("resumeSupported") is not True:
        raise LifecycleError(f"ACP role {role!r} does not advertise exact session resume support")


def operator_config_from_snapshot(snapshot: dict[str, Any], *, fallback: OperatorConfig) -> OperatorConfig:
    operator_data = snapshot.get("operator")
    if not isinstance(operator_data, dict):
        return fallback
    known_keys = {"pane", "codex_thread_id", "tmux"}
    return OperatorConfig(
        pane=str(operator_data["pane"]) if operator_data.get("pane") else fallback.pane,
        codex_thread_id=str(operator_data["codex_thread_id"])
        if operator_data.get("codex_thread_id")
        else fallback.codex_thread_id,
        capabilities={key: item for key, item in operator_data.items() if key not in known_keys},
    )


def first_snapshot_endpoint(snapshot: dict[str, Any]) -> str | None:
    for role_data in snapshot.get("roles", {}).values():
        app_server = role_data.get("app_server") if isinstance(role_data, dict) else None
        if isinstance(app_server, dict) and app_server.get("endpoint"):
            return str(app_server["endpoint"])
    return None


def snapshot_agent_runtime(snapshot: dict[str, Any]) -> str:
    modes = {
        "acp" if isinstance(data, dict) and data.get("mode") == "acp_tui" else "codex"
        for data in snapshot.get("roles", {}).values()
    }
    if len(modes) != 1:
        raise LifecycleError("sleep snapshot mixes Codex and ACP role runtimes")
    return next(iter(modes))


def snapshot_acp_metadata(snapshot: dict[str, Any], config: TeamConfig, role: str) -> dict[str, Any]:
    role_data = snapshot.get("roles", {}).get(role)
    if not isinstance(role_data, dict):
        raise LifecycleError(f"invalid ACP role entry in sleep snapshot: {role}")
    metadata = dict(role_data.get("acp")) if isinstance(role_data.get("acp"), dict) else {}
    capabilities = snapshot_role_capabilities(snapshot, role)
    config_role = config.roles.get(role)
    if config_role is not None:
        capabilities = config_role.capabilities | capabilities
    defaults = {
        "session_id": optional_capability(capabilities, "runtime_session_id"),
        "resume_supported": optional_bool_capability(capabilities, "acp_resume_supported"),
        "control_socket": optional_capability(capabilities, "control_socket"),
        "acp_tui_bin": optional_capability(capabilities, "acp_tui_bin"),
        "acp_agent_command": optional_capability(capabilities, "acp_agent_command"),
        "provider": optional_capability(capabilities, "acp_provider"),
        "model": optional_capability(capabilities, "acp_model"),
        "effort": optional_capability(capabilities, "acp_effort"),
        "handoff_file": optional_capability(capabilities, "last_handoff_file"),
    }
    return {key: value for key, value in defaults.items() if value is not None} | metadata


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


def snapshot_watchdog_runners(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    watchdogs = snapshot.get("watchdogs")
    if not isinstance(watchdogs, dict):
        return {}
    runners: dict[str, dict[str, Any]] = {}
    for name, data in watchdogs.items():
        if not isinstance(data, dict):
            continue
        state = str(data.get("state") or "")
        if state != "running":
            continue
        runners[str(name)] = data
    return runners


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


def resume_acp_role_tmux_commands(
    tmux_bin: str,
    session: str,
    config_path: Path,
    roles: tuple[str, ...],
    role_bindings: dict[str, RoleBinding],
    role_metadata: dict[str, dict[str, Any]],
    agent_layout: str,
    agents_window: str,
) -> list[list[str]]:
    commands: list[list[str]] = []
    for index, role in enumerate(roles):
        binding = role_bindings[role]
        metadata = role_metadata[role]
        exact_session = binding.session_id if metadata["resume_policy"] == "exact" else None
        commands.append(
            acp_role_spawn_command(
                tmux_bin,
                str(metadata["acp_tui_bin"]),
                str(metadata["acp_agent_command"]),
                session,
                binding.worktree,
                config_path,
                role,
                binding.control_socket,
                index,
                agent_layout,
                agents_window,
                exact_session,
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


def resume_watchdogs_from_snapshot(
    store: Store,
    conn: sqlite3.Connection,
    *,
    tmux_bin: str,
    session: str,
    config_path: Path,
    project_root: Path,
    watchdogs_data: dict[str, dict[str, Any]],
) -> dict[str, str]:
    panes: dict[str, str] = {}
    window_present = bool(watchdogs_data) and tmux_window_exists(tmux_bin, session, watchdog_window_name())
    for index, (name, data) in enumerate(watchdogs_data.items()):
        pane = start_resumed_watchdog_runner(
            tmux_bin,
            session,
            config_path,
            project_root,
            name,
            data,
            use_existing_window=window_present or index > 0,
        )
        window_present = True
        panes[name] = pane
        store.upsert_watchdog_runner(
            conn,
            name=name,
            state=str(data.get("state") or "running"),
            interval_seconds=int(data["interval_seconds"]),
            scope_role=optional_string(data.get("scope_role")),
            description=optional_string(data.get("description")),
            goal=optional_string(data.get("goal")),
            notify_role=optional_string(data.get("notify_role")),
            delivery_method=str(data.get("delivery_method") or "report-only"),
            pane=pane,
            window=f"{session}:{watchdog_window_name()}",
            actor="resume",
        )
    return panes


def resume_watchdog_tmux_commands(
    tmux_bin: str,
    session: str,
    config_path: Path,
    project_root: Path,
    watchdogs: dict[str, dict[str, Any]],
) -> list[list[str]]:
    commands: list[list[str]] = []
    for index, (name, data) in enumerate(watchdogs.items()):
        commands.append(
            resumed_watchdog_new_window_command(
                tmux_bin=tmux_bin,
                session=session,
                config_path=config_path,
                project_root=project_root,
                name=name,
                data=data,
                use_existing_window=index > 0,
            )
        )
        commands.extend(watchdog_pane_setup_commands(tmux_bin, "<pane-id>", name=name))
        commands.append(watchdog_layout_command(tmux_bin, session))
    return commands


def start_resumed_watchdog_runner(
    tmux_bin: str,
    session: str,
    config_path: Path,
    project_root: Path,
    name: str,
    data: dict[str, Any],
    use_existing_window: bool,
) -> str:
    command = resumed_watchdog_new_window_command(
        tmux_bin=tmux_bin,
        session=session,
        config_path=config_path,
        project_root=project_root,
        name=name,
        data=data,
        use_existing_window=use_existing_window,
    )
    result = subprocess_run_lifecycle(command)
    pane = result.stdout.strip()
    if not pane:
        raise LifecycleError(f"could not resume watchdog runner {name}: tmux did not return a pane id")
    for command in watchdog_pane_setup_commands(tmux_bin, pane, name=name):
        subprocess_run_lifecycle(command)
    subprocess_run_lifecycle(watchdog_layout_command(tmux_bin, session))
    return pane


def resumed_watchdog_new_window_command(
    *,
    tmux_bin: str,
    session: str,
    config_path: Path,
    project_root: Path,
    name: str,
    data: dict[str, Any],
    use_existing_window: bool,
) -> list[str]:
    return watchdog_spawn_command(
        tmux_bin=tmux_bin,
        session=session,
        config_path=config_path,
        project_root=project_root,
        name=name,
        interval=format_seconds_duration(int(data["interval_seconds"])),
        delivery=str(data.get("delivery_method") or "report-only"),
        role=str(data["scope_role"]) if data.get("scope_role") else None,
        description=str(data["description"]) if data.get("description") else None,
        goal=str(data["goal"]) if data.get("goal") else None,
        notify_role=str(data["notify_role"]) if data.get("notify_role") else None,
        use_existing_window=use_existing_window,
    )


def tmux_window_exists(tmux_bin: str, session: str, window_name: str) -> bool:
    result = subprocess.run(
        [tmux_bin, "list-windows", "-t", session, "-F", "#{window_name}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return False
    return any(line.strip() == window_name for line in result.stdout.splitlines())


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


def inspect_watchdog_targets(
    store: Store,
    conn: sqlite3.Connection,
    tmux_bin: str,
    *,
    dry_run: bool,
) -> dict[str, TmuxTarget]:
    targets: dict[str, TmuxTarget] = {}
    for row in store.list_watchdog_runners(conn):
        if row["state"] not in ("running", "paused"):
            continue
        pane = row["pane"] or ""
        if pane and not dry_run:
            target = inspect_tmux_target(tmux_bin, pane)
        else:
            target = infer_tmux_target(pane)
        targets[str(row["name"])] = target
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
    watchdog_targets: dict[str, TmuxTarget],
    tmux_bin: str,
    session: str | None,
    *,
    dry_run: bool,
    force: bool,
    include_app_server: bool = True,
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
        entry = windows.setdefault(
            key,
            {
                "target": value,
                "window_id": target.window_id,
                "window_name": target.window_name,
                "roles": [],
                "watchdogs": [],
                "kind": "roles",
            },
        )
        entry.setdefault("roles", []).append(role)

    for name, target in watchdog_targets.items():
        key, value = target_window_key(target, session)
        if not key or not value:
            continue
        if target.window_name == CONTROL_PLANE_WINDOW and not force:
            raise LifecycleError(
                f"refusing to manage {CONTROL_PLANE_WINDOW} window for watchdog {name}; rerun with --force if intended"
            )
        entry = windows.setdefault(
            key,
            {
                "target": value,
                "window_id": target.window_id,
                "window_name": target.window_name,
                "roles": [],
                "watchdogs": [],
                "kind": "watchdog",
            },
        )
        entry.setdefault("watchdogs", []).append(name)

    app_server = app_server_window_target(tmux_bin, session, dry_run=dry_run) if include_app_server else None
    if app_server is not None:
        key, value = app_server
        windows.setdefault(
            key,
            {
                "target": value,
                "window_id": value if value.startswith("@") else None,
                "window_name": APP_SERVER_WINDOW,
                "roles": [],
                "watchdogs": [],
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
    watchdog_targets: dict[str, TmuxTarget],
    managed_windows: list[dict[str, Any]],
    commands: list[list[str]],
    kill_session: bool,
    pause_roles: bool,
    dry_run: bool,
    tmux_bin: str,
    acp_sleep_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    roles: dict[str, Any] = {}
    for role_name, role_config in config.roles.items():
        role_row = store.get_role(conn, role_name)
        resolved = store.resolve_role_app_server(conn, role_name, role_row) if role_row is not None else None
        target = role_targets[role_name]
        role_snapshot = {
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
        if acp_sleep_metadata and role_name in acp_sleep_metadata:
            role_snapshot["acp"] = acp_sleep_metadata[role_name]
        roles[role_name] = role_snapshot
    operator = build_operator_snapshot(config, tmux_bin=tmux_bin, session=session, dry_run=dry_run)
    watchdogs = build_watchdog_snapshot(store, conn, watchdog_targets)

    snapshot: dict[str, Any] = {
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
        "watchdogs": watchdogs,
        "pause_roles": pause_roles,
    }
    if operator:
        snapshot["operator"] = operator
    return snapshot


def build_watchdog_snapshot(
    store: Store,
    conn: sqlite3.Connection,
    watchdog_targets: dict[str, TmuxTarget],
) -> dict[str, Any]:
    watchdogs: dict[str, Any] = {}
    for row in store.list_watchdog_runners(conn):
        if row["state"] not in ("running", "paused"):
            continue
        target = watchdog_targets.get(str(row["name"])) or infer_tmux_target(row["pane"] or "")
        watchdogs[str(row["name"])] = {
            "state": row["state"],
            "interval_seconds": int(row["interval_seconds"]),
            "scope_role": row["scope_role"],
            "description": row["description"],
            "goal": row["goal"],
            "notify_role": row["notify_role"],
            "delivery_method": row["delivery_method"],
            "pane": row["pane"],
            "window": row["window"],
            "process_id": row["process_id"],
            "last_run_at": row["last_run_at"],
            "next_run_at": row["next_run_at"],
            "last_finding_count": row["last_finding_count"],
            "last_finding_summary": row["last_finding_summary"],
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
    return watchdogs


def build_operator_snapshot(
    config: TeamConfig,
    *,
    tmux_bin: str,
    session: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    target_value = config.operator.pane
    if not target_value and session:
        target_value = f"{session}:{CONTROL_PLANE_WINDOW}.0"
    if not target_value and not config.operator.codex_thread_id and not config.operator.capabilities:
        return {}
    if target_value and not dry_run:
        target = inspect_tmux_target(tmux_bin, target_value)
    else:
        target = infer_tmux_target(target_value or "")
    pane = target.pane_id or target_value
    data: dict[str, Any] = dict(config.operator.capabilities)
    if pane:
        data["pane"] = pane
    if config.operator.codex_thread_id:
        data["codex_thread_id"] = config.operator.codex_thread_id
    data["tmux"] = {
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
    }
    return data


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


def optional_string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def configured_acp_roles(config: TeamConfig) -> list[str]:
    return sorted(role.name for role in config.roles.values() if role.mode == "acp_tui")
