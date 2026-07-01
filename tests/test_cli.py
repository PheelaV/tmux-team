from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tmux_team.cli import main
from tmux_team.config import TeamConfig
from tmux_team.store import Store


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / ".tmux-team" / "team.toml"
        self.config.parent.mkdir(parents=True)
        runtime = self.root / "runtime"
        worktree = self.root / "collector"
        worktree.mkdir()
        self.config.write_text(
            f"""[team]
name = "test-team"
runtime_dir = "{runtime}"

[roles.orchestrator]
mode = "human_visible"
state = "active"
pane = "test:orchestrator.0"

[roles.trainer]
mode = "human_visible"
state = "paused"

[roles.collector]
mode = "human_visible"
state = "active"
worktree = "{worktree}"
requires_stable_commit = true
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_message_lifecycle(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "B19 failed",
            "--body",
            "Evidence goes here.",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        message_id = out.split()[0]
        self.assertTrue(message_id.startswith("msg_"))

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        self.assertIn(f"id: {message_id}", out)
        self.assertIn("Evidence goes here.", out)

        code, out, err = self.run_cli("inbox", "ack", message_id, "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        self.assertIn("state=acknowledged", out)

        code, out, err = self.run_cli(
            "inbox",
            "complete",
            message_id,
            "--role",
            "orchestrator",
            "--status",
            "done",
            "--summary",
            "handled",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("state=completed", out)

    def test_paused_role_blocks_normal_message_but_records_it(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "trainer",
            "--summary",
            "start training",
            "--body",
            "wait for approval",
            "--no-notify",
        )
        self.assertEqual(code, 2)
        self.assertIn("blocked_by_role_paused", out)
        self.assertIn("blocked: role trainer is paused", err)

        code, out, err = self.run_cli("inbox", "next", "--role", "trainer")
        self.assertEqual(code, 1)
        self.assertIn("no pending messages", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "trainer")
        self.assertEqual(code, 0, err)
        self.assertIn("state=blocked_by_role_paused", out)

    def test_role_state_changes(self) -> None:
        code, out, err = self.run_cli("role", "pause", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("collector state=paused", out)

        code, out, err = self.run_cli("role", "resume", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("collector state=active", out)

        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("collector: state=active", out)

    def test_stable_commit_current_falls_back_to_global(self) -> None:
        code, out, err = self.run_cli("stable", "approve", "abc123", "--by", "tester")
        self.assertEqual(code, 0, err)
        self.assertIn("global: abc123", out)

        code, out, err = self.run_cli("stable", "current", "--role", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("global: abc123", out)

    def test_display_message_notification_never_types_into_pane(self) -> None:
        fake_dir, log_path = self.write_fake_tmux("0\t0\tcodex\n")

        with patch.dict(os.environ, {"PATH": f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
            code, out, err = self.run_cli(
                "send",
                "--to",
                "orchestrator",
                "--summary",
                "wake without typing",
                "--body",
                "body",
                "--notify-method",
                "display-message",
            )

        self.assertEqual(code, 0, err)
        self.assertIn(" queued to=orchestrator ", out)
        self.assertIn("notify: [tmux-team]", out)
        log = log_path.read_text(encoding="utf-8")
        self.assertIn("display-message", log)
        self.assertNotIn("send-keys", log)

    def test_send_keys_notification_is_deferred_in_copy_mode(self) -> None:
        fake_dir, log_path = self.write_fake_tmux("0\t1\tcodex\n")

        with patch.dict(os.environ, {"PATH": f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
            code, out, err = self.run_cli(
                "send",
                "--to",
                "orchestrator",
                "--summary",
                "wake while pane is in copy mode",
                "--body",
                "body",
                "--notify-method",
                "send-keys",
            )

        self.assertEqual(code, 0)
        self.assertIn(" queued to=orchestrator ", out)
        self.assertIn("notify_deferred: pane is in tmux copy/mode", err)
        log = log_path.read_text(encoding="utf-8")
        self.assertIn("display-message -p", log)
        self.assertNotIn("send-keys", log)

    def test_bootstrap_dry_run_plans_visible_remote_tui_team(self) -> None:
        generated_config = self.root / ".tmux-team" / "generated.toml"

        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(generated_config),
            "--runtime-dir",
            ".tmux-team/runtime",
            "--session",
            "tt-bootstrap",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "orchestrator,implementer",
            "--goal",
            "fix the sample task",
            "--dry-run",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("tmux new-session -d -s tt-bootstrap -n control-plane", out)
        self.assertIn("tmux new-window -t tt-bootstrap -n app-server", out)
        self.assertIn("tmux new-window -t tt-bootstrap -n agents", out)
        self.assertIn("tmux split-window -t tt-bootstrap:agents", out)
        self.assertIn("tmux set-option -p -t tt-bootstrap:agents.0 @tmux-team-role orchestrator", out)
        self.assertIn("tmux set-option -p -t tt-bootstrap:agents.1 @tmux-team-role implementer", out)
        self.assertIn("tmux select-pane -t tt-bootstrap:agents.0 -T orchestrator", out)
        self.assertIn("tmux select-pane -t tt-bootstrap:agents.1 -T implementer", out)
        self.assertIn("tmux select-layout -t tt-bootstrap:agents tiled", out)
        self.assertIn("codex app-server --listen ws://127.0.0.1:4500", out)
        self.assertIn("codex --remote ws://127.0.0.1:4500", out)
        self.assertIn("[roles.orchestrator]", out)
        self.assertIn('pane = "tt-bootstrap:agents.0"', out)
        self.assertIn('mode = "app_server_remote_tui"', out)
        self.assertIn('notify_method = "app-server-turn"', out)
        self.assertIn("session: tt-bootstrap", out)
        self.assertFalse(generated_config.exists())

    def test_bootstrap_dry_run_can_launch_roles_in_yolo_mode(self) -> None:
        generated_config = self.root / ".tmux-team" / "generated.toml"

        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(generated_config),
            "--session",
            "tt-bootstrap-yolo",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "orchestrator,implementer",
            "--role-yolo",
            "--dry-run",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("codex --dangerously-bypass-approvals-and-sandbox --remote ws://127.0.0.1:4500", out)
        self.assertIn("codex_yolo = true", out)
        self.assertFalse(generated_config.exists())

    def test_bootstrap_dry_run_can_launch_roles_with_codex_profile(self) -> None:
        generated_config = self.root / ".tmux-team" / "generated.toml"

        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(generated_config),
            "--session",
            "tt-bootstrap-profile",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "orchestrator",
            "--role-profile",
            "tmux-team-role",
            "--dry-run",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("codex --profile tmux-team-role --remote ws://127.0.0.1:4500", out)
        self.assertIn('codex_profile = "tmux-team-role"', out)
        self.assertFalse(generated_config.exists())

    def test_sleep_dry_run_plans_managed_window_teardown(self) -> None:
        self.write_remote_tui_config()

        code, out, err = self.run_cli("sleep", "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn("snapshot: (dry-run)", out)
        self.assertIn("roles: 2", out)
        self.assertIn("roles: target=tt:agents", out)
        self.assertIn("app-server: target=tt:app-server", out)
        self.assertIn("tmux kill-window -t tt:agents", out)
        self.assertIn("tmux kill-window -t tt:app-server", out)
        self.assertFalse((self.root / "runtime" / "sleeps" / "latest.toml").exists())

    def test_sleep_snapshots_and_tears_down_managed_windows(self) -> None:
        self.write_remote_tui_config()
        fake_dir, log_path = self.write_fake_lifecycle_tmux()

        with patch.dict(os.environ, {"PATH": f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
            code, out, err = self.run_cli("sleep")

        self.assertEqual(code, 0, err)
        self.assertIn("snapshot:", out)
        self.assertIn("paused_roles: yes", out)
        log = log_path.read_text(encoding="utf-8")
        self.assertIn("kill-window -t @2", log)
        self.assertIn("kill-window -t @3", log)
        self.assertNotIn("kill-window -t @1", log)

        latest = self.root / "runtime" / "sleeps" / "latest.toml"
        snapshot = tomllib.loads(latest.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["tmux"]["session"], "tt")
        self.assertEqual(snapshot["roles"]["orchestrator"]["app_server"]["thread_id"], "thread-orch")
        self.assertEqual(snapshot["roles"]["implementer"]["tmux"]["window_id"], "@3")

        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("orchestrator: state=paused", out)
        self.assertIn("implementer: state=paused", out)

    def test_app_server_wake_prompt_tells_role_to_drain_multiple_messages(self) -> None:
        store = Store(TeamConfig(name="test", runtime_dir=self.root / "runtime", roles={}))

        prompt = store.app_server_wake_prompt("implementer", 3)

        self.assertIn("3 pending", prompt)
        self.assertIn("one at a time", prompt)
        self.assertIn("repeat", prompt)
        self.assertIn("until it reports no pending messages", prompt)

    def test_app_server_wake_prompt_prefers_project_relative_config_path(self) -> None:
        store = Store(
            TeamConfig(
                name="test",
                runtime_dir=self.root / "runtime",
                roles={},
                config_path=self.root / ".tmux-team" / "team.toml",
                project_root=self.root,
            )
        )

        prompt = store.app_server_wake_prompt("implementer", 1)

        self.assertIn("tmux-team --config .tmux-team/team.toml inbox next --role implementer", prompt)
        self.assertNotIn(str(self.root), prompt)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        return self.run_main("--config", str(self.config), *args)

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([*args])
        return code, stdout.getvalue(), stderr.getvalue()

    def write_fake_tmux(self, inspection_output: str) -> tuple[Path, Path]:
        fake_dir = self.root / "bin"
        fake_dir.mkdir()
        log_path = self.root / "tmux.log"
        tmux = fake_dir / "tmux"
        tmux.write_text(
            f"""#!/bin/sh
printf '%s\\n' "$*" >> {log_path}
if [ "$1" = "display-message" ] && [ "$2" = "-p" ]; then
  printf '{inspection_output}'
  exit 0
fi
if [ "$1" = "send-keys" ]; then
  printf 'send-keys should not be called in this test\\n' >&2
  exit 9
fi
exit 0
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        return fake_dir, log_path

    def write_remote_tui_config(self) -> None:
        runtime = self.root / "runtime"
        self.config.write_text(
            f"""[team]
name = "test-team"
runtime_dir = "{runtime}"

[roles.orchestrator]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:agents.0"
notify_method = "app-server-turn"
app_server_endpoint = "ws://127.0.0.1:4500"
codex_thread_id = "thread-orch"

[roles.implementer]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:agents.1"
notify_method = "app-server-turn"
app_server_endpoint = "ws://127.0.0.1:4500"
codex_thread_id = "thread-impl"
""",
            encoding="utf-8",
        )

    def write_fake_lifecycle_tmux(self) -> tuple[Path, Path]:
        fake_dir = self.root / "lifecycle-bin"
        fake_dir.mkdir()
        log_path = self.root / "lifecycle-tmux.log"
        tmux = fake_dir / "tmux"
        tmux.write_text(
            f"""#!/bin/sh
printf '%s\\n' "$*" >> {log_path}
if [ "$1" = "display-message" ] && [ "$2" = "-p" ]; then
  case "$4" in
    tt:agents.0) printf 'tt\\t@3\\tagents\\t%%10\\torchestrator\\t0\\tbash\\n'; exit 0 ;;
    tt:agents.1) printf 'tt\\t@3\\tagents\\t%%11\\timplementer\\t0\\tbash\\n'; exit 0 ;;
  esac
  printf 'unknown target %s\\n' "$4" >&2
  exit 1
fi
if [ "$1" = "list-windows" ]; then
  printf '@1\\tcontrol-plane\\n@2\\tapp-server\\n@3\\tagents\\n'
  exit 0
fi
if [ "$1" = "kill-window" ]; then
  exit 0
fi
exit 0
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        return fake_dir, log_path


if __name__ == "__main__":
    unittest.main()
