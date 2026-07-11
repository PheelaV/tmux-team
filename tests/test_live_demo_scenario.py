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
    configure_acp_demo_model,
    start_goal,
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
        self.assertNotIn("provider is cursor", goal)
        self.assertIn("operator exact-sleeps and resumes", goal)
        self.assertIn("broadcast --notice --only implementer,collector", goal)
        self.assertIn("broadcast --notice --exclude orchestrator", goal)
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

    def test_configure_acp_demo_model_updates_roles_and_operator(self) -> None:
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
        response = {
            "sessionId": "operator-session",
            "configOptions": [{"id": "model", "currentValue": model}],
        }

        with (
            patch("scripts.live_demo_scenario.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
            patch("scripts.live_demo_scenario.send_control_request", return_value=response) as control,
            patch("scripts.live_demo_scenario.acp_session_model", return_value="old-model"),
            patch("scripts.live_demo_scenario.wait_for_acp_session_idle") as wait_for_idle,
            redirect_stdout(StringIO()),
        ):
            configure_acp_demo_model(self.config, model)

        self.assertEqual(run.call_count, 3)
        self.assertEqual(wait_for_idle.call_count, 4)
        for role, call in zip(("orchestrator", "implementer", "collector"), run.call_args_list, strict=True):
            self.assertIn(role, call.args[0])
            self.assertIn(f"model={model}", call.args[0])
        self.assertEqual(control.call_args.args[1]["value"], model)

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


if __name__ == "__main__":
    unittest.main()
