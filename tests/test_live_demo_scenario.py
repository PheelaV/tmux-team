from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.live_demo_scenario import start_goal, write_acp_goal


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


if __name__ == "__main__":
    unittest.main()
