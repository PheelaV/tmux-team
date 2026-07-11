from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.live_demo_scenario import (
    acp_demo_initial_config,
    prepare_acp_demo_provider,
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
