from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from .acp_tui import ACPControlError, control_socket_path, send_control_request, wait_for_acp_tui
from .app_server import AppServerClient
from .config import (
    CONFIG_PATH_ENV,
    ENV_FILE_PATH,
    ROLE_ENV,
    OperatorConfig,
    load_config,
    resolve_runtime_dir,
    role_scratchpad_path,
)
from .store import Store

ROLE_CONTRACT_VERSION = "2026-07-11.1"
DEFAULT_ROLES = ("orchestrator", "implementer", "collector", "trainer")
DEFAULT_AGENT_LAYOUT = "grouped"
DEFAULT_CONTROL_WINDOW = "tt-control"
DEFAULT_APP_SERVER_WINDOW = "tt-app-server"
DEFAULT_AGENTS_WINDOW = "tt-agents"
AGENT_LAYOUTS = ("grouped", "separate-windows")
AGENT_RUNTIMES = ("codex", "acp", "cursor-acp")
CONTROL_MODES = ("auto", "shell", "codex")
ROLE_PANE_OPTION = "@tmux-team-role"
TT_PREFIX = "tt-"


@dataclass(frozen=True)
class BootstrapResult:
    session: str
    endpoint: str
    config_path: Path
    agent_runtime: str
    role_threads: dict[str, str]
    role_sessions: dict[str, str]
    role_panes: dict[str, str]
    operator_pane: str | None = None
    operator_thread_id: str | None = None


@dataclass(frozen=True)
class RoleBinding:
    thread_id: str
    pane: str
    worktree: Path
    session_id: str = ""
    control_socket: str = ""


@dataclass(frozen=True)
class AgentRuntimeAdapter:
    name: str
    role_mode: str
    notify_method: str
    uses_app_server: bool
    uses_control_socket: bool


CODEX_RUNTIME = AgentRuntimeAdapter("codex", "app_server_remote_tui", "app-server-turn", True, False)
ACP_RUNTIME = AgentRuntimeAdapter("acp", "acp_tui", "control-socket", False, True)


@dataclass(frozen=True)
class RoleLaunchOptions:
    model: str | None = None
    reasoning_effort: str | None = None
    profile: str | None = None
    config_overrides: tuple[str, ...] = ()
    yolo: bool | None = None


class BootstrapError(RuntimeError):
    pass


def bootstrap_team(
    *,
    project_root: Path,
    config_path: Path,
    runtime_dir: str,
    session: str,
    roles: tuple[str, ...],
    endpoint: str,
    codex_bin: str,
    tmux_bin: str,
    goal: str | None,
    force_config: bool,
    start_app_server: bool,
    agent_layout: str,
    control_window: str,
    control_mode: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    dry_run: bool,
    role_launch_options: dict[str, RoleLaunchOptions] | None = None,
    role_scratchpads: dict[str, Path] | None = None,
    role_worktrees: dict[str, Path] | None = None,
    create_missing_worktrees: bool = False,
    worktree_base_ref: str = "HEAD",
    allow_shared_worktree_groups: tuple[frozenset[str], ...] = (),
    allow_dirty_roles: frozenset[str] = frozenset(),
    enable_truecolor: bool = True,
    agent_runtime: str = "codex",
    acp_tui_bin: str = "toad",
    acp_agent_command: str = "agent acp",
    acp_provider: str | None = None,
    cursor_bin: str | None = None,
) -> BootstrapResult:
    project_root = project_root.expanduser().resolve()
    config_path = config_path.expanduser()
    if not config_path.is_absolute():
        config_path = project_root / config_path

    if not roles:
        raise BootstrapError("at least one role is required")
    agent_layout = normalize_agent_layout(agent_layout)
    runtime = agent_runtime_adapter(agent_runtime)
    agent_runtime = runtime.name
    if cursor_bin:
        acp_agent_command = f"{shlex.quote(cursor_bin)} acp"
    control_mode = normalize_control_mode(control_mode)
    if runtime.uses_control_socket and control_mode == "auto":
        control_mode = "shell"
    role_launch_options = role_launch_options or {}
    validate_role_launch_options(roles, role_launch_options)
    if runtime.uses_control_socket:
        validate_acp_launch_options(role_yolo, role_launch_options)
    if config_path.exists() and not force_config and not dry_run:
        raise BootstrapError(f"config already exists: {config_path} (use --force-config to replace)")
    if shutil.which(tmux_bin) is None and not dry_run:
        raise BootstrapError(f"tmux binary not found: {tmux_bin}")
    if runtime.uses_app_server and shutil.which(codex_bin) is None and not dry_run:
        raise BootstrapError(f"codex binary not found: {codex_bin}")
    if runtime.uses_control_socket and shutil.which(acp_tui_bin) is None and not dry_run:
        raise BootstrapError(
            f"ACP TUI binary not found: {acp_tui_bin}. "
            "Install the tmux-team[acp] extra with Python 3.14+, or pass --acp-tui-bin."
        )
    if runtime.uses_control_socket and not acp_agent_command.strip():
        raise BootstrapError("ACP agent command is required")
    if control_mode == "codex" and shutil.which(codex_bin) is None and not dry_run:
        raise BootstrapError(f"codex binary not found for control pane: {codex_bin}")
    if not dry_run and runtime.uses_app_server:
        ensure_start_skill_available()
    role_worktree_paths, worktree_commands = prepare_role_worktrees(
        project_root,
        roles,
        role_worktrees or {},
        create_missing_worktrees=create_missing_worktrees,
        worktree_base_ref=worktree_base_ref,
        allow_shared_worktree_groups=allow_shared_worktree_groups,
        allow_dirty_roles=allow_dirty_roles,
        dry_run=dry_run,
    )
    role_scratchpad_values = resolve_role_scratchpads(project_root, roles, role_scratchpads or {})
    resolved_runtime_dir = resolve_runtime_dir(project_root, runtime_dir)
    role_control_sockets = {role: control_socket_path(resolved_runtime_dir, role) for role in roles}

    if dry_run:
        role_bindings = dry_run_role_bindings(
            roles,
            session,
            agent_layout,
            agents_window,
            role_worktree_paths,
            agent_runtime=agent_runtime,
            role_control_sockets=role_control_sockets,
        )
        for command in worktree_commands + dry_run_tmux_commands(
            tmux_bin,
            codex_bin,
            acp_tui_bin,
            acp_agent_command,
            session,
            endpoint,
            project_root,
            config_path,
            role_bindings,
            start_app_server=start_app_server,
            agent_layout=agent_layout,
            control_window=control_window,
            agents_window=agents_window,
            role_yolo=role_yolo,
            role_profile=role_profile,
            role_launch_options=role_launch_options,
            control_mode=control_mode,
            enable_truecolor=enable_truecolor,
            agent_runtime=agent_runtime,
        ):
            print(shell_join(command))
        print(
            render_team_config(
                "tmux-team",
                runtime_dir,
                endpoint,
                role_bindings,
                role_yolo,
                role_profile,
                role_launch_options,
                role_scratchpad_values,
                operator=OperatorConfig(pane=f"{session}:{control_window}.0"),
                agent_runtime=agent_runtime,
                acp_tui_bin=acp_tui_bin,
                acp_agent_command=acp_agent_command,
                acp_provider=acp_provider,
            )
        )
        return BootstrapResult(
            session=session,
            endpoint=endpoint,
            config_path=config_path,
            agent_runtime=agent_runtime,
            role_threads={role: binding.thread_id for role, binding in role_bindings.items()},
            role_sessions={role: binding.session_id for role, binding in role_bindings.items()},
            role_panes={role: binding.pane for role, binding in role_bindings.items()},
            operator_pane=f"{session}:{control_window}.0",
        )

    provisional_bindings = {
        role: RoleBinding(
            thread_id="",
            pane="",
            worktree=role_worktree_paths[role],
            control_socket=str(role_control_sockets[role]) if runtime.uses_control_socket else "",
        )
        for role in roles
    }
    write_team_config(
        config_path,
        runtime_dir,
        endpoint,
        provisional_bindings,
        role_yolo,
        role_profile,
        role_launch_options,
        role_scratchpad_values,
        force=True,
        agent_runtime=agent_runtime,
        acp_tui_bin=acp_tui_bin,
        acp_agent_command=acp_agent_command,
        acp_provider=acp_provider,
    )
    write_role_env_files(config_path, provisional_bindings)
    write_role_scratchpads(config_path, initial_goal=goal)

    operator_pane = ensure_control_plane_window(
        tmux_bin, codex_bin, session, project_root, config_path, control_window, control_mode
    )
    if enable_truecolor:
        configure_session_truecolor(tmux_bin, session)
    if start_app_server and runtime.uses_app_server:
        for command in app_server_tmux_commands(tmux_bin, codex_bin, session, endpoint, project_root):
            run(command, check=True)

    if runtime.uses_app_server:
        wait_for_app_server(endpoint, timeout=20.0)
        role_bindings = start_role_panes_and_discover_threads(
            tmux_bin,
            codex_bin,
            session,
            endpoint,
            project_root,
            config_path,
            roles,
            role_worktree_paths,
            agent_layout,
            agents_window,
            role_yolo,
            role_profile,
            role_launch_options,
        )
    else:
        role_bindings = start_acp_role_panes(
            tmux_bin,
            acp_tui_bin,
            acp_agent_command,
            session,
            config_path,
            roles,
            role_worktree_paths,
            role_control_sockets,
            agent_layout,
            agents_window,
        )
    write_team_config(
        config_path,
        runtime_dir,
        endpoint,
        role_bindings,
        role_yolo,
        role_profile,
        role_launch_options,
        role_scratchpad_values,
        force=True,
        operator=OperatorConfig(pane=operator_pane),
        agent_runtime=agent_runtime,
        acp_tui_bin=acp_tui_bin,
        acp_agent_command=acp_agent_command,
        acp_provider=acp_provider,
    )
    write_role_env_files(config_path, role_bindings)
    write_role_scratchpads(config_path, initial_goal=goal)

    if goal:
        send_initial_goal(config_path, goal)

    return BootstrapResult(
        session=session,
        endpoint=endpoint,
        config_path=config_path,
        agent_runtime=agent_runtime,
        role_threads={role: binding.thread_id for role, binding in role_bindings.items()},
        role_sessions={role: binding.session_id for role, binding in role_bindings.items()},
        role_panes={role: binding.pane for role, binding in role_bindings.items()},
        operator_pane=operator_pane,
    )


def dry_run_tmux_commands(
    tmux_bin: str,
    codex_bin: str,
    acp_tui_bin: str,
    acp_agent_command: str,
    session: str,
    endpoint: str,
    project_root: Path,
    config_path: Path,
    role_bindings: dict[str, RoleBinding],
    *,
    start_app_server: bool,
    agent_layout: str,
    control_window: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    role_launch_options: dict[str, RoleLaunchOptions],
    control_mode: str,
    enable_truecolor: bool,
    agent_runtime: str,
) -> list[list[str]]:
    commands: list[list[str]] = [
        control_plane_session_command(
            tmux_bin, codex_bin, session, project_root, config_path, control_window, control_mode
        )
    ]
    if enable_truecolor:
        commands.extend(session_truecolor_tmux_commands(tmux_bin, session))
    if start_app_server and agent_runtime == "codex":
        commands.append(
            new_window_command(
                tmux_bin,
                session,
                DEFAULT_APP_SERVER_WINDOW,
                project_root,
                app_server_shell_command(codex_bin, endpoint),
            )
        )
    for index, role in enumerate(role_bindings):
        pane = role_bindings[role].pane
        worktree = role_bindings[role].worktree
        if agent_runtime == "acp":
            spawn_command = acp_role_spawn_command(
                tmux_bin,
                acp_tui_bin,
                acp_agent_command,
                session,
                worktree,
                config_path,
                role,
                role_bindings[role].control_socket,
                index,
                agent_layout,
                agents_window,
            )
        else:
            spawn_command = role_spawn_command(
                tmux_bin,
                codex_bin,
                session,
                endpoint,
                worktree,
                config_path,
                role,
                index,
                agent_layout,
                agents_window,
                role_yolo,
                role_profile,
                role_launch_options.get(role, RoleLaunchOptions()),
            )
        commands.append(spawn_command)
        if agent_layout == "grouped" and index == 0:
            commands.extend(configure_agent_window_commands(tmux_bin, session, agents_window))
        commands.extend(label_role_pane_commands(tmux_bin, pane, role))
        if agent_layout == "grouped" and index > 0:
            commands.extend(select_tiled_layout_commands(tmux_bin, session, agents_window))
    return commands


def session_truecolor_tmux_commands(tmux_bin: str, session: str) -> list[list[str]]:
    return [
        [tmux_bin, "set-option", "-t", session, "default-terminal", "tmux-256color"],
        [tmux_bin, "set-option", "-as", "-t", session, "terminal-features", ",*:RGB"],
        [tmux_bin, "set-environment", "-t", session, "COLORTERM", "truecolor"],
    ]


def session_truecolor_fallback_command(tmux_bin: str, session: str) -> list[str]:
    return [tmux_bin, "set-option", "-as", "-t", session, "terminal-overrides", ",*:Tc"]


def configure_session_truecolor(tmux_bin: str, session: str) -> None:
    commands = session_truecolor_tmux_commands(tmux_bin, session)
    run(commands[0], check=False)
    feature_result = run(commands[1], check=False)
    if feature_result.returncode != 0:
        run(session_truecolor_fallback_command(tmux_bin, session), check=False)
    run(commands[2], check=False)


def app_server_tmux_commands(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
) -> list[list[str]]:
    command = app_server_shell_command(codex_bin, endpoint)
    if tmux_session_exists(tmux_bin, session):
        if tmux_window_exists(tmux_bin, session, DEFAULT_APP_SERVER_WINDOW):
            return []
        return [new_window_command(tmux_bin, session, DEFAULT_APP_SERVER_WINDOW, project_root, command)]
    return [app_server_new_session_command(tmux_bin, session, project_root, command)]


def ensure_control_plane_window(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    project_root: Path,
    config_path: Path,
    control_window: str,
    control_mode: str,
) -> str | None:
    if not tmux_session_exists(tmux_bin, session):
        run(
            control_plane_session_command(
                tmux_bin, codex_bin, session, project_root, config_path, control_window, control_mode
            ),
            check=True,
        )
        return first_pane_id(tmux_bin, f"{session}:{control_window}") or f"{session}:{control_window}.0"

    current_session = detect_current_tmux_session(tmux_bin)
    if current_session == session:
        current_window_id = detect_current_tmux_window_id(tmux_bin)
        if current_window_id:
            run([tmux_bin, "rename-window", "-t", current_window_id, control_window], check=True)
            return detect_current_tmux_pane_id(tmux_bin) or first_pane_id(tmux_bin, current_window_id)

    if not tmux_window_exists(tmux_bin, session, control_window):
        command = control_plane_shell_command(codex_bin, project_root, config_path, control_mode)
        run(new_window_command(tmux_bin, session, control_window, project_root, command), check=True)
    return first_pane_id(tmux_bin, f"{session}:{control_window}") or f"{session}:{control_window}.0"


def start_role_panes_and_discover_threads(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
    config_path: Path,
    roles: tuple[str, ...],
    role_worktrees: dict[str, Path],
    agent_layout: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    role_launch_options: dict[str, RoleLaunchOptions],
) -> dict[str, RoleBinding]:
    role_bindings: dict[str, RoleBinding] = {}
    loaded = set(loaded_threads(endpoint))
    if agent_layout == "grouped":
        prepare_grouped_agent_window(tmux_bin, session, agents_window)
    for index, role in enumerate(roles):
        pane = ensure_role_pane(
            tmux_bin,
            codex_bin,
            session,
            endpoint,
            project_root,
            config_path,
            role_worktrees[role],
            role,
            index,
            agent_layout,
            agents_window,
            role_yolo,
            role_profile,
            role_launch_options.get(role, RoleLaunchOptions()),
        )
        thread_id = wait_for_new_loaded_thread(endpoint, loaded, timeout=20.0)
        role_bindings[role] = RoleBinding(thread_id=thread_id, pane=pane, worktree=role_worktrees[role])
        loaded.add(thread_id)
    return role_bindings


def start_acp_role_panes(
    tmux_bin: str,
    acp_tui_bin: str,
    acp_agent_command: str,
    session: str,
    config_path: Path,
    roles: tuple[str, ...],
    role_worktrees: dict[str, Path],
    role_sockets: dict[str, Path],
    agent_layout: str,
    agents_window: str,
) -> dict[str, RoleBinding]:
    role_bindings: dict[str, RoleBinding] = {}
    if agent_layout == "grouped":
        prepare_grouped_agent_window(tmux_bin, session, agents_window)
    for index, role in enumerate(roles):
        result = run(
            acp_role_spawn_command(
                tmux_bin,
                acp_tui_bin,
                acp_agent_command,
                session,
                role_worktrees[role],
                config_path,
                role,
                str(role_sockets[role]),
                index,
                agent_layout,
                agents_window,
                print_pane=True,
            ),
            check=True,
        )
        pane = result.stdout.strip()
        if agent_layout == "grouped":
            if index == 0:
                configure_agent_window(tmux_bin, session, agents_window)
            else:
                for command in select_tiled_layout_commands(tmux_bin, session, agents_window):
                    run(command, check=False)
        if not pane:
            pane = f"{session}:{agents_window}.{index}" if agent_layout == "grouped" else f"{session}:{tt_name(role)}.0"
        label_role_pane(tmux_bin, pane, role)
        try:
            status = wait_for_acp_tui(role_sockets[role], timeout=30.0)
            send_control_request(
                role_sockets[role],
                {
                    "action": "prompt",
                    "text": role_startup_prompt(role, agent_runtime="acp"),
                    "priority": "normal",
                    "coalesceKey": "tmux-team-startup",
                },
            )
        except ACPControlError as exc:
            raise BootstrapError(f"ACP TUI role {role!r} did not start: {exc}") from exc
        role_bindings[role] = RoleBinding(
            thread_id="",
            pane=pane,
            worktree=role_worktrees[role],
            session_id=str(status.get("sessionId") or ""),
            control_socket=str(role_sockets[role]),
        )
    return role_bindings


def prepare_grouped_agent_window(tmux_bin: str, session: str, agents_window: str) -> None:
    if tmux_window_exists(tmux_bin, session, agents_window):
        raise BootstrapError(
            f"agent layout window already exists: {session}:{agents_window} "
            "(remove it or use --agent-layout separate-windows)"
        )


def ensure_role_pane(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
    config_path: Path,
    role_worktree: Path,
    role: str,
    index: int,
    agent_layout: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    role_launch_options: RoleLaunchOptions,
) -> str:
    spawn_command = role_spawn_command(
        tmux_bin,
        codex_bin,
        session,
        endpoint,
        role_worktree,
        config_path,
        role,
        index,
        agent_layout,
        agents_window,
        role_yolo,
        role_profile,
        role_launch_options,
        print_pane=True,
    )
    if agent_layout == "grouped":
        result = run(spawn_command, check=True)
        pane = result.stdout.strip()
        if index == 0:
            configure_agent_window(tmux_bin, session, agents_window)
        else:
            for command in select_tiled_layout_commands(tmux_bin, session, agents_window):
                run(command, check=True)
        if pane:
            label_role_pane(tmux_bin, pane, role)
            return pane
        return f"{session}:{agents_window}.{index}"

    role_window = tt_name(role)
    if tmux_window_exists(tmux_bin, session, role_window):
        command = role_shell_command(
            codex_bin,
            endpoint,
            role_worktree,
            config_path,
            role,
            role_yolo=role_yolo,
            role_profile=role_profile,
            role_launch_options=role_launch_options,
        )
        run(
            [tmux_bin, "respawn-window", "-k", "-t", f"{session}:{role_window}", "-c", str(role_worktree), command],
            check=True,
        )
        pane = first_pane_id(tmux_bin, f"{session}:{role_window}")
    else:
        result = run(spawn_command, check=True)
        pane = result.stdout.strip()
    if pane:
        label_role_pane(tmux_bin, pane, role)
        return pane
    return f"{session}:{role_window}.0"


def configure_agent_window(tmux_bin: str, session: str, agents_window: str) -> None:
    for command in configure_agent_window_commands(tmux_bin, session, agents_window):
        run(command, check=False)


def configure_agent_window_commands(tmux_bin: str, session: str, agents_window: str) -> list[list[str]]:
    target = f"{session}:{agents_window}"
    return [
        [tmux_bin, "set-window-option", "-t", target, "pane-border-status", "top"],
        [
            tmux_bin,
            "set-window-option",
            "-t",
            target,
            "pane-border-format",
            f"#{{pane_index}}: #{{{ROLE_PANE_OPTION}}}",
        ],
    ]


def select_tiled_layout_commands(tmux_bin: str, session: str, agents_window: str) -> list[list[str]]:
    return [[tmux_bin, "select-layout", "-t", f"{session}:{agents_window}", "tiled"]]


def label_role_pane(tmux_bin: str, pane: str, role: str) -> None:
    for command in label_role_pane_commands(tmux_bin, pane, role):
        run(command, check=False)


def label_role_pane_commands(tmux_bin: str, pane: str, role: str) -> list[list[str]]:
    return [
        [tmux_bin, "set-option", "-p", "-t", pane, ROLE_PANE_OPTION, role],
        [tmux_bin, "select-pane", "-t", pane, "-T", tt_name(role)],
    ]


def send_initial_goal(config_path: Path, goal: str) -> None:
    config = load_config(config_path)
    store = Store(config)
    with store.connect() as conn:
        message = store.create_message(
            conn,
            sender="operator",
            recipient="orchestrator",
            priority="normal",
            summary="initial team goal",
            body=goal,
        )
        ok, details = store.notify_role(conn, "orchestrator", "auto")
        if not ok:
            raise BootstrapError(f"initial goal queued as {message.id}, but role wake failed: {details}")


def write_team_config(
    path: Path,
    runtime_dir: str,
    endpoint: str,
    role_bindings: dict[str, RoleBinding],
    role_yolo: bool,
    role_profile: str | None,
    role_launch_options: dict[str, RoleLaunchOptions],
    role_scratchpads: dict[str, str],
    *,
    force: bool,
    operator: OperatorConfig | None = None,
    agent_runtime: str = "codex",
    acp_tui_bin: str = "toad",
    acp_agent_command: str = "agent acp",
    acp_provider: str | None = None,
) -> None:
    if path.exists() and not force:
        raise BootstrapError(f"config already exists: {path} (use --force-config to replace)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_team_config(
            "tmux-team",
            runtime_dir,
            endpoint,
            role_bindings,
            role_yolo,
            role_profile,
            role_launch_options,
            role_scratchpads,
            operator=operator,
            agent_runtime=agent_runtime,
            acp_tui_bin=acp_tui_bin,
            acp_agent_command=acp_agent_command,
            acp_provider=acp_provider,
        ),
        encoding="utf-8",
    )


def write_role_env_files(config_path: Path, role_bindings: dict[str, RoleBinding]) -> None:
    roles_by_worktree: dict[Path, list[str]] = {}
    for role, binding in role_bindings.items():
        roles_by_worktree.setdefault(binding.worktree.resolve(), []).append(role)

    for role, binding in role_bindings.items():
        path = binding.worktree / ENV_FILE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{CONFIG_PATH_ENV}={config_path}"]
        if len(roles_by_worktree[binding.worktree.resolve()]) == 1:
            lines.append(f"{ROLE_ENV}={role}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_role_scratchpads(config_path: Path, *, initial_goal: str | None) -> None:
    config = load_config(config_path)
    goal_summary = summarize_text(initial_goal)
    for role, role_config in config.roles.items():
        path = role_scratchpad_path(config, role)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            continue
        path.write_text(render_scratchpad_seed(config, role_config, goal_summary), encoding="utf-8")


def render_scratchpad_seed(config, role_config, goal_summary: str) -> str:
    runtime_session_id = role_config.capabilities.get("runtime_session_id")
    runtime_session = (
        f"ACP session: {runtime_session_id or 'unknown'}"
        if role_config.mode == "acp_tui"
        else f"Codex thread: {role_config.capabilities.get('codex_thread_id') or 'unknown'}"
    )
    pane = role_config.pane or "unknown"
    worktree = role_config.worktree or "unknown"
    return (
        f"# {role_config.name} Scratchpad\n\n"
        "Scratchpad memory preserves long-term goals across context compression, sleep/resume, and pane restarts. "
        "It is also an observability surface for this role, other agents, and the human overseer. "
        "Keep the most recent and important state at the top. Inbox messages remain authoritative for delivery.\n\n"
        "## Latest\n"
        f"Role: {role_config.name}\n"
        f"Worktree: {worktree}\n"
        "Commit: unknown\n"
        "Git status: unknown\n"
        "Active task: none yet\n"
        "Current blocker: none recorded\n"
        "Next action: read inbox before acting\n"
        f"Initial goal: {goal_summary or 'none recorded'}\n\n"
        "## Current State\n"
        "Running jobs: none recorded\n"
        "Owned reports/artifacts: none recorded\n"
        f"Runtime dir: {config.runtime_dir}\n"
        f"Pane: {pane}\n"
        f"{runtime_session}\n"
        "## Boundaries\n"
        "Do not launch: expensive, destructive, or external jobs unless explicitly instructed.\n"
        "Do not edit: outside this role's assigned worktree unless explicitly instructed.\n"
        "Do not sync unless: instructed by the orchestrator/operator or the task explicitly says so.\n\n"
        "## Stable Inputs\n"
        "Current stable commit: none recorded\n"
        "Dataset snapshot: none recorded\n"
        "Recently verified provider/router facts: none recorded\n\n"
        "## Next Action\n"
        "If woken with no new task: update this file only if state changed, then park.\n"
        "If current run finishes: record final result/artifact/blocker, then complete the inbox message.\n"
        "If blocker recurs: stop and report it to the orchestrator with evidence.\n"
    )


def render_team_config(
    team_name: str,
    runtime_dir: str,
    endpoint: str,
    role_bindings: dict[str, RoleBinding],
    role_yolo: bool = False,
    role_profile: str | None = None,
    role_launch_options: dict[str, RoleLaunchOptions] | None = None,
    role_scratchpads: dict[str, str] | None = None,
    operator: OperatorConfig | None = None,
    agent_runtime: str = "codex",
    acp_tui_bin: str = "toad",
    acp_agent_command: str = "agent acp",
    acp_provider: str | None = None,
) -> str:
    role_launch_options = role_launch_options or {}
    role_scratchpads = role_scratchpads or {}
    roles: dict[str, dict[str, object]] = {}
    for role, binding in role_bindings.items():
        launch_options = role_launch_options.get(role, RoleLaunchOptions())
        role_data: dict[str, object] = {
            "state": "active",
            "pane": binding.pane,
            "worktree": str(binding.worktree),
            "scratchpad": role_scratchpads.get(role, f".tmux-team/memory/{role}.md"),
        }
        runtime = agent_runtime_adapter(agent_runtime)
        if runtime.uses_control_socket:
            role_data.update(
                {
                    "mode": runtime.role_mode,
                    "notify_method": runtime.notify_method,
                    "control_socket": binding.control_socket,
                    "acp_tui_bin": acp_tui_bin,
                    "acp_agent_command": acp_agent_command,
                }
            )
            if binding.session_id:
                role_data["runtime_session_id"] = binding.session_id
            if acp_provider:
                role_data["acp_provider"] = acp_provider
        else:
            role_data.update(
                {
                    "mode": runtime.role_mode,
                    "notify_method": runtime.notify_method,
                    "app_server_endpoint": endpoint,
                    "codex_thread_id": binding.thread_id,
                }
            )
            if role_yolo or launch_options.yolo:
                role_data["codex_yolo"] = True
            profile = launch_options.profile or role_profile
            if profile:
                role_data["codex_profile"] = profile
            if launch_options.model:
                role_data["codex_model"] = launch_options.model
            if launch_options.reasoning_effort:
                role_data["codex_reasoning_effort"] = launch_options.reasoning_effort
            if launch_options.config_overrides:
                role_data["codex_config"] = list(launch_options.config_overrides)
        roles[role] = role_data
    data: dict[str, object] = {"team": {"name": team_name, "runtime_dir": runtime_dir}, "roles": roles}
    operator_data = render_operator_config(operator)
    if operator_data:
        data["operator"] = operator_data
    return tomli_w.dumps(data)


def render_operator_config(operator: OperatorConfig | None) -> dict[str, object]:
    if operator is None:
        return {}
    data: dict[str, object] = dict(operator.capabilities)
    if operator.pane:
        data["pane"] = operator.pane
    if operator.codex_thread_id:
        data["codex_thread_id"] = operator.codex_thread_id
    return data


def wait_for_app_server(endpoint: str, timeout: float) -> None:
    ready_url = endpoint.replace("ws://", "http://", 1).replace("wss://", "https://", 1).rstrip("/") + "/readyz"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(ready_url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise BootstrapError(f"app-server did not become ready at {ready_url}: {last_error}")


def loaded_threads(endpoint: str) -> list[str]:
    with AppServerClient(endpoint, timeout=10.0) as client:
        client.initialize()
        return client.list_loaded_threads()


def wait_for_new_loaded_thread(endpoint: str, previous: set[str], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last_loaded: list[str] = []
    with AppServerClient(endpoint, timeout=10.0) as client:
        client.initialize()
        while time.monotonic() < deadline:
            last_loaded = client.list_loaded_threads()
            new_threads = [thread_id for thread_id in last_loaded if thread_id not in previous]
            if new_threads:
                return max(new_threads)
            time.sleep(0.2)
    raise BootstrapError(f"role TUI did not create a loaded app-server thread; loaded={last_loaded}")


def free_local_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()
    return f"ws://127.0.0.1:{port}"


def detect_current_tmux_session(tmux_bin: str = "tmux") -> str | None:
    if "TMUX" not in os.environ:
        return None
    if shutil.which(tmux_bin) is None:
        return None
    result = run([tmux_bin, "display-message", "-p", "#{session_name}"], check=False)
    if result.returncode != 0:
        return None
    session = result.stdout.strip()
    return session or None


def detect_current_tmux_window_id(tmux_bin: str = "tmux") -> str | None:
    if "TMUX" not in os.environ:
        return None
    if shutil.which(tmux_bin) is None:
        return None
    result = run([tmux_bin, "display-message", "-p", "#{window_id}"], check=False)
    if result.returncode != 0:
        return None
    window_id = result.stdout.strip()
    return window_id or None


def detect_current_tmux_pane_id(tmux_bin: str = "tmux") -> str | None:
    if "TMUX" not in os.environ:
        return None
    if shutil.which(tmux_bin) is None:
        return None
    result = run([tmux_bin, "display-message", "-p", "#{pane_id}"], check=False)
    if result.returncode != 0:
        return None
    pane_id = result.stdout.strip()
    return pane_id or None


def tmux_session_exists(tmux_bin: str, session: str) -> bool:
    return run([tmux_bin, "has-session", "-t", session], check=False).returncode == 0


def tmux_window_exists(tmux_bin: str, session: str, window: str) -> bool:
    return (
        run([tmux_bin, "list-windows", "-t", session, "-F", "#{window_name}"], check=False)
        .stdout.splitlines()
        .count(window)
        > 0
    )


def new_window_command(
    tmux_bin: str,
    session: str,
    window: str,
    cwd: Path,
    command: str,
    *,
    print_pane: bool = False,
) -> list[str]:
    command_args = [tmux_bin, "new-window"]
    if print_pane:
        command_args.extend(["-P", "-F", "#{pane_id}"])
    command_args.extend(["-t", session, "-n", window, "-c", str(cwd), command])
    return command_args


def split_window_command(
    tmux_bin: str,
    target: str,
    cwd: Path,
    command: str,
    *,
    print_pane: bool = False,
) -> list[str]:
    command_args = [tmux_bin, "split-window"]
    if print_pane:
        command_args.extend(["-P", "-F", "#{pane_id}"])
    command_args.extend(["-t", target, "-c", str(cwd), command])
    return command_args


def app_server_new_session_command(
    tmux_bin: str,
    session: str,
    cwd: Path,
    command: str,
) -> list[str]:
    return [tmux_bin, "new-session", "-d", "-s", session, "-n", DEFAULT_APP_SERVER_WINDOW, "-c", str(cwd), command]


def control_plane_session_command(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    cwd: Path,
    config_path: Path,
    window: str,
    control_mode: str,
) -> list[str]:
    command_args = [tmux_bin, "new-session", "-d", "-s", session, "-n", window, "-c", str(cwd)]
    command = control_plane_shell_command(codex_bin, cwd, config_path, control_mode)
    if command:
        command_args.append(command)
    return command_args


def control_plane_shell_command(codex_bin: str, project_root: Path, config_path: Path, control_mode: str) -> str:
    if control_mode == "shell":
        return keep_open_command('printf "[tmux-team] tt-control shell\\n"', DEFAULT_CONTROL_WINDOW)
    codex_args = [
        "env",
        f"{CONFIG_PATH_ENV}={config_path}",
        codex_bin,
        "--cd",
        str(project_root),
        control_startup_prompt(config_path),
    ]
    command = f"cd {shlex.quote(str(project_root))} && {' '.join(shlex.quote(part) for part in codex_args)}"
    return keep_open_command(command, DEFAULT_CONTROL_WINDOW)


def control_startup_prompt(config_path: Path) -> str:
    return (
        "You are the tmux-team operator control Codex session in `tt-control`.\n"
        "Use the start-tmux-team skill now and read its invariants before operating the team.\n"
        f"The active config is `{config_path}` and is also available through TMUX_TEAM_CONFIG.\n"
        "You are not a managed role. Use `tmux-team status`, `tmux-team send`, and `tmux-team sleep` "
        "to supervise the team when the human asks. Do not claim role inbox work unless explicitly instructed.\n"
    )


def app_server_shell_command(codex_bin: str, endpoint: str) -> str:
    return keep_open_command(
        f"{shlex.quote(codex_bin)} app-server --listen {shlex.quote(endpoint)}",
        DEFAULT_APP_SERVER_WINDOW,
    )


def shell_session_command(tmux_bin: str, session: str, cwd: Path, window: str = DEFAULT_CONTROL_WINDOW) -> list[str]:
    return [tmux_bin, "new-session", "-d", "-s", session, "-n", window, "-c", str(cwd)]


def acp_role_shell_command(
    acp_tui_bin: str,
    acp_agent_command: str,
    project_root: Path,
    config_path: Path,
    role: str,
    control_socket: str,
) -> str:
    tui_args = [
        acp_tui_bin,
        "acp",
        "--project-dir",
        str(project_root),
        "--title",
        f"tmux-team: {role}",
        "--control-socket",
        control_socket,
        acp_agent_command,
    ]
    env_args = ["env", f"{CONFIG_PATH_ENV}={config_path}", f"{ROLE_ENV}={role}", *tui_args]
    command = f"cd {shlex.quote(str(project_root))} && {' '.join(shlex.quote(part) for part in env_args)}"
    return keep_open_command(command, "ACP TUI role")


def role_shell_command(
    codex_bin: str,
    endpoint: str,
    project_root: Path,
    config_path: Path,
    role: str,
    *,
    role_yolo: bool = False,
    role_profile: str | None = None,
    role_launch_options: RoleLaunchOptions | None = None,
) -> str:
    role_launch_options = role_launch_options or RoleLaunchOptions()
    profile = role_launch_options.profile or role_profile
    codex_args = [codex_bin]
    if profile:
        codex_args.extend(["--profile", profile])
    if role_yolo or role_launch_options.yolo:
        codex_args.append("--dangerously-bypass-approvals-and-sandbox")
    if role_launch_options.model:
        codex_args.extend(["--model", role_launch_options.model])
    if role_launch_options.reasoning_effort:
        codex_args.extend(["-c", f"model_reasoning_effort={json.dumps(role_launch_options.reasoning_effort)}"])
    for override in role_launch_options.config_overrides:
        codex_args.extend(["-c", override])
    codex_args.extend(["--cd", str(project_root)])
    codex_args.extend(["--remote", endpoint])
    codex_args.append(role_startup_prompt(role))
    env_args = ["env", f"{CONFIG_PATH_ENV}={config_path}", f"{ROLE_ENV}={role}", *codex_args]
    command = f"cd {shlex.quote(str(project_root))} && {' '.join(shlex.quote(part) for part in env_args)}"
    return keep_open_command(command, "role TUI")


def role_resume_shell_command(
    codex_bin: str,
    endpoint: str,
    project_root: Path,
    config_path: Path,
    role: str,
    thread_id: str,
    *,
    role_yolo: bool = False,
    role_profile: str | None = None,
    role_launch_options: RoleLaunchOptions | None = None,
) -> str:
    role_launch_options = role_launch_options or RoleLaunchOptions()
    profile = role_launch_options.profile or role_profile
    codex_args = [codex_bin, "resume"]
    if profile:
        codex_args.extend(["--profile", profile])
    if role_yolo or role_launch_options.yolo:
        codex_args.append("--dangerously-bypass-approvals-and-sandbox")
    if role_launch_options.model:
        codex_args.extend(["--model", role_launch_options.model])
    if role_launch_options.reasoning_effort:
        codex_args.extend(["-c", f"model_reasoning_effort={json.dumps(role_launch_options.reasoning_effort)}"])
    for override in role_launch_options.config_overrides:
        codex_args.extend(["-c", override])
    codex_args.extend(["--cd", str(project_root)])
    codex_args.extend(["--remote", endpoint])
    codex_args.append(thread_id)
    codex_args.append(role_resume_prompt(role))
    env_args = ["env", f"{CONFIG_PATH_ENV}={config_path}", f"{ROLE_ENV}={role}", *codex_args]
    command = f"cd {shlex.quote(str(project_root))} && {' '.join(shlex.quote(part) for part in env_args)}"
    return keep_open_command(command, "role TUI")


def role_startup_prompt(role: str, agent_runtime: str = "codex") -> str:
    if normalize_agent_runtime(agent_runtime) == "acp":
        agent_description = "ACP TUI"
        shell_description = "ACP agent tool shells"
        wake_description = "control-socket wake"
    else:
        agent_description = "Codex"
        shell_description = "Codex tool shells"
        wake_description = "app-server wake"
    return (
        f"You are the `{role}` role in a tmux-team managed {agent_description} team.\n"
        f"tmux-team role contract version: {ROLE_CONTRACT_VERSION}.\n"
        "Use the start-tmux-team skill now. Read its invariants before acting.\n"
        f"Use the explicit role commands below. Short commands may work when role discovery succeeds, but {shell_description} do not always inherit TMUX_TEAM_ROLE and shared worktrees are ambiguous.\n"
        "Startup loop:\n"
        f"1. Run `tmux-team memory show --role {role}` to load durable role state.\n"
        "2. Append memory only for high-value durable changes: new active task, changed boundary, blocker, long-running work, final result, or next action. Do not append routine startup/parking/status chatter.\n"
        f"3. Run `tmux-team inbox next --role {role}`.\n"
        f"4. If there is no pending message, park and wait for {wake_description}. Do not invent work.\n"
        "5. If a message exists, ack it, compare it against scratchpad boundaries, do the work, update scratchpad only if durable state changed materially, then complete it concisely.\n"
        f"{orchestrator_unblock_first_guidance(role)}"
        f"Use `tmux-team inbox ack <message-id> --role {role}` and `tmux-team inbox complete <message-id> --role {role} ...` for the claimed message.\n"
        "Use `--reply-to-sender` for delegated work results, but not for pure acknowledgement loops.\n"
    )


def role_resume_prompt(role: str) -> str:
    return (
        f"You are resuming the `{role}` role in a tmux-team managed Codex team after sleep.\n"
        f"tmux-team role contract version: {ROLE_CONTRACT_VERSION}.\n"
        "Use the start-tmux-team skill if the operating framework is not already loaded in this context.\n"
        f"Run `tmux-team memory show --role {role}` to reload durable role state, then `tmux-team inbox next --role {role}`.\n"
        "If there is no pending message, park and wait for app-server wake. Do not invent work.\n"
        f"{orchestrator_unblock_first_guidance(role)}"
    )


def orchestrator_unblock_first_guidance(role: str) -> str:
    if role != "orchestrator":
        return ""
    return (
        "Orchestrator unblock-first rule: when new operator or role information can safely unblock another role's setup, "
        "send a bounded gated handoff promptly before local review or bookkeeping. State any hold condition clearly, "
        "continue validation, then send approve/cancel/update follow-up.\n"
    )


def ensure_start_skill_available() -> None:
    skill_path = codex_home() / "skills" / "start-tmux-team" / "SKILL.md"
    if skill_path.exists():
        return
    raise BootstrapError(
        "start-tmux-team Codex skill is not installed in CODEX_HOME. "
        "Install it before bootstrapping role panes with `make install-skill` from a tmux-team "
        f"checkout, or copy skills/start-tmux-team into {skill_path.parent}"
    )


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def keep_open_command(command: str, label: str) -> str:
    script = (
        f"{command}\n"
        "status=$?\n"
        f"printf '\\n[tmux-team] {label} exited with status %s. Shell left open for inspection.\\n' \"$status\"\n"
        'exec "${SHELL:-/bin/sh}"\n'
    )
    shell = "bash" if shutil.which("bash") else "sh"
    return f"{shell} -lc {shlex.quote(script)}"


def role_spawn_command(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
    config_path: Path,
    role: str,
    index: int,
    agent_layout: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    role_launch_options: RoleLaunchOptions,
    *,
    print_pane: bool = False,
) -> list[str]:
    command = role_shell_command(
        codex_bin,
        endpoint,
        project_root,
        config_path,
        role,
        role_yolo=role_yolo,
        role_profile=role_profile,
        role_launch_options=role_launch_options,
    )
    if agent_layout == "grouped":
        if index == 0:
            return new_window_command(tmux_bin, session, agents_window, project_root, command, print_pane=print_pane)
        return split_window_command(
            tmux_bin, f"{session}:{agents_window}", project_root, command, print_pane=print_pane
        )
    return new_window_command(tmux_bin, session, tt_name(role), project_root, command, print_pane=print_pane)


def acp_role_spawn_command(
    tmux_bin: str,
    acp_tui_bin: str,
    acp_agent_command: str,
    session: str,
    project_root: Path,
    config_path: Path,
    role: str,
    control_socket: str,
    index: int,
    agent_layout: str,
    agents_window: str,
    *,
    print_pane: bool = False,
) -> list[str]:
    command = acp_role_shell_command(
        acp_tui_bin,
        acp_agent_command,
        project_root,
        config_path,
        role,
        control_socket,
    )
    if agent_layout == "grouped":
        if index == 0:
            return new_window_command(tmux_bin, session, agents_window, project_root, command, print_pane=print_pane)
        return split_window_command(
            tmux_bin, f"{session}:{agents_window}", project_root, command, print_pane=print_pane
        )
    return new_window_command(tmux_bin, session, tt_name(role), project_root, command, print_pane=print_pane)


def role_resume_spawn_command(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
    config_path: Path,
    role: str,
    thread_id: str,
    index: int,
    agent_layout: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    role_launch_options: RoleLaunchOptions,
    *,
    print_pane: bool = False,
) -> list[str]:
    command = role_resume_shell_command(
        codex_bin,
        endpoint,
        project_root,
        config_path,
        role,
        thread_id,
        role_yolo=role_yolo,
        role_profile=role_profile,
        role_launch_options=role_launch_options,
    )
    if agent_layout == "grouped":
        if index == 0:
            return new_window_command(tmux_bin, session, agents_window, project_root, command, print_pane=print_pane)
        return split_window_command(
            tmux_bin, f"{session}:{agents_window}", project_root, command, print_pane=print_pane
        )
    return new_window_command(tmux_bin, session, tt_name(role), project_root, command, print_pane=print_pane)


def first_pane_id(tmux_bin: str, target: str) -> str:
    result = run([tmux_bin, "list-panes", "-t", target, "-F", "#{pane_id}"], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.splitlines()[0] if result.stdout.splitlines() else ""


def run(command: list[str], check: bool) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and result.returncode != 0:
        raise BootstrapError(
            f"command failed: {shell_join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def shell_join(command: list[str]) -> str:
    return shlex.join(command)


def parse_roles(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_ROLES
    roles = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not roles:
        raise BootstrapError("role list is empty")
    for role in roles:
        if not role.replace("-", "").replace("_", "").isalnum():
            raise BootstrapError(f"invalid role name: {role}")
    return roles


def normalize_control_mode(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in CONTROL_MODES:
        return normalized
    raise BootstrapError(f"invalid control mode: {value} (expected auto, shell, or codex)")


def validate_role_launch_options(roles: tuple[str, ...], options: dict[str, RoleLaunchOptions]) -> None:
    unknown = set(options) - set(roles)
    if unknown:
        raise BootstrapError(f"Codex launch options specified for unknown role(s): {', '.join(sorted(unknown))}")


def validate_acp_launch_options(role_yolo: bool, options: dict[str, RoleLaunchOptions]) -> None:
    unsupported = [
        role
        for role, launch in options.items()
        if launch.model or launch.profile or launch.reasoning_effort or launch.config_overrides or launch.yolo
    ]
    if role_yolo or unsupported:
        suffix = f" for: {', '.join(sorted(unsupported))}" if unsupported else ""
        raise BootstrapError(
            "ACP runtimes do not interpret Codex launch options; put provider-specific flags in --acp-agent-command"
            + suffix
        )


def resolve_role_scratchpads(project_root: Path, roles: tuple[str, ...], overrides: dict[str, Path]) -> dict[str, str]:
    unknown = set(overrides) - set(roles)
    if unknown:
        raise BootstrapError(f"role memory specified for unknown role(s): {', '.join(sorted(unknown))}")
    scratchpads: dict[str, str] = {}
    for role in roles:
        raw_path = overrides.get(role)
        if raw_path is None:
            scratchpads[role] = f".tmux-team/memory/{role}.md"
            continue
        path = raw_path.expanduser()
        if not path.is_absolute():
            scratchpads[role] = str(path)
        else:
            scratchpads[role] = str(path.resolve())
    return scratchpads


def summarize_text(value: str | None, limit: int = 160) -> str:
    if not value:
        return ""
    summary = " ".join(value.split())
    if len(summary) <= limit:
        return summary
    return summary[: limit - 3].rstrip() + "..."


def prepare_role_worktrees(
    project_root: Path,
    roles: tuple[str, ...],
    overrides: dict[str, Path],
    *,
    create_missing_worktrees: bool,
    worktree_base_ref: str,
    allow_shared_worktree_groups: tuple[frozenset[str], ...],
    allow_dirty_roles: frozenset[str],
    dry_run: bool,
) -> tuple[dict[str, Path], list[list[str]]]:
    unknown = set(overrides) - set(roles)
    if unknown:
        raise BootstrapError(f"role worktree specified for unknown role(s): {', '.join(sorted(unknown))}")

    role_worktrees = {role: resolve_role_worktree(project_root, overrides.get(role)) for role in roles}
    validate_shared_worktrees(role_worktrees, set(overrides), allow_shared_worktree_groups)

    commands: list[list[str]] = []
    for role, worktree in role_worktrees.items():
        explicit = role in overrides
        if not explicit:
            continue
        if not worktree.exists():
            if not create_missing_worktrees:
                raise BootstrapError(f"worktree for role {role!r} does not exist: {worktree}")
            commands.append(["git", "-C", str(project_root), "worktree", "add", str(worktree), worktree_base_ref])
            if not dry_run:
                run(commands[-1], check=True)
            continue

        if not worktree.is_dir():
            raise BootstrapError(f"worktree for role {role!r} is not a directory: {worktree}")
        if dry_run:
            continue
        if not is_git_worktree(worktree):
            if create_missing_worktrees and not any(worktree.iterdir()):
                command = ["git", "-C", str(project_root), "worktree", "add", str(worktree), worktree_base_ref]
                commands.append(command)
                run(command, check=True)
                continue
            raise BootstrapError(f"worktree for role {role!r} is not a git worktree: {worktree}")
        if role not in allow_dirty_roles and has_dirty_tracked_files(worktree):
            raise BootstrapError(f"worktree for role {role!r} has dirty tracked files: {worktree}")

    return role_worktrees, commands


def resolve_role_worktree(project_root: Path, value: Path | None) -> Path:
    if value is None:
        return project_root
    worktree = value.expanduser()
    if not worktree.is_absolute():
        worktree = project_root / worktree
    return worktree.resolve()


def validate_shared_worktrees(
    role_worktrees: dict[str, Path],
    explicit_roles: set[str],
    allow_shared_worktree_groups: tuple[frozenset[str], ...],
) -> None:
    by_path: dict[Path, set[str]] = {}
    for role, worktree in role_worktrees.items():
        if role in explicit_roles:
            by_path.setdefault(worktree, set()).add(role)
    for worktree, shared_roles in by_path.items():
        if len(shared_roles) < 2:
            continue
        if any(shared_roles <= allowed for allowed in allow_shared_worktree_groups):
            continue
        roles = ", ".join(sorted(shared_roles))
        raise BootstrapError(
            f"roles share worktree {worktree}: {roles} "
            "(use --allow-shared-worktree ROLE,ROLE to allow this deliberately)"
        )


def is_git_worktree(path: Path) -> bool:
    result = run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def has_dirty_tracked_files(path: Path) -> bool:
    result = run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"], check=True)
    return bool(result.stdout.strip())


def dry_run_role_bindings(
    roles: tuple[str, ...],
    session: str,
    agent_layout: str,
    agents_window: str,
    role_worktrees: dict[str, Path],
    *,
    agent_runtime: str = "codex",
    role_control_sockets: dict[str, Path] | None = None,
) -> dict[str, RoleBinding]:
    role_control_sockets = role_control_sockets or {}
    bindings: dict[str, RoleBinding] = {}
    for index, role in enumerate(roles):
        pane = f"{session}:{agents_window}.{index}" if agent_layout == "grouped" else f"{session}:{tt_name(role)}.0"
        bindings[role] = RoleBinding(
            thread_id=f"dry-thread-{role}" if agent_runtime == "codex" else "",
            pane=pane,
            worktree=role_worktrees[role],
            session_id=f"dry-session-{role}" if agent_runtime == "acp" else "",
            control_socket=str(role_control_sockets.get(role, "")),
        )
    return bindings


def normalize_agent_runtime(value: str) -> str:
    return agent_runtime_adapter(value).name


def agent_runtime_adapter(value: str) -> AgentRuntimeAdapter:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in ("codex", "app-server"):
        return CODEX_RUNTIME
    if normalized in ("acp", "cursor", "cursor-acp"):
        return ACP_RUNTIME
    raise BootstrapError(f"invalid agent runtime: {value} (expected codex or acp)")


def normalize_agent_layout(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in ("grouped", "agents", "single-window", "one-window", "tiled"):
        return "grouped"
    if normalized in ("separate", "separate-windows", "windows", "per-role-window"):
        return "separate-windows"
    raise BootstrapError(f"invalid agent layout: {value} (expected grouped or separate-windows)")


def default_session_name(project_root: Path) -> str:
    name = project_root.resolve().name or "tmux-team"
    return tt_name("".join(char if char.isalnum() or char in ("-", "_") else "-" for char in name))


def tt_name(value: str) -> str:
    return value if value.startswith(TT_PREFIX) else f"{TT_PREFIX}{value}"
