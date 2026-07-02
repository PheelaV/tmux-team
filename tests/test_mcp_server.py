from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from tmux_team.config import load_config
from tmux_team.mcp_server import ToolCallError, call_tool, list_tools, serve_json_rpc
from tmux_team.store import Store


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / ".tmux-team" / "team.toml"
        self.config.parent.mkdir(parents=True)
        runtime = self.root / "runtime"
        self.config.write_text(
            f"""[team]
name = "mcp-test"
runtime_dir = "{runtime}"

[roles.orchestrator]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:tt-agents.0"
notify_method = "app-server-turn"

[roles.implementer]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:tt-agents.1"
notify_method = "app-server-turn"
app_server_endpoint = "ws://127.0.0.1:4500"
codex_thread_id = "thread-impl"

[roles.trainer]
mode = "app_server_remote_tui"
state = "paused"
pane = "tt:tt-agents.2"
notify_method = "app-server-turn"
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_tool_lifecycle_uses_store_without_mcp_dependency(self) -> None:
        store = self.store()
        with store.connect() as conn:
            sent = call_tool(
                store,
                conn,
                "team_send",
                {
                    "to": "orchestrator",
                    "from": "collector",
                    "summary": "test failed",
                    "body": "Evidence goes here.",
                    "wake": False,
                },
            )
            message_id = sent["message"]["id"]
            self.assertEqual(sent["message"]["state"], "queued")

            claimed = call_tool(store, conn, "team_inbox_next", {"role": "orchestrator"})
            self.assertEqual(claimed["message"]["id"], message_id)
            self.assertEqual(claimed["message"]["body"], "Evidence goes here.")

            acknowledged = call_tool(store, conn, "team_ack", {"role": "orchestrator", "message_id": message_id})
            self.assertEqual(acknowledged["message"]["state"], "acknowledged")

            completed = call_tool(
                store,
                conn,
                "team_complete",
                {"role": "orchestrator", "message_id": message_id, "status": "done", "summary": "handled"},
            )
            self.assertEqual(completed["message"]["state"], "completed")
            self.assertEqual(completed["message"]["result_summary"], "handled")

            status = call_tool(store, conn, "team_status", {})
            roles = {role["name"]: role for role in status["roles"]}
            self.assertEqual(roles["orchestrator"]["counts"]["completed"], 1)

    def test_team_complete_can_reply_to_sender(self) -> None:
        store = self.store()
        with store.connect() as conn:
            sent = call_tool(
                store,
                conn,
                "team_send",
                {
                    "to": "orchestrator",
                    "from": "implementer",
                    "summary": "test failed",
                    "body": "Evidence goes here.",
                    "wake": False,
                },
            )
            message_id = sent["message"]["id"]
            call_tool(store, conn, "team_inbox_next", {"role": "orchestrator"})
            call_tool(store, conn, "team_ack", {"role": "orchestrator", "message_id": message_id})

            completed = call_tool(
                store,
                conn,
                "team_complete",
                {
                    "role": "orchestrator",
                    "message_id": message_id,
                    "status": "done",
                    "summary": "handled",
                    "reply_to_sender": True,
                    "reply_wake": False,
                },
            )

            self.assertEqual(completed["message"]["state"], "completed")
            self.assertIn("reply", completed)
            self.assertEqual(completed["reply"]["recipient"], "implementer")
            self.assertEqual(completed["reply"]["sender"], "orchestrator")
            self.assertIn("Completed message", completed["reply"]["body"])

    def test_send_default_wake_uses_app_server_only_and_reports_missing_binding(self) -> None:
        store = self.store()
        with store.connect() as conn:
            sent = call_tool(store, conn, "team_send", {"to": "orchestrator", "summary": "Wake", "body": "body"})

            self.assertEqual(sent["message"]["state"], "queued")
            self.assertFalse(sent["notification"]["ok"])
            self.assertEqual(sent["notification"]["method"], "app-server-turn")
            self.assertIn("no app-server endpoint/thread binding", sent["notification"]["details"])

    def test_notify_rejects_tmux_send_keys_surface(self) -> None:
        store = self.store()
        with store.connect() as conn, self.assertRaises(ToolCallError):
            call_tool(store, conn, "team_notify", {"role": "orchestrator", "method": "send-keys"})

    def test_team_send_uses_project_extension_hooks(self) -> None:
        extension_dir = self.root / ".tmux-team" / "extensions" / "route"
        extension_dir.mkdir(parents=True)
        (extension_dir / "extension.toml").write_text(
            f"""
[extension]
id = "example.route"
version = "0.1.0"

[[hooks]]
event = "message.before_create"
command = "{shlex.quote(sys.executable)} hook.py"
mode = "mutate"
""",
            encoding="utf-8",
        )
        (extension_dir / "hook.py").write_text(
            """
import json
import sys

payload = json.load(sys.stdin)
message = payload["data"]["message"]
message["recipient"] = "implementer"
message["summary"] = "[mcp routed] " + message["summary"]
print(json.dumps({"ok": True, "patch": {"message": message}}))
""",
            encoding="utf-8",
        )

        store = self.store()
        with store.connect() as conn:
            sent = call_tool(store, conn, "team_send", {"to": "orchestrator", "summary": "Wake", "wake": False})

            self.assertEqual(sent["message"]["recipient"], "implementer")
            self.assertEqual(sent["message"]["summary"], "[mcp routed] Wake")

    def test_json_rpc_tools_list_and_call(self) -> None:
        tool_names = {tool["name"] for tool in list_tools()}
        self.assertIn("team_status", tool_names)
        self.assertIn("team_wake", tool_names)

        store = self.store()
        with store.connect() as conn:
            input_stream = StringIO(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                + "\n"
                + json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "team_status", "arguments": {}},
                    }
                )
                + "\n"
            )
            output_stream = StringIO()

            serve_json_rpc(store, conn, input_stream, output_stream)

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(responses[0]["id"], 1)
        self.assertIn("tools", responses[0]["result"])
        self.assertEqual(responses[1]["id"], 2)
        structured = responses[1]["result"]["structuredContent"]
        self.assertEqual(structured["team"], "mcp-test")

    def store(self) -> Store:
        return Store(load_config(self.config))


if __name__ == "__main__":
    unittest.main()
