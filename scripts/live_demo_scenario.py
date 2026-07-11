#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import tomllib
from pathlib import Path
from typing import Any

from tmux_team.acp_tui import send_control_request

DEFAULT_ROOT = Path("/tmp/tmux-team-live-demo")
DEFAULT_SESSION = "tt-live-demo"
DEFAULT_REPO_URL = "https://github.com/PheelaV/tmux-team.git"
DEFAULT_REPO_REF = "v0.1.3"
DEFAULT_REPO_COMMIT = "78602d1497a81f0e8e5026999585a65c1eea19b1"
MARKER = ".tmux-team-live-demo-scenario"
ROLES = ("orchestrator", "implementer", "collector")
TARGET_TEST = "tests.test_store.StoreInboxTests.test_claim_next_prefers_urgent_over_older_normal_message"
CORRELATION_KEYS = {
    "baseline": "urgent-first-baseline",
    "fix": "urgent-first-fix",
    "verification": "urgent-first-collector-verification",
    "post_resume": "post-resume-implementer-test",
}
STABLE_SCOPE = "collector"
WATCHDOG_PRESSURE_NAME = "live-pressure"
WATCHDOG_PRESSURE_CORRELATION = f"watchdog:{WATCHDOG_PRESSURE_NAME}:team:to:orchestrator"
WATCHDOG_RESUME_NAME = "live-resume"
WATCHDOG_RESUME_CORRELATION = f"watchdog:{WATCHDOG_RESUME_NAME}:team:to:orchestrator"


class ScenarioError(RuntimeError):
    pass


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    try:
        if args.command == "setup":
            setup(root, args)
        elif args.command == "bootstrap":
            bootstrap(root, args)
        elif args.command == "start-goal":
            start_goal(root)
        elif args.command == "verify":
            verify(root)
        elif args.command == "sleep":
            sleep_team(root)
        elif args.command == "resume":
            resume_team(root, args)
        elif args.command == "watchdog-now":
            nudge_resumed_watchdog(root)
        elif args.command == "clean":
            clean(root, args)
        else:
            raise ScenarioError(f"unknown command: {args.command}")
    except ScenarioError as exc:
        print(f"live demo scenario failed: {exc}", file=sys.stderr)
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and operate the repeatable tmux-team live demo scenario.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Scenario root directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="Reset and seed the target public-repo scenario")
    setup_parser.add_argument("--force", action="store_true", help="Replace an existing marked scenario root")
    setup_parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    setup_parser.add_argument("--repo-ref", default=DEFAULT_REPO_REF)
    setup_parser.add_argument("--expected-commit", default=DEFAULT_REPO_COMMIT)
    setup_parser.add_argument("--skip-failing-check", action="store_true")

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Start tmux-team against the prepared scenario")
    bootstrap_parser.add_argument("--session", default=DEFAULT_SESSION)
    bootstrap_parser.add_argument("--agent-runtime", choices=("codex", "acp"), default="codex")
    bootstrap_parser.add_argument("--acp-tui-bin", default="toad")
    bootstrap_parser.add_argument("--acp-agent-command", default="agent acp")
    bootstrap_parser.add_argument("--acp-provider", default="cursor")
    bootstrap_parser.add_argument("--acp-model", default="")
    bootstrap_parser.add_argument("--acp-effort", default="")
    bootstrap_parser.add_argument("--acp-fast", choices=("true", "false"), default="")
    bootstrap_parser.add_argument(
        "--acp-startup-timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for provider startup prompts to become idle",
    )
    bootstrap_parser.add_argument(
        "--instruction-profile",
        choices=("compact", "guided"),
        default="guided",
        help="Role startup instruction verbosity",
    )
    bootstrap_parser.add_argument("--defer-goal", action="store_true", help="Start roles without queuing the goal")
    bootstrap_parser.add_argument(
        "--role-yolo", action="store_true", help="Launch managed role panes in Codex YOLO mode"
    )
    bootstrap_parser.add_argument("--force-config", action="store_true", help="Replace an existing team.toml")

    subparsers.add_parser("start-goal", help="Submit the prepared goal after an attach-before-run bootstrap")

    subparsers.add_parser("verify", help="Verify the live run reached real target success")
    subparsers.add_parser("sleep", help="Operator phase: sleep the running live-demo team")

    resume_parser = subparsers.add_parser("resume", help="Operator phase: resume the live-demo team")
    resume_parser.add_argument("--role-yolo", action="store_true", help="Resume managed role panes in Codex YOLO mode")

    subparsers.add_parser(
        "watchdog-now",
        help="Operator phase: shorten the restored post-resume watchdog interval so it fires soon",
    )

    clean_parser = subparsers.add_parser("clean", help="Remove the scenario tmux session and marked root")
    clean_parser.add_argument("--session", default=DEFAULT_SESSION)
    clean_parser.add_argument("--keep-root", action="store_true")

    return parser.parse_args()


def setup(root: Path, args: argparse.Namespace) -> None:
    reset_root(root, args.force)
    project = root / "project"
    implementer = root / "project-implementer"
    collector = root / "project-collector"

    run(["git", "clone", "--quiet", args.repo_url, str(project)])
    run(["git", "-C", str(project), "checkout", "--quiet", "--detach", args.repo_ref])
    base_commit = git_output(project, "rev-parse", "HEAD")
    if args.expected_commit and base_commit != args.expected_commit:
        raise ScenarioError(f"expected {args.expected_commit} for {args.repo_ref}, got {base_commit}")

    run(["git", "-C", str(project), "config", "user.name", "tmux-team demo"])
    run(["git", "-C", str(project), "config", "user.email", "tmux-team-demo@example.invalid"])
    run(["git", "-C", str(project), "switch", "--quiet", "-c", "tt-demo-orchestrator"])
    seed_regression(project)
    run(["git", "-C", str(project), "add", "src/tmux_team/store.py", "tests/test_store.py"])
    run(["git", "-C", str(project), "commit", "--quiet", "-m", "Seed live demo urgent priority regression"])
    seed_commit = git_output(project, "rev-parse", "HEAD")

    run(
        [
            "git",
            "-C",
            str(project),
            "worktree",
            "add",
            "--quiet",
            "-b",
            "tt-demo-implementer",
            str(implementer),
            seed_commit,
        ]
    )
    run(
        [
            "git",
            "-C",
            str(project),
            "worktree",
            "add",
            "--quiet",
            "-b",
            "tt-demo-collector",
            str(collector),
            seed_commit,
        ]
    )

    metadata = {
        "repo_url": args.repo_url,
        "repo_ref": args.repo_ref,
        "base_commit": base_commit,
        "seed_commit": seed_commit,
        "target_test": TARGET_TEST,
        "project": str(project),
        "implementer_worktree": str(implementer),
        "collector_worktree": str(collector),
    }
    (root / "scenario.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_goal(root, metadata)

    if not args.skip_failing_check:
        result = run_target_test(collector, check=False)
        if result.returncode == 0:
            raise ScenarioError("seeded target test unexpectedly passed")
        if "FAILED" not in result.stdout + result.stderr and "AssertionError" not in result.stdout + result.stderr:
            raise ScenarioError(f"seeded test failed unexpectedly:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    print("LIVE DEMO SCENARIO SETUP OK")
    print(f"root: {root}")
    print(f"project: {project}")
    print(f"implementer: {implementer}")
    print(f"collector: {collector}")
    print(f"base: {base_commit}")
    print(f"seed: {seed_commit}")
    print(f"goal: {root / 'goal.md'}")


def bootstrap(root: Path, args: argparse.Namespace) -> None:
    metadata = load_metadata(root)
    if args.agent_runtime == "acp":
        prepare_acp_demo_provider(metadata, args.acp_provider)
        write_acp_goal(root, metadata, args.acp_provider)
    else:
        write_goal(root, metadata)
    command = [
        "tmux-team",
        "bootstrap",
        "--project-root",
        metadata["project"],
        "--session",
        args.session,
        "--roles",
        ",".join(ROLES),
        "--agent-layout",
        "grouped",
        "--role-worktree",
        f"orchestrator={metadata['project']}",
        "--role-worktree",
        f"implementer={metadata['implementer_worktree']}",
        "--role-worktree",
        f"collector={metadata['collector_worktree']}",
    ]
    if not args.defer_goal:
        command.extend(["--goal-file", str(root / "goal.md")])
    if args.agent_runtime == "acp":
        initial_config = acp_demo_initial_config(
            args.acp_provider,
            args.acp_model,
            args.acp_effort,
            args.acp_fast,
        )
        command.extend(
            [
                "--agent-runtime",
                "acp",
                "--acp-tui-bin",
                args.acp_tui_bin,
                "--acp-agent-command",
                args.acp_agent_command,
                "--acp-provider",
                args.acp_provider,
                "--instruction-profile",
                args.instruction_profile,
            ]
        )
        for assignment in initial_config:
            command.extend(["--acp-initial-config", assignment])
    else:
        command.extend(
            [
                "--role-reasoning-effort",
                "orchestrator=high",
                "--role-reasoning-effort",
                "implementer=high",
                "--role-reasoning-effort",
                "collector=high",
            ]
        )
    if args.role_yolo and args.agent_runtime == "codex":
        command.append("--role-yolo")
    if args.force_config:
        command.append("--force-config")
    run(command)
    config_path = Path(metadata["project"]) / ".tmux-team" / "team.toml"
    if args.agent_runtime == "acp":
        verify_acp_demo_ready(args.session, config_path)
        if args.acp_model:
            verify_acp_demo_model(config_path, args.acp_model, timeout=args.acp_startup_timeout)
        verify_acp_demo_categories(config_path, args.acp_effort, args.acp_fast)
    start_dashboard_pane(args.session, config_path)
    print("LIVE DEMO BOOTSTRAP STARTED")
    print(f"session: {args.session}")
    print(f"runtime: {args.agent_runtime}")
    print("dashboard: tt-control split")
    print(f"attach: tmux attach -t {args.session}")
    print(f"project: {metadata['project']}")
    if args.defer_goal:
        print(f"deferred goal: {root / 'goal.md'}")
        print(f"start goal: {Path(__file__).name} --root {root} start-goal")
    print(f"verify later: {Path(__file__).name} --root {root} verify")


def start_goal(root: Path) -> None:
    metadata = load_metadata(root)
    goal_path = root / "goal.md"
    config_path = Path(metadata["project"]) / ".tmux-team" / "team.toml"
    if not goal_path.is_file():
        raise ScenarioError(f"prepared goal is missing: {goal_path}")
    if not config_path.is_file():
        raise ScenarioError(f"live demo team is not bootstrapped: {config_path}")
    result = run(
        [
            "tmux-team",
            "--config",
            str(config_path),
            "send",
            "--from",
            "operator",
            "--to",
            "orchestrator",
            "--priority",
            "high",
            "--summary",
            "Execute the prepared live demo scenario",
            "--body-file",
            str(goal_path),
            "--correlation-key",
            "live-demo-goal",
        ]
    )
    print("LIVE DEMO GOAL STARTED")
    print(result.stdout.strip())


def verify_acp_demo_ready(session: str, config_path: Path) -> None:
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    failure_markers = ("Traceback", "BadIdentifier", "ACP TUI role exited", "Not in allowlist")
    verify_acp_control_ready(session, config, failure_markers)
    for role in ROLES:
        role_config = config["roles"][role]
        pane = str(role_config["pane"])
        status = run(
            ["tmux-team", "--config", str(config_path), "acp", "status", role],
            check=False,
        )
        if status.returncode != 0:
            raise ScenarioError(f"ACP role {role} control socket is unhealthy:\n{status.stderr or status.stdout}")
        capture = run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-120"], check=False)
        if capture.returncode != 0:
            raise ScenarioError(f"could not capture ACP role {role} pane {pane}: {capture.stderr}")
        marker = next((value for value in failure_markers if value in capture.stdout), None)
        if marker is not None:
            raise ScenarioError(f"ACP role {role} failed readiness check ({marker}) in session {session}")


def verify_acp_control_ready(session: str, config: dict[str, Any], failure_markers: tuple[str, ...]) -> None:
    operator = config.get("operator", {})
    pane = str(operator.get("pane") or "")
    socket_value = operator.get("control_socket")
    session_id = operator.get("runtime_session_id")
    if operator.get("agent_runtime") != "acp" or not pane or not socket_value or not session_id:
        raise ScenarioError("ACP operator metadata is incomplete")
    pane_state = run(["tmux", "display-message", "-p", "-t", pane, "#{pane_dead}"], check=False)
    if pane_state.returncode != 0 or pane_state.stdout.strip() != "0":
        raise ScenarioError(f"ACP control pane {pane} is not alive in session {session}")
    status = send_control_request(Path(str(socket_value)), {"action": "status", "sessionId": str(session_id)})
    if status.get("sessionId") != session_id:
        raise ScenarioError("ACP control socket reported a different session")
    capture = run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-120"], check=False)
    marker = next((value for value in failure_markers if value in capture.stdout), None)
    if capture.returncode != 0 or marker is not None:
        raise ScenarioError(f"ACP control agent failed readiness check ({marker or 'capture failed'})")


def verify_acp_demo_model(config_path: Path, model: str, *, timeout: float = 180.0) -> None:
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for role in ROLES:
        role_config = config.get("roles", {}).get(role, {})
        wait_for_acp_session_idle(
            Path(str(role_config.get("control_socket") or "")),
            str(role_config.get("runtime_session_id") or ""),
            f"role {role}",
            timeout=timeout,
        )
        confirmed_model = acp_session_model(
            Path(str(role_config.get("control_socket") or "")),
            str(role_config.get("runtime_session_id") or ""),
        )
        if confirmed_model != model:
            raise ScenarioError(f"ACP role {role} did not start with model {model!r}: {confirmed_model!r}")

    operator = config.get("operator", {})
    socket_value = operator.get("control_socket")
    session_id = operator.get("runtime_session_id")
    if not socket_value or not session_id:
        raise ScenarioError("ACP operator is missing control-socket session metadata")
    wait_for_acp_session_idle(Path(str(socket_value)), str(session_id), "operator", timeout=timeout)
    confirmed_model = acp_session_model(Path(str(socket_value)), str(session_id))
    if confirmed_model != model:
        raise ScenarioError(f"ACP operator did not start with model {model!r}: {confirmed_model!r}")
    print(f"ACP demo model: {model}")


def acp_demo_initial_config(provider: str, model: str, effort: str, fast: str) -> tuple[str, ...]:
    if not model:
        raise ScenarioError("ACP live demo requires an explicit model")
    assignments = [] if provider == "pool" else [f"model={model}"]
    mode_values = {"codex": "agent-full-access", "claude": "bypassPermissions", "pool": "always-allow"}
    if provider in mode_values:
        assignments.append(f"mode={mode_values[provider]}")
    if effort:
        effort_ids = {"codex": "reasoning_effort", "claude": "effort"}
        config_id = effort_ids.get(provider)
        if config_id is None:
            raise ScenarioError(f"ACP provider {provider!r} has no demo effort-option mapping")
        assignments.append(f"{config_id}={effort}")
    if fast:
        fast_ids = {"codex": "fast-mode"}
        config_id = fast_ids.get(provider)
        if config_id is None:
            raise ScenarioError(f"ACP provider {provider!r} has no demo fast-option mapping")
        assignments.append(f"{config_id}={fast}")
    return tuple(assignments)


def verify_acp_demo_categories(config_path: Path, effort: str, fast: str) -> None:
    if not effort and not fast:
        return
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    sessions = [config.get("operator", {}), *(config.get("roles", {}).get(role, {}) for role in ROLES)]
    for index, session in enumerate(sessions):
        socket_value = session.get("control_socket")
        session_id = session.get("runtime_session_id")
        if not socket_value or not session_id:
            raise ScenarioError("ACP demo session is missing config metadata")
        response = send_control_request(
            Path(str(socket_value)), {"action": "configOptions", "sessionId": str(session_id)}
        )
        options = response.get("configOptions", [])
        label = "operator" if index == 0 else ROLES[index - 1]
        if effort and acp_category_value(options, "thought_level") != effort:
            raise ScenarioError(f"ACP {label} did not confirm effort {effort!r}")
        if fast:
            expected_fast = fast == "true"
            if acp_category_value(options, "model_config") != expected_fast:
                raise ScenarioError(f"ACP {label} did not confirm fast={fast}")


def acp_category_value(options: object, category: str) -> object:
    if not isinstance(options, list):
        return None
    option = next((item for item in options if isinstance(item, dict) and item.get("category") == category), None)
    return option.get("currentValue") if option is not None else None


def prepare_acp_demo_provider(metadata: dict[str, str], provider: str) -> None:
    if provider != "claude":
        return
    settings = json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}, indent=2) + "\n"
    worktrees = (metadata["project"], metadata["implementer_worktree"], metadata["collector_worktree"])
    for worktree_value in worktrees:
        worktree = Path(worktree_value)
        path = worktree / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(settings, encoding="utf-8")

    exclude_path = Path(git_output(Path(metadata["project"]), "rev-parse", "--git-path", "info/exclude"))
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    entry = ".claude/settings.local.json"
    if entry not in existing.splitlines():
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude_path.write_text(existing + separator + entry + "\n", encoding="utf-8")


def acp_session_model(socket_path: Path, session_id: str) -> str:
    response = send_control_request(socket_path, {"action": "configOptions", "sessionId": session_id})
    if response.get("sessionId") != session_id:
        raise ScenarioError("ACP config-options response reported a different session")
    for option in response.get("configOptions", []):
        if isinstance(option, dict) and option.get("id") == "model":
            return str(option.get("currentValue") or "")
    raise ScenarioError("ACP session does not advertise a model config option")


def wait_for_acp_session_idle(socket_path: Path, session_id: str, label: str, timeout: float = 60.0) -> None:
    if not str(socket_path) or not session_id:
        raise ScenarioError(f"ACP {label} is missing control-socket session metadata")
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        status = send_control_request(socket_path, {"action": "status", "sessionId": session_id})
        if status.get("sessionId") != session_id:
            raise ScenarioError(f"ACP {label} control socket reported a different session")
        last_state = str(status.get("state") or "unknown")
        if last_state == "idle":
            return
        if last_state == "failed":
            break
        time.sleep(0.2)
    raise ScenarioError(f"ACP {label} did not become idle within {timeout:g}s (state={last_state})")


def verify(root: Path) -> None:
    metadata = load_metadata(root)
    project = Path(metadata["project"])
    collector = Path(metadata["collector_worktree"])
    implementer = Path(metadata["implementer_worktree"])
    config_path = project / ".tmux-team" / "team.toml"
    if not config_path.exists():
        raise ScenarioError(f"missing tmux-team config: {config_path}")
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    roles = config.get("roles", {})
    operator = config.get("operator", {})
    is_acp = all(roles.get(role, {}).get("mode") == "acp_tui" for role in ROLES)
    if not operator.get("pane"):
        raise ScenarioError("team.toml is missing operator pane recovery metadata")
    if is_acp:
        verify_acp_control_ready("configured session", config, ("Traceback", "ACP TUI role exited"))
    for role, key in (
        ("orchestrator", "project"),
        ("implementer", "implementer_worktree"),
        ("collector", "collector_worktree"),
    ):
        actual = Path(str(roles[role]["worktree"])).resolve()
        expected = Path(str(metadata[key])).resolve()
        if actual != expected:
            raise ScenarioError(f"{role} worktree mismatch: expected {expected}, got {actual}")
        if is_acp:
            if roles[role].get("notify_method") != "control-socket":
                raise ScenarioError(f"{role} does not use ACP control-socket delivery")
            if not roles[role].get("control_socket") or not roles[role].get("runtime_session_id"):
                raise ScenarioError(f"{role} is missing ACP socket/session metadata")
            if roles[role].get("acp_resume_supported") is not True:
                raise ScenarioError(f"{role} does not advertise ACP exact resume support")
        elif roles[role].get("codex_reasoning_effort") != "high":
            raise ScenarioError(f"{role} did not preserve configured Codex reasoning effort")
    verify_tmux_truecolor(config)

    result = run_target_test(collector, check=False)
    if result.returncode != 0:
        raise ScenarioError(
            f"collector worktree target test still fails:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    seed_commit = metadata["seed_commit"]
    implementer_head = git_output(implementer, "rev-parse", "HEAD")
    collector_head = git_output(collector, "rev-parse", "HEAD")
    if implementer_head == seed_commit:
        raise ScenarioError("implementer did not create a fix commit")
    if collector_head == seed_commit:
        raise ScenarioError("collector did not verify a fixed commit")
    if collector_head != implementer_head:
        raise ScenarioError(f"collector verified {collector_head}, but implementer head is {implementer_head}")

    db_path = project / ".tmux-team" / "runtime" / "team.sqlite"
    if not db_path.exists():
        raise ScenarioError(f"missing runtime database: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        require_sql_count_exact(
            conn,
            "messages",
            "sender = 'orchestrator' AND recipient = 'collector' "
            f"AND correlation_key = '{CORRELATION_KEYS['baseline']}' AND message_kind = 'task'",
            1,
        )
        require_sql_count_exact(
            conn,
            "messages",
            "sender = 'orchestrator' AND recipient = 'collector' "
            f"AND correlation_key = '{CORRELATION_KEYS['verification']}' AND message_kind = 'task'",
            1,
        )
        require_sql_count_exact(
            conn,
            "messages",
            "sender = 'orchestrator' AND recipient = 'implementer' "
            f"AND correlation_key = '{CORRELATION_KEYS['fix']}' AND message_kind = 'task'",
            1,
        )
        if not is_acp:
            require_sql_count_exact(
                conn,
                "messages",
                "sender = 'orchestrator' AND recipient = 'implementer' "
                f"AND correlation_key = '{CORRELATION_KEYS['post_resume']}' AND message_kind = 'task'",
                1,
            )
        require_sql_count_exact(
            conn,
            "messages",
            "sender = 'collector' AND recipient = 'orchestrator' AND message_kind = 'completion_notice'",
            2,
        )
        require_sql_count_exact(
            conn,
            "messages",
            "sender = 'implementer' AND recipient = 'orchestrator' AND message_kind = 'completion_notice'",
            1 if is_acp else 2,
        )
        require_sql_count(
            conn,
            "messages",
            "sender = 'orchestrator' AND recipient IN ('collector', 'implementer') AND message_kind = 'notice'",
            4,
        )
        require_sql_count_exact(
            conn,
            "messages",
            "sender = 'watchdog:live-pressure' AND recipient = 'orchestrator' "
            f"AND correlation_key = '{WATCHDOG_PRESSURE_CORRELATION}'",
            1,
        )
        if not is_acp:
            require_sql_count_exact(
                conn,
                "messages",
                "sender = 'watchdog:live-resume' AND recipient = 'orchestrator' "
                f"AND correlation_key = '{WATCHDOG_RESUME_CORRELATION}'",
                1,
            )
        require_sql_count_exact(conn, "messages", "state != 'completed'", 0)
        require_sql_count_exact(
            conn,
            "stable_commits",
            f"scope = '{STABLE_SCOPE}' AND commit_sha = '{collector_head}'",
            1,
        )
        require_sql_count_exact(conn, "obligations", "status IN ('active', 'blocked')", 0)
        require_sql_count_range(
            conn,
            "obligations",
            "status IN ('done', 'failed', 'cancelled')",
            minimum=1 if is_acp else 2,
            maximum=2 if is_acp else 4,
        )
        require_sql_count(conn, "events", "type = 'obligation.updated'", 1 if is_acp else 2)
        require_sql_count(conn, "events", "type = 'obligation.completed'", 1 if is_acp else 2)
        require_sql_count(conn, "events", "type = 'watchdog.runner_ran'", 1 if is_acp else 2)
        if not is_acp:
            require_sql_count(conn, "events", "type = 'watchdog.runner_updated' AND ref_id = 'live-resume'", 1)
            require_sql_count(
                conn,
                "events",
                "type = 'watchdog.runner_upserted' AND actor = 'resume' AND ref_id = 'live-resume'",
                1,
            )
            require_sql_count(conn, "events", "type = 'watchdog.runner_stopped' AND ref_id = 'live-resume'", 1)
        require_sql_count(conn, "events", "type = 'team.sleep.snapshot'", 1)
        require_sql_count(conn, "events", "type = 'team.sleep.teardown'", 1)
        require_sql_count(conn, "events", "type = 'team.resume'", 1)
        require_sql_count(conn, "events", "type = 'stable.approved'", 1)
        if not is_acp:
            require_sql_count_exact(conn, "watchdog_runners", "name = 'live-resume' AND state = 'stopped'", 1)
    finally:
        conn.close()

    if is_acp:
        snapshot_path = project / ".tmux-team" / "runtime" / "sleeps" / "latest.toml"
        if not snapshot_path.exists():
            raise ScenarioError(f"missing ACP sleep snapshot: {snapshot_path}")
        snapshot = tomllib.loads(snapshot_path.read_text(encoding="utf-8"))
        for role in ROLES:
            saved = snapshot.get("roles", {}).get(role, {}).get("acp", {}).get("session_id")
            resumed = roles[role].get("runtime_session_id")
            if not saved or saved != resumed:
                raise ScenarioError(f"{role} ACP exact resume mismatch: saved={saved or '-'} resumed={resumed or '-'}")

    milestones = project / ".tmux-team" / "runtime" / "milestones.jsonl"
    if not milestones.exists():
        raise ScenarioError(f"missing milestones file: {milestones}")
    milestone_rows = [json.loads(line) for line in milestones.read_text(encoding="utf-8").splitlines() if line.strip()]
    summaries = "\n".join(str(row.get("summary", "")) for row in milestone_rows)
    if len(milestone_rows) < 3 or "pass" not in summaries.lower():
        raise ScenarioError("expected at least three milestones including a passing-test summary")
    non_orchestrator_milestone_roles = sorted(
        {str(row.get("role")) for row in milestone_rows if row.get("role") not in (None, "", "orchestrator")}
    )
    if non_orchestrator_milestone_roles:
        raise ScenarioError(f"non-orchestrator milestone roles found: {non_orchestrator_milestone_roles}")

    print("LIVE DEMO VERIFY OK")
    print(f"runtime: {'acp' if is_acp else 'codex'}")
    print(f"collector_head: {collector_head}")
    print(f"implementer_head: {implementer_head}")
    print(f"target_test: {TARGET_TEST}")


def verify_tmux_truecolor(config: dict[str, Any]) -> None:
    operator = config.get("operator", {})
    target = str(operator.get("pane") or "")
    if not target:
        roles = config.get("roles", {})
        target = next((str(role.get("pane") or "") for role in roles.values() if role.get("pane")), "")
    if not target:
        raise ScenarioError("cannot verify tmux truecolor: no operator or role pane target in team.toml")

    session = run(["tmux", "display-message", "-p", "-t", target, "#{session_name}"]).stdout.strip()
    if not session:
        raise ScenarioError(f"cannot verify tmux truecolor: could not resolve session for {target}")

    default_terminal = tmux_option(session, "default-terminal")
    if default_terminal != "tmux-256color":
        raise ScenarioError(f"expected tmux default-terminal=tmux-256color, got {default_terminal or '-'}")

    colorterm = run(["tmux", "show-environment", "-t", session, "COLORTERM"], check=False)
    if colorterm.returncode != 0 or colorterm.stdout.strip() != "COLORTERM=truecolor":
        raise ScenarioError(f"expected tmux COLORTERM=truecolor, got {colorterm.stdout.strip() or '-'}")

    terminal_features = tmux_option(session, "terminal-features")
    terminal_overrides = tmux_option(session, "terminal-overrides")
    if "RGB" not in terminal_features and "Tc" not in terminal_overrides:
        raise ScenarioError("expected tmux truecolor capability in terminal-features RGB or terminal-overrides Tc")


def tmux_option(session: str, option: str) -> str:
    result = run(["tmux", "show-options", "-qv", "-t", session, option], check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    result = run(["tmux", "show-options", "-gqv", option], check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def clean(root: Path, args: argparse.Namespace) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", args.session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    )
    if not args.keep_root and root.exists():
        marker = root / MARKER
        if not marker.exists():
            raise ScenarioError(f"refusing to remove unmarked directory: {root}")
        shutil.rmtree(root)
    print("LIVE DEMO CLEAN OK")
    print(f"session: {args.session}")
    if args.keep_root:
        print(f"kept root: {root}")


def sleep_team(root: Path) -> None:
    metadata = load_metadata(root)
    config_path = Path(metadata["project"]) / ".tmux-team" / "team.toml"
    run(["tmux-team", "--config", str(config_path), "sleep"])
    print("LIVE DEMO SLEEP OK")
    print(f"config: {config_path}")
    print("resume next: make live-demo-resume")


def resume_team(root: Path, args: argparse.Namespace) -> None:
    metadata = load_metadata(root)
    config_path = Path(metadata["project"]) / ".tmux-team" / "team.toml"
    command = ["tmux-team", "--config", str(config_path), "resume"]
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    is_acp = all(role.get("mode") == "acp_tui" for role in config.get("roles", {}).values())
    if args.role_yolo and not is_acp:
        command.append("--role-yolo")
    run(command)
    print("LIVE DEMO RESUME OK")
    print(f"config: {config_path}")
    if not is_acp:
        print("nudge watchdog next: make live-demo-watchdog-now")


def nudge_resumed_watchdog(root: Path) -> None:
    metadata = load_metadata(root)
    config_path = Path(metadata["project"]) / ".tmux-team" / "team.toml"
    run(
        [
            "tmux-team",
            "--config",
            str(config_path),
            "watchdog",
            "update",
            WATCHDOG_RESUME_NAME,
            "--interval",
            "5s",
            "--delivery",
            "app-server-turn",
            "--notify-role",
            "orchestrator",
            "--goal",
            "Resume the post-sleep verification operation; stop this watchdog once the implementer reports passing tests.",
        ]
    )
    print("LIVE DEMO WATCHDOG NUDGE OK")
    print(f"name: {WATCHDOG_RESUME_NAME}")
    print("interval: 5s")


def start_dashboard_pane(session: str, config_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    dashboard_command = (
        f"uv run --with-editable . --extra dashboard tmux-team --config {shlex.quote(str(config_path))} dashboard"
    )
    result = run(
        [
            "tmux",
            "split-window",
            "-h",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"{session}:tt-control",
            "-c",
            str(source_root),
            dashboard_command,
        ]
    )
    pane = result.stdout.strip()
    if pane:
        run(["tmux", "select-pane", "-t", pane, "-T", "tt-dashboard"], check=False)
    run(["tmux", "select-layout", "-t", f"{session}:tt-control", "even-horizontal"], check=False)


def reset_root(root: Path, force: bool) -> None:
    if root.exists():
        marker = root / MARKER
        if not marker.exists():
            raise ScenarioError(f"refusing to replace unmarked directory: {root}")
        if not force:
            raise ScenarioError(f"scenario root exists; rerun setup with --force: {root}")
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / MARKER).write_text("owned by scripts/live_demo_scenario.py\n", encoding="utf-8")


def seed_regression(project: Path) -> None:
    store_path = project / "src" / "tmux_team" / "store.py"
    text = store_path.read_text(encoding="utf-8")
    needle = (
        "WHEN 'urgent' THEN 0\n"
        "                    WHEN 'high' THEN 1\n"
        "                    WHEN 'normal' THEN 2\n"
        "                    ELSE 3"
    )
    replacement = (
        "WHEN 'urgent' THEN 3\n"
        "                    WHEN 'high' THEN 1\n"
        "                    WHEN 'normal' THEN 2\n"
        "                    ELSE 0"
    )
    claim_start = text.index("    def claim_next")
    case_start = text.index(needle, claim_start)
    text = text[:case_start] + replacement + text[case_start + len(needle) :]
    store_path.write_text(text, encoding="utf-8")

    test_path = project / "tests" / "test_store.py"
    test_text = test_path.read_text(encoding="utf-8")
    marker = "    def test_claim_next_prefers_urgent_over_older_normal_message"
    if marker in test_text:
        return
    insert = """
    def test_claim_next_prefers_urgent_over_older_normal_message(self) -> None:
        normal = self.create_message(summary="older normal")
        urgent = self.store.create_message(
            self.conn,
            sender="sender",
            recipient="worker",
            priority="urgent",
            summary="newer urgent",
            body="body",
        )

        row = self.store.claim_next(self.conn, "worker", 60)

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["id"], urgent.id)
        self.assertNotEqual(row["id"], normal.id)
"""
    test_text = test_text.replace("\n    def create_message", insert + "\n    def create_message")
    test_path.write_text(test_text, encoding="utf-8")


def write_goal(root: Path, metadata: dict[str, Any]) -> None:
    goal = f"""\
    Live tmux-team demo objective.

    Treat this as a real bugfix in a public repository snapshot, not as a scripted fixture. Do not inspect the tmux-team live-demo setup script or generated scenario metadata as a diagnostic shortcut. Work only from the target repository, tests, runtime state, and role messages.

    Target repository:
    - URL: {metadata["repo_url"]}
    - Ref: {metadata["repo_ref"]}
    - Base commit: {metadata["base_commit"]}

    Role worktrees:
    - orchestrator: {metadata["project"]}
    - implementer: {metadata["implementer_worktree"]}
    - collector: {metadata["collector_worktree"]}

    Target behavior:
    A seeded regression causes tmux-team inbox claiming to violate urgent-first priority ordering. The team must diagnose and fix this from tests and code, then verify the fix from the collector worktree.

    Required team flow:
    1. Orchestrator records a start milestone and starts an obligation for demo verification with a short next-update window. If an active demo obligation already exists after startup/recovery, reuse it instead of creating another.
    2. Orchestrator runs operator show, status --verbose, dashboard --once --no-pane-preview, pane list --all, watchdog, and memory show for its own role before dispatching. Confirm status/dashboard show configured Codex launch settings and fast=unknown.
    3. Orchestrator intentionally exercises watchdog pressure: after the obligation is overdue, run `tmux-team watchdog run --once --name {WATCHDOG_PRESSURE_NAME} --delivery app-server-turn --notify-role orchestrator --description "Live demo pressure check" --goal "Escalate overdue demo obligations"`. Claim the resulting watchdog inbox message for orchestrator and complete it after reconciling the obligation. Do not leave the watchdog pressure message active.
    4. Orchestrator sends a notice-only checkpoint with broadcast --from orchestrator --notice --only implementer,collector.
    5. Orchestrator sends collector exactly one baseline evidence task with --correlation-key {CORRELATION_KEYS["baseline"]}. The collector identifies the minimal failing test command and reports evidence with --reply-to-sender.
    6. Orchestrator updates the obligation after accepting collector evidence.
    7. Orchestrator sends implementer exactly one fix task with --correlation-key {CORRELATION_KEYS["fix"]}. The implementer fixes the production bug in the implementer worktree, runs the targeted test, commits the fix, and reports the commit SHA with --reply-to-sender.
    8. Orchestrator inspects relation state with inbox list --role collector --verbose and inbox list --role implementer --verbose, then inspects at least one role pane with pane capture --lines N --offset N.
    9. Orchestrator approves the implementer fix with stable approve <sha> --role {STABLE_SCOPE}.
    10. Orchestrator sends collector exactly one fix verification task with --correlation-key {CORRELATION_KEYS["verification"]}. The collector uses tmux-team stable sync --role collector --apply or an equivalent checkout of the approved stable commit, then reruns the targeted test in the collector worktree.
    11. Orchestrator updates and completes the initial bugfix obligation and records a milestone that the collector target test passed.
    12. Orchestrator arms the post-resume phase, then parks for operator sleep/resume:
        - Start a second obligation owned by orchestrator for post-resume verification with a short next-update window.
        - Start a persistent watchdog runner named {WATCHDOG_RESUME_NAME} with interval 1m, delivery report-only, notify-role orchestrator, and a goal saying it should resume the post-sleep verification operation and be stopped once implementer tests pass.
        - Do not dispatch the post-resume implementer test task until the watchdog wakes orchestrator after the operator resumes the team and changes the watchdog interval/delivery with live-demo-watchdog-now.
    13. After the operator sleep/resume/nudge, claim and complete the {WATCHDOG_RESUME_NAME} watchdog pressure message. The operator nudge changes the runner to interval 5s and delivery app-server-turn. Then send implementer exactly one post-resume test task with --correlation-key {CORRELATION_KEYS["post_resume"]}. The implementer runs the useful target test command without changing code and reports the passing result with --reply-to-sender.
    14. Orchestrator completes the post-resume obligation, stops watchdog runner {WATCHDOG_RESUME_NAME}, records a final milestone that the post-resume test operation passed, checks watchdog is clean, and broadcasts a notice-only final summary with broadcast --from orchestrator --notice --exclude orchestrator.

    After reviewing completion notices, close them with inbox complete-replies or an explicit inbox complete command so the final inbox has no unfinished work.

    Correlation discipline:
    - Reuse the exact correlation key for retries or follow-ups within the same work thread.
    - Before sending follow-up work, check existing message state with status --verbose or inbox list --verbose.
    - If a collector verification task already exists for {CORRELATION_KEYS["verification"]}, inspect that message or result instead of sending another.
    - Do not use --allow-duplicate in this scenario; redundant independent work is outside the demo objective.

    Useful target test command:
    uv run --with-editable . python -m unittest {TARGET_TEST}

    Success criteria:
    - The target test fails before the fix and passes after the fix.
    - The final passing test is run in the collector worktree, not only the implementer worktree.
    - Implementer produces a real git commit for the fix.
    - Collector verifies that commit in its own worktree.
    - Operator sleep/resume happens after the first passing collector verification.
    - The running {WATCHDOG_RESUME_NAME} watchdog is reinstantiated by resume, then operator-updated to 5s and app-server-turn delivery so it wakes orchestrator for the second operation.
    - Implementer runs the target test again after resume and reports it passing without another code change.
    - Orchestrator stops {WATCHDOG_RESUME_NAME} after the post-resume pass condition is met.
    - Orchestrator records the fix as the collector stable commit before collector verification.
    - Role communication uses tmux-team inbox messages and completion replies, not ad-hoc pane text.
    - The run exercises operator show, status --verbose, dashboard --once, inbox list --verbose, pane list --all, pane capture --lines/--offset, watchdog report-only, watchdog run --once with app-server-turn pressure delivery, watchdog start/update/stop, sleep/resume, obligation start/update/complete, milestones, completion replies, stable approve/sync, broadcast --notice --only, and broadcast --notice --exclude.
    - Non-orchestrator roles do not write milestones.

    Boundaries:
    - Do not edit the tmux-team source checkout that launched this demo.
    - Do not mark success from chat alone; success requires the real test command to pass.
    - Do not route work to tt-control.
    """
    (root / "goal.md").write_text(textwrap.dedent(goal), encoding="utf-8")


def write_acp_goal(root: Path, metadata: dict[str, Any], provider: str) -> None:
    goal = f"""\
    Live tmux-team ACP demo objective.

    Treat this as a real bugfix in a public repository snapshot, not as a scripted fixture. Do not inspect the tmux-team live-demo setup script or generated scenario metadata as a diagnostic shortcut. Work only from the target repository, tests, runtime state, and role messages.

    Target repository:
    - URL: {metadata["repo_url"]}
    - Ref: {metadata["repo_ref"]}
    - Base commit: {metadata["base_commit"]}

    Role worktrees:
    - orchestrator: {metadata["project"]}
    - implementer: {metadata["implementer_worktree"]}
    - collector: {metadata["collector_worktree"]}

    Target behavior:
    A seeded regression causes tmux-team inbox claiming to violate urgent-first priority ordering. Diagnose and fix it from tests and code, then verify the fix from the collector worktree.

    Required team flow:
    1. Orchestrator records a start milestone and starts one obligation for demo verification with `--next-update-in 10s`.
    2. Orchestrator runs operator show, status --verbose, dashboard --once --no-pane-preview, pane list --all, watchdog, memory show, and acp status for every role before dispatching. Confirm the role runtime is ACP, provider is {provider}, and each role has a control socket and runtime session id.
    3. Wait at least 12 seconds, then confirm the obligation is overdue before running `tmux-team watchdog run --once --name {WATCHDOG_PRESSURE_NAME} --delivery control-socket --notify-role orchestrator --description "Live ACP demo pressure check" --goal "Escalate overdue demo obligations"`. Do not run the watchdog early. Claim and complete the resulting watchdog message after reconciling the obligation.
    4. Orchestrator broadcasts one notice-only checkpoint to implementer and collector using `broadcast --notice --only implementer,collector`.
    5. Orchestrator sends collector exactly one baseline task with --correlation-key {CORRELATION_KEYS["baseline"]}. Collector identifies the minimal failing test and reports evidence with --reply-to-sender.
    6. Orchestrator updates the obligation after accepting collector evidence.
    7. Orchestrator sends implementer exactly one fix task with --correlation-key {CORRELATION_KEYS["fix"]}. Implementer fixes production code in its own worktree, runs the targeted test, commits, and replies with the commit SHA.
    8. Orchestrator inspects relation state with inbox list --verbose and one role pane with pane capture --lines and --offset.
    9. Orchestrator approves the implementer commit with stable approve <sha> --role {STABLE_SCOPE}.
    10. Orchestrator sends collector exactly one verification task with --correlation-key {CORRELATION_KEYS["verification"]}. Collector syncs the approved stable commit and reruns the targeted test in its own worktree.
    11. Orchestrator completes the obligation, records a passing-test milestone, checks status/watchdog/acp status, and broadcasts a final notice using `broadcast --notice --exclude orchestrator`.
    12. After reviewing completion notices, close them so the final inbox contains no unfinished work.

    Correlation discipline:
    - Reuse the exact correlation key for retries and follow-ups in one logical thread.
    - Inspect existing message state before sending follow-up work.
    - Do not use --allow-duplicate.
    - After dispatching delegated work, end the turn and rely on the control-socket wake. Do not poll `inbox next` or sleep in a polling loop.
    - Workers return each delegated result once with `inbox complete --reply-to-sender`; do not send the same result as a separate task.
    - For role tasks, omit `--notify-method`; recipient configuration selects control-socket delivery. The explicit `--delivery control-socket` above applies only to the watchdog command.

    Useful target test command:
    uv run --with-editable . python -m unittest {TARGET_TEST}

    Success criteria:
    - The target test fails before the fix and passes afterward.
    - Implementer produces a real commit; collector verifies that same commit in its own worktree.
    - Role communication uses durable inbox messages and completion replies.
    - ACP role wakes use the private control socket, not tmux stdin.
    - The run exercises status, dashboard, inbox relations, pane inspection, watchdog control-socket pressure, obligations, milestones, stable approve/sync, completion replies, and notice broadcasts.
    - Non-orchestrator roles do not write milestones.
    - After role work completes, the operator exact-sleeps and resumes the team; every provider session ID matches the snapshot.

    Boundaries:
    - The orchestrator must not call sleep/resume itself; the operator triggers exact recovery after role work completes.
    - Do not edit the tmux-team source checkout that launched this demo.
    - Do not mark success from chat alone; the real test must pass.
    - Do not route work to tt-control.
    """
    (root / "goal.md").write_text(textwrap.dedent(goal), encoding="utf-8")


def load_metadata(root: Path) -> dict[str, Any]:
    path = root / "scenario.json"
    if not path.exists():
        raise ScenarioError(f"scenario not set up: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run_target_test(worktree: Path, *, check: bool) -> subprocess.CompletedProcess[str]:
    return run(
        ["uv", "run", "--with-editable", ".", "python", "-m", "unittest", TARGET_TEST],
        cwd=worktree,
        check=check,
    )


def require_sql_count(conn: sqlite3.Connection, table: str, where: str, minimum: int) -> None:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}").fetchone()
    count = int(row["count"])
    if count < minimum:
        raise ScenarioError(f"expected at least {minimum} rows in {table} where {where}, got {count}")


def require_sql_count_exact(conn: sqlite3.Connection, table: str, where: str, expected: int) -> None:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}").fetchone()
    count = int(row["count"])
    if count != expected:
        raise ScenarioError(f"expected exactly {expected} rows in {table} where {where}, got {count}")


def require_sql_count_range(conn: sqlite3.Connection, table: str, where: str, *, minimum: int, maximum: int) -> None:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}").fetchone()
    count = int(row["count"])
    if count < minimum or count > maximum:
        raise ScenarioError(f"expected between {minimum} and {maximum} rows in {table} where {where}, got {count}")


def git_output(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise ScenarioError(
            "command failed:"
            f"\n  {' '.join(command)}"
            f"\nexit={result.returncode}"
            f"\nstdout:\n{result.stdout}"
            f"\nstderr:\n{result.stderr}"
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
