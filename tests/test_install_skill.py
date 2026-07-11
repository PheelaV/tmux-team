from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.install_skill import install_skill, provider_skill_root, selected_providers


class InstallSkillTests(unittest.TestCase):
    def test_provider_selection_supports_subsets_and_all(self) -> None:
        self.assertEqual(selected_providers("codex,pool,codex"), ("codex", "pool"))
        self.assertEqual(selected_providers("all"), ("codex", "cursor", "claude", "pool"))
        with self.assertRaises(ValueError):
            selected_providers("unknown")

    def test_provider_roots_use_provider_specific_overrides(self) -> None:
        env = {
            "HOME": "/home/example",
            "CODEX_HOME": "/opt/codex",
            "CURSOR_HOME": "/opt/cursor",
            "CLAUDE_HOME": "/opt/claude",
            "POOL_SKILLS_HOME": "/opt/pool-skills",
        }
        self.assertEqual(provider_skill_root("codex", env), Path("/opt/codex/skills"))
        self.assertEqual(provider_skill_root("cursor", env), Path("/opt/cursor/skills"))
        self.assertEqual(provider_skill_root("claude", env), Path("/opt/claude/skills"))
        self.assertEqual(provider_skill_root("pool", env), Path("/opt/pool-skills"))

    def test_install_copies_skill_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "start-tmux-team"
            (source / "references").mkdir(parents=True)
            (source / "SKILL.md").write_text("skill\n", encoding="utf-8")
            (source / "references" / "invariants.md").write_text("invariants\n", encoding="utf-8")

            destination = install_skill(source, "pool", {"HOME": str(root)})

            self.assertEqual(destination, root / ".config" / "poolside" / "skills" / "start-tmux-team")
            self.assertEqual((destination / "SKILL.md").read_text(encoding="utf-8"), "skill\n")
            self.assertTrue((destination / "references" / "invariants.md").is_file())


if __name__ == "__main__":
    unittest.main()
