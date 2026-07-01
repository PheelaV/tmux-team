from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .bootstrap import (
    AGENT_LAYOUTS,
    DEFAULT_AGENT_LAYOUT,
    DEFAULT_AGENTS_WINDOW,
    DEFAULT_CONTROL_WINDOW,
    BootstrapError,
    bootstrap_team,
    default_session_name,
    detect_current_tmux_session,
    free_local_endpoint,
    parse_roles,
)
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config, write_default_config
from .lifecycle import LifecycleError, sleep_team
from .store import CLAIMABLE_STATES, ROLE_STATES, Store, normalize_priority


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return cmd_init(args)
    if args.command == "bootstrap":
        return cmd_bootstrap(args)

    try:
        config = load_config(args.config, args.runtime_dir)
    except (BootstrapError, ConfigError) as exc:
        print(f"tmux-team: {exc}", file=sys.stderr)
        return 2

    store = Store(config)
    try:
        with store.connect() as conn:
            if args.command == "config":
                return cmd_config(args, config)
            if args.command == "status":
                return cmd_status(args, store, conn)
            if args.command == "send":
                return cmd_send(args, store, conn)
            if args.command == "inbox":
                return cmd_inbox(args, store, conn)
            if args.command == "role":
                return cmd_role(args, store, conn)
            if args.command == "notify":
                return cmd_notify(args, store, conn)
            if args.command == "sleep":
                return cmd_sleep(args, store, conn, config)
            if args.command == "codex":
                return cmd_codex(args, store, conn)
            if args.command == "stable":
                return cmd_stable(args, store, conn)
    except (ConfigError, LifecycleError, ValueError, KeyError, PermissionError) as exc:
        print(f"tmux-team: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmux-team")
    parser.add_argument("--config", help="Path to .tmux-team/team.toml")
    parser.add_argument("--runtime-dir", help="Override runtime directory")

    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="Create a project team config")
    init.add_argument("--config", help="Config path to create")
    init.add_argument("--name", default="default", help="Team name")
    init.add_argument("--runtime-dir", help="Runtime directory to write into the config")

    config = subparsers.add_parser("config", help="Inspect loaded config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="Show resolved config")

    subparsers.add_parser("status", help="Show roles and queue counts")

    bootstrap = subparsers.add_parser("bootstrap", help="Start a pane-resident Codex team in tmux")
    bootstrap.add_argument("--project-root", default=".", help="Project root for the team")
    bootstrap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Config path to create")
    bootstrap.add_argument("--runtime-dir", default=".tmux-team/runtime", help="Runtime directory for team state")
    bootstrap.add_argument("--session", default=None, help="tmux session name; defaults to project directory name")
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
    bootstrap.add_argument("--goal", default=None, help="Initial goal body to queue to orchestrator after startup")
    bootstrap.add_argument("--goal-file", default=None, help="Read initial goal body from a file")
    bootstrap.add_argument("--force-config", action="store_true", help="Replace an existing .tmux-team/team.toml")
    bootstrap.add_argument(
        "--no-start-app-server", action="store_true", help="Use an already-running app-server endpoint"
    )
    bootstrap.add_argument(
        "--dry-run", action="store_true", help="Print planned tmux commands and generated config without executing"
    )

    send = subparsers.add_parser("send", help="Queue a message")
    send.add_argument("--to", required=True, dest="recipient")
    send.add_argument("--from", default="operator", dest="sender")
    send.add_argument("--priority", default="normal", choices=("urgent", "high", "normal", "low"))
    send.add_argument("--summary", required=True)
    body = send.add_mutually_exclusive_group()
    body.add_argument("--body", help="Inline body text, or '-' to read stdin")
    body.add_argument("--body-file", help="Path to markdown body")
    send.add_argument("--force", action="store_true", help="Queue even if the role is paused or draining")
    send.add_argument("--no-notify", action="store_true", help="Do not notify the target pane")
    send.add_argument(
        "--notify-method",
        default="auto",
        help="Notification method: auto, display-message, send-keys, or app-server-turn",
    )

    inbox = subparsers.add_parser("inbox", help="Work with role inboxes")
    inbox_sub = inbox.add_subparsers(dest="inbox_command", required=True)
    inbox_next = inbox_sub.add_parser("next", help="Claim and print the next message")
    inbox_next.add_argument("--role", required=True)
    inbox_next.add_argument("--claim-seconds", type=int, default=3600)
    inbox_list = inbox_sub.add_parser("list", help="List inbox messages")
    inbox_list.add_argument("--role", required=True)
    inbox_list.add_argument("--state", action="append", help="Filter state; repeatable")
    inbox_list.add_argument("--limit", type=int, default=50)
    inbox_ack = inbox_sub.add_parser("ack", help="Acknowledge a message")
    inbox_ack.add_argument("message_id")
    inbox_ack.add_argument("--role", required=True)
    inbox_complete = inbox_sub.add_parser("complete", help="Complete a message")
    inbox_complete.add_argument("message_id")
    inbox_complete.add_argument("--role", required=True)
    inbox_complete.add_argument("--status", default="done")
    inbox_complete.add_argument("--summary", default="")

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

    notify = subparsers.add_parser("notify", help="Notify a role about pending work")
    notify.add_argument("role")
    notify.add_argument("--method", default="auto")

    sleep = subparsers.add_parser("sleep", help="Snapshot current bindings and tear down managed tmux team windows")
    sleep.add_argument(
        "--session", default=None, help="tmux session to tear down; inferred from role panes when omitted"
    )
    sleep.add_argument("--tmux-bin", default="tmux")
    sleep.add_argument(
        "--dry-run", action="store_true", help="Print snapshot/teardown plan without writing or killing panes"
    )
    sleep.add_argument("--force", action="store_true", help="Allow managing an unexpected control-plane role window")
    sleep.add_argument(
        "--kill-session", action="store_true", help="Kill the whole tmux session instead of managed windows only"
    )
    sleep.add_argument(
        "--no-pause-roles",
        action="store_true",
        help="Do not mark active/draining roles paused after snapshotting",
    )

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
        goal = args.goal
        if args.goal_file:
            try:
                goal = Path(args.goal_file).expanduser().read_text(encoding="utf-8")
            except OSError as exc:
                raise BootstrapError(f"could not read goal file {args.goal_file}: {exc}") from exc
        result = bootstrap_team(
            project_root=project_root,
            config_path=Path(args.config),
            runtime_dir=args.runtime_dir,
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
            agents_window=args.agents_window,
            role_yolo=args.role_yolo,
            role_profile=args.role_profile,
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


def cmd_config(args: argparse.Namespace, config) -> int:
    if args.config_command != "show":
        return 2
    print(f"team: {config.name}")
    print(f"config: {config.config_path or '(none)'}")
    print(f"project_root: {config.project_root}")
    print(f"runtime_dir: {config.runtime_dir}")
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
    print("roles:")
    for role in store.list_roles(conn):
        role_counts = counts.get(role["name"], {})
        pending = sum(role_counts.get(state, 0) for state in CLAIMABLE_STATES)
        claimed = role_counts.get("claimed", 0)
        acknowledged = role_counts.get("acknowledged", 0)
        completed = role_counts.get("completed", 0)
        pane = role["pane"] or "-"
        print(
            f"  {role['name']}: state={role['state']} mode={role['mode']} pane={pane} "
            f"pending={pending} claimed={claimed} ack={acknowledged} done={completed}"
        )
    return 0


def cmd_send(args: argparse.Namespace, store: Store, conn) -> int:
    normalize_priority(args.priority)
    role = store.get_role(conn, args.recipient)
    if role is None:
        raise KeyError(f"Unknown recipient role: {args.recipient}")

    body = read_body(args)
    state = "queued"
    exit_code = 0
    if role["state"] != "active" and not args.force and args.priority != "urgent":
        state = f"blocked_by_role_{role['state']}"
        exit_code = 2

    message = store.create_message(
        conn,
        sender=args.sender,
        recipient=args.recipient,
        priority=args.priority,
        summary=args.summary,
        body=body,
        state=state,
    )
    print(f"{message.id} {message.state} to={message.recipient} priority={message.priority}")
    print(f"body: {message.body_path}")

    if state == "queued" and not args.no_notify:
        ok, details = store.notify_role(conn, args.recipient, args.notify_method)
        if ok:
            print(f"notify: {details}")
        else:
            print(f"notify_failed: {details}", file=sys.stderr)

    if state != "queued":
        print(f"blocked: role {args.recipient} is {role['state']}", file=sys.stderr)
    return exit_code


def cmd_inbox(args: argparse.Namespace, store: Store, conn) -> int:
    if args.inbox_command == "next":
        row = store.claim_next(conn, args.role, args.claim_seconds)
        if row is None:
            print(f"no pending messages for {args.role}")
            return 1
        print_message(row, include_body=True)
        return 0

    if args.inbox_command == "list":
        states = tuple(args.state) if args.state else None
        rows = store.list_messages(conn, role=args.role, states=states, limit=args.limit)
        if not rows:
            print(f"no messages for {args.role}")
            return 0
        for row in rows:
            print(message_one_line(row))
        return 0

    if args.inbox_command == "ack":
        row = store.ack_message(conn, args.role, args.message_id)
        print(message_one_line(row))
        return 0

    if args.inbox_command == "complete":
        row = store.complete_message(conn, args.role, args.message_id, args.status, args.summary)
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
    store.set_role_state(conn, args.role, state)
    print(f"{args.role} state={state}")
    return 0


def cmd_notify(args: argparse.Namespace, store: Store, conn) -> int:
    ok, details = store.notify_role(conn, args.role, args.method)
    if ok:
        print(details)
        return 0
    print(details, file=sys.stderr)
    return 1


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
    if result.snapshot_path:
        print(f"snapshot: {result.snapshot_path}")
        print(f"latest: {result.latest_path}")
    else:
        print("snapshot: (dry-run)")
    print("managed_windows:")
    if result.managed_windows:
        for window in result.managed_windows:
            roles = ",".join(window.get("roles") or []) or "-"
            print(
                f"  {window['kind']}: target={window['target']} window={window.get('window_name') or '-'} roles={roles}"
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


def cmd_codex(args: argparse.Namespace, store: Store, conn) -> int:
    role = store.get_role(conn, args.role)
    if role is None:
        raise KeyError(f"Unknown role: {args.role}")

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
        ok, details = store.notify_role(conn, args.role, "app-server-turn")
        if ok:
            print(details)
            return 0
        print(details, file=sys.stderr)
        return 1

    return 2


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
    return (
        f"{row['id']} state={row['state']} from={row['sender']} to={row['recipient']} "
        f"priority={row['priority']} summary={row['summary']}"
    )


def print_stable_row(row) -> None:
    note = f" note={row['note']}" if row["note"] else ""
    print(
        f"{row['scope']}: {row['commit_sha']} approved_by={row['approved_by']} approved_at={row['approved_at']}{note}"
    )


def format_command(command: Sequence[str]) -> str:
    return " ".join(command)


if __name__ == "__main__":
    raise SystemExit(main())
