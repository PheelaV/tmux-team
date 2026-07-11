from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tmux_team.acp_tui import ACPControlError
from tmux_team.cli import main
from tmux_team.config import _config_update_lock, load_config, update_role_capabilities
from tmux_team.runtime_switch import (
    HANDOFF_BODY_CHARS,
    RuntimeSwitchError,
    _runtime_role_lock,
    configure_runtime_options,
    format_runtime_options,
    prepare_runtime_handoff,
    quiesce_runtime_session,
    runtime_options,
    runtime_show,
    switch_runtime,
)
from tmux_team.store import Store


def select_option(
    config_id: str,
    current: str,
    values: tuple[str, ...],
    *,
    category: str,
) -> dict:
    return {
        "id": config_id,
        "name": config_id.title(),
        "category": category,
        "type": "select",
        "currentValue": current,
        "options": [{"value": value, "name": value.title()} for value in values],
    }


def boolean_option(config_id: str, current: bool, *, category: str) -> dict:
    return {
        "id": config_id,
        "name": config_id.title(),
        "category": category,
        "type": "boolean",
        "currentValue": current,
    }


def advertised_options(
    *,
    model: str = "small",
    effort: str = "high",
    effort_values: tuple[str, ...] = ("low", "high"),
    brave: bool = False,
) -> list[dict]:
    return [
        select_option("model-choice", model, ("small", "large"), category="model"),
        select_option("reasoning", effort, effort_values, category="thought_level"),
        select_option("session-mode", "code", ("code", "ask"), category="mode"),
        select_option("format", "balanced", ("balanced", "strict"), category="model_config"),
        boolean_option("brave", brave, category="future_safety"),
    ]


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

    def test_runtime_options_formats_unknown_categories_and_grouped_choices(
        self,
    ) -> None:
        grouped = {
            "id": "deployment",
            "name": "Deployment",
            "category": "future_provider_category",
            "type": "select",
            "currentValue": "safe",
            "options": [
                {
                    "id": "cloud",
                    "name": "Cloud",
                    "options": [
                        {"value": "fast", "name": "Fast"},
                        {"value": "safe", "name": "Safe"},
                    ],
                }
            ],
        }
        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                return_value={
                    "sessionId": "old-session",
                    "configOptions": [grouped],
                },
            ) as control,
        ):
            result = runtime_options(self.store, conn, "worker")

        control.assert_called_once_with(
            self.socket_path.resolve(),
            {"action": "configOptions", "sessionId": "old-session"},
        )
        output = format_runtime_options(result)
        self.assertIn("id=deployment", output)
        self.assertIn("category=future_provider_category", output)
        self.assertIn("type=select", output)
        self.assertIn('current="safe"', output)
        self.assertIn('"id":"cloud"', output)
        self.assertIn('"value":"fast"', output)

        with self.store.connect() as conn, self.assertRaisesRegex(RuntimeSwitchError, "requires an acp_tui role"):
            runtime_options(self.store, conn, "observer")

    def test_configure_parses_values_replaces_full_state_and_persists_events(
        self,
    ) -> None:
        initial = advertised_options()
        after_model = advertised_options(model="large", effort="low", effort_values=("low", "xhigh"))
        after_effort = advertised_options(model="large", effort="xhigh", effort_values=("low", "xhigh"))
        after_boolean = advertised_options(
            model="large",
            effort="xhigh",
            effort_values=("low", "xhigh"),
            brave=True,
        )
        responses = [
            {"sessionId": "old-session", "configOptions": initial},
            {
                "state": "idle",
                "sessionId": "old-session",
                "queueDepth": 0,
                "acceptingPrompts": True,
            },
            {"sessionId": "old-session", "configOptions": after_model},
            {"sessionId": "old-session", "configOptions": after_effort},
            {"sessionId": "old-session", "configOptions": after_boolean},
        ]
        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                side_effect=responses,
            ) as control,
        ):
            result = configure_runtime_options(
                self.store,
                conn,
                "worker",
                ("model-choice=large", "reasoning=xhigh", "brave=true"),
                actor="orchestrator",
            )
            role = self.store.get_role(conn, "worker")
            events = conn.execute(
                """
                SELECT payload_json
                FROM events
                WHERE type = 'role.runtime_config_changed'
                ORDER BY id
                """
            ).fetchall()

        self.assertEqual(
            [call.args[1] for call in control.call_args_list],
            [
                {"action": "configOptions", "sessionId": "old-session"},
                {"action": "status", "sessionId": "old-session"},
                {
                    "action": "setConfig",
                    "sessionId": "old-session",
                    "configId": "model-choice",
                    "value": "large",
                },
                {
                    "action": "setConfig",
                    "sessionId": "old-session",
                    "configId": "reasoning",
                    "value": "xhigh",
                },
                {
                    "action": "setConfig",
                    "sessionId": "old-session",
                    "configId": "brave",
                    "value": True,
                },
            ],
        )
        self.assertEqual(
            result.changes,
            (
                ("model-choice", "large"),
                ("reasoning", "xhigh"),
                ("brave", True),
            ),
        )
        expected_config = {option["id"]: option["currentValue"] for option in after_boolean}
        capabilities = json.loads(role["capabilities_json"])
        self.assertEqual(capabilities["acp_config"], expected_config)
        self.assertEqual(capabilities["acp_model"], "large")
        self.assertEqual(capabilities["acp_effort"], "xhigh")
        self.assertEqual(capabilities["acp_mode"], "code")
        self.assertNotIn("model_config", capabilities)
        persisted = tomllib.loads(self.config_path.read_text(encoding="utf-8"))["roles"]["worker"]
        self.assertEqual(persisted["acp_config"], expected_config)
        self.assertEqual(len(events), 3)
        first_event = json.loads(events[0]["payload_json"])
        self.assertEqual(first_event["configId"], "model-choice")
        self.assertEqual(first_event["old"]["reasoning"], "high")
        self.assertEqual(first_event["new"]["reasoning"], "low")
        lineage = [
            json.loads(line)
            for line in (self.runtime / "handoffs" / "worker" / "lineage.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(lineage), 3)
        self.assertEqual(lineage[2]["requested_value"], True)
        self.assertEqual(lineage[2]["event"], "config_changed")

    def test_configure_rejects_unknown_and_invalid_values_before_mutation(
        self,
    ) -> None:
        for assignment, message in (
            ("missing=value", "unknown config option"),
            ("model-choice=invented", "invalid value"),
            ("brave=True", "expected true or false"),
            ("brave=on", "expected true or false"),
        ):
            with self.subTest(assignment=assignment):
                requests: list[dict] = []

                def control(_socket_path, request, requests=requests):
                    requests.append(request)
                    if request["action"] == "configOptions":
                        return {
                            "sessionId": "old-session",
                            "configOptions": advertised_options(),
                        }
                    return {
                        "state": "idle",
                        "sessionId": "old-session",
                        "queueDepth": 0,
                        "acceptingPrompts": True,
                    }

                with (
                    self.store.connect() as conn,
                    patch(
                        "tmux_team.runtime_switch.send_control_request",
                        side_effect=control,
                    ),
                    self.assertRaisesRegex(RuntimeSwitchError, message),
                ):
                    configure_runtime_options(self.store, conn, "worker", (assignment,))
                self.assertEqual(
                    [request["action"] for request in requests],
                    ["configOptions", "status"],
                )

    def test_configure_rejects_non_idle_quiesced_and_changed_sessions(
        self,
    ) -> None:
        for status in (
            {"state": "busy", "acceptingPrompts": True},
            {"state": "asking", "acceptingPrompts": True},
            {"state": "starting", "acceptingPrompts": True},
            {"state": "failed", "acceptingPrompts": True},
            {"state": "idle", "acceptingPrompts": False},
        ):
            with self.subTest(status=status):
                responses = [
                    {
                        "sessionId": "old-session",
                        "configOptions": advertised_options(),
                    },
                    {
                        **status,
                        "sessionId": "old-session",
                        "queueDepth": 0,
                    },
                ]
                with (
                    self.store.connect() as conn,
                    patch(
                        "tmux_team.runtime_switch.send_control_request",
                        side_effect=responses,
                    ) as control,
                    self.assertRaises(RuntimeSwitchError),
                ):
                    configure_runtime_options(self.store, conn, "worker", ("model-choice=large",))
                self.assertEqual(control.call_count, 2)

        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                return_value={
                    "sessionId": "different-session",
                    "configOptions": advertised_options(),
                },
            ) as control,
            self.assertRaisesRegex(RuntimeSwitchError, "runtime session changed"),
        ):
            configure_runtime_options(self.store, conn, "worker", ("model-choice=large",))
        self.assertEqual(control.call_count, 1)

    def test_configure_rejects_changed_session_or_unconfirmed_value(self) -> None:
        for set_response, message in (
            (
                {
                    "sessionId": "different-session",
                    "configOptions": advertised_options(model="large"),
                },
                "runtime session changed",
            ),
            (
                {
                    "sessionId": "old-session",
                    "configOptions": advertised_options(),
                },
                "did not confirm",
            ),
        ):
            with self.subTest(message=message):
                responses = [
                    {
                        "sessionId": "old-session",
                        "configOptions": advertised_options(),
                    },
                    {
                        "state": "idle",
                        "sessionId": "old-session",
                        "queueDepth": 0,
                        "acceptingPrompts": True,
                    },
                    set_response,
                ]
                with (
                    self.store.connect() as conn,
                    patch(
                        "tmux_team.runtime_switch.send_control_request",
                        side_effect=responses,
                    ),
                    self.assertRaisesRegex(RuntimeSwitchError, message),
                ):
                    configure_runtime_options(
                        self.store,
                        conn,
                        "worker",
                        ("model-choice=large",),
                    )
                persisted = tomllib.loads(self.config_path.read_text(encoding="utf-8"))["roles"]["worker"]
                self.assertNotIn("acp_config", persisted)

    def test_configure_partial_success_persists_last_confirmation(self) -> None:
        initial = advertised_options()
        confirmed = advertised_options(model="large", effort="low")
        responses = [
            {"sessionId": "old-session", "configOptions": initial},
            {
                "state": "idle",
                "sessionId": "old-session",
                "queueDepth": 0,
                "acceptingPrompts": True,
            },
            {"sessionId": "old-session", "configOptions": confirmed},
            ACPControlError("provider rejected later option"),
        ]
        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                side_effect=responses,
            ),
            self.assertRaisesRegex(ACPControlError, "provider rejected later option"),
        ):
            configure_runtime_options(
                self.store,
                conn,
                "worker",
                ("model-choice=large", "brave=true"),
            )

        with self.store.connect() as conn:
            role = self.store.get_role(conn, "worker")
            event_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM events
                WHERE type = 'role.runtime_config_changed'
                """
            ).fetchone()[0]
        capabilities = json.loads(role["capabilities_json"])
        self.assertEqual(capabilities["acp_model"], "large")
        self.assertEqual(capabilities["acp_effort"], "low")
        self.assertFalse(capabilities["acp_config"]["brave"])
        self.assertEqual(event_count, 1)
        lineage = (self.runtime / "handoffs" / "worker" / "lineage.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(lineage.splitlines()), 1)

    def test_runtime_configure_cli_and_authorization(self) -> None:
        responses = [
            {
                "sessionId": "old-session",
                "configOptions": advertised_options(),
            },
            {
                "state": "idle",
                "sessionId": "old-session",
                "queueDepth": 0,
                "acceptingPrompts": True,
            },
            {
                "sessionId": "old-session",
                "configOptions": advertised_options(brave=True),
            },
        ]
        with patch("tmux_team.runtime_switch.send_control_request", side_effect=responses):
            code, stdout, stderr = self.run_main("runtime", "configure", "worker", "--set", "brave=true")
        self.assertEqual(code, 0, stderr)
        self.assertIn("worker session_id=old-session configured=1", stdout)

        with patch("tmux_team.runtime_switch.send_control_request") as control:
            code, _stdout, stderr = self.run_main(
                "--actor",
                "observer",
                "runtime",
                "configure",
                "worker",
                "--set",
                "brave=false",
            )
        self.assertEqual(code, 2)
        self.assertIn("not authorized to change role state", stderr)
        control.assert_not_called()

        with patch(
            "tmux_team.runtime_switch.send_control_request",
            return_value={
                "sessionId": "old-session",
                "configOptions": advertised_options(brave=True),
            },
        ):
            code, stdout, stderr = self.run_main("--actor", "observer", "runtime", "options", "worker")
        self.assertEqual(code, 0, stderr)
        self.assertIn("id=brave", stdout)

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
        self.assertIn("- Role state: draining", capsule)
        self.assertEqual(role["state"], "draining")
        with self.store.connect() as conn:
            event = conn.execute(
                "SELECT payload_json FROM events WHERE type = 'role.runtime_handoff_prepared' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(json.loads(event["payload_json"])["source_session_id"], "live-old")

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
        self.assertEqual(role["state"], "draining")

    def test_switch_cancel_success_updates_config_lineage_and_sqlite(self) -> None:
        handoff = self.write_handoff()
        requests: list[str] = []

        def control_request(_socket_path, request, timeout=5.0):
            del timeout
            requests.append(request["action"])
            if request["action"] == "status":
                return {"state": "asking", "sessionId": "old-session"}
            if request["action"] == "cancel":
                return {"submitted": True}
            return {"state": "accepted", "sessionId": "new-session"}

        completed = subprocess.CompletedProcess([], 0, "", "")
        with self.store.connect() as conn:
            with (
                patch("tmux_team.runtime_switch.send_control_request", side_effect=control_request),
                patch(
                    "tmux_team.runtime_switch.wait_for_idle",
                    return_value={"state": "idle", "sessionId": "old-session"},
                ) as wait_idle,
                patch("tmux_team.runtime_switch.quiesce_runtime_session"),
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
        self.assertEqual(result.old_session_id, "old-session")
        self.assertEqual(result.new_session_id, "new-session")
        self.assertEqual(role["state"], "active")
        capabilities = json.loads(role["capabilities_json"])
        self.assertEqual(capabilities["runtime_session_id"], "new-session")
        self.assertEqual(capabilities["previous_runtime_session_id"], "old-session")
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
        self.assertEqual(lineage["old"]["session_id"], "old-session")
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
        self.assertEqual(role["state"], "draining")
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
            patch("tmux_team.runtime_switch.quiesce_runtime_session"),
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

    def test_config_capability_update_waits_for_exclusive_lock(self) -> None:
        completed = threading.Event()

        def update() -> None:
            update_role_capabilities(self.config_path, "observer", {"runtime_session_id": "observer-new"})
            completed.set()

        with _config_update_lock(self.config_path.resolve()):
            thread = threading.Thread(target=update)
            thread.start()
            time.sleep(0.05)
            self.assertFalse(completed.is_set())
        thread.join(timeout=2)

        self.assertTrue(completed.is_set())
        data = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["observer"]["runtime_session_id"], "observer-new")
        self.assertEqual(data["roles"]["worker"]["runtime_session_id"], "old-session")

    def test_runtime_configure_waits_for_role_operation_lock(self) -> None:
        completed = threading.Event()

        def acquire() -> None:
            with _runtime_role_lock(self.runtime, "worker"):
                completed.set()

        with _runtime_role_lock(self.runtime, "worker"):
            thread = threading.Thread(target=acquire)
            thread.start()
            time.sleep(0.05)
            self.assertFalse(completed.is_set())
        thread.join(timeout=2)

        self.assertTrue(completed.is_set())

    def test_switch_rejects_tampered_or_cross_role_handoff(self) -> None:
        handoff = self.write_handoff()
        handoff.write_text("# modified after prepare\n", encoding="utf-8")

        with self.store.connect() as conn, self.assertRaisesRegex(RuntimeSwitchError, "changed after preparation"):
            switch_runtime(
                self.store,
                conn,
                "worker",
                acp_agent_command="new-agent acp",
                handoff_file=handoff,
                dry_run=True,
            )

        unrelated = self.runtime / "handoffs" / "other" / "handoff.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("# other role\n", encoding="utf-8")
        with self.store.connect() as conn, self.assertRaisesRegex(RuntimeSwitchError, "not a prepared capsule"):
            switch_runtime(
                self.store,
                conn,
                "worker",
                acp_agent_command="new-agent acp",
                handoff_file=unrelated,
                dry_run=True,
            )

    def test_switch_rejects_stale_prepared_handoff(self) -> None:
        stale = self.write_handoff(summary="first")
        latest = self.write_handoff(summary="second")
        self.assertNotEqual(stale, latest)

        with self.store.connect() as conn, self.assertRaisesRegex(RuntimeSwitchError, "handoff is stale"):
            switch_runtime(
                self.store,
                conn,
                "worker",
                acp_agent_command="new-agent acp",
                handoff_file=stale,
                dry_run=True,
            )

    def test_prepare_rejects_oversized_body_before_state_change(self) -> None:
        with self.store.connect() as conn, self.assertRaisesRegex(RuntimeSwitchError, "handoff body exceeds"):
            prepare_runtime_handoff(
                self.store,
                conn,
                "worker",
                summary="oversized",
                body="x" * (HANDOFF_BODY_CHARS + 1),
            )
        with self.store.connect() as conn:
            self.assertEqual(self.store.get_role(conn, "worker")["state"], "active")

    def test_cli_rejects_oversized_body_file(self) -> None:
        body_path = self.root / "oversized.md"
        body_path.write_text("x" * (HANDOFF_BODY_CHARS + 1), encoding="utf-8")

        code, _stdout, stderr = self.run_main(
            "runtime",
            "prepare",
            "worker",
            "--summary",
            "oversized",
            "--body-file",
            str(body_path),
        )

        self.assertEqual(code, 2)
        self.assertIn("handoff body exceeds", stderr)

    def test_quiesce_rejects_newly_queued_turn(self) -> None:
        with (
            patch(
                "tmux_team.runtime_switch.send_control_request",
                return_value={
                    "state": "idle",
                    "sessionId": "old-session",
                    "queueDepth": 1,
                    "acceptingPrompts": False,
                },
            ),
            self.assertRaisesRegex(RuntimeSwitchError, "queued prompt"),
        ):
            quiesce_runtime_session(self.socket_path, role="worker", expected_session_id="old-session")

    def test_switch_does_not_respawn_if_turn_starts_during_quiescence_check(self) -> None:
        handoff = self.write_handoff()
        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                side_effect=[
                    {"state": "idle", "sessionId": "old-session", "queueDepth": 0},
                    {
                        "state": "busy",
                        "sessionId": "old-session",
                        "queueDepth": 0,
                        "acceptingPrompts": False,
                    },
                ],
            ),
            patch("tmux_team.runtime_switch.subprocess.run") as run,
            self.assertRaisesRegex(RuntimeSwitchError, "state=busy"),
        ):
            switch_runtime(
                self.store,
                conn,
                "worker",
                acp_agent_command="new-agent acp",
                handoff_file=handoff,
            )

        run.assert_not_called()

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

    def write_handoff(self, *, summary: str = "Prepared handoff") -> Path:
        with (
            self.store.connect() as conn,
            patch(
                "tmux_team.runtime_switch.send_control_request",
                return_value={"state": "idle", "sessionId": "old-session", "queueDepth": 0},
            ),
        ):
            return prepare_runtime_handoff(self.store, conn, "worker", summary=summary)

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(self.config_path), *args])
        return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
