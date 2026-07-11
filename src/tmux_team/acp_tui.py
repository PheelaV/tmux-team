from __future__ import annotations

import json
import secrets
import socket
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 65_536


class ACPControlError(RuntimeError):
    pass


def control_socket_path(runtime_dir: Path, role: str) -> Path:
    return runtime_dir / "acp" / f"{role}.sock"


def send_control_request(
    socket_path: Path,
    request: dict[str, Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    path = socket_path.expanduser().resolve()
    payload = dict(request)
    payload.setdefault("version", PROTOCOL_VERSION)
    payload.setdefault("id", secrets.token_hex(8))
    request_id = payload["id"]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
            with client.makefile("r", encoding="utf-8") as reader:
                line = reader.readline(MAX_LINE_BYTES + 1)
    except OSError as exc:
        raise ACPControlError(f"ACP TUI control socket {path} is unavailable: {exc}") from exc

    if len(line.encode()) > MAX_LINE_BYTES:
        raise ACPControlError("ACP TUI control response was too large")
    try:
        response = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ACPControlError("ACP TUI control socket returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise ACPControlError("ACP TUI control response is not an object")
    if response.get("version") != PROTOCOL_VERSION:
        raise ACPControlError(f"ACP TUI returned unsupported protocol version: {response.get('version')!r}")
    if response.get("id") != request_id:
        raise ACPControlError("ACP TUI control response id did not match the request")
    if not response.get("ok"):
        error = response.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "request_failed")
            message = str(error.get("message") or "request failed")
            raise ACPControlError(f"{code}: {message}")
        raise ACPControlError(str(error or "ACP TUI control request failed"))
    return response


def wait_for_acp_tui(socket_path: Path, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            ping = send_control_request(socket_path, {"action": "ping"}, timeout=1.0)
            if ping.get("state") == "failed":
                raise ACPControlError("ACP TUI reported failed state")
            status = send_control_request(socket_path, {"action": "status"}, timeout=1.0)
            state = str(status.get("state") or "")
            if state == "failed":
                raise ACPControlError("ACP TUI reported failed state")
            if state and state != "starting":
                return status
        except ACPControlError as exc:
            last_error = exc
        time.sleep(0.2)
    raise ACPControlError(f"ACP TUI did not become ready: {last_error or 'still starting'}")
