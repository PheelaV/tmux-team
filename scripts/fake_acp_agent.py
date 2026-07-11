#!/usr/bin/env python3
"""Deterministic ACP agent used by local TUI integration tests."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

SESSION_ID = os.environ.get("FAKE_ACP_SESSION_ID", "fake-acp-session")


def config_options(model: str, effort: str, mode: str, fast: bool) -> list[dict[str, Any]]:
    efforts = ["low", "medium", "high"] if model == "fake-small" else ["high", "xhigh"]
    if effort not in efforts:
        effort = efforts[0]
    return [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": model,
            "options": [
                {"value": "fake-small", "name": "Fake Small"},
                {"value": "fake-large", "name": "Fake Large"},
            ],
        },
        {
            "id": "reasoning_effort",
            "name": "Reasoning effort",
            "category": "thought_level",
            "type": "select",
            "currentValue": effort,
            "options": [{"value": value, "name": value.title()} for value in efforts],
        },
        {
            "id": "mode",
            "name": "Mode",
            "category": "mode",
            "type": "select",
            "currentValue": mode,
            "options": [
                {"value": "agent", "name": "Agent"},
                {"value": "read-only", "name": "Read-only"},
            ],
        },
        {
            "id": "fast-mode",
            "name": "Fast mode",
            "category": "model_config",
            "type": "boolean",
            "currentValue": fast,
        },
    ]


def send(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def respond(request_id: object, result: dict[str, Any] | None = None) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result or {}})


def main() -> int:
    model = "fake-small"
    effort = "medium"
    mode = "agent"
    fast = False
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
                    "configOptions": config_options(model, effort, mode, fast),
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
        elif method == "session/load":
            respond(request_id, {"configOptions": config_options(model, effort, mode, fast)})
        elif method == "session/set_mode":
            mode = str(params.get("modeId") or mode)
            respond(request_id)
        elif method == "session/set_config_option":
            config_id = params.get("configId")
            value = params.get("value")
            if config_id == "model" and value in ("fake-small", "fake-large"):
                model = value
                effort = "medium" if model == "fake-small" else "high"
            elif config_id == "reasoning_effort" and isinstance(value, str):
                effort = value
            elif config_id == "mode" and value in ("agent", "read-only"):
                mode = value
            elif config_id == "fast-mode" and isinstance(value, bool):
                fast = value
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": f"invalid config option: {config_id}"},
                    }
                )
                continue
            respond(
                request_id,
                {"configOptions": config_options(model, effort, mode, fast)},
            )
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
