from __future__ import annotations

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

from .app_server import AppServerClient
from .config import load_config
from .store import Store

DEFAULT_ROLES = ("orchestrator", "implementer", "collector", "trainer")
DEFAULT_AGENT_LAYOUT = "grouped"
DEFAULT_CONTROL_WINDOW = "tt-control"
DEFAULT_APP_SERVER_WINDOW = "tt-app-server"
DEFAULT_AGENTS_WINDOW = "tt-agents"
AGENT_LAYOUTS = ("grouped", "separate-windows")
ROLE_PANE_OPTION = "@tmux-team-role"
TT_PREFIX = "tt-"


@dataclass(frozen=True)
class BootstrapResult:
    session: str
    endpoint: str
    config_path: Path
    role_threads: dict[str, str]
    role_panes: dict[str, str]


@dataclass(frozen=True)
class RoleBinding:
    thread_id: str
    pane: str


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
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
    dry_run: bool,
) -> BootstrapResult:
    project_root = project_root.expanduser().resolve()
    config_path = config_path.expanduser()
    if not config_path.is_absolute():
        config_path = project_root / config_path

    if not roles:
        raise BootstrapError("at least one role is required")
    agent_layout = normalize_agent_layout(agent_layout)
    if config_path.exists() and not force_config and not dry_run:
        raise BootstrapError(f"config already exists: {config_path} (use --force-config to replace)")
    if shutil.which(tmux_bin) is None and not dry_run:
        raise BootstrapError(f"tmux binary not found: {tmux_bin}")
    if shutil.which(codex_bin) is None and not dry_run:
        raise BootstrapError(f"codex binary not found: {codex_bin}")

    if dry_run:
        role_bindings = dry_run_role_bindings(roles, session, agent_layout, agents_window)
        for command in dry_run_tmux_commands(
            tmux_bin,
            codex_bin,
            session,
            endpoint,
            project_root,
            role_bindings,
            start_app_server=start_app_server,
            agent_layout=agent_layout,
            control_window=control_window,
            agents_window=agents_window,
            role_yolo=role_yolo,
            role_profile=role_profile,
        ):
            print(shell_join(command))
        print(render_team_config("tmux-team", runtime_dir, endpoint, role_bindings, role_yolo, role_profile))
        return BootstrapResult(
            session=session,
            endpoint=endpoint,
            config_path=config_path,
            role_threads={role: binding.thread_id for role, binding in role_bindings.items()},
            role_panes={role: binding.pane for role, binding in role_bindings.items()},
        )

    ensure_control_plane_window(tmux_bin, session, project_root, control_window)
    if start_app_server:
        for command in app_server_tmux_commands(tmux_bin, codex_bin, session, endpoint, project_root):
            run(command, check=True)

    wait_for_app_server(endpoint, timeout=20.0)
    role_bindings = start_role_panes_and_discover_threads(
        tmux_bin,
        codex_bin,
        session,
        endpoint,
        project_root,
        roles,
        agent_layout,
        agents_window,
        role_yolo,
        role_profile,
    )
    write_team_config(config_path, runtime_dir, endpoint, role_bindings, role_yolo, role_profile, force=force_config)

    if goal:
        send_initial_goal(config_path, goal)

    return BootstrapResult(
        session=session,
        endpoint=endpoint,
        config_path=config_path,
        role_threads={role: binding.thread_id for role, binding in role_bindings.items()},
        role_panes={role: binding.pane for role, binding in role_bindings.items()},
    )


def dry_run_tmux_commands(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
    role_bindings: dict[str, RoleBinding],
    *,
    start_app_server: bool,
    agent_layout: str,
    control_window: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
) -> list[list[str]]:
    commands: list[list[str]] = [shell_session_command(tmux_bin, session, project_root, control_window)]
    if start_app_server:
        app_server_command = keep_open_command(
            f"{shlex.quote(codex_bin)} app-server --listen {shlex.quote(endpoint)}",
            DEFAULT_APP_SERVER_WINDOW,
        )
        commands.append(
            new_window_command(tmux_bin, session, DEFAULT_APP_SERVER_WINDOW, project_root, app_server_command)
        )
    for index, role in enumerate(role_bindings):
        command = role_shell_command(codex_bin, endpoint, project_root, role_yolo=role_yolo, role_profile=role_profile)
        if agent_layout == "grouped":
            if index == 0:
                commands.append(new_window_command(tmux_bin, session, agents_window, project_root, command))
                commands.extend(label_role_pane_commands(tmux_bin, f"{session}:{agents_window}.0", role))
            else:
                commands.append(split_window_command(tmux_bin, f"{session}:{agents_window}", project_root, command))
                commands.extend(label_role_pane_commands(tmux_bin, role_bindings[role].pane, role))
                commands.append([tmux_bin, "select-layout", "-t", f"{session}:{agents_window}", "tiled"])
        else:
            commands.append(
                role_new_window_command(
                    tmux_bin,
                    codex_bin,
                    session,
                    endpoint,
                    project_root,
                    role,
                    role_yolo=role_yolo,
                    role_profile=role_profile,
                )
            )
            commands.extend(label_role_pane_commands(tmux_bin, role_bindings[role].pane, role))
    return commands


def app_server_tmux_commands(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
) -> list[list[str]]:
    command = keep_open_command(
        f"{shlex.quote(codex_bin)} app-server --listen {shlex.quote(endpoint)}",
        DEFAULT_APP_SERVER_WINDOW,
    )
    if tmux_session_exists(tmux_bin, session):
        if tmux_window_exists(tmux_bin, session, DEFAULT_APP_SERVER_WINDOW):
            return []
        return [new_window_command(tmux_bin, session, DEFAULT_APP_SERVER_WINDOW, project_root, command)]
    return [app_server_new_session_command(tmux_bin, session, project_root, command)]


def ensure_control_plane_window(tmux_bin: str, session: str, project_root: Path, control_window: str) -> None:
    if not tmux_session_exists(tmux_bin, session):
        run(shell_session_command(tmux_bin, session, project_root, control_window), check=True)
        return

    current_session = detect_current_tmux_session(tmux_bin)
    if current_session == session:
        current_window_id = detect_current_tmux_window_id(tmux_bin)
        if current_window_id:
            run([tmux_bin, "rename-window", "-t", current_window_id, control_window], check=True)
            return

    if not tmux_window_exists(tmux_bin, session, control_window):
        command = keep_open_command(f'printf "[tmux-team] {control_window} shell\\n"', control_window)
        run(new_window_command(tmux_bin, session, control_window, project_root, command), check=True)


def start_role_panes_and_discover_threads(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
    roles: tuple[str, ...],
    agent_layout: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
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
            role,
            index,
            agent_layout,
            agents_window,
            role_yolo,
            role_profile,
        )
        thread_id = wait_for_new_loaded_thread(endpoint, loaded, timeout=20.0)
        role_bindings[role] = RoleBinding(thread_id=thread_id, pane=pane)
        loaded.add(thread_id)
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
    role: str,
    index: int,
    agent_layout: str,
    agents_window: str,
    role_yolo: bool,
    role_profile: str | None,
) -> str:
    command = role_shell_command(codex_bin, endpoint, project_root, role_yolo=role_yolo, role_profile=role_profile)
    if agent_layout == "grouped":
        if index == 0:
            result = run(
                new_window_command(tmux_bin, session, agents_window, project_root, command, print_pane=True), check=True
            )
            pane = result.stdout.strip()
            configure_agent_window(tmux_bin, session, agents_window)
        else:
            result = run(
                split_window_command(tmux_bin, f"{session}:{agents_window}", project_root, command, print_pane=True),
                check=True,
            )
            pane = result.stdout.strip()
            run([tmux_bin, "select-layout", "-t", f"{session}:{agents_window}", "tiled"], check=True)
        if pane:
            label_role_pane(tmux_bin, pane, role)
            return pane
        return f"{session}:{agents_window}.{index}"

    role_window = tt_name(role)
    if tmux_window_exists(tmux_bin, session, role_window):
        run(
            [tmux_bin, "respawn-window", "-k", "-t", f"{session}:{role_window}", "-c", str(project_root), command],
            check=True,
        )
        pane = first_pane_id(tmux_bin, f"{session}:{role_window}")
    else:
        result = run(
            role_new_window_command(
                tmux_bin,
                codex_bin,
                session,
                endpoint,
                project_root,
                role,
                role_yolo=role_yolo,
                role_profile=role_profile,
                print_pane=True,
            ),
            check=True,
        )
        pane = result.stdout.strip()
    if pane:
        label_role_pane(tmux_bin, pane, role)
        return pane
    return f"{session}:{role_window}.0"


def configure_agent_window(tmux_bin: str, session: str, agents_window: str) -> None:
    target = f"{session}:{agents_window}"
    run([tmux_bin, "set-window-option", "-t", target, "pane-border-status", "top"], check=False)
    run(
        [
            tmux_bin,
            "set-window-option",
            "-t",
            target,
            "pane-border-format",
            f"#{{pane_index}}: #{{{ROLE_PANE_OPTION}}}",
        ],
        check=False,
    )


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
        ok, details = store.notify_role(conn, "orchestrator", "app-server-turn")
        if not ok:
            raise BootstrapError(f"initial goal queued as {message.id}, but app-server wake failed: {details}")


def write_team_config(
    path: Path,
    runtime_dir: str,
    endpoint: str,
    role_bindings: dict[str, RoleBinding],
    role_yolo: bool,
    role_profile: str | None,
    *,
    force: bool,
) -> None:
    if path.exists() and not force:
        raise BootstrapError(f"config already exists: {path} (use --force-config to replace)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_team_config("tmux-team", runtime_dir, endpoint, role_bindings, role_yolo, role_profile),
        encoding="utf-8",
    )


def render_team_config(
    team_name: str,
    runtime_dir: str,
    endpoint: str,
    role_bindings: dict[str, RoleBinding],
    role_yolo: bool = False,
    role_profile: str | None = None,
) -> str:
    roles: dict[str, dict[str, str | bool]] = {}
    for role, binding in role_bindings.items():
        role_data: dict[str, str | bool] = {
            "mode": "app_server_remote_tui",
            "state": "active",
            "pane": binding.pane,
            "notify_method": "app-server-turn",
            "app_server_endpoint": endpoint,
            "codex_thread_id": binding.thread_id,
        }
        if role_yolo:
            role_data["codex_yolo"] = True
        if role_profile:
            role_data["codex_profile"] = role_profile
        roles[role] = role_data
    return tomli_w.dumps({"team": {"name": team_name, "runtime_dir": runtime_dir}, "roles": roles})


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


def shell_session_command(tmux_bin: str, session: str, cwd: Path, window: str = DEFAULT_CONTROL_WINDOW) -> list[str]:
    return [tmux_bin, "new-session", "-d", "-s", session, "-n", window, "-c", str(cwd)]


def role_shell_command(
    codex_bin: str,
    endpoint: str,
    project_root: Path,
    *,
    role_yolo: bool = False,
    role_profile: str | None = None,
) -> str:
    codex_args = [codex_bin]
    if role_profile:
        codex_args.extend(["--profile", role_profile])
    if role_yolo:
        codex_args.append("--dangerously-bypass-approvals-and-sandbox")
    codex_args.extend(["--remote", endpoint])
    command = f"cd {shlex.quote(str(project_root))} && {' '.join(shlex.quote(part) for part in codex_args)}"
    return keep_open_command(command, "role TUI")


def keep_open_command(command: str, label: str) -> str:
    script = (
        f"{command}\n"
        "status=$?\n"
        f"printf '\\n[tmux-team] {label} exited with status %s. Shell left open for inspection.\\n' \"$status\"\n"
        'exec "${SHELL:-/bin/sh}"\n'
    )
    return f"sh -lc {shlex.quote(script)}"


def role_new_window_command(
    tmux_bin: str,
    codex_bin: str,
    session: str,
    endpoint: str,
    project_root: Path,
    role: str,
    *,
    role_yolo: bool = False,
    role_profile: str | None = None,
    print_pane: bool = False,
) -> list[str]:
    return new_window_command(
        tmux_bin,
        session,
        tt_name(role),
        project_root,
        role_shell_command(codex_bin, endpoint, project_root, role_yolo=role_yolo, role_profile=role_profile),
        print_pane=print_pane,
    )


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


def dry_run_role_bindings(
    roles: tuple[str, ...],
    session: str,
    agent_layout: str,
    agents_window: str,
) -> dict[str, RoleBinding]:
    bindings: dict[str, RoleBinding] = {}
    for index, role in enumerate(roles):
        pane = f"{session}:{agents_window}.{index}" if agent_layout == "grouped" else f"{session}:{tt_name(role)}.0"
        bindings[role] = RoleBinding(thread_id=f"dry-thread-{role}", pane=pane)
    return bindings


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
