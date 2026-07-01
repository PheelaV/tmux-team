from __future__ import annotations

import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from tmux_team.cli import main
from tmux_team.config import load_config
from tmux_team.extensions.manifest import inspect_extensions


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / ".tmux-team" / "team.toml"
        self.config.parent.mkdir(parents=True)
        runtime = self.root / "runtime"
        self.config.write_text(
            f"""[team]
name = "extension-test"
runtime_dir = "{runtime}"

[roles.orchestrator]
mode = "human_visible"
state = "active"

[roles.collector]
mode = "human_visible"
state = "active"
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_inspect_extensions_loads_project_manifest(self) -> None:
        self.write_extension(
            "route",
            """
[extension]
id = "example.route"
name = "Route"
version = "0.1.0"
api_version = "1"

[[hooks]]
event = "message.before_create"
command = "python3 hook.py"
mode = "mutate"
timeout_ms = 1000
order = 10
""",
            "import json\n",
        )

        inspection = inspect_extensions(load_config(self.config))

        self.assertEqual(inspection.errors, ())
        self.assertEqual(len(inspection.manifests), 1)
        manifest = inspection.manifests[0]
        self.assertEqual(manifest.id, "example.route")
        self.assertEqual(manifest.source, "project")
        self.assertEqual(manifest.hooks[0].event, "message.before_create")

    def test_ext_list_and_doctor(self) -> None:
        self.write_extension(
            "route",
            f"""
[extension]
id = "example.route"
version = "0.1.0"

[[hooks]]
event = "message.before_create"
command = "{shlex.quote(sys.executable)} hook.py"
mode = "mutate"
""",
            "import json\n",
        )

        code, out, err = self.run_cli("ext", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("example.route", out)
        self.assertIn("hooks=1", out)

        code, out, err = self.run_cli("ext", "doctor")
        self.assertEqual(code, 0, err)
        self.assertIn("extensions ok: 1", out)

    def test_ext_doctor_reports_invalid_manifest_without_loading_service(self) -> None:
        extension_dir = self.root / ".tmux-team" / "extensions" / "broken"
        extension_dir.mkdir(parents=True)
        (extension_dir / "extension.toml").write_text(
            """
[extension]
version = "0.1.0"
""",
            encoding="utf-8",
        )

        code, out, err = self.run_cli("ext", "doctor")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("extension errors:", err)
        self.assertIn("missing required field: id", err)

    def test_message_before_create_hook_can_route_message(self) -> None:
        self.write_extension(
            "route",
            f"""
[extension]
id = "example.route"
version = "0.1.0"

[[hooks]]
event = "message.before_create"
command = "{shlex.quote(sys.executable)} hook.py"
mode = "mutate"
""",
            """
import json
import sys

payload = json.load(sys.stdin)
message = payload["data"]["message"]
message["recipient"] = "collector"
message["priority"] = "urgent"
message["summary"] = "[routed] " + message["summary"]
print(json.dumps({"ok": True, "patch": {"message": message}}))
""",
        )

        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--summary",
            "source health",
            "--body",
            "evidence",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("to=collector", out)
        self.assertIn("priority=urgent", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("[routed] source health", out)

    def test_decision_hook_can_deny_claim(self) -> None:
        self.write_extension(
            "claim-gate",
            f"""
[extension]
id = "example.claim-gate"
version = "0.1.0"

[[hooks]]
event = "message.before_claim"
command = "{shlex.quote(sys.executable)} hook.py"
mode = "decision"
""",
            """
import json

print(json.dumps({"ok": True, "decision": "deny", "reason": "collector freeze"}))
""",
        )
        code, out, err = self.run_cli(
            "send",
            "--to",
            "collector",
            "--summary",
            "task",
            "--body",
            "body",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("inbox", "next", "--role", "collector")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("collector freeze", err)

    def test_invalid_log_level_is_rejected(self) -> None:
        code, out, err = self.run_cli("--log-level", "LOUD", "status")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("invalid log level", err)

    def write_extension(self, name: str, manifest: str, hook: str) -> None:
        extension_dir = self.root / ".tmux-team" / "extensions" / name
        extension_dir.mkdir(parents=True)
        (extension_dir / "extension.toml").write_text(manifest, encoding="utf-8")
        (extension_dir / "hook.py").write_text(hook, encoding="utf-8")

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(self.config), *args])
        return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
