from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tmux_team.config import TeamConfig

HOOK_MODES = ("observe", "mutate", "decision")


@dataclass(frozen=True)
class HookSpec:
    event: str
    command: str
    mode: str
    timeout_ms: int
    order: int


@dataclass(frozen=True)
class ExtensionManifest:
    id: str
    name: str
    version: str
    api_version: str
    path: Path
    source: str
    hooks: tuple[HookSpec, ...]


@dataclass(frozen=True)
class ExtensionLoadError:
    path: Path
    source: str
    message: str


@dataclass(frozen=True)
class ExtensionInspection:
    manifests: tuple[ExtensionManifest, ...]
    errors: tuple[ExtensionLoadError, ...]


class ExtensionError(RuntimeError):
    pass


def inspect_extensions(config: TeamConfig) -> ExtensionInspection:
    manifests: list[ExtensionManifest] = []
    errors: list[ExtensionLoadError] = []
    if not config.extensions.enabled:
        return ExtensionInspection((), ())

    for root, source in extension_roots(config):
        if not root.exists():
            continue
        if not root.is_dir():
            errors.append(ExtensionLoadError(root, source, "extension root is not a directory"))
            continue
        for extension_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            manifest_path = extension_dir / "extension.toml"
            if not manifest_path.exists():
                continue
            try:
                manifests.append(load_manifest(manifest_path, source))
            except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
                errors.append(ExtensionLoadError(manifest_path, source, str(exc)))

    manifests.sort(key=lambda manifest: (manifest.source, manifest.id))
    return ExtensionInspection(tuple(manifests), tuple(errors))


def load_extensions(config: TeamConfig) -> tuple[ExtensionManifest, ...]:
    inspection = inspect_extensions(config)
    if inspection.errors:
        details = "; ".join(f"{error.path}: {error.message}" for error in inspection.errors)
        raise ExtensionError(f"invalid extension manifest: {details}")
    return inspection.manifests


def extension_roots(config: TeamConfig) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = []
    if config.extensions.project and config.project_root is not None:
        roots.append((config.project_root / ".tmux-team" / "extensions", "project"))
    return roots


def load_manifest(path: Path, source: str) -> ExtensionManifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_extension = data.get("extension")
    if not isinstance(raw_extension, dict):
        raise ValueError("missing [extension] table")

    extension_id = required_str(raw_extension, "id")
    version = str(raw_extension.get("version") or "0.1.0")
    api_version = str(raw_extension.get("api_version") or "1")
    name = str(raw_extension.get("name") or extension_id)

    hooks = tuple(parse_hook(raw_hook, index) for index, raw_hook in enumerate(data.get("hooks", [])))
    return ExtensionManifest(
        id=extension_id,
        name=name,
        version=version,
        api_version=api_version,
        path=path.parent,
        source=source,
        hooks=tuple(sorted(hooks, key=lambda hook: (hook.order, hook.event, hook.command))),
    )


def parse_hook(raw_hook: Any, index: int) -> HookSpec:
    if not isinstance(raw_hook, dict):
        raise ValueError(f"hooks[{index}] must be a TOML table")

    event = required_str(raw_hook, "event")
    command = required_str(raw_hook, "command")
    mode = str(raw_hook.get("mode") or "observe").strip().lower()
    if mode not in HOOK_MODES:
        raise ValueError(f"hooks[{index}].mode must be one of: {', '.join(HOOK_MODES)}")

    timeout_ms = int_value(raw_hook.get("timeout_ms", 3000), f"hooks[{index}].timeout_ms")
    if timeout_ms <= 0:
        raise ValueError(f"hooks[{index}].timeout_ms must be positive")
    order = int_value(raw_hook.get("order", 100), f"hooks[{index}].order")
    return HookSpec(event=event, command=command, mode=mode, timeout_ms=timeout_ms, order=order)


def required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required field: {key}")
    return str(value)


def int_value(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
