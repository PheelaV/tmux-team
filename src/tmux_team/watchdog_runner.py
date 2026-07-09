from __future__ import annotations

import shlex
from pathlib import Path

WATCHDOG_PANE_OPTION = "@tmux-team-watchdog"
WATCHDOGS_WINDOW = "tt-watchdogs"


def watchdog_pane_title(name: str) -> str:
    return f"tt-watchdog-{name}"


def watchdog_window_name(name: str | None = None) -> str:
    return name or WATCHDOGS_WINDOW


def watchdog_window_target(session: str, window_name: str | None = None) -> str:
    return f"{session}:{watchdog_window_name(window_name)}"


def watchdog_run_command(
    *,
    config_path: Path | None,
    name: str,
    interval: str,
    delivery: str,
    role: str | None = None,
    description: str | None = None,
    goal: str | None = None,
    notify_role: str | None = None,
    unacked_warn_seconds: int | None = None,
    ack_warn_seconds: int | None = None,
    obligation_grace_seconds: int | None = None,
) -> list[str]:
    command = ["tmux-team"]
    if config_path is not None:
        command.extend(["--config", str(config_path)])
    command.extend(
        [
            "watchdog",
            "run",
            "--name",
            name,
            "--interval",
            interval,
            "--delivery",
            delivery,
        ]
    )
    if unacked_warn_seconds is not None:
        command.extend(["--unacked-warn-seconds", str(unacked_warn_seconds)])
    if ack_warn_seconds is not None:
        command.extend(["--ack-warn-seconds", str(ack_warn_seconds)])
    if obligation_grace_seconds is not None:
        command.extend(["--obligation-grace-seconds", str(obligation_grace_seconds)])
    if role:
        command.extend(["--role", role])
    if description:
        command.extend(["--description", description])
    if goal:
        command.extend(["--goal", goal])
    if notify_role:
        command.extend(["--notify-role", notify_role])
    return command


def watchdog_spawn_command(
    *,
    tmux_bin: str,
    session: str,
    config_path: Path | None,
    project_root: Path,
    name: str,
    interval: str,
    delivery: str,
    role: str | None = None,
    description: str | None = None,
    goal: str | None = None,
    notify_role: str | None = None,
    window_name: str | None = None,
    unacked_warn_seconds: int | None = None,
    ack_warn_seconds: int | None = None,
    obligation_grace_seconds: int | None = None,
    use_existing_window: bool = False,
) -> list[str]:
    resolved_window_name = watchdog_window_name(window_name)
    run_command = shlex.join(
        watchdog_run_command(
            config_path=config_path,
            name=name,
            interval=interval,
            delivery=delivery,
            role=role,
            description=description,
            goal=goal,
            notify_role=notify_role,
            unacked_warn_seconds=unacked_warn_seconds,
            ack_warn_seconds=ack_warn_seconds,
            obligation_grace_seconds=obligation_grace_seconds,
        )
    )
    if use_existing_window:
        return [
            tmux_bin,
            "split-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            watchdog_window_target(session, resolved_window_name),
            "-c",
            str(project_root),
            run_command,
        ]
    return [
        tmux_bin,
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        session,
        "-n",
        resolved_window_name,
        "-c",
        str(project_root),
        run_command,
    ]


def watchdog_pane_setup_commands(
    tmux_bin: str,
    pane: str,
    *,
    name: str,
) -> list[list[str]]:
    title = watchdog_pane_title(name)
    return [
        [tmux_bin, "set-option", "-p", "-t", pane, WATCHDOG_PANE_OPTION, name],
        [tmux_bin, "select-pane", "-t", pane, "-T", title],
    ]


def watchdog_layout_command(tmux_bin: str, session: str, window_name: str | None = None) -> list[str]:
    return [tmux_bin, "select-layout", "-t", watchdog_window_target(session, window_name), "tiled"]
