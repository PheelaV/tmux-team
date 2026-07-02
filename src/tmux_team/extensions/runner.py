from __future__ import annotations

import json
import logging
import secrets
import shlex
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tmux_team.config import TeamConfig
from tmux_team.extensions.manifest import ExtensionManifest, HookSpec, load_extensions
from tmux_team.store import Store

logger = logging.getLogger(__name__)


class HookError(RuntimeError):
    pass


class HookDenied(PermissionError):
    pass


@dataclass(frozen=True)
class HookResult:
    data: dict[str, Any]
    denied: bool = False
    reason: str | None = None


class HookRunner:
    def __init__(self, config: TeamConfig, manifests: tuple[ExtensionManifest, ...] | None = None):
        self.config = config
        self.manifests = manifests if manifests is not None else load_extensions(config)

    def run(
        self,
        store: Store,
        conn: sqlite3.Connection,
        event: str,
        data: dict[str, Any],
        *,
        actor: str | None = None,
        dry_run: bool = False,
    ) -> HookResult:
        current = data
        hooks = self.hooks_for(event)
        for manifest, hook in hooks:
            result = self.run_hook(store, conn, manifest, hook, event, current, actor=actor, dry_run=dry_run)
            if result.denied:
                raise HookDenied(result.reason or f"extension {manifest.id} denied {event}")
            current = result.data
        return HookResult(data=current)

    def hooks_for(self, event: str) -> list[tuple[ExtensionManifest, HookSpec]]:
        hooks: list[tuple[ExtensionManifest, HookSpec]] = []
        for manifest in self.manifests:
            for hook in manifest.hooks:
                if hook.event == event:
                    hooks.append((manifest, hook))
        return sorted(hooks, key=lambda item: (item[1].order, item[0].id, item[1].command))

    def run_hook(
        self,
        store: Store,
        conn: sqlite3.Connection,
        manifest: ExtensionManifest,
        hook: HookSpec,
        event: str,
        data: dict[str, Any],
        *,
        actor: str | None,
        dry_run: bool,
    ) -> HookResult:
        invocation_id = f"hook_{secrets.token_hex(8)}"
        payload = self.payload(invocation_id, manifest, event, data, actor=actor, dry_run=dry_run)
        command = shlex.split(hook.command)
        if not command:
            raise HookError(f"extension {manifest.id} hook command is empty")

        logger.debug(
            "running extension hook",
            extra={"extension_id": manifest.id, "event": event, "hook_mode": hook.mode},
        )
        store.record_event(
            conn,
            "extension.invoked",
            actor,
            invocation_id,
            {
                "extension_id": manifest.id,
                "event": event,
                "mode": hook.mode,
                "command": hook.command,
            },
        )
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(payload, sort_keys=True),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=manifest.path,
                timeout=hook.timeout_ms / 1000,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("extension hook failed: %s", exc)
            self.record_failure(store, conn, actor, invocation_id, manifest, event, hook, str(exc))
            if fail_closed(hook):
                raise HookError(f"extension {manifest.id} failed on {event}: {exc}") from exc
            return HookResult(data=data)

        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
            logger.warning("extension hook exited nonzero: %s", details)
            self.record_failure(store, conn, actor, invocation_id, manifest, event, hook, details)
            if fail_closed(hook):
                raise HookError(f"extension {manifest.id} failed on {event}: {details}")
            return HookResult(data=data)

        try:
            hook_output = parse_hook_output(completed.stdout)
        except ValueError as exc:
            logger.warning("extension hook returned invalid output: %s", exc)
            self.record_failure(store, conn, actor, invocation_id, manifest, event, hook, str(exc))
            if fail_closed(hook):
                raise HookError(f"extension {manifest.id} returned invalid JSON on {event}: {exc}") from exc
            return HookResult(data=data)

        if hook_output.get("ok") is False:
            reason = str(hook_output.get("reason") or hook_output.get("message") or "hook returned ok=false")
            logger.warning("extension hook returned ok=false: %s", reason)
            self.record_failure(store, conn, actor, invocation_id, manifest, event, hook, reason)
            if fail_closed(hook):
                raise HookError(f"extension {manifest.id} failed on {event}: {reason}")
            return HookResult(data=data)

        decision = str(hook_output.get("decision") or "allow").strip().lower()
        if decision == "deny":
            if not can_deny(hook):
                details = f"mode {hook.mode} cannot deny"
                self.record_failure(store, conn, actor, invocation_id, manifest, event, hook, details)
                if fail_closed(hook):
                    raise HookError(f"extension {manifest.id} returned unsupported decision on {event}: {details}")
                return HookResult(data=data)
            reason = str(hook_output.get("reason") or f"extension {manifest.id} denied {event}")
            store.record_event(
                conn,
                "extension.denied",
                actor,
                invocation_id,
                {"extension_id": manifest.id, "event": event, "reason": reason},
            )
            conn.commit()
            return HookResult(data=data, denied=True, reason=reason)

        patched = data
        patch = hook_output.get("patch")
        if patch is not None:
            if not can_patch(hook):
                details = f"mode {hook.mode} cannot patch"
                self.record_failure(store, conn, actor, invocation_id, manifest, event, hook, details)
                if fail_closed(hook):
                    raise HookError(f"extension {manifest.id} returned unsupported patch on {event}: {details}")
                return HookResult(data=data)
            if not isinstance(patch, dict):
                self.record_failure(store, conn, actor, invocation_id, manifest, event, hook, "patch must be an object")
                if fail_closed(hook):
                    raise HookError(f"extension {manifest.id} returned non-object patch on {event}")
            else:
                patched = merge_patch(data, patch)
                store.record_event(
                    conn,
                    "extension.mutated",
                    actor,
                    invocation_id,
                    {"extension_id": manifest.id, "event": event},
                )

        store.record_event(
            conn,
            "extension.completed",
            actor,
            invocation_id,
            {"extension_id": manifest.id, "event": event, "mode": hook.mode},
        )
        conn.commit()
        return HookResult(data=patched)

    def payload(
        self,
        invocation_id: str,
        manifest: ExtensionManifest,
        event: str,
        data: dict[str, Any],
        *,
        actor: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        return {
            "api_version": manifest.api_version,
            "event": event,
            "invocation_id": invocation_id,
            "extension": {
                "id": manifest.id,
                "version": manifest.version,
            },
            "team": {
                "name": self.config.name,
                "project_root": str(self.config.project_root) if self.config.project_root is not None else None,
                "runtime_dir": str(self.config.runtime_dir),
                "config_path": str(self.config.config_path) if self.config.config_path is not None else None,
            },
            "actor": actor,
            "dry_run": dry_run,
            "data": data,
        }

    def record_failure(
        self,
        store: Store,
        conn: sqlite3.Connection,
        actor: str | None,
        invocation_id: str,
        manifest: ExtensionManifest,
        event: str,
        hook: HookSpec,
        details: str,
    ) -> None:
        store.record_event(
            conn,
            "extension.failed",
            actor,
            invocation_id,
            {
                "extension_id": manifest.id,
                "event": event,
                "mode": hook.mode,
                "details": truncate(details, 2000),
            },
        )
        conn.commit()


def parse_hook_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {"ok": True}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("hook output must be a JSON object")
    return value


def fail_closed(hook: HookSpec) -> bool:
    return hook.mode in ("mutate", "decision") or ".before" in hook.event


def can_deny(hook: HookSpec) -> bool:
    return hook.mode == "decision"


def can_patch(hook: HookSpec) -> bool:
    return hook.mode == "mutate"


def merge_patch(target: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = value
    return result


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
