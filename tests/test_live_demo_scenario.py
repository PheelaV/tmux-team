from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.live_demo_scenario import (
    MAX_RESUME_PRESSURE_MESSAGES,
    WATCHDOG_RESUME_CORRELATION,
    ScenarioError,
    acp_demo_initial_config,
    bootstrap,
    materialize_approved_commit,
    nudge_resumed_watchdog,
    prepare_acp_demo_provider,
    require_bounded_resume_pressure,
    reset_root,
    resume_team,
    scenario_paths,
    start_goal,
    verify_acp_demo_model,
    wait_for_acp_session_idle,
    write_acp_goal,
)


class LiveDemoScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.config = self.project / ".tmux-team" / "team.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text('[team]\nname = "demo"\n', encoding="utf-8")
        self.metadata = {
            "repo_url": "https://example.invalid/project.git",
            "repo_ref": "v1",
            "base_commit": "abc123",
            "project": str(self.project),
            "implementer_worktree": str(self.root / "implementer"),
            "collector_worktree": str(self.root / "collector"),
        }
        (self.root / "scenario.json").write_text(json.dumps(self.metadata), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_acp_goal_uses_configured_provider(self) -> None:
        write_acp_goal(self.root, self.metadata, "claude")

        goal = (self.root / "goal.md").read_text(encoding="utf-8")
        self.assertIn("provider is claude", goal)
        self.assertIn("--next-update-in 10s", goal)
        self.assertIn("Do not run the watchdog early", goal)
        self.assertNotIn("provider is cursor", goal)
        self.assertIn("operator exact-sleeps and resumes", goal)
        self.assertIn("broadcast --notice --only implementer,collector", goal)
        self.assertIn("broadcast --notice --exclude orchestrator", goal)
        self.assertIn("Do not poll `inbox next`", goal)
        self.assertIn("do not send the same result as a separate task", goal)
        self.assertIn("omit `--notify-method`", goal)
        self.assertNotIn("--notice-only", goal)

    def test_start_goal_submits_deferred_goal_durably(self) -> None:
        (self.root / "goal.md").write_text("Execute the scenario.\n", encoding="utf-8")
        completed = subprocess.CompletedProcess([], 0, "msg_1 queued\n", "")

        with patch("scripts.live_demo_scenario.run", return_value=completed) as run, redirect_stdout(StringIO()):
            start_goal(self.root)

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["tmux-team", "--config", str(self.config)])
        self.assertIn("--body-file", command)
        self.assertIn(str(self.root / "goal.md"), command)
        self.assertIn("live-demo-goal", command)

    def test_flat_project_layout_keeps_registered_project_at_root(self) -> None:
        project, implementer, collector = scenario_paths(self.root, flat_project=True)

        self.assertEqual(project, self.root)
        self.assertEqual(implementer, self.root.parent / f"{self.root.name}-implementer")
        self.assertEqual(collector, self.root.parent / f"{self.root.name}-collector")

    def test_reuses_only_an_otherwise_empty_marked_root(self) -> None:
        provisioned = self.root / "provisioned"
        provisioned.mkdir()
        marker = provisioned / ".tmux-team-live-demo-scenario"
        marker.write_text("provisioned\n", encoding="utf-8")

        reset_root(provisioned, False, reuse_marked_root=True)

        self.assertTrue(marker.exists())

    def test_bootstrap_passes_named_codex_role_profile(self) -> None:
        args = Namespace(
            session="tt-demo",
            agent_runtime="codex",
            acp_tui_bin="toad",
            acp_agent_command="agent acp",
            acp_provider="cursor",
            acp_model="",
            acp_effort="",
            acp_fast="",
            acp_startup_timeout=180.0,
            instruction_profile="guided",
            defer_goal=True,
            role_profile="tmux-team-role",
            role_yolo=False,
            force_config=False,
        )
        completed = subprocess.CompletedProcess([], 0, "", "")

        with (
            patch("scripts.live_demo_scenario.run", return_value=completed) as run,
            patch("scripts.live_demo_scenario.start_dashboard_pane"),
            redirect_stdout(StringIO()),
        ):
            bootstrap(self.root, args)

        command = run.call_args.args[0]
        self.assertIn("--role-profile", command)
        self.assertEqual(command[command.index("--role-profile") + 1], "tmux-team-role")
        self.assertNotIn("--role-yolo", command)

    def test_resume_passes_named_codex_role_profile(self) -> None:
        self.config.write_text(
            """[team]
name = "demo"

[roles.orchestrator]
mode = "codex_tui"
""",
            encoding="utf-8",
        )
        args = Namespace(role_profile="tmux-team-role", role_yolo=False)
        completed = subprocess.CompletedProcess([], 0, "", "")

        with patch("scripts.live_demo_scenario.run", return_value=completed) as run, redirect_stdout(StringIO()):
            resume_team(self.root, args)

        self.assertEqual(
            run.call_args.args[0],
            ["tmux-team", "--config", str(self.config), "resume", "--role-profile", "tmux-team-role"],
        )

    def test_watchdog_nudge_recovers_failed_runner_as_operator(self) -> None:
        sleep = self.project / ".tmux-team" / "runtime" / "sleeps"
        sleep.mkdir(parents=True)
        (sleep / "latest.toml").write_text(
            '[tmux]\nsession = "tt-recovered"\n',
            encoding="utf-8",
        )
        completed = [
            subprocess.CompletedProcess([], 0, '[{"name":"live-resume","state":"failed"}]\n', ""),
            subprocess.CompletedProcess([], 0, "live-resume state=running\n", ""),
            subprocess.CompletedProcess([], 0, "live-resume state=running interval=5s\n", ""),
        ]

        with patch("scripts.live_demo_scenario.run", side_effect=completed) as run, redirect_stdout(StringIO()):
            nudge_resumed_watchdog(self.root)

        start = run.call_args_list[1].args[0]
        self.assertIn("watchdog", start)
        self.assertIn("start", start)
        self.assertIn("--name", start)
        self.assertEqual(start[start.index("--session") + 1], "tt-recovered")
        update = run.call_args_list[2].args[0]
        self.assertIn("update", update)
        self.assertEqual(update[update.index("--interval") + 1], "5s")

    def test_watchdog_nudge_updates_running_runner_without_recovery(self) -> None:
        completed = [
            subprocess.CompletedProcess([], 0, '[{"name":"live-resume","state":"running"}]\n', ""),
            subprocess.CompletedProcess([], 0, "live-resume state=running interval=5s\n", ""),
        ]

        with patch("scripts.live_demo_scenario.run", side_effect=completed) as run, redirect_stdout(StringIO()):
            nudge_resumed_watchdog(self.root)

        self.assertEqual(run.call_count, 2)
        self.assertIn("status", run.call_args_list[0].args[0])
        self.assertIn("update", run.call_args_list[1].args[0])

    def test_materializes_only_the_approved_exact_role_tree(self) -> None:
        project = self.root / "materialize-project"
        implementer = self.root / "materialize-implementer"
        collector = self.root / "materialize-collector"
        private = self.root / "private-commit"

        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.name", "demo"], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.email", "demo@example.invalid"], check=True)
        source = project / "src" / "tmux_team" / "store.py"
        source.parent.mkdir(parents=True)
        source.write_text("priority = 'broken'\n", encoding="utf-8")
        (project / "CHANGELOG.md").write_text("# Changes\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "seed"], check=True)
        seed = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(project), "worktree", "add", "-qb", "implementer", str(implementer), seed],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(project), "worktree", "add", "-qb", "collector", str(collector), seed],
            check=True,
        )
        for worktree in (implementer, collector):
            (worktree / "src" / "tmux_team" / "store.py").write_text("priority = 'fixed'\n", encoding="utf-8")
            (worktree / "CHANGELOG.md").write_text("# Changes\n\n- Fix priority.\n", encoding="utf-8")

        subprocess.run(["git", "clone", "-q", str(project), str(private)], check=True)
        subprocess.run(["git", "-C", str(private), "config", "user.name", "implementer"], check=True)
        subprocess.run(["git", "-C", str(private), "config", "user.email", "implementer@example.invalid"], check=True)
        (private / "src" / "tmux_team" / "store.py").write_text("priority = 'fixed'\n", encoding="utf-8")
        (private / "CHANGELOG.md").write_text("# Changes\n\n- Fix priority.\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(private), "add", "CHANGELOG.md", "src/tmux_team/store.py"],
            check=True,
        )
        subprocess.run(["git", "-C", str(private), "commit", "-qm", "Fix priority"], check=True)
        approved = subprocess.run(
            ["git", "-C", str(private), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

        runtime = project / ".tmux-team" / "runtime"
        runtime.mkdir(parents=True)
        conn = sqlite3.connect(runtime / "team.sqlite")
        conn.executescript(
            """
            CREATE TABLE stable_commits (
              scope TEXT PRIMARY KEY,
              commit_sha TEXT NOT NULL,
              approved_by TEXT NOT NULL,
              approved_at TEXT NOT NULL,
              note TEXT
            );
            CREATE TABLE events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              type TEXT NOT NULL,
              actor TEXT,
              ref_id TEXT,
              payload_json TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO stable_commits VALUES (?, ?, ?, ?, ?)",
            ("collector", approved, "orchestrator", "2026-07-27T00:00:00+00:00", "reviewed"),
        )
        conn.commit()
        conn.close()
        metadata = {
            "project": str(project),
            "implementer_worktree": str(implementer),
            "collector_worktree": str(collector),
            "seed_commit": seed,
        }
        (self.root / "scenario.json").write_text(json.dumps(metadata), encoding="utf-8")
        passing = subprocess.CompletedProcess([], 0, "OK\n", "")

        with (
            patch("scripts.live_demo_scenario.find_private_commit_clone", return_value=private),
            patch("scripts.live_demo_scenario.run_target_test", return_value=passing),
            redirect_stdout(StringIO()),
        ):
            materialize_approved_commit(self.root)
            materialize_approved_commit(self.root)

        for worktree in (implementer, collector):
            head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(head, approved)
        conn = sqlite3.connect(runtime / "team.sqlite")
        count = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'scenario.commit_materialized'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_resume_watchdog_allows_bounded_pressure_while_work_completes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE messages(sender TEXT, recipient TEXT, correlation_key TEXT)"
        )
        rows = [
            ("watchdog:live-resume", "orchestrator", WATCHDOG_RESUME_CORRELATION)
            for _ in range(4)
        ]
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?)", rows)

        require_bounded_resume_pressure(conn)

        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?)",
            [
                ("watchdog:live-resume", "orchestrator", WATCHDOG_RESUME_CORRELATION)
                for _ in range(MAX_RESUME_PRESSURE_MESSAGES - 4 + 1)
            ],
        )
        with self.assertRaises(ScenarioError):
            require_bounded_resume_pressure(conn)
        conn.close()

    def test_verify_acp_demo_model_checks_roles_and_operator_without_mutation(self) -> None:
        model = "test-model[reasoning=medium]"
        self.config.write_text(
            f'''[team]
name = "demo"

[operator]
control_socket = "{self.root / "operator.sock"}"
runtime_session_id = "operator-session"

[roles.orchestrator]
control_socket = "{self.root / "orchestrator.sock"}"
runtime_session_id = "orchestrator-session"

[roles.implementer]
control_socket = "{self.root / "implementer.sock"}"
runtime_session_id = "implementer-session"

[roles.collector]
control_socket = "{self.root / "collector.sock"}"
runtime_session_id = "collector-session"
''',
            encoding="utf-8",
        )
        with (
            patch("scripts.live_demo_scenario.acp_session_model", return_value=model) as current_model,
            patch("scripts.live_demo_scenario.wait_for_acp_session_idle") as wait_for_idle,
            redirect_stdout(StringIO()),
        ):
            verify_acp_demo_model(self.config, model)

        self.assertEqual(wait_for_idle.call_count, 4)
        self.assertEqual(current_model.call_count, 4)

    def test_wait_for_acp_session_idle_tolerates_startup_turn(self) -> None:
        responses = [
            {"sessionId": "session-1", "state": "busy"},
            {"sessionId": "session-1", "state": "idle"},
        ]
        with (
            patch("scripts.live_demo_scenario.send_control_request", side_effect=responses) as control,
            patch("scripts.live_demo_scenario.time.sleep"),
        ):
            wait_for_acp_session_idle(self.root / "role.sock", "session-1", "role implementer", timeout=1)

        self.assertEqual(control.call_count, 2)

    def test_acp_demo_initial_config_uses_provider_specific_option_ids(self) -> None:
        self.assertEqual(
            acp_demo_initial_config("codex", "gpt-5.6-terra", "medium", "false"),
            (
                "model=gpt-5.6-terra",
                "mode=agent-full-access",
                "reasoning_effort=medium",
                "fast-mode=false",
            ),
        )
        self.assertEqual(
            acp_demo_initial_config("claude", "us.anthropic.claude-opus-4-8", "medium", ""),
            ("model=us.anthropic.claude-opus-4-8", "mode=bypassPermissions", "effort=medium"),
        )
        self.assertEqual(
            acp_demo_initial_config("pool", "deployment-model", "", ""),
            ("mode=always-allow",),
        )

    def test_prepare_claude_demo_provider_writes_ignored_local_permission_settings(self) -> None:
        for key in ("project", "implementer_worktree", "collector_worktree"):
            Path(self.metadata[key]).mkdir(parents=True, exist_ok=True)
        exclude = self.root / "exclude"

        with patch("scripts.live_demo_scenario.git_output", return_value=str(exclude)):
            prepare_acp_demo_provider(self.metadata, "claude")

        for key in ("project", "implementer_worktree", "collector_worktree"):
            settings = json.loads(
                (Path(self.metadata[key]) / ".claude" / "settings.local.json").read_text(encoding="utf-8")
            )
            self.assertEqual(settings["permissions"]["defaultMode"], "bypassPermissions")
        self.assertEqual(exclude.read_text(encoding="utf-8"), ".claude/settings.local.json\n")


if __name__ == "__main__":
    unittest.main()
