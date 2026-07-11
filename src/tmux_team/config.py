from __future__ import annotations

import fcntl
import os
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from .policy import RolePolicy, TeamPolicy, parse_role_policy, parse_team_policy

DEFAULT_CONFIG_PATH = Path(".tmux-team/team.toml")
DEFAULT_RUNTIME_DIR = Path(".tmux-team/runtime")
ENV_FILE_PATH = Path(".tmux-team/team.env")
CONFIG_PATH_ENV = "TMUX_TEAM_CONFIG"
ROLE_ENV = "TMUX_TEAM_ROLE"
RUNTIME_HOME_ENV = "TMUX_TEAM_HOME"
RUNTIME_DIR_ENV = "TMUX_TEAM_RUNTIME_DIR"


@dataclass(frozen=True)
class ExtensionSettings:
    enabled: bool = True
    project: bool = True


@dataclass(frozen=True)
class RoleConfig:
    name: str
    mode: str = "human_visible"
    state: str = "active"
    pane: str | None = None
    worktree: str | None = None
    scratchpad: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    policy: RolePolicy = field(default_factory=RolePolicy)


@dataclass(frozen=True)
class OperatorConfig:
    pane: str | None = None
    codex_thread_id: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TeamConfig:
    name: str
    runtime_dir: Path
    roles: dict[str, RoleConfig]
    config_path: Path | None = None
    project_root: Path | None = None
    policy: TeamPolicy = field(default_factory=TeamPolicy)
    extensions: ExtensionSettings = field(default_factory=ExtensionSettings)
    operator: OperatorConfig = field(default_factory=OperatorConfig)


class ConfigError(RuntimeError):
    pass


def find_config(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        pointer = parent / ENV_FILE_PATH
        if pointer.exists():
            target = config_path_from_env_file(pointer)
            if target is not None:
                return target
        candidate = parent / DEFAULT_CONFIG_PATH
        if candidate.exists():
            return candidate
    return None


def load_config(
    config_path: Path | str | None = None,
    runtime_dir_override: Path | str | None = None,
    start: Path | None = None,
) -> TeamConfig:
    config_value = config_path or config_path_env()
    explicit_path = Path(config_value).expanduser() if config_value else None
    discovered_path = explicit_path or find_config(start)

    if discovered_path is None:
        project_root = (start or Path.cwd()).resolve()
        runtime_dir = resolve_runtime_dir(project_root, runtime_dir_override or runtime_dir_env())
        return TeamConfig(
            name="default",
            runtime_dir=runtime_dir,
            roles={},
            operator=OperatorConfig(),
            config_path=None,
            project_root=project_root,
            policy=TeamPolicy(),
            extensions=ExtensionSettings(),
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
    try:
        team_policy = parse_team_policy(team_data)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    try:
        extension_settings = parse_extension_settings(team_data.get("extensions"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    runtime_value = runtime_dir_override or runtime_dir_env() or team_data.get("runtime_dir")
    runtime_dir = resolve_runtime_dir(project_root, runtime_value)
    operator = parse_operator_config(data.get("operator"))

    roles: dict[str, RoleConfig] = {}
    for role_name, role_data in data.get("roles", {}).items():
        if not isinstance(role_data, dict):
            raise ConfigError(f"Role {role_name!r} must be a TOML table")
        known_keys = {"mode", "state", "pane", "worktree", "scratchpad", "policy"}
        capabilities = {key: value for key, value in role_data.items() if key not in known_keys}
        state = str(role_data.get("state") or ("paused" if role_data.get("mode") == "paused" else "active"))
        try:
            role_policy = parse_role_policy(role_data.get("policy"))
        except ValueError as exc:
            raise ConfigError(f"Invalid policy for role {role_name!r}: {exc}") from exc
        roles[str(role_name)] = RoleConfig(
            name=str(role_name),
            mode=str(role_data.get("mode") or "human_visible"),
            state=state,
            pane=_optional_str(role_data.get("pane")),
            worktree=_optional_str(role_data.get("worktree")),
            scratchpad=_optional_str(role_data.get("scratchpad")),
            capabilities=capabilities,
            policy=role_policy,
        )

    return TeamConfig(
        name=team_name,
        runtime_dir=runtime_dir,
        roles=roles,
        operator=operator,
        config_path=path,
        project_root=project_root.resolve(),
        policy=team_policy,
        extensions=extension_settings,
    )


def write_default_config(path: Path, name: str, runtime_dir: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ConfigError(f"Config already exists: {path}")
    runtime = runtime_dir or runtime_dir_env() or str(DEFAULT_RUNTIME_DIR)
    data = {
        "team": {"name": name, "runtime_dir": runtime},
        "roles": {
            "orchestrator": {
                "mode": "human_visible",
                "state": "active",
                "pane": "session:0",
                "can_edit": False,
                "can_launch_slurm": False,
            },
            "implementer": {
                "mode": "human_visible",
                "state": "active",
                "pane": "session:1",
                "can_edit": True,
                "can_launch_slurm": False,
            },
        },
    }
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def write_operator_config(path: Path, operator: OperatorConfig) -> None:
    path = path.expanduser().resolve()
    with _config_update_lock(path):
        data = _read_config_data(path)
        operator_data: dict[str, Any] = dict(operator.capabilities)
        if operator.pane:
            operator_data["pane"] = operator.pane
        if operator.codex_thread_id:
            operator_data["codex_thread_id"] = operator.codex_thread_id
        if operator_data:
            data["operator"] = operator_data
        else:
            data.pop("operator", None)
        _write_config_data_atomic(path, data)


def update_role_capabilities(path: Path, role: str, updates: Mapping[str, Any | None]) -> None:
    path = path.expanduser().resolve()
    with _config_update_lock(path):
        data = _read_config_data(path)
        roles = data.get("roles")
        if not isinstance(roles, dict) or not isinstance(roles.get(role), dict):
            raise ConfigError(f"Unknown role: {role}")
        structural_keys = {"mode", "state", "pane", "worktree", "scratchpad", "policy"}
        invalid = structural_keys.intersection(updates)
        if invalid:
            raise ConfigError(f"Not a role capability: {sorted(invalid)[0]}")
        role_data = roles[role]
        for key, value in updates.items():
            if value is None:
                role_data.pop(key, None)
            else:
                role_data[key] = value

        _write_config_data_atomic(path, data)


@contextmanager
def _config_update_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_config_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


def _write_config_data_atomic(path: Path, data: dict[str, Any]) -> None:
    mode = path.stat().st_mode & 0o777
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary_name = handle.name
            handle.write(tomli_w.dumps(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def resolve_runtime_dir(project_root: Path, value: Path | str | None) -> Path:
    if value:
        runtime_dir = Path(str(value)).expanduser()
        if not runtime_dir.is_absolute():
            runtime_dir = project_root / runtime_dir
    else:
        runtime_dir = project_root / DEFAULT_RUNTIME_DIR
    return runtime_dir.resolve()


def role_scratchpad_path(config: TeamConfig, role: str) -> Path:
    role_config = config.roles.get(role)
    if role_config is None:
        raise ConfigError(f"Unknown role: {role}")
    raw_path = role_config.scratchpad or f".tmux-team/memory/{role}.md"
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        base = config.project_root or Path.cwd()
        path = base / path
    return path.resolve()


def runtime_dir_env() -> str | None:
    return os.environ.get(RUNTIME_HOME_ENV) or os.environ.get(RUNTIME_DIR_ENV)


def config_path_env() -> str | None:
    return os.environ.get(CONFIG_PATH_ENV)


def config_path_from_env_file(path: Path) -> Path | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith(f"{CONFIG_PATH_ENV}="):
            continue
        value = line.split("=", 1)[1].strip()
        if not value:
            return None
        return Path(value).expanduser()
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def parse_operator_config(value: Any) -> OperatorConfig:
    if value is None:
        return OperatorConfig()
    if not isinstance(value, dict):
        raise ConfigError("operator must be a TOML table")
    known_keys = {"pane", "codex_thread_id"}
    return OperatorConfig(
        pane=_optional_str(value.get("pane")),
        codex_thread_id=_optional_str(value.get("codex_thread_id")),
        capabilities={key: item for key, item in value.items() if key not in known_keys},
    )


def parse_extension_settings(value: Any) -> ExtensionSettings:
    if value is None:
        return ExtensionSettings()
    if not isinstance(value, dict):
        raise ValueError("team.extensions must be a TOML table")
    return ExtensionSettings(
        enabled=_optional_bool(value, "enabled", True),
        project=_optional_bool(value, "project", True),
    )


def _optional_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"team.extensions.{key} must be a boolean")
