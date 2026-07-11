from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tmux_team.acp_tui import ACPControlError
from tmux_team.cli import main
from tmux_team.config import load_config, update_role_capabilities
from tmux_team.runtime_switch import (
    RuntimeSwitchError,
    prepare_runtime_handoff,
    runtime_show,
    switch_runtime,
)
from tmux_team.store import Store


class RuntimeSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.scratchpad = self.root / "memory" / "worker.md"
        self.scratchpad.parent.mkdir()
        self.scratchpad.write_text("latest durable role state\n", encoding="utf-8")
        self.socket_path = self.runtime / "acp" / "worker.sock"
        self.config_path = self.root / ".tmux-team" / "team.toml"
        self.config_path.parent.mkdir()
        self.config_path.write_text(
            f"""[team]
name = "runtime-test"
runtime_dir = "{self.runtime}"
custom_team_value = "keep-team"

[operator]
pane = "%0"
custom_operator_value = "keep-operator"

[roles.worker]
mode = "acp_tui"
state = "active"
pane = "%1"
worktree = "{self.worktree}"
scratchpad = "{self.scratchpad}"
notify_method = "control-socket"
control_socket = "{self.socket_path}"
acp_tui_bin = "toad"
acp_agent_command = "old-agent acp"
acp_provider = "old-provider"
acp_model = "old-model"
acp_effort = "high"
runtime_session_id = "old-session"
custom_role_value = "keep-role"

[roles.observer]
mode = "human_visible"
state = "active"
pane = "%2"
custom_observer_value = "keep-observer"
""",
            encoding="utf-8",
        )
        self.config = load_config(self.config_path)
        self.store = Store(self.config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_capsule_excludes_inbox_body_and_drains_after_write(self) -> None:
        with self.store.connect() as conn:
            message = self.store.create_message(
                conn,
                sender="orchestrator",
                recipient="worker",
                priority="high",
                summary="continue parser work",
                body="SECRET INBOX TASK BODY",
            )
            claimed = self.store.claim_next(conn, "worker", 300)
            self.assertEqual(claimed["id"], message.id)
            self.store.ack_message(conn, "worker", message.id)
            todo = self.store.add_todo(
                conn,
                role="worker",
                message_id=message.id,
                text="run focused test",
                actor="worker",
            )
            with patch(
                "tmux_team.runtime_switch.send_control_request",
                return_value={"state": "idle", "sessionId": "live-old"},
            ):
                handoff = prepare_runtime_handoff(
                    self.store,
                    conn,
                    "worker",
                    summary="Switch provider after preserving active work.",
                    body="Operator-only handoff detail.",
                )
            role = self.store.get_role(conn, "worker")

        capsule = handoff.read_text(encoding="utf-8")
        self.assertNotIn("SECRET INBOX TASK BODY", capsule)
        self.assertIn(message.id, capsule)
        self.assertIn("continue parser work", capsule)
        self.assertIn(todo["id"], capsule)
        self.assertIn("run focused test", capsule)
        self.assertIn("latest durable role state", capsule)
        self.assertEqual(role["state"], "draining")

    def test_switch_refuses_active_turn_without_cancel(self) -> None:
        handoff = self.write_handoff()
        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                return_value={"state": "busy", "sessionId": "old-session"},
            ),
            patch("tmux_team.runtime_switch.subprocess.run") as run,
            self.assertRaisesRegex(RuntimeSwitchError, "--cancel-active"),
        ):
            switch_runtime(
                self.store,
                conn,
                "worker",
                acp_agent_command="new-agent acp",
                handoff_file=handoff,
            )
        with self.store.connect() as conn:
            role = self.store.get_role(conn, "worker")

        run.assert_not_called()
        self.assertEqual(role["state"], "active")

    def test_switch_cancel_success_updates_config_lineage_and_sqlite(self) -> None:
        handoff = self.write_handoff()
        requests: list[str] = []

        def control_request(_socket_path, request, timeout=5.0):
            del timeout
            requests.append(request["action"])
            if request["action"] == "status":
                return {"state": "asking", "sessionId": "live-old-session"}
            if request["action"] == "cancel":
                return {"submitted": True}
            return {"state": "accepted", "sessionId": "new-session"}

        completed = subprocess.CompletedProcess([], 0, "", "")
        with self.store.connect() as conn:
            with (
                patch("tmux_team.runtime_switch.send_control_request", side_effect=control_request),
                patch(
                    "tmux_team.runtime_switch.wait_for_idle",
                    return_value={"state": "idle", "sessionId": "live-old-session"},
                ) as wait_idle,
                patch(
                    "tmux_team.runtime_switch.wait_for_acp_tui",
                    return_value={"state": "idle", "sessionId": "new-session"},
                ),
                patch("tmux_team.runtime_switch.subprocess.run", return_value=completed) as run,
            ):
                result = switch_runtime(
                    self.store,
                    conn,
                    "worker",
                    acp_agent_command="new-agent --model new-model acp",
                    handoff_file=handoff,
                    provider="new-provider",
                    model="new-model",
                    effort="xhigh",
                    cancel_active=True,
                    tmux_bin="tmux-test",
                )
            role = self.store.get_role(conn, "worker")
            event = conn.execute(
                "SELECT payload_json FROM events WHERE type = 'role.runtime_switched' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        wait_idle.assert_called_once()
        self.assertEqual(requests, ["status", "cancel", "prompt"])
        command = run.call_args.args[0]
        self.assertEqual(
            command[:7],
            ("tmux-test", "respawn-pane", "-k", "-t", "%1", "-c", str(self.worktree.resolve())),
        )
        self.assertIn("new-agent --model new-model acp", command[7])
        self.assertEqual(result.old_session_id, "live-old-session")
        self.assertEqual(result.new_session_id, "new-session")
        self.assertEqual(role["state"], "active")
        capabilities = json.loads(role["capabilities_json"])
        self.assertEqual(capabilities["runtime_session_id"], "new-session")
        self.assertEqual(capabilities["previous_runtime_session_id"], "live-old-session")
        self.assertEqual(capabilities["acp_provider"], "new-provider")
        self.assertEqual(capabilities["acp_model"], "new-model")
        self.assertEqual(capabilities["acp_effort"], "xhigh")
        self.assertEqual(capabilities["last_handoff_file"], str(handoff.resolve()))

        data = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["team"]["custom_team_value"], "keep-team")
        self.assertEqual(data["operator"]["custom_operator_value"], "keep-operator")
        self.assertEqual(data["roles"]["worker"]["custom_role_value"], "keep-role")
        self.assertEqual(data["roles"]["observer"]["custom_observer_value"], "keep-observer")
        lineage_path = self.runtime / "handoffs" / "worker" / "lineage.jsonl"
        lineage = json.loads(lineage_path.read_text(encoding="utf-8").strip())
        self.assertEqual(lineage["old"]["session_id"], "live-old-session")
        self.assertEqual(lineage["new"]["session_id"], "new-session")
        self.assertEqual(json.loads(event["payload_json"])["handoff_file"], str(handoff.resolve()))

    def test_dry_run_print_plan_without_mutation(self) -> None:
        handoff = self.write_handoff()
        original_config = self.config_path.read_bytes()
        with self.store.connect() as conn:
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            with (
                patch("tmux_team.runtime_switch.send_control_request") as control,
                patch("tmux_team.runtime_switch.subprocess.run") as run,
            ):
                result = switch_runtime(
                    self.store,
                    conn,
                    "worker",
                    acp_agent_command="new-agent acp",
                    handoff_file=handoff,
                    provider="new-provider",
                    dry_run=True,
                )
            role = self.store.get_role(conn, "worker")
            final_event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        self.assertTrue(result.dry_run)
        control.assert_not_called()
        run.assert_not_called()
        self.assertEqual(role["state"], "active")
        self.assertEqual(final_event_count, event_count)
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertFalse((self.runtime / "handoffs" / "worker" / "lineage.jsonl").exists())

    def test_failure_after_respawn_leaves_role_draining(self) -> None:
        handoff = self.write_handoff()
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                return_value={"state": "idle", "sessionId": "old-session"},
            ),
            patch("tmux_team.runtime_switch.subprocess.run", return_value=completed),
            patch(
                "tmux_team.runtime_switch.wait_for_acp_tui",
                side_effect=ACPControlError("replacement unavailable"),
            ),
            self.assertRaisesRegex(ACPControlError, "replacement unavailable"),
        ):
            switch_runtime(
                self.store,
                conn,
                "worker",
                acp_agent_command="new-agent acp",
                handoff_file=handoff,
            )
        with self.store.connect() as conn:
            role = self.store.get_role(conn, "worker")

        self.assertEqual(role["state"], "draining")
        data = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["worker"]["runtime_session_id"], "old-session")

    def test_config_capability_update_preserves_unrelated_values(self) -> None:
        update_role_capabilities(
            self.config_path,
            "worker",
            {
                "acp_agent_command": "replacement acp",
                "runtime_session_id": "replacement-session",
                "acp_model": None,
            },
        )

        data = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["team"]["custom_team_value"], "keep-team")
        self.assertEqual(data["operator"]["custom_operator_value"], "keep-operator")
        self.assertEqual(data["roles"]["worker"]["custom_role_value"], "keep-role")
        self.assertEqual(data["roles"]["observer"]["custom_observer_value"], "keep-observer")
        self.assertEqual(data["roles"]["worker"]["acp_agent_command"], "replacement acp")
        self.assertEqual(data["roles"]["worker"]["runtime_session_id"], "replacement-session")
        self.assertNotIn("acp_model", data["roles"]["worker"])

    def test_runtime_show_supports_non_acp_role(self) -> None:
        with self.store.connect() as conn:
            output = runtime_show(self.store, conn, "observer")

        self.assertIn("observer state=active mode=human_visible", output)
        self.assertIn("provider: -", output)
        code, stdout, stderr = self.run_main("runtime", "show", "observer")
        self.assertEqual(code, 0, stderr)
        self.assertIn("observer state=active mode=human_visible", stdout)

    def test_runtime_switch_uses_role_state_policy(self) -> None:
        handoff = self.write_handoff()
        code, _stdout, stderr = self.run_main(
            "--actor",
            "observer",
            "runtime",
            "switch",
            "worker",
            "--acp-agent-command",
            "new-agent acp",
            "--handoff-file",
            str(handoff),
            "--dry-run",
        )

        self.assertEqual(code, 2)
        self.assertIn("not authorized to change role state", stderr)

    def write_handoff(self) -> Path:
        path = self.runtime / "handoffs" / "worker" / "handoff.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# existing handoff\n", encoding="utf-8")
        return path

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(self.config_path), *args])
        return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
