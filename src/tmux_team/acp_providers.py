from __future__ import annotations

import shlex
from dataclasses import dataclass


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
    "pool": ACPProviderPreset(
        "pool acp",
        "Install Poolside Pool from https://poolside.ai/get-started and run `pool login`.",
    ),
}


class ACPProviderError(ValueError):
    pass


def resolve_acp_provider(provider: str | None, command: str | None) -> tuple[str, str | None]:
    provider_name = provider.strip().lower() if provider else None
    command_value = command.strip() if command else ""
    if command_value:
        return command_value, provider_name

    provider_name = provider_name or "cursor"
    preset = ACP_PROVIDER_PRESETS.get(provider_name)
    if preset is None:
        raise ACPProviderError(
            f"ACP provider {provider_name!r} has no built-in command; pass --acp-agent-command explicitly"
        )
    return preset.command, provider_name


def acp_command_executable(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ACPProviderError(f"invalid ACP agent command: {exc}") from exc
    if not parts:
        raise ACPProviderError("ACP agent command is required")
    if parts[0] != "env":
        return parts[0]
    for part in parts[1:]:
        if "=" not in part:
            return part
    raise ACPProviderError("ACP agent command contains environment assignments but no executable")


def acp_provider_install_hint(provider: str | None) -> str | None:
    preset = ACP_PROVIDER_PRESETS.get(provider or "")
    return preset.install_hint if preset else None
