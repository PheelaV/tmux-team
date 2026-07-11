from __future__ import annotations

import json
import socket
import tempfile
import threading
import tomllib
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

from tmux_team.acp_tui import ACPControlError, control_socket_path, send_control_request, wait_for_acp_tui
from tmux_team.config import RoleConfig, TeamConfig
from tmux_team.store import Store


class FakeControlSocket:
    def __init__(self, path: Path, responses: list[dict[str, Any]]):
        self.path = path
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(timeout=2):
            raise RuntimeError("fake control socket did not start")

    def join(self) -> None:
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError("fake control socket did not stop")

    def _run(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            server.listen()
            self.ready.set()
            for response_values in self.responses:
                connection, _ = server.accept()
                with connection:
                    with connection.makefile("r", encoding="utf-8") as reader:
                        request = json.loads(reader.readline())
                    self.requests.append(request)
                    response = {
                        "version": request["version"],
                        "id": request["id"],
                        "ok": True,
                        **response_values,
                    }
                    connection.sendall((json.dumps(response) + "\n").encode())


class ACPTUIControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.socket_path = control_socket_path(self.root / "runtime", "implementer")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_protocol_client_sends_versioned_request(self) -> None:
        server = FakeControlSocket(
            self.socket_path,
            [{"state": "accepted", "sessionId": "session-1", "queueDepth": 0}],
        )
        server.start()

        response = send_control_request(
            self.socket_path,
            {
                "action": "prompt",
                "text": "Check the durable inbox.",
                "priority": "normal",
                "coalesceKey": "inbox",
            },
        )
        server.join()

        self.assertEqual(response["state"], "accepted")
        self.assertEqual(server.requests[0]["version"], 1)
        self.assertEqual(server.requests[0]["action"], "prompt")
        self.assertEqual(server.requests[0]["text"], "Check the durable inbox.")

    def test_readiness_uses_ping_and_status_until_idle(self) -> None:
        server = FakeControlSocket(
            self.socket_path,
            [
                {"state": "starting"},
                {"state": "starting"},
                {"state": "idle"},
                {"state": "idle", "sessionId": "session-1", "queueDepth": 0},
            ],
        )
        server.start()

        status = wait_for_acp_tui(self.socket_path, timeout=2)
        server.join()

        self.assertEqual(status["sessionId"], "session-1")
        self.assertEqual([request["action"] for request in server.requests], ["ping", "status", "ping", "status"])

    def test_structured_protocol_error_is_reported(self) -> None:
        server = FakeControlSocket(self.socket_path, [{"ok": False, "error": {"code": "not_ready", "message": "wait"}}])
        server.start()

        with self.assertRaisesRegex(ACPControlError, "not_ready: wait"):
            send_control_request(self.socket_path, {"action": "status"})
        server.join()

    def test_store_routes_compact_urgent_wake_through_control_socket(self) -> None:
        server = FakeControlSocket(
            self.socket_path,
            [{"state": "queued", "sessionId": "session-1", "queueDepth": 1}],
        )
        server.start()
        config = TeamConfig(
            name="acp-test",
            runtime_dir=self.root / "runtime",
            project_root=self.root,
            roles={
                "implementer": RoleConfig(
                    name="implementer",
                    mode="acp_tui",
                    capabilities={"notify_method": "control-socket", "control_socket": str(self.socket_path)},
                )
            },
        )
        store = Store(config)
        with closing(store.connect()) as conn:
            message = store.create_message(
                conn,
                sender="orchestrator",
                recipient="implementer",
                priority="urgent",
                summary="fix parser",
                body="SECRET TASK BODY",
            )
            ok, details = store.notify_role(conn, "implementer", "auto")
            row = conn.execute("SELECT state, attempts FROM messages WHERE id = ?", (message.id,)).fetchone()
        server.join()

        self.assertTrue(ok, details)
        self.assertEqual((row["state"], row["attempts"]), ("notified", 1))
        request = server.requests[0]
        self.assertEqual(request["priority"], "urgent")
        self.assertEqual(request["coalesceKey"], "inbox")
        self.assertIn("fix parser", request["text"])
        self.assertNotIn("SECRET TASK BODY", request["text"])

    def test_distinct_notices_use_distinct_coalescing_keys(self) -> None:
        server = FakeControlSocket(
            self.socket_path,
            [
                {"state": "queued", "sessionId": "session-1", "queueDepth": 1},
                {"state": "queued", "sessionId": "session-1", "queueDepth": 2},
            ],
        )
        server.start()
        config = TeamConfig(
            name="acp-test",
            runtime_dir=self.root / "runtime",
            project_root=self.root,
            roles={
                "implementer": RoleConfig(
                    name="implementer",
                    mode="acp_tui",
                    capabilities={"notify_method": "control-socket", "control_socket": str(self.socket_path)},
                )
            },
        )
        store = Store(config)
        with closing(store.connect()) as conn:
            store.sync_roles(conn, config.roles.values())
            first = store.notify_role(
                conn,
                "implementer",
                "control-socket",
                notice_message_id="notice-1",
                notice_summary="First checkpoint",
            )
            second = store.notify_role(
                conn,
                "implementer",
                "control-socket",
                notice_message_id="notice-2",
                notice_summary="Second checkpoint",
            )
        server.join()

        self.assertTrue(first[0])
        self.assertTrue(second[0])
        self.assertEqual(
            [request["coalesceKey"] for request in server.requests],
            ["notice:notice-1", "notice:notice-2"],
        )

    def test_acp_extra_keeps_base_python_311_compatible(self) -> None:
        pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(pyproject["project"]["requires-python"], ">=3.11")
        self.assertEqual(
            pyproject["project"]["optional-dependencies"]["acp"],
            [
                "batrachian-toad @ git+https://github.com/PheelaV/toad.git@feature/acp-config-options ; "
                "python_version >= '3.14'"
            ],
        )


if __name__ == "__main__":
    unittest.main()
