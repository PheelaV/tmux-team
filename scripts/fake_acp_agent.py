#!/usr/bin/env python3
"""Deterministic ACP agent used by local TUI integration tests."""

from __future__ import annotations

import json
import sys
from typing import Any

SESSION_ID = "fake-acp-session"


def send(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def respond(request_id: object, result: dict[str, Any] | None = None) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result or {}})


def main() -> int:
    for line in sys.stdin:
        request = json.loads(line)
        if not isinstance(request, dict) or "id" not in request:
            continue
        request_id = request["id"]
        method = request.get("method")
        params = request.get("params") or {}

        if method == "initialize":
            respond(
                request_id,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {
                            "audio": False,
                            "embeddedContent": False,
                            "image": False,
                        },
                    },
                    "authMethods": [],
                },
            )
        elif method == "session/new":
            respond(
                request_id,
                {
                    "sessionId": SESSION_ID,
                    "modes": {
                        "currentModeId": "agent",
                        "availableModes": [
                            {
                                "id": "agent",
                                "name": "Agent",
                                "description": "Fake writable agent mode",
                            }
                        ],
                    },
                },
            )
        elif method in ("session/load", "session/set_mode"):
            respond(request_id)
        elif method == "session/prompt":
            prompt = params.get("prompt") or []
            text = " ".join(
                str(block.get("text") or "")
                for block in prompt
                if isinstance(block, dict) and block.get("type") == "text"
            )
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": params.get("sessionId") or SESSION_ID,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"fake ACP received: {text}"},
                        },
                    },
                }
            )
            respond(request_id, {"stopReason": "end_turn"})
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unsupported method: {method}"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
