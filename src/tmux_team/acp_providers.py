from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ACPProviderPreset:
    command: str
    install_hint: str


ACP_PROVIDER_PRESETS = {
    "cursor": ACPProviderPreset("agent acp", "Install Cursor Agent and run `agent login`."),
    "codex": ACPProviderPreset(
        "codex-acp",
        "Install with `npm install -g @agentclientprotocol/codex-acp` and authenticate Codex.",
    ),
    "claude": ACPProviderPreset(
        "claude-agent-acp",
        "Install with `npm install -g @agentclientprotocol/claude-agent-acp` and authenticate Claude Code.",
    ),
}


class ACPProviderError(ValueError):
    pass


def resolve_acp_provider(provider: str | None, command: str | None) -> tuple[str, str]:
    command_value = command.strip() if command else ""
    executable = Path(acp_command_executable(command_value)).name if command_value else ""
    provider_name = (
        provider.strip().lower()
        if provider
        else {"codex-acp": "codex", "claude-agent-acp": "claude"}.get(executable, "cursor")
    )
    preset = ACP_PROVIDER_PRESETS.get(provider_name)
    if preset is None:
        raise ACPProviderError(f"unsupported ACP provider {provider_name!r}; choose cursor, codex, or claude")
    command_value = command_value or preset.command
    if executable in {"pool", "poolside"}:
        raise ACPProviderError("the Poolside ACP command is unsupported; choose Cursor, Codex, or Claude Code")
    return command_value, provider_name


def acp_command_executable(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ACPProviderError(f"invalid ACP agent command: {exc}") from exc
    if not parts:
        raise ACPProviderError("ACP agent command is required")
    if Path(parts[0]).name != "env":
        return parts[0]
    for part in parts[1:]:
        if "=" not in part:
            return part
    raise ACPProviderError("ACP agent command contains environment assignments but no executable")


def acp_provider_install_hint(provider: str | None) -> str | None:
    preset = ACP_PROVIDER_PRESETS.get(provider or "")
    return preset.install_hint if preset else None
