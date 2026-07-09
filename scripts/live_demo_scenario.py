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
import tomllib
from pathlib import Path
from typing import Any

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
    bootstrap_parser.add_argument(
        "--role-yolo", action="store_true", help="Launch managed role panes in Codex YOLO mode"
    )
    bootstrap_parser.add_argument("--force-config", action="store_true", help="Replace an existing team.toml")

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
        "--role-reasoning-effort",
        "orchestrator=high",
        "--role-reasoning-effort",
        "implementer=high",
        "--role-reasoning-effort",
        "collector=high",
        "--goal-file",
        str(root / "goal.md"),
    ]
    if args.role_yolo:
        command.append("--role-yolo")
    if args.force_config:
        command.append("--force-config")
    run(command)
    start_dashboard_pane(args.session, Path(metadata["project"]) / ".tmux-team" / "team.toml")
    print("LIVE DEMO BOOTSTRAP STARTED")
    print(f"session: {args.session}")
    print("dashboard: tt-control split")
    print(f"attach: tmux attach -t {args.session}")
    print(f"project: {metadata['project']}")
    print(f"verify later: {Path(__file__).name} --root {root} verify")


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
    if not operator.get("pane"):
        raise ScenarioError("team.toml is missing operator pane recovery metadata")
    for role, key in (
        ("orchestrator", "project"),
        ("implementer", "implementer_worktree"),
        ("collector", "collector_worktree"),
    ):
        actual = Path(str(roles[role]["worktree"])).resolve()
        expected = Path(str(metadata[key])).resolve()
        if actual != expected:
            raise ScenarioError(f"{role} worktree mismatch: expected {expected}, got {actual}")
        if roles[role].get("codex_reasoning_effort") != "high":
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
            "sender = 'orchestrator' AND recipient = 'collector' AND message_kind = 'task'",
            2,
        )
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
            2,
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
        require_sql_count_range(conn, "obligations", "status IN ('done', 'failed', 'cancelled')", minimum=2, maximum=4)
        require_sql_count(conn, "events", "type = 'obligation.updated'", 2)
        require_sql_count(conn, "events", "type = 'obligation.completed'", 2)
        require_sql_count(conn, "events", "type = 'watchdog.runner_ran'", 2)
        require_sql_count(conn, "events", "type = 'watchdog.runner_updated' AND ref_id = 'live-resume'", 1)
        require_sql_count(
            conn, "events", "type = 'watchdog.runner_upserted' AND actor = 'resume' AND ref_id = 'live-resume'", 1
        )
        require_sql_count(conn, "events", "type = 'watchdog.runner_stopped' AND ref_id = 'live-resume'", 1)
        require_sql_count(conn, "events", "type = 'team.sleep.snapshot'", 1)
        require_sql_count(conn, "events", "type = 'team.sleep.teardown'", 1)
        require_sql_count(conn, "events", "type = 'team.resume'", 1)
        require_sql_count(conn, "events", "type = 'stable.approved'", 1)
        require_sql_count_exact(conn, "watchdog_runners", "name = 'live-resume' AND state = 'stopped'", 1)
    finally:
        conn.close()

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
    if args.role_yolo:
        command.append("--role-yolo")
    run(command)
    print("LIVE DEMO RESUME OK")
    print(f"config: {config_path}")
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
