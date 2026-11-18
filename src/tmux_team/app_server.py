from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class AppServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppServerWake:
    thread_id: str
    submission_id: str
    kind: str
    status: str | None


class AppServerClient:
    def __init__(self, endpoint: str, timeout: float = 10.0):
        self.endpoint = endpoint
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._request_id = 0

    def __enter__(self) -> AppServerClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        if self.endpoint.startswith("unix://"):
            socket_path = Path(self.endpoint.removeprefix("unix://"))
            if not socket_path.is_absolute():
                raise AppServerError("app-server Unix endpoint requires an absolute socket path")
            host, path = "localhost", "/"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(self.timeout)
                sock.connect(str(socket_path))
            except BaseException:
                sock.close()
                raise
        else:
            parsed = urlparse(self.endpoint)
            if parsed.scheme != "ws":
                raise AppServerError(f"expected a Unix socket or local ws:// app-server endpoint: {self.endpoint}")
            if parsed.hostname is None:
                raise AppServerError(f"app-server endpoint is missing a host: {self.endpoint}")
            if not is_loopback_host(parsed.hostname):
                raise AppServerError(f"app-server endpoint must be loopback-only: {self.endpoint}")
            sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=self.timeout)
            host = parsed.netloc
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

        try:
            self._upgrade(sock, host, path)
        except BaseException:
            sock.close()
            raise
        self._socket = sock

    def _upgrade(self, sock: socket.socket, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = _read_http_response(sock)
        if " 101 " not in response.split("\r\n", 1)[0]:
            raise AppServerError(
                f"app-server websocket upgrade failed: {response.splitlines()[0] if response else '(empty)'}"
            )
        expected_accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode(
            "ascii"
        )
        headers = _parse_headers(response)
        if headers.get("sec-websocket-accept") != expected_accept:
            raise AppServerError("app-server websocket upgrade returned an invalid accept key")

    def close(self) -> None:
        if self._socket is None:
            return
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        try:
            self._socket.close()
        finally:
            self._socket = None

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "tmux-team",
                    "title": "tmux-team",
                    "version": __version__,
                },
                "capabilities": {
                    "experimentalApi": True,
                },
            },
        )
        self.notify("initialized")
        return result

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send_json(payload)

        while True:
            message = self._recv_json()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise AppServerError(f"{method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"{method} returned a non-object result")
            return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send_json(payload)

    def list_loaded_threads(self) -> list[str]:
        result = self.request("thread/loaded/list", {})
        data = result.get("data")
        if not isinstance(data, list):
            raise AppServerError("thread/loaded/list result did not include a data list")
        return [str(thread_id) for thread_id in data]

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        result = self.request("thread/read", {"threadId": thread_id})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise AppServerError("thread/read did not confirm the requested thread")
        return thread

    def queue_message(self, thread_id: str, text: str, client_user_message_id: str) -> AppServerWake:
        result = self.request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text, "text_elements": []}],
                "clientUserMessageId": client_user_message_id,
            },
        )
        submission = result.get("queuedSubmission")
        submission_id = submission.get("id") if isinstance(submission, dict) else None
        if not isinstance(submission_id, str) or not submission_id:
            raise AppServerError("thread/queue/add result did not include a queued submission ID")
        return AppServerWake(thread_id, submission_id, "queue", "queued")

    def start_turn(self, thread_id: str, text: str, client_user_message_id: str | None = None) -> AppServerWake:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": text,
                    "text_elements": [],
                }
            ],
        }
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        result = self.request("turn/start", params)
        turn = result.get("turn")
        if not isinstance(turn, dict):
            raise AppServerError("turn/start result did not include a turn object")
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise AppServerError("turn/start result did not include turn.id")
        status = turn.get("status")
        return AppServerWake(thread_id, turn_id, "turn", str(status) if status is not None else None)

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"), opcode=0x1)

    def _recv_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x1:
                decoded = json.loads(payload.decode("utf-8"))
                if isinstance(decoded, dict):
                    return decoded
                raise AppServerError("received non-object JSON-RPC message")
            if opcode == 0x8:
                raise AppServerError("app-server websocket closed")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        if self._socket is None:
            raise AppServerError("app-server websocket is not connected")
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length < 65536:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        if self._socket is None:
            raise AppServerError("app-server websocket is not connected")
        header = _recv_exact(self._socket, 2)
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(self._socket, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(self._socket, 8))[0]
        mask = _recv_exact(self._socket, 4) if masked else b""
        payload = _recv_exact(self._socket, length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload


def submit_app_server_wake(
    *,
    endpoint: str,
    thread_id: str,
    prompt: str,
    client_user_message_id: str,
    timeout: float = 10.0,
    urgent: bool = False,
) -> AppServerWake:
    with AppServerClient(endpoint, timeout=timeout) as client:
        client.initialize()
        if urgent:
            # On an active thread Codex steers the existing turn atomically.
            return client.start_turn(thread_id, prompt, client_user_message_id=client_user_message_id)
        return client.queue_message(thread_id, prompt, client_user_message_id)


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _read_http_response(sock: socket.socket) -> str:
    chunks: list[bytes] = []
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) > 65536:
            raise AppServerError("app-server websocket upgrade response was too large")
    return data.decode("iso-8859-1", errors="replace")


def _parse_headers(response: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in response.split("\r\n")[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise AppServerError("app-server websocket closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
