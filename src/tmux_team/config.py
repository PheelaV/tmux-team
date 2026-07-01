from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(".tmux-team/team.toml")
DEFAULT_RUNTIME_DIR = Path(".tmux-team/runtime")


@dataclass(frozen=True)
class RoleConfig:
    name: str
    mode: str = "human_visible"
    state: str = "active"
    pane: str | None = None
    worktree: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TeamConfig:
    name: str
    runtime_dir: Path
    roles: dict[str, RoleConfig]
    config_path: Path | None = None
    project_root: Path | None = None


class ConfigError(RuntimeError):
    pass


def find_config(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        candidate = parent / DEFAULT_CONFIG_PATH
        if candidate.exists():
            return candidate
    return None


def load_config(
    config_path: Path | str | None = None,
    runtime_dir_override: Path | str | None = None,
    start: Path | None = None,
) -> TeamConfig:
    explicit_path = Path(config_path).expanduser() if config_path else None
    discovered_path = explicit_path or find_config(start)

    if discovered_path is None:
        project_root = (start or Path.cwd()).resolve()
        runtime_dir = (
            Path(runtime_dir_override).expanduser() if runtime_dir_override else project_root / DEFAULT_RUNTIME_DIR
        )
        return TeamConfig(
            name="default",
            runtime_dir=runtime_dir.resolve(),
            roles={},
            config_path=None,
            project_root=project_root,
        )

    path = discovered_path.resolve()
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    project_root = path.parent.parent if path.parent.name == ".tmux-team" else path.parent
    team_data = data.get("team", {})
    team_name = str(team_data.get("name") or "default")

    runtime_value = runtime_dir_override or os.environ.get("TMUX_TEAM_RUNTIME_DIR") or team_data.get("runtime_dir")
    if runtime_value:
        runtime_dir = Path(str(runtime_value)).expanduser()
        if not runtime_dir.is_absolute():
            runtime_dir = project_root / runtime_dir
    else:
        runtime_dir = project_root / DEFAULT_RUNTIME_DIR

    roles: dict[str, RoleConfig] = {}
    for role_name, role_data in data.get("roles", {}).items():
        if not isinstance(role_data, dict):
            raise ConfigError(f"Role {role_name!r} must be a TOML table")
        known_keys = {"mode", "state", "pane", "worktree"}
        capabilities = {key: value for key, value in role_data.items() if key not in known_keys}
        state = str(role_data.get("state") or ("paused" if role_data.get("mode") == "paused" else "active"))
        roles[str(role_name)] = RoleConfig(
            name=str(role_name),
            mode=str(role_data.get("mode") or "human_visible"),
            state=state,
            pane=_optional_str(role_data.get("pane")),
            worktree=_optional_str(role_data.get("worktree")),
            capabilities=capabilities,
        )

    return TeamConfig(
        name=team_name,
        runtime_dir=runtime_dir.resolve(),
        roles=roles,
        config_path=path,
        project_root=project_root.resolve(),
    )


def write_default_config(path: Path, name: str, runtime_dir: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ConfigError(f"Config already exists: {path}")
    runtime = runtime_dir or ".tmux-team/runtime"
    text = f"""[team]
name = "{_toml_string(name)}"
runtime_dir = "{_toml_string(runtime)}"

[roles.orchestrator]
mode = "human_visible"
state = "active"
pane = "session:0"
can_edit = false
can_launch_slurm = false

[roles.implementer]
mode = "human_visible"
state = "active"
pane = "session:1"
can_edit = true
can_launch_slurm = false
"""
    path.write_text(text, encoding="utf-8")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
