from __future__ import annotations

import base64
import hashlib
import json
import socket
import sqlite3
import struct
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tmux_team.app_server import AppServerClient, AppServerError, is_loopback_host
from tmux_team.cli import main

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class AppServerDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / ".tmux-team" / "team.toml"
        self.runtime = self.root / "runtime"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            f"""[team]
name = "app-server-test"
runtime_dir = "{self.runtime}"

[roles.implementer]
mode = "human_visible"
state = "active"
notify_method = "app-server-turn"
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_app_server_turn_wakes_role_without_tmux_input(self) -> None:
        with FakeAppServer() as server:
            code, out, err = self.run_cli(
                "codex",
                "bind",
                "implementer",
                "--endpoint",
                server.endpoint,
                "--thread-id",
                "thread_123",
            )
            self.assertEqual(code, 0, err)
            self.assertIn("thread_id=thread_123", out)

            with patch("socket.create_connection", server.create_connection):
                code, out, err = self.run_cli(
                    "send",
                    "--to",
                    "implementer",
                    "--from",
                    "orchestrator",
                    "--summary",
                    "fix calculator regression",
                    "--body",
                    "REAL TASK BODY SHOULD STAY IN SQLITE",
                    "--notify-method",
                    "app-server-turn",
                )

        self.assertEqual(code, 0, err)
        self.assertIn("notify: app-server turn submitted thread=thread_123 turn=turn_fake", out)
        self.assertEqual(server.methods, ["initialize", "initialized", "turn/start"])
        self.assertEqual(server.turn_start_params["threadId"], "thread_123")
        wake_text = server.turn_start_params["input"][0]["text"]
        self.assertIn("pending tmux-team inbox message", wake_text)
        self.assertIn("tmux-team --config", wake_text)
        self.assertIn("inbox next --role implementer", wake_text)
        self.assertNotIn("REAL TASK BODY SHOULD STAY IN SQLITE", wake_text)

        conn = sqlite3.connect(self.runtime / "team.sqlite")
        conn.row_factory = sqlite3.Row
        message = conn.execute("SELECT state, attempts FROM messages WHERE recipient = 'implementer'").fetchone()
        notification = conn.execute(
            "SELECT method, state, details FROM notifications ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(message["state"], "notified")
        self.assertEqual(message["attempts"], 1)
        self.assertEqual(notification["method"], "app-server-turn")
        self.assertEqual(notification["state"], "submitted")
        self.assertIn("turn_fake", notification["details"])

    def test_app_server_endpoint_must_be_loopback(self) -> None:
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("example.com"))
        self.assertFalse(is_loopback_host("192.168.1.10"))

        with self.assertRaisesRegex(AppServerError, "loopback-only"):
            AppServerClient("ws://example.com:4500").connect()

    def test_app_server_client_lists_loaded_threads(self) -> None:
        with FakeAppServer() as server:
            server.loaded_thread_ids = ["thread_a", "thread_b"]
            with (
                patch("socket.create_connection", server.create_connection),
                AppServerClient(server.endpoint) as client,
            ):
                client.initialize()
                threads = client.list_loaded_threads()

        self.assertEqual(threads, ["thread_a", "thread_b"])
        self.assertEqual(server.methods, ["initialize", "initialized", "thread/loaded/list"])

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(self.config), *args])
        return code, stdout.getvalue(), stderr.getvalue()


class FakeAppServer:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.thread_resume_params: dict[str, Any] = {}
        self.turn_start_params: dict[str, Any] = {}
        self.loaded_thread_ids: list[str] = []

    def __enter__(self) -> FakeAppServer:
        self.endpoint = "ws://127.0.0.1:1"
        self.threads: list[threading.Thread] = []
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for thread in self.threads:
            thread.join(timeout=2)

    def create_connection(self, address, timeout=None):
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(timeout)
        server_sock.settimeout(timeout)
        thread = threading.Thread(target=self._handle, args=(server_sock,), daemon=True)
        self.threads.append(thread)
        thread.start()
        return client_sock

    def _handle(self, sock: socket.socket) -> None:
        with sock:
            request = read_until(sock, b"\r\n\r\n").decode("iso-8859-1")
            key = ""
            for line in request.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            )
            sock.sendall(response.encode("ascii"))

            while True:
                try:
                    opcode, payload = recv_ws_frame(sock)
                except OSError:
                    return
                if opcode == 0x8:
                    return
                if opcode != 0x1:
                    continue
                message = json.loads(payload.decode("utf-8"))
                method = message.get("method")
                if method:
                    self.methods.append(method)
                if "id" not in message:
                    continue

                request_id = message["id"]
                if method == "initialize":
                    send_ws_json(sock, {"id": request_id, "result": {"codexHome": "/tmp/fake-codex"}})
                elif method == "thread/loaded/list":
                    send_ws_json(sock, {"id": request_id, "result": {"data": self.loaded_thread_ids}})
                elif method == "thread/resume":
                    self.thread_resume_params = message["params"]
                    send_ws_json(
                        sock, {"id": request_id, "result": {"thread": {"id": self.thread_resume_params["threadId"]}}}
                    )
                elif method == "turn/start":
                    self.turn_start_params = message["params"]
                    send_ws_json(
                        sock, {"id": request_id, "result": {"turn": {"id": "turn_fake", "status": "inProgress"}}}
                    )
                else:
                    send_ws_json(sock, {"id": request_id, "error": {"message": f"unexpected method {method}"}})


def read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def recv_ws_frame(sock: socket.socket) -> tuple[int, bytes]:
    header = recv_exact(sock, 2)
    first, second = header
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def send_ws_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    length = len(data)
    if length < 126:
        header = bytes([0x81, length])
    elif length < 65536:
        header = bytes([0x81, 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x81, 127]) + struct.pack("!Q", length)
    sock.sendall(header + data)


def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


if __name__ == "__main__":
    unittest.main()
