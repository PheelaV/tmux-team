from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .bootstrap import (
    AGENT_LAYOUTS,
    CONTROL_MODES,
    DEFAULT_AGENT_LAYOUT,
    DEFAULT_AGENTS_WINDOW,
    DEFAULT_CONTROL_WINDOW,
    ROLE_CONTRACT_VERSION,
    ROLE_PANE_OPTION,
    BootstrapError,
    RoleLaunchOptions,
    bootstrap_team,
    default_session_name,
    detect_current_tmux_pane_id,
    detect_current_tmux_session,
    free_local_endpoint,
    parse_roles,
)
from .config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    DEFAULT_RUNTIME_DIR,
    ROLE_ENV,
    RUNTIME_HOME_ENV,
    ConfigError,
    OperatorConfig,
    load_config,
    role_scratchpad_path,
    runtime_dir_env,
    write_default_config,
    write_operator_config,
)
from .dashboard import (
    DashboardDependencyError,
    collect_dashboard_snapshot,
    render_dashboard_snapshot,
    run_textual_dashboard,
)
from .display import (
    codex_settings_summary,
    format_seconds_duration,
    role_capabilities,
    watchdog_runner_display_state,
)
from .extensions.manifest import ExtensionError, inspect_extensions
from .extensions.runner import HookDenied, HookError
from .lifecycle import LifecycleError, resume_team, sleep_team
from .policy import PolicyContext, authorize, normalize_policy_mode
from .service import TeamService
from .store import (
    MESSAGE_STATE_FILTERS,
    OBLIGATION_ACTIVE_STATES,
    OBLIGATION_PAUSED_STATE,
    OBLIGATION_STATES,
    OBLIGATION_VISIBLE_STATES,
    ROLE_STATES,
    STALE_CLAIMED_STATE,
    TODO_STATES,
    Store,
    normalize_priority,
    normalize_watchdog_runner_name,
    parse_utc_datetime,
    pending_count_from_state_counts,
)
from .watchdog_runner import (
    watchdog_layout_command,
    watchdog_pane_setup_commands,
    watchdog_spawn_command,
    watchdog_window_name,
)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_PANE_SUMMARY_MAX_BYTES = 20_000
DEFAULT_PANE_SUMMARY_TIMEOUT_SECONDS = 120.0
DEFAULT_WATCHDOG_INTERVAL = "15m"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(normalize_time_option_values(raw_argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        configure_logging(args.log_level, args.log_file)
    except ValueError as exc:
        print(f"tmux-team: {exc}", file=sys.stderr)
        return 2

    if args.command == "init":
        return cmd_init(args)
    if args.command == "bootstrap":
        return cmd_bootstrap(args)

    try:
        config = load_config(args.config, args.runtime_dir)
        policy_context = build_policy_context(args, config)
        apply_actor_defaults(args, policy_context)
        authorize_cli_command(args, config, policy_context)
    except (BootstrapError, ConfigError, ExtensionError, PermissionError, ValueError, KeyError) as exc:
        print(f"tmux-team: {exc}", file=sys.stderr)
        return 2

    store = Store(config)
    try:
        with store.connect() as conn:
            if args.command == "config":
                return cmd_config(args, config)
            if args.command == "status":
                return cmd_status(args, store, conn)
            if args.command == "dashboard":
                return cmd_dashboard(args, store, conn, config)
            if args.command == "operator":
                return cmd_operator(args, config)
            if args.command == "ext":
                return cmd_ext(args, config)

            service = TeamService(store)
            if args.command == "send":
                return cmd_send(args, service, conn)
            if args.command == "broadcast":
                return cmd_broadcast(args, service, conn)
            if args.command == "inbox":
                return cmd_inbox(args, service, conn)
            if args.command == "memory":
                return cmd_memory(args, config)
            if args.command == "todo":
                return cmd_todo(args, store, conn)
            if args.command == "milestone":
                return cmd_milestone(args, store, conn)
            if args.command == "obligation":
                return cmd_obligation(args, store, conn)
            if args.command == "role":
                return cmd_role(args, store, conn)
            if args.command == "pane":
                return cmd_pane(args, store, conn)
            if args.command == "notify":
                return cmd_notify(args, service, conn)
            if args.command == "watchdog":
                return cmd_watchdog(args, store, service, conn)
            if args.command == "sleep":
                return cmd_sleep(args, store, conn, config)
            if args.command == "resume":
                return cmd_resume(args, store, conn, config)
            if args.command == "codex":
                return cmd_codex(args, store, service, conn)
            if args.command == "stable":
                return cmd_stable(args, store, conn)
    except (
        ConfigError,
        ExtensionError,
        HookDenied,
        HookError,
        LifecycleError,
        ValueError,
        KeyError,
        PermissionError,
    ) as exc:
        print(f"tmux-team: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 2


def configure_logging(level: str | None = None, log_file: str | None = None) -> None:
    numeric_level = parse_log_level(level or "WARNING")
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    else:
        handlers = [logging.StreamHandler()]
    logging.basicConfig(level=numeric_level, format=LOG_FORMAT, handlers=handlers, force=True)


def parse_log_level(level: str) -> int:
    value = getattr(logging, level.strip().upper(), None)
    if not isinstance(value, int):
        raise ValueError(f"invalid log level: {level}")
    return value


def normalize_time_option_values(argv: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in ("--since", "--until") and index + 1 < len(argv):
            next_value = argv[index + 1]
            if next_value.startswith("-") and parse_duration(next_value) is not None:
                normalized.append(f"{value}={next_value}")
                index += 2
                continue
        normalized.append(value)
        index += 1
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmux-team")
    parser.add_argument("--version", action="version", version=f"tmux-team {__version__}")
    parser.add_argument("--config", help=f"Path to .tmux-team/team.toml; overrides ${CONFIG_PATH_ENV}")
    parser.add_argument("--runtime-dir", help="Override runtime directory")
    parser.add_argument("--actor", help=f"Authenticated role actor for policy enforcement; defaults to ${ROLE_ENV}")
    parser.add_argument("--log-level", default="WARNING", help="Python log level: DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--log-file", help="Write logs to a file instead of stderr")
    parser.add_argument(
        "--policy-mode",
        help="Policy mode override: strict or permissive. Use permissive as an explicit breakglass opt-out.",
    )

    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="Create a project team config")
    init.add_argument("--config", help="Config path to create")
    init.add_argument("--name", default="default", help="Team name")
    init.add_argument("--runtime-dir", help="Runtime directory to write into the config")

    config = subparsers.add_parser("config", help="Inspect loaded config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="Show resolved config")

    status = subparsers.add_parser("status", help="Show roles and queue counts")
    status.add_argument("--verbose", action="store_true", help="Show active message summaries per role")
    status.add_argument("--active-limit", type=int, default=3, help="Maximum active messages to show per role")
    status.add_argument(
        "--unacked-warn-seconds",
        type=int,
        default=300,
        help="Warn in verbose output when claimed work is not acknowledged after this many seconds",
    )

    dashboard = subparsers.add_parser("dashboard", help="Open a read-only operator dashboard")
    dashboard.add_argument("--refresh", type=float, default=2.0, help="Live dashboard refresh interval in seconds")
    dashboard.add_argument("--once", action="store_true", help="Print one text snapshot and exit")
    dashboard.add_argument("--role", help="Limit the dashboard to one role")
    dashboard.add_argument("--no-pane-preview", action="store_true", help="Do not capture role pane tails")
    dashboard.add_argument("--pane-lines", type=int, default=8, help="Pane tail lines to show when preview is enabled")
    dashboard.add_argument(
        "--provenance", action="store_true", help="Show row-level dashboard source/confidence labels"
    )
    dashboard.add_argument("--tmux-bin", default="tmux")

    operator = subparsers.add_parser("operator", help="Inspect or update operator recovery metadata")
    operator_sub = operator.add_subparsers(dest="operator_command", required=True)
    operator_bind = operator_sub.add_parser("bind", help="Record the operator/control pane metadata in team.toml")
    operator_bind.add_argument("--pane", help="Operator tmux pane id/target; defaults to current tmux pane")
    operator_bind.add_argument("--codex-thread-id", help="Operator Codex thread id, when known")
    operator_bind.add_argument("--tmux-bin", default="tmux")
    operator_sub.add_parser("show", help="Show operator recovery metadata")

    ext = subparsers.add_parser("ext", help="Inspect tmux-team extensions")
    ext_sub = ext.add_subparsers(dest="ext_command", required=True)
    ext_sub.add_parser("list", help="List discovered extensions")
    ext_sub.add_parser("doctor", help="Validate extension manifests")

    bootstrap = subparsers.add_parser("bootstrap", help="Start a pane-resident Codex team in tmux")
    bootstrap.add_argument("--project-root", default=".", help="Project root for the team")
    bootstrap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config path to create")
    bootstrap.add_argument(
        "--runtime-dir",
        default=None,
        help=f"Runtime directory for team state; defaults to ${RUNTIME_HOME_ENV} or {DEFAULT_RUNTIME_DIR}",
    )
    bootstrap.add_argument(
        "--session",
        default=None,
        help="tmux session name; defaults to current tmux session or tt-<project>",
    )
    bootstrap.add_argument(
        "--roles", default=None, help="Comma-separated roles; default: orchestrator,implementer,collector,trainer"
    )
    bootstrap.add_argument(
        "--endpoint",
        default=None,
        help="Existing or desired app-server endpoint; default picks a free local ws:// port",
    )
    bootstrap.add_argument("--codex-bin", default="codex")
    bootstrap.add_argument("--tmux-bin", default="tmux")
    bootstrap.add_argument(
        "--agent-layout",
        default=DEFAULT_AGENT_LAYOUT,
        choices=AGENT_LAYOUTS,
        help="Role pane layout: grouped puts all agents in one tiled window; separate-windows uses one window per role",
    )
    bootstrap.add_argument(
        "--control-window", default=DEFAULT_CONTROL_WINDOW, help="Name for the launcher/operator window"
    )
    bootstrap.add_argument(
        "--control-mode",
        default="auto",
        choices=CONTROL_MODES,
        help="tt-control startup mode: auto reuses an existing tmux launcher or starts Codex; shell starts a plain shell",
    )
    bootstrap.add_argument("--agents-window", default=DEFAULT_AGENTS_WINDOW, help="Window name for grouped agent panes")
    bootstrap.add_argument(
        "--role-yolo",
        action="store_true",
        help="Launch managed role Codex TUIs with --dangerously-bypass-approvals-and-sandbox",
    )
    bootstrap.add_argument(
        "--role-profile",
        default=None,
        help="Launch managed role Codex TUIs with the named Codex profile",
    )
    bootstrap.add_argument(
        "--role-codex-profile",
        action="append",
        default=[],
        metavar="ROLE=PROFILE",
        help="Override Codex profile for one role; repeatable",
    )
    bootstrap.add_argument(
        "--role-model",
        action="append",
        default=[],
        metavar="ROLE=MODEL",
        help="Launch one role with a specific Codex model; repeatable",
    )
    bootstrap.add_argument(
        "--role-reasoning-effort",
        action="append",
        default=[],
        metavar="ROLE=EFFORT",
        help="Launch one role with a specific model_reasoning_effort config value; repeatable",
    )
    bootstrap.add_argument(
        "--role-codex-config",
        action="append",
        default=[],
        metavar="ROLE=KEY=VALUE",
        help="Pass a Codex -c key=value override to one role; repeatable",
    )
    bootstrap.add_argument(
        "--role-worktree",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help="Launch a role from a specific git worktree; repeatable",
    )
    bootstrap.add_argument(
        "--role-memory",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help="Use an existing or custom scratchpad memory path for one role; repeatable",
    )
    bootstrap.add_argument(
        "--create-missing-worktrees",
        action="store_true",
        help="Create missing role worktrees with git worktree add",
    )
    bootstrap.add_argument(
        "--worktree-base-ref",
        default="HEAD",
        help="Base ref for --create-missing-worktrees; default: HEAD",
    )
    bootstrap.add_argument(
        "--allow-shared-worktree",
        action="append",
        default=[],
        metavar="ROLE,ROLE",
        help="Allow a deliberate shared worktree for a comma-separated role group; repeatable",
    )
    bootstrap.add_argument(
        "--allow-dirty-role",
        action="append",
        default=[],
        metavar="ROLE",
        help="Allow dirty tracked files in one role worktree; repeatable",
    )
    bootstrap.add_argument("--goal", default=None, help="Initial goal body to queue to orchestrator after startup")
    bootstrap.add_argument("--goal-file", default=None, help="Read initial goal body from a file")
    bootstrap.add_argument("--force-config", action="store_true", help="Replace an existing .tmux-team/team.toml")
    bootstrap.add_argument(
        "--no-start-app-server", action="store_true", help="Use an already-running app-server endpoint"
    )
    bootstrap.add_argument(
        "--no-truecolor",
        action="store_true",
        help="Do not set tmux truecolor options on the managed session",
    )
    bootstrap.add_argument(
        "--dry-run", action="store_true", help="Print planned tmux commands and generated config without executing"
    )

    send = subparsers.add_parser("send", help="Queue a message")
    send.add_argument("--to", required=True, dest="recipient")
    send.add_argument("--from", default=None, dest="sender")
    send.add_argument("--priority", default="normal", choices=("urgent", "high", "normal", "low"))
    send.add_argument("--summary", required=True)
    body = send.add_mutually_exclusive_group()
    body.add_argument("--body", help="Inline body text, or '-' to read stdin")
    body.add_argument("--body-file", help="Path to markdown body")
    send.add_argument("--force", action="store_true", help="Queue even if the role is paused or draining")
    send.add_argument("--no-notify", action="store_true", help="Do not notify the target pane")
    send.add_argument("--correlation-key", help="Stable key for related or duplicate work")
    send.add_argument("--related-to", help="Related message id")
    send.add_argument("--supersedes", help="Message id this message replaces")
    send.add_argument("--allow-duplicate", action="store_true", help="Do not warn about matching active work")
    send.add_argument(
        "--notify-method",
        default="auto",
        help="Notification method: auto, display-message, send-keys, or app-server-turn",
    )

    broadcast = subparsers.add_parser("broadcast", help="Queue one message per recipient role")
    broadcast_scope = broadcast.add_mutually_exclusive_group()
    broadcast_scope.add_argument(
        "--only",
        "--to",
        action="append",
        dest="only",
        help="Only address these roles, comma-separated or repeatable; 'all' means all roles except sender",
    )
    broadcast_scope.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude these roles from the default all-roles target set; comma-separated or repeatable",
    )
    broadcast.add_argument("--from", default=None, dest="sender")
    broadcast.add_argument("--priority", default="normal", choices=("urgent", "high", "normal", "low"))
    broadcast.add_argument("--summary", required=True)
    broadcast_body = broadcast.add_mutually_exclusive_group()
    broadcast_body.add_argument("--body", help="Inline body text, or '-' to read stdin")
    broadcast_body.add_argument("--body-file", help="Path to markdown body")
    broadcast.add_argument("--force", action="store_true", help="Queue even if a role is paused or draining")
    broadcast.add_argument("--no-notify", action="store_true", help="Do not notify target panes")
    broadcast.add_argument("--notice", action="store_true", help="Record a notice instead of inbox work")
    broadcast.add_argument(
        "--notify-method",
        default="auto",
        help="Notification method: auto, display-message, send-keys, or app-server-turn",
    )

    inbox = subparsers.add_parser("inbox", help="Work with role inboxes")
    inbox_sub = inbox.add_subparsers(dest="inbox_command", required=True)
    inbox_next = inbox_sub.add_parser("next", help="Claim and print the next message")
    inbox_next.add_argument("--role", help=f"Role inbox; defaults to --actor or ${ROLE_ENV}")
    inbox_next.add_argument("--claim-seconds", type=int, default=3600)
    inbox_next.add_argument("--auto-ack", action="store_true", help="Acknowledge the claimed message immediately")
    inbox_list = inbox_sub.add_parser("list", help="List inbox messages")
    inbox_list.add_argument("--role", help=f"Role inbox; defaults to --actor or ${ROLE_ENV}")
    inbox_list.add_argument(
        "--state",
        action="append",
        choices=MESSAGE_STATE_FILTERS,
        help="Filter by stored state or derived pending/stale_claimed state; repeatable",
    )
    inbox_list.add_argument("--limit", type=int, default=50)
    inbox_list.add_argument("--verbose", action="store_true", help="Show correlation and relation metadata")
    inbox_reclaimable = inbox_sub.add_parser(
        "reclaimable", help="List expired claimed messages that inbox next can reclaim"
    )
    inbox_reclaimable.add_argument("--role", help=f"Role inbox; defaults to --actor or ${ROLE_ENV}")
    inbox_reclaimable.add_argument("--limit", type=int, default=50)
    inbox_ack = inbox_sub.add_parser("ack", help="Acknowledge a message")
    inbox_ack.add_argument("message_id")
    inbox_ack.add_argument("--role", help=f"Role inbox; defaults to --actor or ${ROLE_ENV}")
    inbox_complete = inbox_sub.add_parser("complete", help="Complete a message")
    inbox_complete.add_argument("message_id")
    inbox_complete.add_argument("--role", help=f"Role inbox; defaults to --actor or ${ROLE_ENV}")
    inbox_complete.add_argument("--status", default="done")
    inbox_complete.add_argument("--summary", default="")
    inbox_complete_body = inbox_complete.add_mutually_exclusive_group()
    inbox_complete_body.add_argument("--body", help="Detailed result text, or '-' to read stdin")
    inbox_complete_body.add_argument("--body-file", help="Path to detailed result text")
    inbox_complete.add_argument(
        "--reply-to-sender",
        action="store_true",
        help="Queue a completion reply to the original sender and wake it when it is a managed role",
    )
    inbox_complete.add_argument("--reply-no-notify", action="store_true", help="Queue the reply without waking sender")
    inbox_complete.add_argument(
        "--allow-open-todos",
        action="store_true",
        help="Complete even when the message still has open role todos",
    )
    inbox_complete_replies = inbox_sub.add_parser(
        "complete-replies", help="Complete claimed or acknowledged completion notices"
    )
    inbox_complete_replies.add_argument("--role", help=f"Role inbox; defaults to --actor or ${ROLE_ENV}")
    inbox_complete_replies.add_argument("--limit", type=int, default=50)
    inbox_complete_replies.add_argument("--status", default="done")
    inbox_complete_replies.add_argument("--summary", default="completion notice recorded")

    memory = subparsers.add_parser("memory", help="Inspect or update role scratchpad memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_path = memory_sub.add_parser("path", help="Print the role scratchpad path")
    memory_path.add_argument("--role", help=f"Role scratchpad; defaults to --actor or ${ROLE_ENV}")
    memory_show = memory_sub.add_parser("show", help="Print the role scratchpad")
    memory_show.add_argument("--role", help=f"Role scratchpad; defaults to --actor or ${ROLE_ENV}")
    memory_append = memory_sub.add_parser("append", help="Record a durable note near the top of the role scratchpad")
    memory_append.add_argument("--role", help=f"Role scratchpad; defaults to --actor or ${ROLE_ENV}")
    memory_body = memory_append.add_mutually_exclusive_group()
    memory_body.add_argument("--body", help="Inline note text, or '-' to read stdin")
    memory_body.add_argument("--body-file", help="Path to markdown note")
    memory_append.add_argument("note", nargs="?", help="Note text; kept for quick one-line updates")

    todo = subparsers.add_parser("todo", help="Track role-owned checklist items for active inbox work")
    todo_sub = todo.add_subparsers(dest="todo_command", required=True)
    todo_list = todo_sub.add_parser("list", help="List role todos")
    todo_list.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    todo_list.add_argument("--message", dest="message_id", help="Limit to one inbox message id")
    todo_list.add_argument("--state", action="append", choices=TODO_STATES, help="Filter state; repeatable")
    todo_list.add_argument("--limit", type=int, default=50)
    todo_add = todo_sub.add_parser("add", help="Add a todo for an active inbox message")
    todo_add.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    todo_add.add_argument("--message", dest="message_id", required=True, help="Active inbox message id")
    todo_add.add_argument("text", help="Todo text")
    todo_done = todo_sub.add_parser("done", help="Mark a todo done")
    todo_done.add_argument("todo_id")
    todo_done.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    todo_reopen = todo_sub.add_parser("reopen", help="Reopen a completed todo")
    todo_reopen.add_argument("todo_id")
    todo_reopen.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    todo_supersede = todo_sub.add_parser("supersede", help="Supersede a todo with a replacement todo")
    todo_supersede.add_argument("todo_id")
    todo_supersede.add_argument("text", help="Replacement todo text")
    todo_supersede.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    todo_clear = todo_sub.add_parser("clear", help="Delete todos for one role/message")
    todo_clear.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    todo_clear.add_argument("--message", dest="message_id", required=True, help="Inbox message id")
    todo_recover = todo_sub.add_parser("recover", help="Show active role work and associated todos")
    todo_recover.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    todo_recover.add_argument("--limit", type=int, default=10)

    milestone = subparsers.add_parser("milestone", help="Record or inspect append-only team milestones")
    milestone_sub = milestone.add_subparsers(dest="milestone_command", required=True)
    milestone_add = milestone_sub.add_parser("add", help="Append a milestone to runtime milestones.jsonl")
    milestone_add.add_argument("--summary", required=True, help="Concise milestone summary")
    milestone_add.add_argument(
        "--role",
        help=f"Legacy single subject role; defaults to --actor or ${ROLE_ENV} when no --subject-role/--team is set",
    )
    milestone_subject = milestone_add.add_mutually_exclusive_group()
    milestone_subject.add_argument(
        "--subject-role",
        action="append",
        default=[],
        help="Role the milestone is about; repeatable or comma-separated",
    )
    milestone_subject.add_argument("--team", action="store_true", help="Record a team-wide milestone")
    milestone_add.add_argument("--kind", default="milestone", help="Milestone kind, e.g. result, blocker, routing")
    milestone_add.add_argument("--ref", dest="ref_id", help="Related message id, commit, job id, or artifact id")
    milestone_add.add_argument("--tag", action="append", default=[], help="Tag for filtering; repeatable")
    milestone_add.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE", help="Metadata; repeatable")
    milestone_body = milestone_add.add_mutually_exclusive_group()
    milestone_body.add_argument("--body", help="Optional detail text, or '-' to read stdin")
    milestone_body.add_argument("--body-file", help="Path to optional detail text")

    milestone_list = milestone_sub.add_parser("list", help="List recorded milestones")
    milestone_list.add_argument("--since", help="Start time: ISO timestamp or relative duration like -4h, 30m, 2d")
    milestone_list.add_argument("--until", help="End time: ISO timestamp or relative duration")
    milestone_list.add_argument("--today", action="store_true", help="Show milestones since local midnight")
    milestone_list.add_argument("--role", help="Filter by legacy role or subject role")
    milestone_list.add_argument("--subject-role", help="Filter by subject role")
    milestone_list.add_argument("--team", action="store_true", help="Show team-wide milestones")
    milestone_list.add_argument("--kind", help="Filter by kind")
    milestone_list.add_argument("--tag", action="append", default=[], help="Require tag; repeatable")
    milestone_list.add_argument("--limit", type=int, default=50)
    milestone_list.add_argument("--json", action="store_true", help="Print JSON array instead of human output")

    obligation = subparsers.add_parser("obligation", help="Track role-owned commitments with expected updates")
    obligation_sub = obligation.add_subparsers(dest="obligation_command", required=True)
    obligation_start = obligation_sub.add_parser("start", help="Start a role-owned obligation")
    obligation_start.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    obligation_start.add_argument("--summary", required=True, help="Concise obligation summary")
    obligation_start.add_argument("--goal", help="Done condition or longer-lived objective")
    obligation_start.add_argument("--next-update-in", help="Expected next update duration, e.g. 15m")

    obligation_update = obligation_sub.add_parser("update", help="Record an obligation update or blocker")
    obligation_update.add_argument("obligation_id")
    obligation_update.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    obligation_update.add_argument("--summary", required=True, help="Current obligation state")
    obligation_update.add_argument("--state", default="active", choices=OBLIGATION_ACTIVE_STATES)
    obligation_update.add_argument("--next-update-in", help="Expected next update duration, e.g. 15m")

    obligation_pause = obligation_sub.add_parser("pause", help="Pause an obligation until resume or review")
    obligation_pause.add_argument("obligation_id")
    obligation_pause.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    obligation_pause.add_argument("--reason", required=True, help="Why the obligation is intentionally paused")
    obligation_pause_review = obligation_pause.add_mutually_exclusive_group()
    obligation_pause_review.add_argument("--review-in", help="When to review the pause, e.g. 30m")
    obligation_pause_review.add_argument("--review-at", help="Absolute ISO review timestamp")

    obligation_resume = obligation_sub.add_parser("resume", help="Resume a paused obligation")
    obligation_resume.add_argument("obligation_id")
    obligation_resume.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    obligation_resume.add_argument("--summary", required=True, help="Fresh resumed obligation state")
    obligation_resume.add_argument("--next-update-in", help="Expected next update duration, e.g. 15m")

    obligation_complete = obligation_sub.add_parser("complete", help="Complete an obligation")
    obligation_complete.add_argument("obligation_id")
    obligation_complete.add_argument("--role", help=f"Owning role; defaults to --actor or ${ROLE_ENV}")
    obligation_complete.add_argument("--status", default="done", choices=("done", "failed", "cancelled"))
    obligation_complete.add_argument("--summary", required=True, help="Terminal obligation summary")

    obligation_list = obligation_sub.add_parser("list", help="List obligations")
    obligation_list.add_argument("--role", help="Owning role; defaults to --actor or all roles for operator")
    obligation_list.add_argument("--state", action="append", choices=OBLIGATION_STATES, help="Filter state; repeatable")
    obligation_list.add_argument("--limit", type=int, default=50)

    role = subparsers.add_parser("role", help="Inspect or change role state")
    role_sub = role.add_subparsers(dest="role_command", required=True)
    role_sub.add_parser("list", help="List roles")
    for state_command, state in (
        ("pause", "paused"),
        ("resume", "active"),
        ("drain", "draining"),
        ("retire", "retired"),
        ("fail", "failed"),
    ):
        role_state = role_sub.add_parser(state_command, help=f"Set role state to {state}")
        role_state.add_argument("role")

    pane = subparsers.add_parser("pane", help="Inspect managed role tmux panes")
    pane_sub = pane.add_subparsers(dest="pane_command", required=True)
    pane_list = pane_sub.add_parser("list", help="List managed role panes")
    pane_list.add_argument("--all", action="store_true", help="Include unmanaged panes in managed role windows")
    pane_list.add_argument("--tmux-bin", default="tmux")
    pane_capture = pane_sub.add_parser("capture", help="Print recent stdout/history from a role pane")
    pane_capture.add_argument("role")
    pane_capture.add_argument(
        "--lines",
        "--limit",
        type=int,
        default=80,
        dest="lines",
        help="Number of pane history lines to print",
    )
    pane_capture.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip this many newest pane history lines before printing",
    )
    pane_capture.add_argument("--summary", action="store_true", help="Summarize captured pane output with codex exec")
    pane_capture.add_argument(
        "--summary-max-bytes",
        type=int,
        default=DEFAULT_PANE_SUMMARY_MAX_BYTES,
        help="Maximum captured pane text bytes to send to codex exec in summary mode",
    )
    pane_capture.add_argument(
        "--summary-timeout",
        type=float,
        default=DEFAULT_PANE_SUMMARY_TIMEOUT_SECONDS,
        help="Maximum seconds to wait for codex exec in summary mode",
    )
    pane_capture.add_argument("--tmux-bin", default="tmux")

    notify = subparsers.add_parser("notify", help="Notify a role about pending work")
    notify.add_argument("role")
    notify.add_argument("--method", default="auto")

    watchdog = subparsers.add_parser("watchdog", help="Check for stale or stuck team state")
    watchdog.add_argument("--role", help="Limit checks to one role")
    watchdog.add_argument("--unacked-warn-seconds", type=int, default=300)
    watchdog.add_argument("--ack-warn-seconds", type=int, default=3600)
    watchdog.add_argument("--obligation-grace-seconds", type=int, default=0)
    watchdog.add_argument("--json", action="store_true", help="Print findings as JSON")
    watchdog_sub = watchdog.add_subparsers(dest="watchdog_command")
    watchdog_run = watchdog_sub.add_parser("run", help="Run a visible periodic watchdog loop in this pane")
    watchdog_run.add_argument("--name", default="default", help="Runner name")
    watchdog_run.add_argument("--interval", default=DEFAULT_WATCHDOG_INTERVAL, help="Run interval such as 15m")
    watchdog_run.add_argument("--role", help="Limit checks to one role")
    watchdog_run.add_argument("--description", help="Human-readable runner purpose")
    watchdog_run.add_argument("--goal", help="Pressure goal or done condition")
    watchdog_run.add_argument("--notify-role", help="Role to receive durable escalation messages")
    watchdog_run.add_argument("--delivery", default="report-only", help="Delivery method label for status output")
    watchdog_run.add_argument("--unacked-warn-seconds", type=int, default=300)
    watchdog_run.add_argument("--ack-warn-seconds", type=int, default=3600)
    watchdog_run.add_argument("--obligation-grace-seconds", type=int, default=0)
    watchdog_run.add_argument("--once", action="store_true", help="Run once, record state, deliver pressure, and stop")
    watchdog_run.add_argument("--iterations", type=int, help="Exit after this many iterations; useful for tests")
    watchdog_start = watchdog_sub.add_parser("start", help="Start a watchdog runner in a visible tmux window")
    watchdog_start.add_argument("--name", default="default", help="Runner name")
    watchdog_start.add_argument("--interval", default=DEFAULT_WATCHDOG_INTERVAL, help="Run interval such as 15m")
    watchdog_start.add_argument("--role", help="Limit checks to one role")
    watchdog_start.add_argument("--description", help="Human-readable runner purpose")
    watchdog_start.add_argument("--goal", help="Pressure goal or done condition")
    watchdog_start.add_argument("--notify-role", help="Role to receive durable escalation messages")
    watchdog_start.add_argument("--delivery", default="report-only", help="Delivery method label for status output")
    watchdog_start.add_argument("--unacked-warn-seconds", type=int, default=300)
    watchdog_start.add_argument("--ack-warn-seconds", type=int, default=3600)
    watchdog_start.add_argument("--obligation-grace-seconds", type=int, default=0)
    watchdog_start.add_argument("--session", help="tmux session; defaults to current tmux session")
    watchdog_start.add_argument("--window-name", help="tmux watchdog window name; defaults to tt-watchdogs")
    watchdog_start.add_argument("--tmux-bin", default="tmux")
    watchdog_start.add_argument("--dry-run", action="store_true", help="Print planned tmux commands without executing")
    watchdog_stop = watchdog_sub.add_parser("stop", help="Stop a watchdog runner and optionally kill its tmux pane")
    watchdog_stop.add_argument("name", nargs="?", default="default")
    watchdog_stop.add_argument("--tmux-bin", default="tmux")
    watchdog_stop.add_argument("--no-kill-pane", action="store_true", help="Only mark the runner stopped in state")
    watchdog_pause = watchdog_sub.add_parser("pause", help="Pause a watchdog runner without terminalizing it")
    watchdog_pause.add_argument("name", nargs="?", default="default")
    watchdog_pause.add_argument("--reason", required=True, help="Why the runner is intentionally paused")
    watchdog_pause_review = watchdog_pause.add_mutually_exclusive_group()
    watchdog_pause_review.add_argument("--review-in", help="When to review the pause, e.g. 30m")
    watchdog_pause_review.add_argument("--review-at", help="Absolute ISO review timestamp")
    watchdog_resume = watchdog_sub.add_parser("resume", help="Resume a paused watchdog runner")
    watchdog_resume.add_argument("name", nargs="?", default="default")
    watchdog_update = watchdog_sub.add_parser("update", help="Update a running or paused watchdog runner")
    watchdog_update.add_argument("name", nargs="?", default="default")
    watchdog_update.add_argument("--interval", help="Run interval such as 15m")
    watchdog_update.add_argument("--role", help="Limit checks to one role")
    watchdog_update.add_argument("--team", action="store_true", help="Clear role scope and inspect the whole team")
    watchdog_update.add_argument("--description", help="Human-readable runner purpose")
    watchdog_update.add_argument("--goal", help="Pressure goal or done condition")
    watchdog_update.add_argument("--notify-role", help="Role to receive durable escalation messages")
    watchdog_update.add_argument("--no-notify-role", action="store_true", help="Clear the explicit notify target")
    watchdog_update.add_argument("--delivery", help="Delivery method label or app-server-turn for pressure")
    watchdog_list = watchdog_sub.add_parser("list", help="List watchdog runner lifecycle state")
    watchdog_list.add_argument("--json", action="store_true")
    watchdog_list.add_argument("--limit", type=int, default=50)
    watchdog_list.add_argument("--stale-grace-seconds", type=int, default=60)
    watchdog_status = watchdog_sub.add_parser("status", help="Show one watchdog runner or all runners")
    watchdog_status.add_argument("name", nargs="?")
    watchdog_status.add_argument("--json", action="store_true")
    watchdog_status.add_argument("--stale-grace-seconds", type=int, default=60)

    sleep = subparsers.add_parser("sleep", help="Snapshot current bindings and tear down managed tmux team windows")
    sleep.add_argument(
        "--session", default=None, help="tmux session to tear down; inferred from role panes when omitted"
    )
    sleep.add_argument("--tmux-bin", default="tmux")
    sleep.add_argument(
        "--dry-run", action="store_true", help="Print snapshot/teardown plan without writing or killing panes"
    )
    sleep.add_argument("--force", action="store_true", help="Allow managing an unexpected tt-control role window")
    sleep.add_argument(
        "--kill-session", action="store_true", help="Kill the whole tmux session instead of managed windows only"
    )
    sleep.add_argument(
        "--no-pause-roles",
        action="store_true",
        help="Do not mark active/draining roles paused after snapshotting",
    )

    resume = subparsers.add_parser("resume", help="Resume managed panes from a tmux-team sleep snapshot")
    resume.add_argument("--snapshot", help="Sleep snapshot path; defaults to runtime sleeps/latest.toml")
    resume.add_argument("--session", help="tmux session to restore into; defaults to the snapshot session")
    resume.add_argument("--endpoint", help="App-server endpoint; defaults to first endpoint in the snapshot")
    resume.add_argument("--codex-bin", default="codex")
    resume.add_argument("--tmux-bin", default="tmux")
    resume.add_argument(
        "--agent-layout",
        default=DEFAULT_AGENT_LAYOUT,
        choices=AGENT_LAYOUTS,
        help="Role pane layout for resumed role panes",
    )
    resume.add_argument("--agents-window", default=DEFAULT_AGENTS_WINDOW, help="Name for grouped role-agent window")
    resume.add_argument("--role-yolo", action="store_true", help="Resume managed role TUIs with Codex YOLO mode")
    resume.add_argument("--role-profile", default=None, help="Codex profile to pass to every managed role TUI")
    resume.add_argument("--role-codex-profile", action="append", default=[], metavar="ROLE=PROFILE")
    resume.add_argument("--role-model", action="append", default=[], metavar="ROLE=MODEL")
    resume.add_argument("--role-reasoning-effort", action="append", default=[], metavar="ROLE=EFFORT")
    resume.add_argument("--role-codex-config", action="append", default=[], metavar="ROLE=KEY=VALUE")
    resume.add_argument("--no-start-app-server", action="store_true", help="Use an already-running app-server endpoint")
    resume.add_argument("--no-reactivate-roles", action="store_true", help="Do not set resumed roles active")
    resume.add_argument(
        "--no-truecolor",
        action="store_true",
        help="Do not set tmux truecolor options on the restored session",
    )
    resume.add_argument("--dry-run", action="store_true", help="Print planned tmux commands without executing")

    codex = subparsers.add_parser("codex", help="Manage Codex app-server role bindings")
    codex_sub = codex.add_subparsers(dest="codex_command", required=True)
    codex_bind = codex_sub.add_parser("bind", help="Bind a role to a Codex app-server thread")
    codex_bind.add_argument("role")
    codex_bind.add_argument("--endpoint", required=True, help="Codex app-server endpoint, e.g. ws://127.0.0.1:4500")
    codex_bind.add_argument("--thread-id", required=True, help="Codex thread id shown by /status or app-server tooling")
    codex_show = codex_sub.add_parser("show", help="Show a role's Codex app-server binding")
    codex_show.add_argument("role")
    codex_wake = codex_sub.add_parser("wake", help="Submit a wake turn to a role's Codex app-server thread")
    codex_wake.add_argument("role")
    codex_context = codex_sub.add_parser("session-context", help="Print reset-safe role context for Codex hooks")
    codex_context.add_argument("--role", help=f"Role context; defaults to --actor or ${ROLE_ENV}")
    codex_context.add_argument(
        "--max-memory-chars",
        type=int,
        default=4000,
        help="Maximum scratchpad characters to include; 0 omits scratchpad content",
    )

    stable = subparsers.add_parser("stable", help="Manage stable commit approvals")
    stable_sub = stable.add_subparsers(dest="stable_command", required=True)
    stable_approve = stable_sub.add_parser("approve", help="Approve a stable commit")
    stable_approve.add_argument("commit")
    stable_approve.add_argument("--role", default="global", help="Role scope, or global")
    stable_approve.add_argument("--by", default=None)
    stable_approve.add_argument("--note", default=None)
    stable_current = stable_sub.add_parser("current", help="Show approved stable commits")
    stable_current.add_argument("--role", default=None)
    stable_sync = stable_sub.add_parser("sync", help="Print or apply stable checkout for a role")
    stable_sync.add_argument("--role", required=True)
    stable_sync.add_argument("--apply", action="store_true", help="Run git checkout --detach")

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config or DEFAULT_CONFIG_PATH).expanduser()
    try:
        write_default_config(path, args.name, args.runtime_dir)
    except ConfigError as exc:
        print(f"tmux-team: {exc}", file=sys.stderr)
        return 2
    print(f"created {path}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    try:
        project_root = Path(args.project_root).expanduser().resolve()
        session = args.session or detect_current_tmux_session(args.tmux_bin) or default_session_name(project_root)
        endpoint = args.endpoint or free_local_endpoint()
        roles = parse_roles(args.roles)
        role_worktrees = parse_role_worktrees(args.role_worktree)
        role_scratchpads = parse_role_paths(args.role_memory, "--role-memory")
        role_launch_options = parse_role_launch_options(
            role_profiles=parse_assignments(args.role_codex_profile, "--role-codex-profile"),
            role_models=parse_assignments(args.role_model, "--role-model"),
            role_reasoning_efforts=parse_assignments(args.role_reasoning_effort, "--role-reasoning-effort"),
            role_config_overrides=parse_role_config_overrides(args.role_codex_config),
        )
        shared_worktrees = parse_role_groups(args.allow_shared_worktree)
        allow_dirty_roles = frozenset(args.allow_dirty_role)
        goal = args.goal
        if args.goal_file:
            try:
                goal = Path(args.goal_file).expanduser().read_text(encoding="utf-8")
            except OSError as exc:
                raise BootstrapError(f"could not read goal file {args.goal_file}: {exc}") from exc
        result = bootstrap_team(
            project_root=project_root,
            config_path=Path(args.config),
            runtime_dir=args.runtime_dir or runtime_dir_env() or str(DEFAULT_RUNTIME_DIR),
            session=session,
            roles=roles,
            endpoint=endpoint,
            codex_bin=args.codex_bin,
            tmux_bin=args.tmux_bin,
            goal=goal,
            force_config=args.force_config,
            start_app_server=not args.no_start_app_server,
            agent_layout=args.agent_layout,
            control_window=args.control_window,
            control_mode=args.control_mode,
            agents_window=args.agents_window,
            role_yolo=args.role_yolo,
            role_profile=args.role_profile,
            role_launch_options=role_launch_options,
            role_scratchpads=role_scratchpads,
            role_worktrees=role_worktrees,
            create_missing_worktrees=args.create_missing_worktrees,
            worktree_base_ref=args.worktree_base_ref,
            allow_shared_worktree_groups=shared_worktrees,
            allow_dirty_roles=allow_dirty_roles,
            enable_truecolor=not args.no_truecolor,
            dry_run=args.dry_run,
        )
    except BootstrapError as exc:
        print(f"tmux-team: {exc}", file=sys.stderr)
        return 2

    print(f"session: {result.session}")
    print(f"endpoint: {result.endpoint}")
    print(f"config: {result.config_path}")
    print("roles:")
    for role, thread_id in result.role_threads.items():
        print(f"  {role}: thread_id={thread_id}")
    return 0


def build_policy_context(args: argparse.Namespace, config) -> PolicyContext:
    mode = args.policy_mode or config.policy.mode
    actor = args.actor or os.environ.get(ROLE_ENV) or infer_role_from_tmux_pane(config) or infer_role_from_cwd(config)
    return PolicyContext(actor=actor, mode=normalize_policy_mode(mode))


def parse_role_worktrees(values: Sequence[str]) -> dict[str, Path]:
    return parse_role_paths(values, "--role-worktree")


def parse_role_paths(values: Sequence[str], flag: str) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for value in values:
        role, path = parse_assignment(value, flag)
        parsed[role] = Path(path)
    return parsed


def parse_assignments(values: Sequence[str], flag: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, assigned = parse_assignment(value, flag)
        parsed[key] = assigned
    return parsed


def parse_role_config_overrides(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    parsed: dict[str, list[str]] = {}
    for value in values:
        role, override = parse_assignment(value, "--role-codex-config")
        if "=" not in override:
            raise BootstrapError("--role-codex-config expects ROLE=KEY=VALUE")
        parsed.setdefault(role, []).append(override)
    return {role: tuple(overrides) for role, overrides in parsed.items()}


def parse_role_launch_options(
    *,
    role_profiles: dict[str, str],
    role_models: dict[str, str],
    role_reasoning_efforts: dict[str, str],
    role_config_overrides: dict[str, tuple[str, ...]],
) -> dict[str, RoleLaunchOptions]:
    roles = set(role_profiles) | set(role_models) | set(role_reasoning_efforts) | set(role_config_overrides)
    return {
        role: RoleLaunchOptions(
            model=role_models.get(role),
            reasoning_effort=role_reasoning_efforts.get(role),
            profile=role_profiles.get(role),
            config_overrides=role_config_overrides.get(role, ()),
        )
        for role in roles
    }


def parse_role_groups(values: Sequence[str]) -> tuple[frozenset[str], ...]:
    groups: list[frozenset[str]] = []
    for value in values:
        roles = frozenset(part.strip() for part in value.split(",") if part.strip())
        if len(roles) < 2:
            raise BootstrapError("--allow-shared-worktree expects at least two comma-separated roles")
        groups.append(roles)
    return tuple(groups)


def resolve_broadcast_recipients(args: argparse.Namespace, roles: dict) -> tuple[str, ...]:
    requested = split_csv_values(args.only or ())
    excluded = set(split_csv_values(args.exclude or ()))
    all_roles = tuple(roles)
    if not all_roles:
        raise ValueError("broadcast requires at least one configured role")
    unknown_excluded = tuple(sorted(role for role in excluded if role not in roles))
    if unknown_excluded:
        raise KeyError(f"Unknown excluded role: {', '.join(unknown_excluded)}")

    if not requested or "all" in requested:
        recipients = [role for role in all_roles if role != args.sender]
    else:
        recipients = requested

    result: list[str] = []
    seen: set[str] = set()
    for role in recipients:
        if role in excluded:
            continue
        if role not in roles:
            raise KeyError(f"Unknown recipient role: {role}")
        if role not in seen:
            seen.add(role)
            result.append(role)
    if not result:
        raise ValueError("broadcast has no recipients after applying exclusions")
    return tuple(result)


def split_csv_values(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(result)


def parse_assignment(value: str, flag: str) -> tuple[str, str]:
    if "=" not in value:
        raise BootstrapError(f"{flag} expects ROLE=PATH")
    role, path = value.split("=", 1)
    role = role.strip()
    path = path.strip()
    if not role or not path:
        raise BootstrapError(f"{flag} expects ROLE=PATH")
    return role, path


def apply_actor_defaults(args: argparse.Namespace, policy_context: PolicyContext) -> None:
    if args.command == "send" and args.sender is None:
        args.sender = policy_context.actor or "operator"
    if args.command == "broadcast" and args.sender is None:
        args.sender = policy_context.actor or "operator"
    if args.command == "inbox" and getattr(args, "role", None) is None:
        args.role = policy_context.actor
        if args.role is None:
            raise ValueError(f"inbox --role is required unless --actor or ${ROLE_ENV} is set")
    if args.command == "memory" and getattr(args, "role", None) is None:
        args.role = policy_context.actor
        if args.role is None:
            raise ValueError(f"memory --role is required unless --actor or ${ROLE_ENV} is set")
    if args.command == "todo" and getattr(args, "role", None) is None:
        args.role = policy_context.actor
        if args.role is None:
            raise ValueError(f"todo --role is required unless --actor or ${ROLE_ENV} is set")
    if args.command == "milestone" and args.milestone_command == "add":
        if args.role is None and not args.subject_role and not args.team:
            args.role = policy_context.actor
        if args.actor is None:
            args.actor = policy_context.actor or "operator"
    if args.command == "obligation":
        if getattr(args, "role", None) is None and policy_context.actor is not None:
            args.role = policy_context.actor
        if args.obligation_command != "list" and args.role is None:
            raise ValueError(
                f"obligation {args.obligation_command} --role is required unless --actor or ${ROLE_ENV} is set"
            )
    if args.command == "codex" and args.codex_command == "session-context" and args.role is None:
        args.role = policy_context.actor
        if args.role is None:
            raise ValueError(f"codex session-context --role is required unless --actor or ${ROLE_ENV} is set")


def infer_role_from_cwd(config) -> str | None:
    cwd = Path.cwd().resolve()
    matches: list[str] = []
    for role in config.roles.values():
        if not role.worktree:
            continue
        try:
            worktree = Path(role.worktree).expanduser().resolve()
        except OSError:
            continue
        if cwd == worktree or worktree in cwd.parents:
            matches.append(role.name)
    if len(matches) == 1:
        return matches[0]
    return None


def infer_role_from_tmux_pane(config) -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, f"#{{{ROLE_PANE_OPTION}}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    role = result.stdout.strip()
    if role in config.roles:
        return role
    return None


def authorize_cli_command(args: argparse.Namespace, config, policy_context: PolicyContext) -> None:
    if args.command == "send":
        authorize(
            config,
            policy_context,
            "message.send",
            sender=args.sender,
            recipient=args.recipient,
        )
        return

    if args.command == "broadcast":
        for recipient in resolve_broadcast_recipients(args, config.roles):
            authorize(
                config,
                policy_context,
                "message.send",
                sender=args.sender,
                recipient=recipient,
            )
        return

    if args.command == "inbox":
        authorize(config, policy_context, f"inbox.{args.inbox_command}", role=args.role)
        return

    if args.command == "memory":
        action = "memory.update" if args.memory_command == "append" else "memory.read"
        authorize(config, policy_context, action, role=args.role)
        return

    if args.command == "todo":
        action = f"todo.{args.todo_command}"
        authorize(config, policy_context, action, role=args.role)
        return

    if args.command == "milestone":
        if args.milestone_command == "add":
            authorize(config, policy_context, "milestone.add", role=args.role or "")
        else:
            authorize(config, policy_context, "milestone.list", role=args.role or "")
        return

    if args.command == "obligation":
        authorize(config, policy_context, f"obligation.{args.obligation_command}", role=args.role or "")
        return

    if args.command == "role" and args.role_command != "list":
        authorize(config, policy_context, "role.state.change", role=args.role)
        return

    if args.command == "pane" and args.pane_command == "capture":
        authorize(config, policy_context, "pane.capture", role=args.role)
        return

    if args.command == "pane" and args.pane_command == "list":
        authorize(config, policy_context, "pane.list", all=str(args.all).lower())
        return

    if args.command == "notify":
        authorize(config, policy_context, "role.notify", role=args.role, method=args.method)
        return

    if args.command == "watchdog":
        command = args.watchdog_command or "check"
        action = "watchdog.list" if command in ("check", "list", "status") else "watchdog.manage"
        authorize(config, policy_context, action)
        return

    if args.command == "sleep":
        authorize(config, policy_context, "team.sleep")
        return

    if args.command == "resume":
        authorize(config, policy_context, "team.resume")
        return

    if args.command == "operator" and args.operator_command == "bind":
        authorize(config, policy_context, "operator.metadata")
        return

    if args.command == "codex" and args.codex_command == "bind":
        authorize(config, policy_context, "codex.bind", role=args.role)
        return

    if args.command == "codex" and args.codex_command == "wake":
        authorize(config, policy_context, "role.notify", role=args.role, method="app-server-turn")
        return

    if args.command == "codex" and args.codex_command == "session-context":
        authorize(config, policy_context, "memory.read", role=args.role)
        return

    if args.command == "stable" and args.stable_command == "approve":
        authorize(config, policy_context, "stable.approve", role=args.role)
        return


def cmd_config(args: argparse.Namespace, config) -> int:
    if args.config_command != "show":
        return 2
    print(f"team: {config.name}")
    print(f"config: {config.config_path or '(none)'}")
    print(f"project_root: {config.project_root}")
    print(f"runtime_dir: {config.runtime_dir}")
    print(f"operator: {operator_one_line(config.operator)}")
    print("roles:")
    for role in sorted(config.roles.values(), key=lambda item: item.name):
        pane = role.pane or "-"
        worktree = role.worktree or "-"
        print(f"  {role.name}: mode={role.mode} state={role.state} pane={pane} worktree={worktree}")
    return 0


def cmd_status(args: argparse.Namespace, store: Store, conn) -> int:
    counts = store.active_counts(conn)
    print(f"team: {store.config.name}")
    print(f"runtime_dir: {store.runtime_dir}")
    if args.verbose:
        print(f"operator: {operator_one_line(store.config.operator)}")
    print("roles:")
    for role in store.list_roles(conn):
        role_counts = counts.get(role["name"], {})
        stale_claimed = role_counts.get(STALE_CLAIMED_STATE, 0)
        pending = pending_count_from_state_counts(role_counts)
        claimed = role_counts.get("claimed", 0)
        acknowledged = role_counts.get("acknowledged", 0)
        completed = role_counts.get("completed", 0)
        pane = role["pane"] or "-"
        worktree = role["worktree"] or "-"
        print(
            f"  {role['name']}: state={role['state']} mode={role['mode']} pane={pane} worktree={worktree} "
            f"pending={pending} stale_claimed={stale_claimed} claimed={claimed} ack={acknowledged} done={completed}"
        )
        if args.verbose:
            print(f"    codex: {codex_settings_summary(role_capabilities(role))}")
            rows = store.list_active_messages(conn, role=role["name"], limit=args.active_limit)
            if not rows:
                print("    active: none")
            else:
                print("    active:")
                todo_counts = store.open_todo_counts(conn, role=role["name"], message_ids=(row["id"] for row in rows))
                for row in rows:
                    line = active_message_line(row, args.unacked_warn_seconds)
                    open_todos = todo_counts.get(row["id"], 0)
                    if open_todos:
                        line = f"{line} todos_open={open_todos}"
                    print(f"      {line}")
            obligations = store.list_obligations(
                conn, role=role["name"], states=OBLIGATION_VISIBLE_STATES, limit=args.active_limit
            )
            if obligations:
                print("    obligations:")
                for obligation in obligations:
                    print(f"      {obligation_one_line(obligation)}")
    if args.verbose:
        runners = store.list_watchdog_runners(conn, limit=args.active_limit)
        print("watchdog_runners:")
        if not runners:
            print("  none")
        else:
            for runner in runners:
                print(f"  {watchdog_runner_one_line(runner, stale_grace_seconds=60)}")
    return 0


def cmd_operator(args: argparse.Namespace, config) -> int:
    if args.operator_command == "show":
        print(f"operator: {operator_one_line(config.operator)}")
        return 0
    if args.operator_command == "bind":
        if config.config_path is None:
            raise ConfigError("operator bind requires a config file")
        pane = args.pane or detect_current_tmux_pane_id(args.tmux_bin) or config.operator.pane
        thread_id = args.codex_thread_id or config.operator.codex_thread_id
        if not pane and not thread_id:
            raise ValueError("operator bind needs --pane, --codex-thread-id, or a current tmux pane")
        operator = OperatorConfig(
            pane=pane,
            codex_thread_id=thread_id,
            capabilities=config.operator.capabilities,
        )
        write_operator_config(config.config_path, operator)
        print(f"operator: {operator_one_line(operator)}")
        return 0
    return 2


def operator_one_line(operator: OperatorConfig) -> str:
    pane = operator.pane or "unknown"
    thread = operator.codex_thread_id or "unknown"
    return f"pane={pane} codex_thread_id={thread}"


def cmd_dashboard(args: argparse.Namespace, store: Store, conn, config) -> int:
    if args.refresh <= 0:
        raise ValueError("dashboard --refresh must be greater than 0")
    if args.pane_lines < 0:
        raise ValueError("dashboard --pane-lines must be 0 or greater")
    include_pane_preview = not args.no_pane_preview and args.pane_lines > 0

    if args.once:
        snapshot = collect_dashboard_snapshot(
            store,
            conn,
            role_filter=args.role,
            include_pane_preview=include_pane_preview,
            pane_lines=args.pane_lines,
            tmux_bin=args.tmux_bin,
        )
        print(render_dashboard_snapshot(snapshot, provenance=args.provenance), end="")
        return 0

    try:
        return run_textual_dashboard(
            config,
            role_filter=args.role,
            refresh=args.refresh,
            include_pane_preview=include_pane_preview,
            pane_line_count=args.pane_lines,
            tmux_bin=args.tmux_bin,
            provenance=args.provenance,
        )
    except DashboardDependencyError as exc:
        raise ValueError(str(exc)) from exc


def cmd_ext(args: argparse.Namespace, config) -> int:
    inspection = inspect_extensions(config)
    if args.ext_command == "list":
        if not config.extensions.enabled:
            print("extensions: disabled")
            return 0
        if not inspection.manifests and not inspection.errors:
            print("extensions: none")
            return 0
        print("extensions:")
        for manifest in inspection.manifests:
            print(
                f"  {manifest.id} version={manifest.version} source={manifest.source} "
                f"hooks={len(manifest.hooks)} path={manifest.path}"
            )
        if inspection.errors:
            print("invalid:")
            for error in inspection.errors:
                print(f"  {error.path}: {error.message}")
        return 0

    if args.ext_command == "doctor":
        if not config.extensions.enabled:
            print("extensions disabled")
            return 0
        if inspection.errors:
            print("extension errors:", file=sys.stderr)
            for error in inspection.errors:
                print(f"  {error.path}: {error.message}", file=sys.stderr)
            return 1
        print(f"extensions ok: {len(inspection.manifests)}")
        for manifest in inspection.manifests:
            print(f"  {manifest.id}: hooks={len(manifest.hooks)}")
        return 0

    return 2


def cmd_memory(args: argparse.Namespace, config) -> int:
    path = role_scratchpad_path(config, args.role)

    if args.memory_command == "path":
        print(path)
        return 0

    if args.memory_command == "show":
        if not path.exists():
            print(f"{path} (missing)")
            return 0
        print(path.read_text(encoding="utf-8"), end="")
        return 0

    if args.memory_command == "append":
        note = read_memory_note(args)
        if not note:
            raise ValueError("memory append requires a note or '-'")
        record_memory_update(path, note)
        print(path)
        return 0

    return 2


def cmd_todo(args: argparse.Namespace, store: Store, conn) -> int:
    if args.todo_command == "list":
        states = tuple(args.state) if args.state else None
        rows = store.list_todos(conn, role=args.role, message_id=args.message_id, states=states, limit=args.limit)
        if not rows:
            target = f"{args.role}/{args.message_id}" if args.message_id else args.role
            print(f"no todos for {target}")
            return 0
        for row in rows:
            print(todo_one_line(row))
        return 0

    if args.todo_command == "add":
        row = store.add_todo(
            conn,
            role=args.role,
            message_id=args.message_id,
            text=args.text,
            actor=args.actor or args.role,
        )
        print(todo_one_line(row))
        return 0

    if args.todo_command == "done":
        row = store.complete_todo(conn, role=args.role, todo_id=args.todo_id, actor=args.actor or args.role)
        print(todo_one_line(row))
        return 0

    if args.todo_command == "reopen":
        row = store.reopen_todo(conn, role=args.role, todo_id=args.todo_id, actor=args.actor or args.role)
        print(todo_one_line(row))
        return 0

    if args.todo_command == "supersede":
        old, new = store.supersede_todo(
            conn,
            role=args.role,
            todo_id=args.todo_id,
            replacement_text=args.text,
            actor=args.actor or args.role,
        )
        print(f"superseded: {todo_one_line(old)}")
        print(f"replacement: {todo_one_line(new)}")
        return 0

    if args.todo_command == "clear":
        deleted = store.clear_todos(
            conn,
            role=args.role,
            message_id=args.message_id,
            actor=args.actor or args.role,
        )
        print(f"cleared {deleted} todo(s) for {args.role}/{args.message_id}")
        return 0

    if args.todo_command == "recover":
        return cmd_todo_recover(args, store, conn)

    return 2


def cmd_todo_recover(args: argparse.Namespace, store: Store, conn) -> int:
    active_messages = store.list_in_progress_messages(conn, role=args.role, limit=args.limit)
    if not active_messages:
        print(f"no active claimed or acknowledged messages for {args.role}")
    else:
        print(f"active work for {args.role}:")
        for row in active_messages:
            print(f"  {active_message_line(row)}")
            todos = store.list_todos(conn, role=args.role, message_id=row["id"], limit=args.limit)
            if not todos:
                print("    todos: none")
            else:
                for todo in todos:
                    print(f"    {todo_one_line(todo)}")

    recent = store.list_todos(conn, role=args.role, states=("done", "superseded"), limit=args.limit)
    if recent:
        print("recent completed/superseded todos:")
        for row in recent:
            print(f"  {todo_one_line(row)}")
    return 0


def cmd_milestone(args: argparse.Namespace, store: Store, conn) -> int:
    if args.milestone_command == "add":
        body = read_optional_body(args)
        subject_roles = milestone_subject_roles(args)
        scope = "team" if args.team else None
        milestone = store.record_milestone(
            conn,
            actor=args.actor or "operator",
            role=args.role,
            subject_roles=subject_roles,
            scope=scope,
            kind=args.kind,
            summary=args.summary,
            body=body,
            ref_id=args.ref_id,
            tags=tuple(args.tag),
            metadata=parse_metadata(args.meta),
        )
        print(
            f"{milestone['created_at']} {milestone['kind']} "
            f"recorded_by={milestone['recorded_by']} subject={milestone_subject_label(milestone)} "
            f"{milestone['summary']}"
        )
        print(f"path: {store.milestones_path}")
        return 0

    if args.milestone_command == "list":
        since, until = milestone_time_window(args)
        rows = store.list_milestones(
            since=since,
            until=until,
            role=args.role,
            subject_role=args.subject_role,
            scope="team" if args.team else None,
            kind=args.kind,
            tags=tuple(args.tag),
            limit=args.limit,
        )
        if args.json:
            print(json_dumps(rows))
            return 0
        if not rows:
            print("no milestones")
            return 0
        for row in rows:
            print(format_milestone(row))
        return 0

    return 2


def cmd_obligation(args: argparse.Namespace, store: Store, conn) -> int:
    if args.obligation_command == "start":
        row = store.start_obligation(
            conn,
            role=args.role,
            summary=args.summary,
            goal=args.goal,
            created_by=args.actor or "operator",
            next_update_at=obligation_next_update_at(args.next_update_in),
        )
        print(obligation_one_line(row))
        return 0

    if args.obligation_command == "update":
        row = store.update_obligation(
            conn,
            role=args.role,
            obligation_id=args.obligation_id,
            summary=args.summary,
            status=args.state,
            next_update_at=obligation_next_update_at(args.next_update_in),
            actor=args.actor or "operator",
        )
        print(obligation_one_line(row))
        return 0

    if args.obligation_command == "pause":
        row = store.pause_obligation(
            conn,
            role=args.role,
            obligation_id=args.obligation_id,
            reason=args.reason,
            review_at=review_at_from_args(args.review_in, args.review_at),
            actor=args.actor or "operator",
        )
        print(obligation_one_line(row))
        return 0

    if args.obligation_command == "resume":
        row = store.resume_obligation(
            conn,
            role=args.role,
            obligation_id=args.obligation_id,
            summary=args.summary,
            next_update_at=obligation_next_update_at(args.next_update_in),
            actor=args.actor or "operator",
        )
        print(obligation_one_line(row))
        return 0

    if args.obligation_command == "complete":
        row = store.complete_obligation(
            conn,
            role=args.role,
            obligation_id=args.obligation_id,
            status=args.status,
            summary=args.summary,
            actor=args.actor or "operator",
        )
        print(obligation_one_line(row))
        return 0

    if args.obligation_command == "list":
        states = tuple(args.state) if args.state else OBLIGATION_VISIBLE_STATES
        rows = store.list_obligations(conn, role=args.role, states=states, limit=args.limit)
        if not rows:
            role = args.role or "all roles"
            print(f"no obligations for {role}")
            return 0
        for row in rows:
            print(obligation_one_line(row))
        return 0

    return 2


def cmd_send(args: argparse.Namespace, service: TeamService, conn) -> int:
    normalize_priority(args.priority)
    body = read_body(args)
    result = service.send_message(
        conn,
        sender=args.sender,
        recipient=args.recipient,
        priority=args.priority,
        summary=args.summary,
        body=body,
        force=args.force,
        wake=not args.no_notify,
        notify_method=args.notify_method,
        correlation_key=args.correlation_key,
        related_to=args.related_to,
        supersedes=args.supersedes,
        allow_duplicate=args.allow_duplicate,
        actor=args.actor or args.sender,
    )
    message = result.message
    print(f"{message.id} {message.state} to={message.recipient} priority={message.priority}")
    print(f"body: {message.body_path}")

    if result.notification is not None:
        if result.notification.ok:
            print(f"notify: {result.notification.details}")
        else:
            print(f"notify_failed: {result.notification.details}", file=sys.stderr)

    print_duplicate_warnings(result.duplicates)

    if result.blocked is not None:
        print(f"blocked: role {result.blocked['role']} is {result.blocked['state']}", file=sys.stderr)
        return 2
    return 0


def cmd_broadcast(args: argparse.Namespace, service: TeamService, conn) -> int:
    normalize_priority(args.priority)
    body = read_body(args)
    recipients = resolve_broadcast_recipients(args, service.store.config.roles)
    print(f"broadcast: {len(recipients)} recipient(s)")
    blocked = False
    for recipient in recipients:
        if args.notice:
            result = service.send_notice(
                conn,
                sender=args.sender,
                recipient=recipient,
                summary=args.summary,
                body=body,
                force=args.force,
                wake=not args.no_notify,
                notify_method=args.notify_method,
                actor=args.actor or args.sender,
            )
        else:
            result = service.send_message(
                conn,
                sender=args.sender,
                recipient=recipient,
                priority=args.priority,
                summary=args.summary,
                body=body,
                force=args.force,
                wake=not args.no_notify,
                notify_method=args.notify_method,
                actor=args.actor or args.sender,
            )
        message = result.message
        print(f"{message.id} {message.state} to={message.recipient} priority={message.priority}")
        print(f"body: {message.body_path}")
        if result.notification is not None:
            if result.notification.ok:
                print(f"notify: {result.notification.details}")
            else:
                print(f"notify_failed: {result.notification.details}", file=sys.stderr)
        print_duplicate_warnings(result.duplicates)
        if result.blocked is not None:
            blocked = True
            print(f"blocked: role {result.blocked['role']} is {result.blocked['state']}", file=sys.stderr)
    return 2 if blocked else 0


def cmd_inbox(args: argparse.Namespace, service: TeamService, conn) -> int:
    if args.inbox_command == "next":
        row = service.claim_next(conn, args.role, args.claim_seconds, actor=args.actor)
        if row is None:
            active_rows = service.store.list_in_progress_messages(conn, role=args.role, limit=5)
            if active_rows:
                print(f"no pending messages for {args.role}")
                print("active work already claimed or acknowledged:")
                todo_counts = service.store.open_todo_counts(
                    conn, role=args.role, message_ids=(active_row["id"] for active_row in active_rows)
                )
                for active_row in active_rows:
                    line = active_message_line(active_row)
                    open_todos = todo_counts.get(active_row["id"], 0)
                    if open_todos:
                        line = f"{line} todos_open={open_todos}"
                    print(f"  {line}")
                    todos = service.store.list_todos(
                        conn, role=args.role, message_id=active_row["id"], states=("open",), limit=5
                    )
                    for todo in todos:
                        print(f"    {todo_one_line(todo)}")
                print("recover: tmux-team todo recover")
                return 1
            print(f"no pending messages for {args.role}")
            return 1
        if args.auto_ack:
            row = service.ack_message(conn, args.role, row["id"], actor=args.actor)
        print_message(row, include_body=True)
        return 0

    if args.inbox_command == "list":
        states = tuple(args.state) if args.state else None
        rows = service.store.list_messages(conn, role=args.role, states=states, limit=args.limit)
        if not rows:
            print(f"no messages for {args.role}")
            return 0
        for row in rows:
            print(message_one_line(row))
            if args.verbose:
                print(f"  {message_metadata_line(row)}")
        return 0

    if args.inbox_command == "reclaimable":
        rows = service.store.list_reclaimable_messages(conn, role=args.role, limit=args.limit)
        if not rows:
            print(f"no reclaimable messages for {args.role}")
            return 0
        for row in rows:
            print(message_one_line(row))
            print(f"  claimed_by={row['claimed_by'] or '-'} claim_expires_at={row['claim_expires_at'] or '-'}")
        return 0

    if args.inbox_command == "ack":
        row = service.ack_message(conn, args.role, args.message_id, actor=args.actor)
        print(message_one_line(row))
        return 0

    if args.inbox_command == "complete":
        open_todos = service.store.open_todo_count(conn, role=args.role, message_id=args.message_id)
        if open_todos and not args.allow_open_todos:
            raise ValueError(
                f"Message {args.message_id} has {open_todos} open todo(s); "
                "complete or supersede them, or pass --allow-open-todos"
            )
        summary = completion_summary(args)
        result = service.complete_message_with_optional_reply(
            conn,
            args.role,
            args.message_id,
            args.status,
            summary,
            reply_to_sender=args.reply_to_sender,
            reply_wake=not args.reply_no_notify,
            actor=args.actor,
        )
        print(message_one_line(result.message))
        if result.reply is not None:
            print(
                f"reply: {result.reply.message.id} {result.reply.message.state} "
                f"to={result.reply.message.recipient} priority={result.reply.message.priority}"
            )
            if result.reply.notification is not None:
                if result.reply.notification.ok:
                    print(f"reply_notify: {result.reply.notification.details}")
                else:
                    print(f"reply_notify_failed: {result.reply.notification.details}", file=sys.stderr)
        if result.reply_skipped is not None:
            print(f"reply_skipped: {result.reply_skipped}", file=sys.stderr)
        return 0

    if args.inbox_command == "complete-replies":
        rows = service.store.complete_completion_notices(
            conn,
            role=args.role,
            result_status=args.status,
            result_summary=args.summary,
            limit=args.limit,
            actor=args.actor or args.role,
        )
        print(f"completed {len(rows)} completion notice(s) for {args.role}")
        for row in rows:
            print(message_one_line(row))
        return 0

    return 2


def cmd_role(args: argparse.Namespace, store: Store, conn) -> int:
    if args.role_command == "list":
        for role in store.list_roles(conn):
            pane = role["pane"] or "-"
            worktree = role["worktree"] or "-"
            print(f"{role['name']} state={role['state']} mode={role['mode']} pane={pane} worktree={worktree}")
        return 0

    state_by_command = {
        "pause": "paused",
        "resume": "active",
        "drain": "draining",
        "retire": "retired",
        "fail": "failed",
    }
    state = state_by_command.get(args.role_command)
    if state not in ROLE_STATES:
        return 2
    store.set_role_state(conn, args.role, state, actor=args.actor or "operator")
    print(f"{args.role} state={state}")
    return 0


def cmd_pane(args: argparse.Namespace, store: Store, conn) -> int:
    if args.pane_command == "list":
        return cmd_pane_list(args, store, conn)

    if args.pane_command != "capture":
        return 2
    if args.lines <= 0:
        raise ValueError("pane capture --lines must be greater than 0")
    if args.offset < 0:
        raise ValueError("pane capture --offset must be 0 or greater")
    if args.summary and args.summary_max_bytes <= 0:
        raise ValueError("pane capture --summary-max-bytes must be greater than 0")
    if args.summary and args.summary_timeout <= 0:
        raise ValueError("pane capture --summary-timeout must be greater than 0")
    role = store.get_role(conn, args.role)
    if role is None:
        raise KeyError(f"Unknown role: {args.role}")
    pane = role["pane"]
    if not pane:
        print(f"role {args.role} has no pane", file=sys.stderr)
        return 1
    command = [args.tmux_bin, "capture-pane", "-p", "-t", pane, "-S", f"-{args.lines + args.offset}"]
    if args.offset:
        command.extend(["-E", f"-{args.offset + 1}"])
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        print(f"tmux-team: could not run {args.tmux_bin}: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"{args.tmux_bin} exited {result.returncode}").strip()
        print(details, file=sys.stderr)
        return 1
    if args.summary:
        summary = summarize_pane_capture(
            role=args.role,
            pane=pane,
            text=result.stdout,
            max_bytes=args.summary_max_bytes,
            timeout_seconds=args.summary_timeout,
        )
        print(summary, end="" if summary.endswith("\n") else "\n")
        return 0
    if args.offset:
        print(f"# pane {args.role} ({pane}) {args.lines} lines offset {args.offset}")
    else:
        print(f"# pane {args.role} ({pane}) last {args.lines} lines")
    print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return 0


def cmd_pane_list(args: argparse.Namespace, store: Store, conn) -> int:
    roles = store.list_roles(conn)
    managed_by_target = {role["pane"]: role for role in roles if role["pane"]}
    watchdogs = store.list_watchdog_runners(conn)
    watchdog_by_target = {row["pane"]: row for row in watchdogs if row["pane"]}
    print("managed panes:")
    if not managed_by_target:
        print("  none")
    for role in roles:
        pane = role["pane"] or "-"
        print(
            f"  role={role['name']} managed=true pane={pane} state={role['state']} worktree={role['worktree'] or '-'}"
        )

    if not args.all:
        return 0

    print("all panes in managed windows:")
    windows = sorted(
        {
            target
            for pane in tuple(managed_by_target) + tuple(watchdog_by_target)
            if (target := pane_window_target(args.tmux_bin, pane))
        }
    )
    if not windows:
        print("  none")
        return 0
    seen = False
    for window in windows:
        for pane in list_tmux_window_panes(args.tmux_bin, window):
            seen = True
            role = managed_by_target.get(pane["target"]) or managed_by_target.get(pane["id"])
            watchdog = watchdog_by_target.get(pane["target"]) or watchdog_by_target.get(pane["id"])
            role_name = role["name"] if role is not None else "-"
            managed = "true" if role is not None else "false"
            suffix = ""
            if watchdog is not None:
                suffix = f" watchdog={watchdog['name']} infrastructure=watchdog"
            print(
                f"  role={role_name} managed={managed} pane={pane['target']} pane_id={pane['id']} "
                f"command={pane['command'] or '-'} path={pane['path'] or '-'}{suffix}"
            )
    if not seen:
        print("  none")
    return 0


def cmd_notify(args: argparse.Namespace, service: TeamService, conn) -> int:
    result = service.notify_role(conn, args.role, args.method, actor=args.actor)
    if result.ok:
        print(result.details)
        return 0
    print(result.details, file=sys.stderr)
    return 1


def cmd_watchdog(args: argparse.Namespace, store: Store, service: TeamService, conn) -> int:
    if args.watchdog_command == "run":
        return cmd_watchdog_run(args, store, service, conn)
    if args.watchdog_command == "start":
        return cmd_watchdog_start(args, store, conn)
    if args.watchdog_command == "stop":
        return cmd_watchdog_stop(args, store, conn)
    if args.watchdog_command == "pause":
        return cmd_watchdog_pause(args, store, conn)
    if args.watchdog_command == "resume":
        return cmd_watchdog_resume(args, store, conn)
    if args.watchdog_command == "update":
        return cmd_watchdog_update(args, store, conn)
    if args.watchdog_command in ("list", "status"):
        return cmd_watchdog_list(args, store, conn)

    findings = watchdog_findings(
        store,
        conn,
        role=args.role,
        unacked_warn_seconds=args.unacked_warn_seconds,
        ack_warn_seconds=args.ack_warn_seconds,
        obligation_grace_seconds=args.obligation_grace_seconds,
    )
    if args.json:
        print(json_dumps(findings))
    elif not findings:
        print("watchdog ok")
    else:
        for finding in findings:
            print(format_watchdog_finding(finding))
    return 0


def cmd_watchdog_run(args: argparse.Namespace, store: Store, service: TeamService, conn) -> int:
    name = normalize_watchdog_runner_name(args.name)
    interval_seconds = parse_positive_duration_seconds(args.interval, "--interval")
    if args.once and args.iterations is not None:
        raise ValueError("watchdog run accepts either --once or --iterations, not both")
    if args.iterations is not None and args.iterations <= 0:
        raise ValueError("watchdog run --iterations must be greater than 0")
    max_iterations = 1 if args.once else args.iterations

    pane = os.environ.get("TMUX_PANE")
    window = current_tmux_window()
    process_id = os.getpid()
    iteration = 0
    try:
        while True:
            try:
                existing = store.get_watchdog_runner(conn, name)
            except KeyError:
                existing = None
            if existing is not None and existing["state"] in ("stopped", "failed"):
                existing = None
            if existing is not None and existing["state"] == "paused":
                print_watchdog_runner_header(existing, stale_grace_seconds=0)
                print("watchdog paused")
                sys.stdout.flush()
                if not sleep_watchdog_interval(
                    store,
                    conn,
                    name=name,
                    interval_seconds=paused_watchdog_sleep_seconds(existing, interval_seconds),
                    wake_on_pause=False,
                ):
                    print(f"watchdog runner {name} stopped by durable state")
                    return 0
                continue
            effective_interval = int(existing["interval_seconds"]) if existing is not None else interval_seconds
            effective_role = existing["scope_role"] if existing is not None else args.role
            effective_description = existing["description"] if existing is not None else args.description
            effective_goal = existing["goal"] if existing is not None else args.goal
            effective_notify_role = existing["notify_role"] if existing is not None else args.notify_role
            effective_delivery = existing["delivery_method"] if existing is not None else args.delivery
            now = datetime.now(UTC).replace(microsecond=0)
            next_run = None if args.once else (now + timedelta(seconds=effective_interval)).isoformat()
            findings = watchdog_findings(
                store,
                conn,
                role=effective_role,
                unacked_warn_seconds=args.unacked_warn_seconds,
                ack_warn_seconds=args.ack_warn_seconds,
                obligation_grace_seconds=args.obligation_grace_seconds,
            )
            summary = summarize_watchdog_findings(findings)
            row = store.record_watchdog_runner_run(
                conn,
                name=name,
                interval_seconds=effective_interval,
                scope_role=effective_role,
                description=effective_description,
                goal=effective_goal,
                notify_role=effective_notify_role,
                delivery_method=effective_delivery,
                pane=pane,
                window=window,
                process_id=process_id,
                last_run_at=now.isoformat(),
                next_run_at=next_run,
                finding_count=len(findings),
                finding_summary=summary,
                actor=name,
            )
            if row["state"] == "paused":
                # The runner may be paused between the preflight read above and
                # the run write; the store returns the paused row without
                # mutating it back to running.
                print_watchdog_runner_header(row, stale_grace_seconds=0)
                print("watchdog paused")
                sys.stdout.flush()
                continue
            print_watchdog_runner_header(row, stale_grace_seconds=0)
            if findings:
                for finding in findings:
                    print(format_watchdog_finding(finding))
                pressure = deliver_watchdog_pressure(
                    service,
                    conn,
                    name=name,
                    findings=findings,
                    scope_role=effective_role,
                    notify_role=effective_notify_role,
                    delivery_method=effective_delivery,
                    description=effective_description,
                    goal=effective_goal,
                )
                if pressure:
                    print(pressure)
            else:
                print("watchdog ok")
            sys.stdout.flush()

            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                store.stop_watchdog_runner(conn, name=name, actor=name)
                return 0
            if not sleep_watchdog_interval(store, conn, name=name, interval_seconds=effective_interval):
                print(f"watchdog runner {name} stopped by durable state")
                return 0
    except KeyboardInterrupt:
        stop_watchdog_runner_if_exists(store, conn, name=name, actor=name)
        print(f"watchdog runner {name} stopped")
        return 130
    except Exception as exc:
        stop_watchdog_runner_if_exists(store, conn, name=name, state="failed", error=str(exc), actor=name)
        raise


def deliver_watchdog_pressure(
    service: TeamService,
    conn,
    *,
    name: str,
    findings: list[dict[str, str]],
    scope_role: str | None,
    notify_role: str | None,
    delivery_method: str,
    description: str | None,
    goal: str | None,
) -> str:
    if not findings or not watchdog_delivery_enabled(delivery_method):
        return ""
    recipient = watchdog_notify_target(service.store, conn, scope_role=scope_role, notify_role=notify_role)
    correlation_key = watchdog_pressure_correlation_key(name, scope_role, recipient)
    duplicates = service.store.find_duplicate_messages(
        conn,
        recipient=recipient,
        summary="",
        correlation_key=correlation_key,
    )
    if duplicates:
        duplicate = duplicates[0]
        return (
            f"pressure_skipped: active message {duplicate['id']} "
            f"state={duplicate['state']} to={recipient} correlation_key={correlation_key}"
        )

    sender = f"watchdog:{name}"
    summary = watchdog_pressure_summary(findings)
    result = service.send_message(
        conn,
        sender=sender,
        recipient=recipient,
        priority=watchdog_pressure_priority(findings),
        summary=summary,
        body=watchdog_pressure_body(
            name=name,
            findings=findings,
            scope_role=scope_role,
            description=description,
            goal=goal,
        ),
        force=True,
        wake=True,
        notify_method=delivery_method,
        correlation_key=correlation_key,
        actor=sender,
    )
    line = (
        f"pressure: {result.message.id} state={result.message.state} "
        f"to={recipient} priority={result.message.priority} correlation_key={correlation_key}"
    )
    if result.notification is not None:
        if result.notification.ok:
            line = f"{line} notify={result.notification.details}"
        else:
            line = f"{line} notify_failed={result.notification.details}"
    return line


def watchdog_delivery_enabled(delivery_method: str) -> bool:
    return delivery_method.strip().lower() not in ("", "none", "report-only")


def watchdog_notify_target(store: Store, conn, *, scope_role: str | None, notify_role: str | None) -> str:
    if notify_role:
        if store.get_role(conn, notify_role) is None:
            raise KeyError(f"Unknown notify role: {notify_role}")
        return notify_role
    if scope_role:
        if store.get_role(conn, scope_role) is None:
            raise KeyError(f"Unknown role: {scope_role}")
        return scope_role
    if store.get_role(conn, "orchestrator") is not None:
        return "orchestrator"
    roles = store.list_roles(conn)
    if not roles:
        raise KeyError("No roles configured for watchdog pressure delivery")
    return str(roles[0]["name"])


def watchdog_pressure_correlation_key(name: str, scope_role: str | None, recipient: str) -> str:
    scope = scope_role or "team"
    return f"watchdog:{name}:{scope}:to:{recipient}"


def watchdog_pressure_priority(findings: list[dict[str, str]]) -> str:
    return "urgent" if any(finding["severity"] == "urgent" for finding in findings) else "high"


def watchdog_pressure_summary(findings: list[dict[str, str]]) -> str:
    first = findings[0]
    extra = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return f"Watchdog findings: {first['kind']} {first['role']} {first['ref']}{extra}"


def watchdog_pressure_body(
    *,
    name: str,
    findings: list[dict[str, str]],
    scope_role: str | None,
    description: str | None,
    goal: str | None,
) -> str:
    lines = [
        f"Watchdog: {name}",
        f"Scope: {scope_role or 'team'}",
    ]
    if description:
        lines.append(f"Description: {description}")
    if goal:
        lines.append(f"Goal: {goal}")
    lines.extend(
        [
            "",
            "Findings:",
        ]
    )
    for finding in findings:
        lines.append(
            f"- severity={finding['severity']} kind={finding['kind']} "
            f"role={finding['role']} ref={finding['ref']} summary={finding['summary']}"
        )
    lines.extend(
        [
            "",
            "Reconcile these findings from durable state. Update or complete the relevant obligation/message, "
            "or update/pause/stop this watchdog runner if the pressure is no longer useful.",
        ]
    )
    return "\n".join(lines)


def cmd_watchdog_start(args: argparse.Namespace, store: Store, conn) -> int:
    name = normalize_watchdog_runner_name(args.name)
    interval_seconds = parse_positive_duration_seconds(args.interval, "--interval")
    if args.role and store.get_role(conn, args.role) is None:
        raise KeyError(f"Unknown role: {args.role}")
    if args.notify_role and store.get_role(conn, args.notify_role) is None:
        raise KeyError(f"Unknown notify role: {args.notify_role}")
    existing = store.list_watchdog_runners(conn, name=name)
    if existing and watchdog_runner_display_state(existing[0], stale_grace_seconds=60) == "running":
        raise ValueError(f"watchdog runner {name} is already running; stop it first")
    session = args.session or detect_current_tmux_session(args.tmux_bin)
    if not session:
        raise ValueError("watchdog start requires --session unless run inside tmux")
    window_name = watchdog_window_name(args.window_name)
    project_root = store.config.project_root or Path.cwd()
    use_existing_window = False if args.dry_run else tmux_window_exists(args.tmux_bin, session, window_name)
    tmux_command = watchdog_spawn_command(
        tmux_bin=args.tmux_bin,
        session=session,
        config_path=store.config.config_path,
        project_root=project_root,
        name=name,
        interval=args.interval,
        delivery=args.delivery,
        role=args.role,
        description=args.description,
        goal=args.goal,
        notify_role=args.notify_role,
        window_name=window_name,
        unacked_warn_seconds=args.unacked_warn_seconds,
        ack_warn_seconds=args.ack_warn_seconds,
        obligation_grace_seconds=args.obligation_grace_seconds,
        use_existing_window=use_existing_window,
    )
    option_commands = [
        *watchdog_pane_setup_commands(args.tmux_bin, "{pane}", name=name),
        watchdog_layout_command(args.tmux_bin, session, window_name),
    ]
    if args.dry_run:
        print(format_command(tmux_command))
        for command_template in option_commands:
            print(format_command([part if part != "{pane}" else "<pane-id>" for part in command_template]))
        return 0

    result = subprocess.run(tmux_command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"{args.tmux_bin} exited {result.returncode}").strip()
        store.upsert_watchdog_runner(
            conn,
            name=name,
            state="failed",
            interval_seconds=interval_seconds,
            scope_role=args.role,
            description=args.description,
            goal=args.goal,
            notify_role=args.notify_role,
            delivery_method=args.delivery,
            window=f"{session}:{window_name}",
            actor=args.actor or "operator",
        )
        store.stop_watchdog_runner(conn, name=name, state="failed", error=details, actor=args.actor or "operator")
        raise ValueError(f"could not start watchdog runner {name}: {details}")
    pane = result.stdout.strip()
    if not pane:
        raise ValueError(f"could not start watchdog runner {name}: tmux did not return a pane id")
    for command_template in option_commands:
        subprocess.run(
            [part if part != "{pane}" else pane for part in command_template],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    row = store.upsert_watchdog_runner(
        conn,
        name=name,
        state="running",
        interval_seconds=interval_seconds,
        scope_role=args.role,
        description=args.description,
        goal=args.goal,
        notify_role=args.notify_role,
        delivery_method=args.delivery,
        pane=pane,
        window=f"{session}:{window_name}",
        next_run_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        actor=args.actor or "operator",
    )
    print(watchdog_runner_one_line(row, stale_grace_seconds=60))
    print(f"tmux: {session}:{window_name} pane={pane}")
    return 0


def cmd_watchdog_stop(args: argparse.Namespace, store: Store, conn) -> int:
    row = store.get_watchdog_runner(conn, args.name)
    pane = row["pane"]
    if pane and not args.no_kill_pane:
        result = subprocess.run(
            [args.tmux_bin, "kill-pane", "-t", pane],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or f"{args.tmux_bin} exited {result.returncode}").strip()
            raise ValueError(f"could not kill watchdog pane {pane}: {details}")
    updated = store.stop_watchdog_runner(conn, name=args.name, actor=args.actor or "operator")
    print(watchdog_runner_one_line(updated, stale_grace_seconds=60))
    return 0


def cmd_watchdog_pause(args: argparse.Namespace, store: Store, conn) -> int:
    row = store.pause_watchdog_runner(
        conn,
        name=args.name,
        reason=args.reason,
        review_at=review_at_from_args(args.review_in, args.review_at),
        actor=args.actor or "operator",
    )
    print(watchdog_runner_one_line(row, stale_grace_seconds=60))
    return 0


def cmd_watchdog_resume(args: argparse.Namespace, store: Store, conn) -> int:
    row = store.resume_watchdog_runner(conn, name=args.name, actor=args.actor or "operator")
    print(watchdog_runner_one_line(row, stale_grace_seconds=60))
    return 0


def cmd_watchdog_update(args: argparse.Namespace, store: Store, conn) -> int:
    if args.role and args.team:
        raise ValueError("watchdog update accepts either --role or --team, not both")
    if args.notify_role and args.no_notify_role:
        raise ValueError("watchdog update accepts either --notify-role or --no-notify-role, not both")
    interval_seconds = parse_positive_duration_seconds(args.interval, "--interval") if args.interval else None
    row = store.update_watchdog_runner(
        conn,
        name=args.name,
        interval_seconds=interval_seconds,
        scope_role=args.role,
        clear_scope_role=args.team,
        description=args.description,
        goal=args.goal,
        notify_role=args.notify_role,
        clear_notify_role=args.no_notify_role,
        delivery_method=args.delivery,
        actor=args.actor or "operator",
    )
    print(watchdog_runner_one_line(row, stale_grace_seconds=60))
    return 0


def cmd_watchdog_list(args: argparse.Namespace, store: Store, conn) -> int:
    name = args.name if args.watchdog_command == "status" else None
    limit = getattr(args, "limit", 50)
    rows = store.list_watchdog_runners(conn, name=name, limit=limit)
    if args.json:
        print(json_dumps([watchdog_runner_dict(row, args.stale_grace_seconds) for row in rows]))
        return 0
    if not rows:
        target = name or "watchdog runners"
        print(f"no {target}")
        return 0
    for row in rows:
        print(watchdog_runner_one_line(row, stale_grace_seconds=args.stale_grace_seconds))
    return 0


def stop_watchdog_runner_if_exists(
    store: Store,
    conn,
    *,
    name: str,
    state: str = "stopped",
    error: str | None = None,
    actor: str = "operator",
) -> None:
    try:
        store.stop_watchdog_runner(conn, name=name, state=state, error=error, actor=actor)
    except KeyError:
        return


def sleep_watchdog_interval(
    store: Store,
    conn,
    *,
    name: str,
    interval_seconds: int,
    wake_on_pause: bool = True,
) -> bool:
    deadline = time.monotonic() + interval_seconds
    started_interval = interval_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 1.0))
        try:
            row = store.get_watchdog_runner(conn, name)
        except KeyError:
            return False
        if row["state"] == "paused" and wake_on_pause:
            return True
        if row["state"] not in ("running", "paused"):
            return False
        if int(row["interval_seconds"]) != started_interval:
            return True


def paused_watchdog_sleep_seconds(row, default_interval_seconds: int) -> int:
    review_at = row["review_at"]
    if review_at:
        remaining = parse_utc_datetime(str(review_at)) - datetime.now(UTC)
        seconds = int(remaining.total_seconds())
        if seconds > 0:
            return min(default_interval_seconds, seconds)
    return default_interval_seconds


def parse_positive_duration_seconds(value: str, flag: str) -> int:
    raw = value.strip()
    if not raw:
        raise ValueError(f"{flag} must be a positive duration such as 15m, 1h, or 2d")
    if raw[0] not in ("-", "+"):
        raw = f"+{raw}"
    duration = parse_duration(raw)
    if duration is None or duration.total_seconds() <= 0:
        raise ValueError(f"{flag} must be a positive duration such as 15m, 1h, or 2d")
    return max(1, int(duration.total_seconds()))


def current_tmux_window() -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    try:
        return resolve_pane_window_target(os.environ.get("TMUX_TEAM_TMUX_BIN", "tmux"), pane)
    except ValueError:
        return None


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


def summarize_watchdog_findings(findings: list[dict[str, str]]) -> str:
    if not findings:
        return "ok"
    first = findings[0]
    suffix = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return f"{first['severity']} {first['kind']} role={first['role']} ref={first['ref']}{suffix}"


def print_watchdog_runner_header(row, *, stale_grace_seconds: int) -> None:
    print("tmux-team watchdog runner")
    print(f"name: {row['name']}")
    print(f"state: {watchdog_runner_display_state(row, stale_grace_seconds)}")
    print(f"interval: {format_seconds_duration(int(row['interval_seconds']))}")
    print(f"scope: {row['scope_role'] or 'team'}")
    print(f"description: {row['description'] or '-'}")
    print(f"goal: {row['goal'] or '-'}")
    print(f"notify role: {row['notify_role'] or '-'}")
    print(f"delivery: {row['delivery_method']}")
    print(f"last run: {row['last_run_at'] or '-'}")
    print(f"next run: {row['next_run_at'] or '-'}")
    print(f"last finding: {row['last_finding_summary'] or '-'}")
    if row["state"] == "paused":
        print(f"paused reason: {row['paused_reason'] or '-'}")
        print(f"paused at: {row['paused_at'] or '-'}")
        print(f"paused by: {row['paused_by'] or '-'}")
        print(f"review at: {row['review_at'] or '-'}")
    print(f"backing pane: {row['pane'] or '-'}")
    print(f"safe to close: {watchdog_runner_safe_to_close(row, stale_grace_seconds)}")


def watchdog_runner_one_line(row, *, stale_grace_seconds: int) -> str:
    state = watchdog_runner_display_state(row, stale_grace_seconds)
    parts = [
        f"{row['name']}",
        f"state={state}",
        f"interval={format_seconds_duration(int(row['interval_seconds']))}",
        f"scope={row['scope_role'] or 'team'}",
        f"notify_role={row['notify_role'] or '-'}",
        f"delivery={row['delivery_method']}",
        f"last_run={row['last_run_at'] or '-'}",
        f"next_run={row['next_run_at'] or '-'}",
        f"findings={row['last_finding_count']}",
        f"summary={row['last_finding_summary'] or '-'}",
        f"pane={row['pane'] or '-'}",
        f"window={row['window'] or '-'}",
        f"pid={row['process_id'] or '-'}",
        f"safe_to_close={watchdog_runner_safe_to_close(row, stale_grace_seconds)}",
    ]
    if row["description"]:
        parts.append(f"description={row['description']}")
    if row["goal"]:
        parts.append(f"goal={row['goal']}")
    if row["state"] == "paused":
        if row["review_at"]:
            parts.append(f"review_at={row['review_at']}")
        if row["paused_by"]:
            parts.append(f"paused_by={row['paused_by']}")
        if row["paused_reason"]:
            parts.append(f"reason={row['paused_reason']}")
    if row["last_error"]:
        parts.append(f"error={row['last_error']}")
    return " ".join(parts)


def watchdog_runner_dict(row, stale_grace_seconds: int) -> dict[str, object]:
    return {
        "name": row["name"],
        "state": row["state"],
        "display_state": watchdog_runner_display_state(row, stale_grace_seconds),
        "interval_seconds": int(row["interval_seconds"]),
        "scope_role": row["scope_role"],
        "description": row["description"],
        "goal": row["goal"],
        "notify_role": row["notify_role"],
        "delivery_method": row["delivery_method"],
        "pane": row["pane"],
        "window": row["window"],
        "process_id": row["process_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_run_at": row["last_run_at"],
        "next_run_at": row["next_run_at"],
        "last_finding_count": int(row["last_finding_count"]),
        "last_finding_summary": row["last_finding_summary"],
        "last_error": row["last_error"],
        "paused_reason": row["paused_reason"],
        "paused_at": row["paused_at"],
        "paused_by": row["paused_by"],
        "review_at": row["review_at"],
        "safe_to_close": watchdog_runner_safe_to_close(row, stale_grace_seconds),
    }


def watchdog_runner_safe_to_close(row, stale_grace_seconds: int) -> str:
    state = watchdog_runner_display_state(row, stale_grace_seconds)
    return "yes" if state in ("stopped", "failed") else "no"


def cmd_sleep(args: argparse.Namespace, store: Store, conn, config) -> int:
    result = sleep_team(
        config,
        store,
        conn,
        tmux_bin=args.tmux_bin,
        session=args.session,
        dry_run=args.dry_run,
        force=args.force,
        kill_session=args.kill_session,
        pause_roles=not args.no_pause_roles,
    )
    print(f"sleep_id: {result.sleep_id}")
    print(f"session: {result.session or '-'}")
    print(f"roles: {result.role_count}")
    print(f"watchdogs: {result.watchdog_count}")
    if result.snapshot_path:
        print(f"snapshot: {result.snapshot_path}")
        print(f"latest: {result.latest_path}")
    else:
        print("snapshot: (dry-run)")
    print("managed_windows:")
    if result.managed_windows:
        for window in result.managed_windows:
            roles = ",".join(window.get("roles") or []) or "-"
            watchdogs = ",".join(window.get("watchdogs") or []) or "-"
            print(
                f"  {window['kind']}: target={window['target']} window={window.get('window_name') or '-'} "
                f"roles={roles} watchdogs={watchdogs}"
            )
    else:
        print("  none")
    print("teardown:")
    if result.commands:
        for command in result.commands:
            print(f"  {format_command(command)}")
    else:
        print("  none")
    if args.dry_run:
        print("dry-run: no snapshot written and no tmux windows killed")
    else:
        print(f"paused_roles: {'no' if args.no_pause_roles else 'yes'}")
    return 0


def cmd_resume(args: argparse.Namespace, store: Store, conn, config) -> int:
    role_launch_options = parse_role_launch_options(
        role_profiles=parse_assignments(args.role_codex_profile, "--role-codex-profile"),
        role_models=parse_assignments(args.role_model, "--role-model"),
        role_reasoning_efforts=parse_assignments(args.role_reasoning_effort, "--role-reasoning-effort"),
        role_config_overrides=parse_role_config_overrides(args.role_codex_config),
    )
    result = resume_team(
        config,
        store,
        conn,
        snapshot_path=Path(args.snapshot).expanduser() if args.snapshot else None,
        tmux_bin=args.tmux_bin,
        codex_bin=args.codex_bin,
        session=args.session,
        endpoint=args.endpoint,
        agent_layout=args.agent_layout,
        agents_window=args.agents_window,
        role_yolo=args.role_yolo,
        role_profile=args.role_profile,
        role_launch_options=role_launch_options,
        start_app_server=not args.no_start_app_server,
        reactivate_roles=not args.no_reactivate_roles,
        enable_truecolor=not args.no_truecolor,
        dry_run=args.dry_run,
    )
    print(f"snapshot: {result.snapshot_path}")
    print(f"session: {result.session}")
    print(f"endpoint: {result.endpoint}")
    print(f"roles: {result.role_count}")
    print(f"watchdogs: {len(result.watchdog_panes)}")
    print(f"reactivated_roles: {'yes' if result.reactivated_roles else 'no'}")
    restored = ",".join(result.restored_launch_roles) if result.restored_launch_roles else "-"
    print(f"codex_launch_settings: restored={restored} fast=unknown")
    print("role_threads:")
    for role, thread_id in result.role_threads.items():
        pane = result.role_panes.get(role) or "-"
        print(f"  {role}: thread_id={thread_id} pane={pane}")
    if result.watchdog_panes:
        print("watchdog_panes:")
        for name, pane in result.watchdog_panes.items():
            print(f"  {name}: pane={pane or '-'}")
    print("commands:")
    if result.commands:
        for command in result.commands:
            print(f"  {format_command(command)}")
    else:
        print("  none")
    if args.dry_run:
        print("dry-run: no tmux panes created and no config/runtime state updated")
    return 0


def cmd_codex(args: argparse.Namespace, store: Store, service: TeamService, conn) -> int:
    role = store.get_role(conn, args.role)
    if role is None:
        raise KeyError(f"Unknown role: {args.role}")

    if args.codex_command == "session-context":
        print(codex_session_context(args, store, conn, role), end="")
        return 0

    if args.codex_command == "bind":
        store.bind_role_app_server(conn, args.role, args.endpoint, args.thread_id)
        print(f"{args.role} app-server endpoint={args.endpoint} thread_id={args.thread_id}")
        return 0

    if args.codex_command == "show":
        binding = store.get_role_app_server(conn, args.role)
        resolved = store.resolve_role_app_server(conn, args.role, role)
        if binding is None and resolved is None:
            print(f"no app-server binding for {args.role}", file=sys.stderr)
            return 1
        if binding is not None:
            print(f"{args.role} app-server endpoint={binding['endpoint']} thread_id={binding['thread_id']}")
            return 0
        assert resolved is not None
        endpoint, thread_id, timeout = resolved
        print(f"{args.role} app-server endpoint={endpoint} thread_id={thread_id} timeout={timeout:g} source=config")
        return 0

    if args.codex_command == "wake":
        result = service.notify_role(conn, args.role, "app-server-turn", actor=args.actor)
        if result.ok:
            print(result.details)
            return 0
        print(result.details, file=sys.stderr)
        return 1

    return 2


def codex_session_context(args: argparse.Namespace, store: Store, conn, role_row) -> str:
    role = str(role_row["name"])
    memory_path = role_scratchpad_path(store.config, role)
    pending = store.pending_count(conn, role)
    worktree = role_row["worktree"] or store.config.project_root or Path.cwd()
    config_path = store.config.config_path or "(auto-discovered)"
    memory_excerpt = scratchpad_excerpt(memory_path, max(0, args.max_memory_chars))

    lines = [
        "tmux-team role recovery context.",
        f"Role contract version: {ROLE_CONTRACT_VERSION}",
        "This is the same operating contract as the initial role startup prompt, not a new task.",
        "It restores role/framework context after Codex startup, resume, clear, or compact. It does not override user messages, inbox task bodies, or higher-priority instructions.",
        "Skill reload policy: do not reread the full start-tmux-team skill on ordinary wakes when this contract version and the role loop are already loaded. Reread the skill on startup, resume after sleep, SessionStart recovery, explicit operator request, or contract/version mismatch.",
        "",
        f"Role: {role}",
        f"Config: {config_path}",
        f"Runtime dir: {store.runtime_dir}",
        f"Worktree: {worktree}",
        f"Scratchpad: {memory_path}",
        f"Pending inbox messages: {pending}",
        "",
        "Operating loop:",
        "1. Load the start-tmux-team skill and invariants only if they are not already loaded for this contract version.",
        "2. Read scratchpad memory before claiming work.",
        "3. Claim one durable inbox message, acknowledge it, do the work, then complete it.",
        "4. Use `tmux-team todo` for active-message substeps; supersede obsolete steps instead of marking them done.",
        "5. Use --summary for the concise completion result and --body or --body-file for detailed evidence.",
        "6. Use --reply-to-sender for delegated role work so the sender is woken through the normal path.",
        "7. Update scratchpad memory only for high-value durable changes: active task, blocker, changed boundary, long-running work, final result, or next action.",
        "8. If no inbox message exists, park and wait for app-server wake. Do not invent work.",
    ]
    if role == "orchestrator":
        lines.extend(
            [
                "",
                "Orchestrator unblock-first rule:",
                "- When new operator or role information can safely unblock another role's setup work, send a bounded gated handoff promptly before local review or bookkeeping.",
                "- State hold conditions clearly, continue validation, then send approve/cancel/update follow-up.",
                "- Do not block downstream prep on redundant verification already supplied by the worker unless forwarding it would create irreversible external effects or violate an explicit safety gate.",
            ]
        )
    active_todos = active_todo_context_lines(store, conn, role)
    if active_todos:
        lines.extend(["", "Active todos:", *active_todos])
    if memory_excerpt:
        lines.extend(["", "Scratchpad excerpt:", memory_excerpt])
    else:
        lines.extend(["", "Scratchpad excerpt: (missing or omitted)"])
    return "\n".join(lines).rstrip() + "\n"


def active_todo_context_lines(
    store: Store, conn, role: str, *, message_limit: int = 5, todo_limit: int = 10
) -> list[str]:
    lines: list[str] = []
    for message in store.list_in_progress_messages(conn, role=role, limit=message_limit):
        todos = store.list_todos(conn, role=role, message_id=message["id"], limit=todo_limit)
        if not todos:
            continue
        lines.append(f"- {message['id']} {message['state']} summary={message['summary']}")
        for todo in todos:
            lines.append(f"  {todo_one_line(todo)}")
    return lines


def scratchpad_excerpt(path: Path, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def cmd_stable(args: argparse.Namespace, store: Store, conn) -> int:
    if args.stable_command == "approve":
        approved_by = args.by or os.environ.get("USER") or getpass.getuser()
        store.approve_stable_commit(
            conn,
            scope=args.role,
            commit_sha=args.commit,
            approved_by=approved_by,
            note=args.note,
        )
        print(f"{args.role}: {args.commit} approved_by={approved_by}")
        return 0

    if args.stable_command == "current":
        if args.role:
            row = store.current_stable_commit(conn, args.role)
            if row is None:
                print(f"no stable commit for {args.role}")
                return 1
            print_stable_row(row)
            return 0
        rows = store.list_stable_commits(conn)
        if not rows:
            print("no stable commits")
            return 0
        for row in rows:
            print_stable_row(row)
        return 0

    if args.stable_command == "sync":
        return cmd_stable_sync(args, store, conn)

    return 2


def cmd_stable_sync(args: argparse.Namespace, store: Store, conn) -> int:
    role = store.get_role(conn, args.role)
    if role is None:
        raise KeyError(f"Unknown role: {args.role}")
    row = store.current_stable_commit(conn, args.role)
    if row is None:
        print(f"no stable commit for {args.role}", file=sys.stderr)
        return 1
    worktree = role["worktree"]
    if not worktree:
        print(f"role {args.role} has no worktree", file=sys.stderr)
        return 1

    command = ["git", "-C", worktree, "checkout", "--detach", row["commit_sha"]]
    if not args.apply:
        print(" ".join(command))
        return 0

    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).expanduser().read_text(encoding="utf-8")
    if args.body == "-":
        return sys.stdin.read()
    if args.body is not None:
        return args.body
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def read_optional_body(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        return Path(args.body_file).expanduser().read_text(encoding="utf-8")
    if getattr(args, "body", None) == "-":
        return sys.stdin.read()
    if getattr(args, "body", None) is not None:
        return args.body
    return ""


def completion_summary(args: argparse.Namespace) -> str:
    body = read_optional_body(args).strip()
    summary = args.summary.strip()
    if not body:
        return summary
    if not summary:
        return body
    return f"{summary}\n\n{body}"


def read_memory_note(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        return Path(args.body_file).expanduser().read_text(encoding="utf-8")
    if getattr(args, "body", None) == "-":
        return sys.stdin.read()
    if getattr(args, "body", None) is not None:
        return args.body
    if args.note == "-":
        return sys.stdin.read()
    if args.note is not None:
        return args.note
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def record_memory_update(path: Path, note: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = path.read_text(encoding="utf-8") if path.exists() else f"# {path.stem} Scratchpad\n\n"
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    block = f"### {timestamp}\n{note.strip()}\n\n"
    marker = "## Latest Updates\n"
    if marker in content:
        content = content.replace(marker, marker + "\n" + block, 1)
    elif "## Latest\n" in content:
        content = content.replace("## Latest\n", f"{marker}\n{block}## Latest\n", 1)
    else:
        first_break = content.find("\n\n")
        if first_break == -1:
            content = f"{content.rstrip()}\n\n{marker}\n{block}"
        else:
            content = content[: first_break + 2] + f"\n{marker}\n{block}" + content[first_break + 2 :]
    path.write_text(content, encoding="utf-8")


def parse_metadata(values: Sequence[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for value in values:
        key, assigned = parse_assignment(value, "--meta")
        metadata[key] = assigned
    return metadata


def milestone_subject_roles(args: argparse.Namespace) -> tuple[str, ...]:
    roles = split_csv_values(args.subject_role or ())
    if roles:
        return roles
    if args.team:
        return ()
    if args.role:
        return (args.role,)
    return ()


def milestone_time_window(args: argparse.Namespace) -> tuple[datetime | None, datetime | None]:
    if args.today and args.since:
        raise ValueError("milestone list accepts either --today or --since, not both")
    since = local_midnight_utc() if args.today else parse_time_arg(args.since)
    until = parse_time_arg(args.until)
    return since, until


def parse_time_arg(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.lower() == "now":
        return datetime.now(UTC).replace(microsecond=0)
    duration = parse_duration(raw)
    if duration is not None:
        return (datetime.now(UTC) + duration).replace(microsecond=0)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(UTC)


def parse_duration(value: str) -> timedelta | None:
    sign = -1
    raw = value
    if raw[0] in ("-", "+"):
        sign = -1 if raw[0] == "-" else 1
        raw = raw[1:]
    if not raw:
        return None
    number = raw[:-1]
    unit = raw[-1:]
    if unit not in ("s", "m", "h", "d") or not number:
        return None
    try:
        amount = float(number)
    except ValueError:
        return None
    seconds_by_unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return timedelta(seconds=sign * amount * seconds_by_unit[unit])


def local_midnight_utc() -> datetime:
    now = datetime.now().astimezone()
    local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC)


def json_dumps(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def format_milestone(row: dict) -> str:
    kind = row.get("kind") or "milestone"
    ref_id = row.get("ref_id") or "-"
    tags = ",".join(row.get("tags") or []) or "-"
    recorded_by = row.get("recorded_by") or row.get("actor") or "-"
    line = (
        f"{row.get('created_at')} [{kind}] recorded_by={recorded_by} "
        f"subject={milestone_subject_label(row)} ref={ref_id} tags={tags} {row.get('summary')}"
    )
    body = str(row.get("body") or "").strip()
    if body:
        first_line = body.splitlines()[0]
        line += f"\n  {first_line}"
    return line


def milestone_subject_label(row: dict) -> str:
    scope = row.get("scope")
    if scope == "team":
        return "team"
    subject_roles = tuple(str(role) for role in row.get("subject_roles") or ())
    if subject_roles:
        return ",".join(subject_roles)
    return str(row.get("role") or "-")


def watchdog_findings(
    store: Store,
    conn,
    *,
    role: str | None,
    unacked_warn_seconds: int,
    ack_warn_seconds: int,
    obligation_grace_seconds: int,
) -> list[dict[str, str]]:
    roles = [role] if role else [row["name"] for row in store.list_roles(conn)]
    findings: list[dict[str, str]] = []
    now = datetime.now(UTC).replace(microsecond=0)
    unacked_cutoff = (now - timedelta(seconds=unacked_warn_seconds)).isoformat()
    ack_cutoff = (now - timedelta(seconds=ack_warn_seconds)).isoformat()
    obligation_cutoff = (now - timedelta(seconds=obligation_grace_seconds)).isoformat()

    for role_name in roles:
        if store.get_role(conn, role_name) is None:
            raise KeyError(f"Unknown role: {role_name}")
        for row in conn.execute(
            """
            SELECT * FROM messages
            WHERE recipient = ?
              AND priority = 'urgent'
              AND state IN ('queued', 'notified', 'retrying')
            ORDER BY created_at
            """,
            (role_name,),
        ):
            findings.append(watchdog_message_finding("urgent_pending", "urgent", row))

        for row in store.list_reclaimable_messages(conn, role=role_name, limit=50):
            findings.append(watchdog_message_finding("stale_claimed", "warning", row))

        for row in conn.execute(
            """
            SELECT * FROM messages
            WHERE recipient = ?
              AND state = 'claimed'
              AND acknowledged_at IS NULL
              AND updated_at <= ?
            ORDER BY updated_at
            """,
            (role_name, unacked_cutoff),
        ):
            findings.append(watchdog_message_finding("claimed_unacked", "warning", row))

        for row in conn.execute(
            """
            SELECT * FROM messages
            WHERE recipient = ?
              AND state = 'acknowledged'
              AND message_kind = 'task'
              AND updated_at <= ?
            ORDER BY updated_at
            """,
            (role_name, ack_cutoff),
        ):
            findings.append(watchdog_message_finding("old_acknowledged", "warning", row))

        for row in conn.execute(
            """
            SELECT * FROM obligations
            WHERE role = ?
              AND status IN ('active', 'blocked')
              AND next_update_at IS NOT NULL
              AND next_update_at <= ?
            ORDER BY next_update_at
            """,
            (role_name, obligation_cutoff),
        ):
            findings.append(
                {
                    "severity": "warning",
                    "kind": "obligation_overdue",
                    "role": row["role"],
                    "ref": row["id"],
                    "summary": row["current_summary"],
                }
            )

        for row in conn.execute(
            """
            SELECT * FROM obligations
            WHERE role = ?
              AND status = 'paused'
              AND review_at IS NOT NULL
              AND review_at <= ?
            ORDER BY review_at
            """,
            (role_name, now.isoformat()),
        ):
            findings.append(
                {
                    "severity": "warning",
                    "kind": "obligation_review_due",
                    "role": row["role"],
                    "ref": row["id"],
                    "summary": row["paused_reason"] or row["current_summary"],
                }
            )
    runner_clauses = ["state = 'paused'", "review_at IS NOT NULL", "review_at <= ?"]
    runner_params: list[str] = [now.isoformat()]
    if role:
        runner_clauses.append("scope_role = ?")
        runner_params.append(role)
    for row in conn.execute(
        f"""
        SELECT * FROM watchdog_runners
        WHERE {" AND ".join(runner_clauses)}
        ORDER BY review_at
        """,
        tuple(runner_params),
    ):
        findings.append(
            {
                "severity": "warning",
                "kind": "watchdog_runner_review_due",
                "role": row["scope_role"] or "team",
                "ref": row["name"],
                "summary": row["paused_reason"] or row["last_finding_summary"] or "paused watchdog runner review due",
            }
        )
    return findings


def watchdog_message_finding(kind: str, severity: str, row) -> dict[str, str]:
    return {
        "severity": severity,
        "kind": kind,
        "role": row["recipient"],
        "ref": row["id"],
        "summary": row["summary"],
    }


def format_watchdog_finding(finding: dict[str, str]) -> str:
    return (
        f"severity={finding['severity']} kind={finding['kind']} role={finding['role']} "
        f"ref={finding['ref']} summary={finding['summary']}"
    )


def print_message(row, include_body: bool = False) -> None:
    print(f"id: {row['id']}")
    print(f"from: {row['sender']}")
    print(f"to: {row['recipient']}")
    print(f"priority: {row['priority']}")
    print(f"state: {row['state']}")
    print(f"summary: {row['summary']}")
    print(f"body: {row['body_path']}")
    if include_body:
        print("")
        print(Path(row["body_path"]).read_text(encoding="utf-8"))


def message_one_line(row) -> str:
    state = row_value(row, "display_state", row["state"])
    return (
        f"{row['id']} state={state} from={row['sender']} to={row['recipient']} "
        f"priority={row['priority']} summary={row['summary']}"
    )


def message_metadata_line(row) -> str:
    return (
        f"kind={row_value(row, 'message_kind', 'task') or 'task'} "
        f"correlation_key={row_value(row, 'correlation_key', None) or '-'} "
        f"related_to={row_value(row, 'related_to', None) or '-'} "
        f"supersedes={row_value(row, 'supersedes', None) or '-'}"
    )


def todo_one_line(row) -> str:
    marker = {"open": "[ ]", "done": "[x]", "superseded": "[-]"}.get(row["state"], "[?]")
    parts = [
        marker,
        row["id"],
        f"state={row['state']}",
        f"role={row['role']}",
        f"message={row['message_id']}",
    ]
    if row["superseded_by"]:
        parts.append(f"superseded_by={row['superseded_by']}")
    parts.append(f"text={row['text']}")
    return " ".join(parts)


def print_duplicate_warnings(rows) -> None:
    for row in rows:
        print(
            f"duplicate_warning: active message {row['id']} "
            f"state={row['state']} to={row['recipient']} summary={row['summary']}",
            file=sys.stderr,
        )


def active_message_line(row, unacked_warn_seconds: int | None = None) -> str:
    state = row_value(row, "display_state", row["state"])
    parts = [
        row["id"],
        f"state={state}",
        f"priority={row['priority']}",
        f"from={row['sender']}",
        f"age={format_age(row['created_at'])}",
    ]
    if row["claim_expires_at"]:
        parts.append(f"claim_expires_at={row['claim_expires_at']}")
    if claimed_unacked_warning(row, unacked_warn_seconds):
        parts.append(f"warning=claimed_unacked claim_age={format_age(row['updated_at'])}")
    parts.append(f"summary={row['summary']}")
    return " ".join(parts)


def obligation_one_line(row) -> str:
    parts = [
        row["id"],
        f"role={row['role']}",
        f"state={row['status']}",
        f"age={format_age(row['created_at'])}",
        f"updated={row['updated_at']}",
    ]
    if row["goal"]:
        parts.append(f"goal={row['goal']}")
    if row["next_update_at"]:
        parts.append(f"next_update_at={row['next_update_at']}")
    if row["status"] == OBLIGATION_PAUSED_STATE:
        if row["review_at"]:
            parts.append(f"review_at={row['review_at']}")
        if row["paused_by"]:
            parts.append(f"paused_by={row['paused_by']}")
        if row["paused_at"]:
            parts.append(f"paused_at={row['paused_at']}")
        if row["paused_reason"]:
            parts.append(f"reason={row['paused_reason']}")
    parts.append(f"summary={row['current_summary']}")
    return " ".join(parts)


def format_age(created_at: str) -> str:
    age = datetime.now(UTC) - parse_utc_datetime(created_at)
    seconds = max(0, int(age.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def claimed_unacked_warning(row, threshold_seconds: int | None) -> bool:
    if threshold_seconds is None:
        return False
    if row_value(row, "display_state", row["state"]) != "claimed":
        return False
    if row["acknowledged_at"]:
        return False
    age = datetime.now(UTC) - parse_utc_datetime(row["updated_at"])
    return int(age.total_seconds()) >= threshold_seconds


def obligation_next_update_at(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw[0] not in ("-", "+"):
        raw = f"+{raw}"
    duration = parse_duration(raw)
    if duration is None or duration.total_seconds() <= 0:
        raise ValueError("--next-update-in must be a positive duration such as 15m, 1h, or 2d")
    return (datetime.now(UTC) + duration).replace(microsecond=0).isoformat()


def review_at_from_args(review_in: str | None, review_at: str | None) -> str | None:
    if review_in:
        raw = review_in.strip()
        if not raw:
            return None
        if raw[0] not in ("-", "+"):
            raw = f"+{raw}"
        duration = parse_duration(raw)
        if duration is None or duration.total_seconds() <= 0:
            raise ValueError("--review-in must be a positive duration such as 30m, 1h, or 2d")
        return (datetime.now(UTC) + duration).replace(microsecond=0).isoformat()
    if review_at:
        parsed = parse_time_arg(review_at)
        if parsed is None:
            raise ValueError("--review-at must be an ISO timestamp or relative duration")
        return parsed.replace(microsecond=0).isoformat()
    return None


def pane_window_target(tmux_bin: str, pane: str) -> str | None:
    if pane.startswith("%"):
        return resolve_pane_window_target(tmux_bin, pane)
    if "." not in pane:
        return pane or None
    return pane.rsplit(".", 1)[0] or None


def resolve_pane_window_target(tmux_bin: str, pane: str) -> str:
    result = subprocess.run(
        [tmux_bin, "display-message", "-p", "-t", pane, "#{session_name}:#{window_name}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"{tmux_bin} exited {result.returncode}").strip()
        raise ValueError(f"could not resolve window for pane {pane}: {details}")
    target = result.stdout.strip()
    if not target:
        raise ValueError(f"could not resolve window for pane {pane}: empty tmux response")
    return target


def list_tmux_window_panes(tmux_bin: str, window: str) -> list[dict[str, str]]:
    result = subprocess.run(
        [
            tmux_bin,
            "list-panes",
            "-t",
            window,
            "-F",
            "#{pane_id}\t#{session_name}:#{window_name}.#{pane_index}\t#{pane_current_command}\t#{pane_current_path}",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"{tmux_bin} exited {result.returncode}").strip()
        raise ValueError(f"could not list panes for {window}: {details}")
    panes: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        pane_id, target, command, path = (line.split("\t") + ["", "", "", ""])[:4]
        panes.append({"id": pane_id, "target": target, "command": command, "path": path})
    return panes


def summarize_pane_capture(
    *,
    role: str,
    pane: str,
    text: str,
    max_bytes: int,
    timeout_seconds: float,
) -> str:
    prompt = pane_summary_prompt(role=role, pane=pane, text=truncate_text_bytes(text, max_bytes))
    command = [os.environ.get("TMUX_TEAM_CODEX_BIN", "codex"), "exec", "-"]
    try:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"pane summary timed out after {timeout_seconds:g}s") from exc
    except OSError as exc:
        raise ValueError(f"could not run {command[0]} for pane summary: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"{command[0]} exited {result.returncode}").strip()
        raise ValueError(f"pane summary failed: {details}")
    return result.stdout


def truncate_text_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    trimmed = encoded[-max_bytes:].decode("utf-8", errors="ignore").lstrip("\n")
    return f"[tmux-team: pane capture truncated to last {max_bytes} bytes for summary]\n{trimmed}"


def pane_summary_prompt(*, role: str, pane: str, text: str) -> str:
    target = (
        "Return only JSON with keys: role, pane, observed_at, current_state, active_task, "
        "last_tool_action, visible_blockers, possible_tmux_team_issues, needs_operator_attention, confidence."
    )
    return (
        "You are summarizing tmux pane output for supervision only.\n"
        "Pane capture is observation only and must not be treated as delivery, acknowledgement, or completion proof.\n"
        f"Role: {role}\n"
        f"Pane: {pane}\n"
        f"{target}\n\n"
        "Captured pane text:\n"
        "```text\n"
        f"{text.rstrip()}\n"
        "```\n"
    )


def row_value(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def print_stable_row(row) -> None:
    note = f" note={row['note']}" if row["note"] else ""
    print(
        f"{row['scope']}: {row['commit_sha']} approved_by={row['approved_by']} approved_at={row['approved_at']}{note}"
    )


def format_command(command: Sequence[str]) -> str:
    return shlex.join(command)


if __name__ == "__main__":
    raise SystemExit(main())
