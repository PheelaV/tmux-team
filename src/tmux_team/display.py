from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from .store import parse_utc_datetime


def format_seconds_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def role_capabilities(row) -> dict[str, object]:
    try:
        data = json.loads(row["capabilities_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def codex_settings_summary(capabilities: dict[str, object]) -> str:
    parts: list[str] = []
    if capabilities.get("codex_yolo") is True:
        parts.append("yolo=yes")
    for key, label in (
        ("codex_profile", "profile"),
        ("codex_model", "model"),
        ("codex_reasoning_effort", "effort"),
    ):
        value = capabilities.get(key)
        if value:
            parts.append(f"{label}={value}")
    config_overrides = capabilities.get("codex_config")
    if isinstance(config_overrides, list):
        parts.append(f"config_overrides={len(config_overrides)}")
    elif config_overrides:
        parts.append("config_overrides=1")
    launch = " ".join(parts) if parts else "launch=unknown"
    return f"{launch} fast=unknown"


def watchdog_runner_display_state(row, stale_grace_seconds: int) -> str:
    if row["state"] != "running":
        return str(row["state"])
    next_run_at = row["next_run_at"]
    if not next_run_at:
        return "stale"
    stale_at = parse_utc_datetime(str(next_run_at)) + timedelta(seconds=stale_grace_seconds)
    if stale_at < datetime.now(UTC):
        return "stale"
    return "running"
