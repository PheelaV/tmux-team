from __future__ import annotations

import unittest

from tmux_team.acp_providers import ACPProviderError, acp_command_executable, resolve_acp_provider


class ACPProviderTests(unittest.TestCase):
    def test_known_providers_resolve_standard_commands(self) -> None:
        self.assertEqual(resolve_acp_provider("cursor", None), ("agent acp", "cursor"))
        self.assertEqual(resolve_acp_provider("codex", None), ("codex-acp", "codex"))
        self.assertEqual(resolve_acp_provider("claude", None), ("claude-agent-acp", "claude"))

    def test_cursor_remains_default_when_provider_and_command_are_omitted(self) -> None:
        self.assertEqual(resolve_acp_provider(None, None), ("agent acp", "cursor"))

    def test_explicit_command_can_override_supported_provider(self) -> None:
        self.assertEqual(resolve_acp_provider("cursor", "agent --force acp"), ("agent --force acp", "cursor"))

    def test_unsupported_providers_are_rejected_with_or_without_overrides(self) -> None:
        for provider, command in (("custom", "custom-acp"), ("pool", None), (None, "env FLAG=1 /bin/pool acp")):
            with self.subTest(provider=provider, command=command), self.assertRaises(ACPProviderError):
                resolve_acp_provider(provider, command)

    def test_executable_resolution_skips_env_assignments(self) -> None:
        self.assertEqual(
            acp_command_executable("env INITIAL_AGENT_MODE=agent-full-access codex-acp"),
            "codex-acp",
        )


if __name__ == "__main__":
    unittest.main()
