from __future__ import annotations

import subprocess
import tempfile
import tomllib
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tmux_team.bootstrap import RoleLaunchOptions
from tmux_team.config import load_config
from tmux_team.lifecycle import (
    LifecycleError,
    merge_role_launch_options,
    prepare_acp_roles_for_sleep,
    resume_team,
    role_launch_options_from_capabilities,
)
from tmux_team.store import Store


class ACPLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.socket = self.runtime / "acp" / "orchestrator.sock"
        self.config_path = self.root / ".tmux-team" / "team.toml"
        self.config_path.parent.mkdir()
        self.config_path.write_text(
            f"""[team]
name = "acp-lifecycle"
runtime_dir = "{self.runtime}"

[roles.orchestrator]
mode = "acp_tui"
state = "active"
pane = "tt:tt-agents.0"
worktree = "{self.worktree}"
scratchpad = "{self.root / "memory.md"}"
notify_method = "control-socket"
control_socket = "{self.socket}"
acp_tui_bin = "toad"
acp_agent_command = "agent acp"
acp_provider = "cursor"
runtime_session_id = "saved-session"
acp_resume_supported = true
""",
            encoding="utf-8",
        )
        self.config = load_config(self.config_path)
        self.store = Store(self.config)
        with closing(self.store.connect()) as conn:
            self.store.sync_roles(conn, self.config.roles.values())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_instruction_profile_is_restored_and_may_be_overridden(self) -> None:
        saved = role_launch_options_from_capabilities({"instruction_profile": "compact"})
        self.assertEqual(saved.instruction_profile, "compact")

        merged = merge_role_launch_options(
            {"orchestrator": saved},
            {"orchestrator": RoleLaunchOptions(instruction_profile="guided")},
        )
        self.assertEqual(merged["orchestrator"].instruction_profile, "guided")

    def test_sleep_prepares_exact_snapshot_metadata_and_handoff(self) -> None:
        actions: list[str] = []

        def control(_path, request, timeout=5.0):
            del timeout
            actions.append(request["action"])
            return {
                "state": "idle",
                "sessionId": "saved-session",
                "queueDepth": 0,
                "resumeSupported": True,
                "acceptingPrompts": request["action"] != "quiesce",
            }

        with (
            closing(self.store.connect()) as conn,
            patch("tmux_team.lifecycle.send_control_request", side_effect=control),
            patch("tmux_team.runtime_switch.send_control_request", side_effect=control),
        ):
            metadata = prepare_acp_roles_for_sleep(
                self.config,
                self.store,
                conn,
                policy="exact",
                dry_run=False,
            )
            role = self.store.get_role(conn, "orchestrator")

        self.assertEqual(actions, ["status", "quiesce", "status"])
        self.assertEqual(metadata["orchestrator"]["session_id"], "saved-session")
        self.assertTrue(metadata["orchestrator"]["resume_supported"])
        self.assertTrue(Path(metadata["orchestrator"]["handoff_file"]).is_file())
        self.assertEqual(role["state"], "draining")

    def test_sleep_rolls_back_quiescence_and_role_state_on_race(self) -> None:
        actions: list[str] = []

        def control(_path, request, timeout=5.0):
            del timeout
            actions.append(request["action"])
            if request["action"] == "status":
                return {
                    "state": "idle",
                    "sessionId": "saved-session",
                    "queueDepth": 0,
                    "resumeSupported": True,
                    "acceptingPrompts": True,
                }
            if request["action"] == "quiesce":
                return {
                    "state": "busy",
                    "sessionId": "saved-session",
                    "queueDepth": 0,
                    "resumeSupported": True,
                    "acceptingPrompts": False,
                }
            return {
                "state": "busy",
                "sessionId": "saved-session",
                "queueDepth": 0,
                "acceptingPrompts": True,
            }

        with (
            closing(self.store.connect()) as conn,
            patch("tmux_team.lifecycle.send_control_request", side_effect=control),
            self.assertRaisesRegex(LifecycleError, "state=busy"),
        ):
            prepare_acp_roles_for_sleep(self.config, self.store, conn, policy="exact", dry_run=False)

        with closing(self.store.connect()) as conn:
            self.assertEqual(self.store.get_role(conn, "orchestrator")["state"], "active")
        self.assertEqual(actions, ["status", "quiesce", "unquiesce"])

    def test_exact_resume_dry_run_uses_saved_session_id(self) -> None:
        snapshot = self.write_snapshot(policy="exact", resume_supported=True)
        with closing(self.store.connect()) as conn:
            result = resume_team(
                self.config,
                self.store,
                conn,
                snapshot_path=snapshot,
                dry_run=True,
            )

        command_text = "\n".join(" ".join(command) for command in result.commands)
        self.assertEqual(result.agent_runtime, "acp")
        self.assertIsNone(result.endpoint)
        self.assertIn("--session-id saved-session", command_text)

    def test_handoff_resume_is_explicit_and_omits_saved_session(self) -> None:
        handoff = self.runtime / "handoffs" / "orchestrator" / "sleep.md"
        handoff.parent.mkdir(parents=True)
        handoff.write_text("# handoff\n", encoding="utf-8")
        snapshot = self.write_snapshot(policy="exact", resume_supported=True, handoff=handoff)
        with closing(self.store.connect()) as conn:
            result = resume_team(
                self.config,
                self.store,
                conn,
                snapshot_path=snapshot,
                acp_resume_policy="handoff",
                dry_run=True,
            )

        command_text = "\n".join(" ".join(command) for command in result.commands)
        self.assertNotIn("--session-id", command_text)

    def test_exact_resume_never_silently_falls_back(self) -> None:
        snapshot = self.write_snapshot(policy="exact", resume_supported=False)
        with closing(self.store.connect()) as conn, self.assertRaisesRegex(LifecycleError, "not capability-verified"):
            resume_team(
                self.config,
                self.store,
                conn,
                snapshot_path=snapshot,
                dry_run=True,
            )

    def test_exact_resume_verifies_identity_updates_binding_and_rewakes_pending(self) -> None:
        snapshot = self.write_snapshot(policy="exact", resume_supported=True)
        with closing(self.store.connect()) as conn:
            self.store.create_message(
                conn,
                sender="operator",
                recipient="orchestrator",
                priority="normal",
                summary="Resume pending work",
                body="Continue from durable state.",
            )

        completed = subprocess.CompletedProcess([], 0, "%9\n", "")
        ready = {
            "state": "idle",
            "sessionId": "saved-session",
            "queueDepth": 0,
            "resumeSupported": True,
        }
        with (
            closing(self.store.connect()) as conn,
            patch("tmux_team.lifecycle.resolve_tool_executable", return_value="/bin/toad"),
            patch("tmux_team.lifecycle.configure_session_truecolor"),
            patch("tmux_team.lifecycle.prepare_grouped_agent_window"),
            patch("tmux_team.lifecycle.subprocess_run_lifecycle", return_value=completed) as run,
            patch("tmux_team.lifecycle.configure_agent_window"),
            patch("tmux_team.lifecycle.select_tiled_layout_commands", return_value=[]),
            patch("tmux_team.lifecycle.label_role_pane"),
            patch("tmux_team.lifecycle.wait_for_acp_tui", return_value=ready),
            patch.object(self.store, "notify_role", return_value=(True, "notified")) as notify,
        ):
            result = resume_team(
                self.config,
                self.store,
                conn,
                snapshot_path=snapshot,
                dry_run=False,
            )

        self.assertEqual(result.role_panes["orchestrator"], "%9")
        self.assertEqual(result.role_threads["orchestrator"], "saved-session")
        spawn_command = run.call_args_list[0].args[0]
        self.assertIn("--session-id", spawn_command[-1])
        self.assertIn("saved-session", spawn_command[-1])
        notify.assert_called_once()
        config = load_config(self.config_path)
        self.assertEqual(config.roles["orchestrator"].pane, "%9")
        self.assertEqual(config.roles["orchestrator"].capabilities["runtime_session_id"], "saved-session")
        self.assertTrue(config.roles["orchestrator"].capabilities["acp_resume_supported"])

    def write_snapshot(
        self,
        *,
        policy: str,
        resume_supported: bool,
        handoff: Path | None = None,
    ) -> Path:
        path = self.runtime / "sleeps" / "latest.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        handoff_line = f'handoff_file = "{handoff}"\n' if handoff else ""
        path.write_text(
            f"""schema_version = 1
sleep_id = "sleep_acp"
created_at = "2026-07-11T00:00:00+00:00"
dry_run = false
pause_roles = true

[team]
name = "acp-lifecycle"
project_root = "{self.root}"
config_path = "{self.config_path}"
runtime_dir = "{self.runtime}"

[tmux]
session = "tt-acp"
kill_session = false

[roles.orchestrator]
mode = "acp_tui"
state = "active"
pane = "tt-acp:tt-agents.0"
worktree = "{self.worktree}"

[roles.orchestrator.capabilities]
notify_method = "control-socket"
control_socket = "{self.socket}"
acp_tui_bin = "toad"
acp_agent_command = "agent acp"
runtime_session_id = "saved-session"
acp_resume_supported = {str(resume_supported).lower()}

[roles.orchestrator.acp]
resume_policy = "{policy}"
session_id = "saved-session"
resume_supported = {str(resume_supported).lower()}
control_socket = "{self.socket}"
acp_tui_bin = "toad"
acp_agent_command = "agent acp"
{handoff_line}
[roles.orchestrator.tmux]
target = "tt-acp:tt-agents.0"
session = "tt-acp"
window_name = "tt-agents"
pane_id = "%1"
live = true
""",
            encoding="utf-8",
        )
        tomllib.loads(path.read_text(encoding="utf-8"))
        return path


if __name__ == "__main__":
    unittest.main()
