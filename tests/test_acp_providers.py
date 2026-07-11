from __future__ import annotations

import unittest

from tmux_team.acp_providers import ACPProviderError, acp_command_executable, resolve_acp_provider


class ACPProviderTests(unittest.TestCase):
    def test_known_providers_resolve_standard_commands(self) -> None:
        self.assertEqual(resolve_acp_provider("cursor", None), ("agent acp", "cursor"))
        self.assertEqual(resolve_acp_provider("codex", None), ("codex-acp", "codex"))
        self.assertEqual(resolve_acp_provider("claude", None), ("claude-agent-acp", "claude"))
        self.assertEqual(resolve_acp_provider("pool", None), ("pool acp", "pool"))

    def test_cursor_remains_default_when_provider_and_command_are_omitted(self) -> None:
        self.assertEqual(resolve_acp_provider(None, None), ("agent acp", "cursor"))

    def test_explicit_command_supports_custom_provider(self) -> None:
        self.assertEqual(resolve_acp_provider("custom", "custom-acp --stdio"), ("custom-acp --stdio", "custom"))

    def test_unknown_provider_requires_explicit_command(self) -> None:
        with self.assertRaisesRegex(ACPProviderError, "pass --acp-agent-command"):
            resolve_acp_provider("custom", None)

    def test_executable_resolution_skips_env_assignments(self) -> None:
        self.assertEqual(
            acp_command_executable("env INITIAL_AGENT_MODE=agent-full-access codex-acp"),
            "codex-acp",
        )


if __name__ == "__main__":
    unittest.main()
