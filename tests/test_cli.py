from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tmux_team.bootstrap import (
    BootstrapError,
    RoleBinding,
    default_session_name,
    prepare_role_worktrees,
    role_startup_prompt,
    write_role_env_files,
)
from tmux_team.cli import infer_role_from_tmux_pane, main
from tmux_team.config import RoleConfig, TeamConfig, load_config
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
pane = "test:collector.0"
worktree = "{worktree}"
requires_stable_commit = true
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_session_name_is_tt_prefixed(self) -> None:
        self.assertEqual(default_session_name(Path("/tmp/my project")), "tt-my-project")
        self.assertEqual(default_session_name(Path("/tmp/tt-existing")), "tt-existing")

    def test_runtime_dir_uses_cli_then_env_then_config(self) -> None:
        env_runtime = self.root / "env-runtime"
        cli_runtime = self.root / "cli-runtime"

        with patch.dict(os.environ, {"TMUX_TEAM_HOME": str(env_runtime), "TMUX_TEAM_RUNTIME_DIR": ""}):
            self.assertEqual(load_config(self.config).runtime_dir, env_runtime.resolve())
            self.assertEqual(load_config(self.config, cli_runtime).runtime_dir, cli_runtime.resolve())

    def test_runtime_home_env_works_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            runtime = Path(temp) / "state"
            root.mkdir()

            with patch.dict(os.environ, {"TMUX_TEAM_HOME": str(runtime), "TMUX_TEAM_RUNTIME_DIR": ""}):
                config = load_config(start=root)

            self.assertEqual(config.runtime_dir, runtime.resolve())

    def test_config_env_works_without_config_arg(self) -> None:
        with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": str(self.config)}):
            code, out, err = self.run_main("status")

        self.assertEqual(code, 0, err)
        self.assertIn("team: test-team", out)

    def test_config_arg_overrides_config_env(self) -> None:
        other = self.root / "other.toml"
        other_runtime = self.root / "other-runtime"
        other.write_text(
            f"""[team]
name = "other-team"
runtime_dir = "{other_runtime}"
""",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": str(other)}):
            code, out, err = self.run_main("--config", str(self.config), "status")

        self.assertEqual(code, 0, err)
        self.assertIn("team: test-team", out)
        self.assertNotIn("other-team", out)

    def test_role_env_defaults_inbox_role_and_sender(self) -> None:
        with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": str(self.config), "TMUX_TEAM_ROLE": "collector"}):
            code, out, err = self.run_main(
                "send",
                "--to",
                "collector",
                "--summary",
                "self task",
                "--body",
                "body",
                "--no-notify",
            )
            self.assertEqual(code, 0, err)
            message_id = out.split()[0]

            code, out, err = self.run_main("inbox", "next")

        self.assertEqual(code, 0, err)
        self.assertIn(f"id: {message_id}", out)
        self.assertIn("from: collector", out)

    def test_memory_commands_default_to_role_and_keep_latest_near_top(self) -> None:
        with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": str(self.config), "TMUX_TEAM_ROLE": "collector"}):
            code, out, err = self.run_main("memory", "append", "--body", "Active task: inspect failing tests")
            self.assertEqual(code, 0, err)
            memory_path = Path(out.strip())

            code, out, err = self.run_main("memory", "show")

        self.assertEqual(code, 0, err)
        self.assertEqual(memory_path, (self.root / ".tmux-team" / "memory" / "collector.md").resolve())
        self.assertIn("## Latest Updates", out)
        self.assertIn("Active task: inspect failing tests", out)
        self.assertLess(out.index("## Latest Updates"), out.index("Active task: inspect failing tests"))

    def test_memory_can_read_body_file(self) -> None:
        note = self.root / "memory-note.md"
        note.write_text("Current blocker: provider quota", encoding="utf-8")

        code, out, err = self.run_cli("memory", "append", "--role", "collector", "--body-file", str(note))
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("memory", "show", "--role", "collector")

        self.assertEqual(code, 0, err)
        self.assertIn("Current blocker: provider quota", out)

    def test_milestone_add_and_list_jsonl(self) -> None:
        with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": str(self.config), "TMUX_TEAM_ROLE": "orchestrator"}):
            code, out, err = self.run_main(
                "milestone",
                "add",
                "--role",
                "collector",
                "--summary",
                "targeted test failed",
                "--kind",
                "evidence",
                "--ref",
                "msg_123",
                "--tag",
                "test",
                "--body",
                "romanize_syllable returned broken-a",
            )
            self.assertEqual(code, 0, err)
            self.assertIn("evidence role=collector targeted test failed", out)

            code, out, err = self.run_main("milestone", "list", "--since", "-4h")

        self.assertEqual(code, 0, err)
        self.assertIn("targeted test failed", out)
        self.assertIn("romanize_syllable returned broken-a", out)
        milestone_path = self.root / "runtime" / "milestones.jsonl"
        rows = [json.loads(line) for line in milestone_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["actor"], "orchestrator")
        self.assertEqual(rows[0]["role"], "collector")
        self.assertEqual(rows[0]["kind"], "evidence")
        self.assertEqual(rows[0]["ref_id"], "msg_123")
        self.assertEqual(rows[0]["tags"], ["test"])

    def test_milestone_list_today_can_print_json(self) -> None:
        code, out, err = self.run_cli("milestone", "add", "--summary", "goal completed", "--role", "orchestrator")
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("milestone", "list", "--today", "--json")

        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(rows[0]["summary"], "goal completed")

    def test_milestone_add_defaults_role_from_env_actor(self) -> None:
        with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": str(self.config), "TMUX_TEAM_ROLE": "orchestrator"}):
            code, out, err = self.run_main("milestone", "add", "--summary", "orchestrator checkpoint")

        self.assertEqual(code, 0, err)
        self.assertIn("role=orchestrator", out)

        milestone_path = self.root / "runtime" / "milestones.jsonl"
        rows = [json.loads(line) for line in milestone_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["actor"], "orchestrator")
        self.assertEqual(rows[0]["role"], "orchestrator")

    def test_non_orchestrator_actor_cannot_record_milestone(self) -> None:
        with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": str(self.config), "TMUX_TEAM_ROLE": "collector"}):
            code, _out, err = self.run_main(
                "milestone",
                "add",
                "--summary",
                "too noisy",
            )

        self.assertEqual(code, 2)
        self.assertIn("not authorized to record milestones", err)

    def test_worktree_env_file_and_cwd_infer_config_and_role(self) -> None:
        worktree = self.root / "collector"
        env_file = worktree / ".tmux-team" / "team.env"
        env_file.parent.mkdir()
        env_file.write_text(f"TMUX_TEAM_CONFIG={self.config}\n", encoding="utf-8")
        subdir = worktree / "py_scripts"
        subdir.mkdir()

        code, out, err = self.run_cli(
            "send",
            "--to",
            "collector",
            "--from",
            "orchestrator",
            "--summary",
            "worktree task",
            "--body",
            "body",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        message_id = out.split()[0]

        old_cwd = Path.cwd()
        try:
            os.chdir(subdir)
            with patch.dict(os.environ, {"TMUX_TEAM_CONFIG": "", "TMUX_TEAM_ROLE": "", "TMUX_PANE": ""}):
                code, out, err = self.run_main("inbox", "next")
        finally:
            os.chdir(old_cwd)

        self.assertEqual(code, 0, err)
        self.assertIn(f"id: {message_id}", out)

    def test_role_env_file_records_role_only_for_unique_worktree(self) -> None:
        collector = self.root / "collector"
        trainer = self.root / "trainer"
        collector.mkdir(exist_ok=True)
        trainer.mkdir()
        write_role_env_files(
            self.config,
            {
                "collector": RoleBinding(thread_id="thread-collector", pane="%1", worktree=collector),
                "trainer": RoleBinding(thread_id="thread-trainer", pane="%2", worktree=trainer),
            },
        )

        collector_env = (collector / ".tmux-team" / "team.env").read_text(encoding="utf-8")
        trainer_env = (trainer / ".tmux-team" / "team.env").read_text(encoding="utf-8")
        self.assertIn(f"TMUX_TEAM_CONFIG={self.config}", collector_env)
        self.assertIn("TMUX_TEAM_ROLE=collector", collector_env)
        self.assertIn("TMUX_TEAM_ROLE=trainer", trainer_env)

    def test_role_env_file_omits_role_for_shared_worktree(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        write_role_env_files(
            self.config,
            {
                "orchestrator": RoleBinding(thread_id="thread-orch", pane="%1", worktree=shared),
                "implementer": RoleBinding(thread_id="thread-impl", pane="%2", worktree=shared),
            },
        )

        env_text = (shared / ".tmux-team" / "team.env").read_text(encoding="utf-8")
        self.assertIn(f"TMUX_TEAM_CONFIG={self.config}", env_text)
        self.assertNotIn("TMUX_TEAM_ROLE=", env_text)

    def test_tmux_pane_option_infers_role_when_worktree_is_shared(self) -> None:
        config = TeamConfig(
            name="test",
            runtime_dir=self.root / "runtime",
            roles={
                "orchestrator": RoleConfig(name="orchestrator", worktree=str(self.root)),
                "implementer": RoleConfig(name="implementer", worktree=str(self.root)),
            },
        )

        completed = subprocess.CompletedProcess(["tmux"], 0, stdout="implementer\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%1"}),
            patch("tmux_team.cli.subprocess.run", return_value=completed),
        ):
            self.assertEqual(infer_role_from_tmux_pane(config), "implementer")

    def test_message_lifecycle(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "test failed",
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

    def test_expired_claim_is_visible_and_reclaimable(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "stalled task",
            "--body",
            "Evidence goes here.",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        message_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator", "--claim-seconds", "0")
        self.assertEqual(code, 0, err)
        self.assertIn(f"id: {message_id}", out)

        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("orchestrator:", out)
        self.assertIn("pending=1 stale_claimed=1", out)

        code, out, err = self.run_cli("inbox", "reclaimable", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{message_id} state=stale_claimed", out)
        self.assertIn("claim_expires_at=", out)

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        self.assertIn(f"id: {message_id}", out)

    def test_status_verbose_shows_active_message_summaries(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--priority",
            "high",
            "--summary",
            "collect active evidence",
            "--body",
            "Evidence goes here.",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        message_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("inbox", "ack", message_id, "--role", "orchestrator")
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("status", "--verbose", "--active-limit", "2")

        self.assertEqual(code, 0, err)
        self.assertIn("active:", out)
        self.assertIn(f"{message_id} state=acknowledged", out)
        self.assertIn("priority=high", out)
        self.assertIn("from=collector", out)
        self.assertIn("summary=collect active evidence", out)
        self.assertIn("claim_expires_at=", out)

    def test_claimed_unacked_warning_and_auto_ack(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "unacked task",
            "--body",
            "body",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        first_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        self.assertIn(f"id: {first_id}", out)

        code, out, err = self.run_cli("status", "--verbose", "--unacked-warn-seconds", "0")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{first_id} state=claimed", out)
        self.assertIn("warning=claimed_unacked", out)

        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "auto ack task",
            "--body",
            "body",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        second_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator", "--auto-ack")
        self.assertEqual(code, 0, err)
        self.assertIn(f"id: {second_id}", out)
        self.assertIn("state: acknowledged", out)

    def test_watch_lifecycle_and_status_verbose(self) -> None:
        code, out, err = self.run_cli(
            "watch",
            "start",
            "--role",
            "collector",
            "--summary",
            "monitor external run",
            "--terminal-condition",
            "run terminalizes",
            "--next-update-in",
            "5m",
            "--ref",
            "msg_123",
        )
        self.assertEqual(code, 0, err)
        watch_id = out.split()[0]
        self.assertTrue(watch_id.startswith("watch_"))
        self.assertIn("role=collector", out)
        self.assertIn("state=active", out)
        self.assertIn("next_update_at=", out)
        self.assertIn("ref=msg_123", out)

        code, out, err = self.run_cli(
            "watch",
            "update",
            watch_id,
            "--role",
            "collector",
            "--summary",
            "heartbeat ok",
            "--next-update-in",
            "10m",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("summary=heartbeat ok", out)

        code, out, err = self.run_cli("status", "--verbose")
        self.assertEqual(code, 0, err)
        self.assertIn("watches:", out)
        self.assertIn(f"{watch_id} role=collector state=active", out)
        self.assertIn("summary=heartbeat ok", out)

        code, out, err = self.run_cli(
            "watch",
            "complete",
            watch_id,
            "--role",
            "collector",
            "--status",
            "done",
            "--summary",
            "run terminalized",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("state=done", out)

        code, out, err = self.run_cli("watch", "list", "--role", "collector", "--state", "done")
        self.assertEqual(code, 0, err)
        self.assertIn("summary=run terminalized", out)

    def test_send_correlation_warns_about_active_duplicates(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "collect evidence",
            "--body",
            "body",
            "--correlation-key",
            "case-1",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        first_id = out.split()[0]
        self.assertEqual(err, "")

        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "trainer",
            "--summary",
            "collect different evidence",
            "--body",
            "body",
            "--correlation-key",
            "case-1",
            "--related-to",
            first_id,
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"duplicate_warning: active message {first_id}", err)

        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "trainer",
            "--summary",
            "collect different evidence",
            "--body",
            "body",
            "--correlation-key",
            "case-1",
            "--allow-duplicate",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("duplicate_warning", err)

        code, out, err = self.run_cli("inbox", "list", "--role", "orchestrator", "--verbose")

        self.assertEqual(code, 0, err)
        self.assertIn("correlation_key=case-1", out)
        self.assertIn(f"related_to={first_id}", out)

    def test_broadcast_creates_one_message_per_recipient(self) -> None:
        code, out, err = self.run_cli(
            "broadcast",
            "--only",
            "orchestrator,collector",
            "--from",
            "operator",
            "--summary",
            "checkpoint",
            "--body",
            "Report current status.",
            "--no-notify",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("broadcast: 2 recipient(s)", out)
        self.assertEqual(out.count(" queued to="), 2)
        self.assertIn("to=orchestrator", out)
        self.assertIn("to=collector", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        self.assertIn("summary=checkpoint", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("summary=checkpoint", out)

    def test_broadcast_notice_does_not_create_pending_inbox_work(self) -> None:
        code, out, err = self.run_cli(
            "broadcast",
            "--notice",
            "--only",
            "orchestrator,collector",
            "--from",
            "operator",
            "--summary",
            "policy updated",
            "--body",
            "Read the current operating notes before lifecycle work.",
            "--no-notify",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("broadcast: 2 recipient(s)", out)
        self.assertEqual(out.count(" completed to="), 2)

        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("collector:", out)
        self.assertIn("pending=0", out)

        code, out, err = self.run_cli("inbox", "next", "--role", "collector")
        self.assertEqual(code, 1)
        self.assertIn("no pending messages for collector", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "collector", "--state", "completed", "--verbose")
        self.assertEqual(code, 0, err)
        self.assertIn("summary=policy updated", out)
        self.assertIn("kind=notice", out)

    def test_broadcast_rejects_only_and_exclude_together(self) -> None:
        code, out, err = self.run_cli(
            "broadcast",
            "--only",
            "collector",
            "--exclude",
            "trainer",
            "--from",
            "orchestrator",
            "--summary",
            "checkpoint",
            "--body",
            "Report current status.",
            "--no-notify",
        )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not allowed with argument", err)

    def test_broadcast_rejects_unknown_excluded_role(self) -> None:
        code, out, err = self.run_cli(
            "broadcast",
            "--exclude",
            "collectro",
            "--from",
            "orchestrator",
            "--summary",
            "checkpoint",
            "--body",
            "Report current status.",
            "--no-notify",
        )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Unknown excluded role: collectro", err)

    def test_broadcast_defaults_to_all_roles_except_sender(self) -> None:
        code, out, err = self.run_cli(
            "broadcast",
            "--from",
            "orchestrator",
            "--summary",
            "checkpoint",
            "--body",
            "Report current status.",
            "--no-notify",
        )

        self.assertEqual(code, 2)
        self.assertIn("to=collector", out)
        self.assertIn("to=trainer", out)
        self.assertNotIn("to=orchestrator", out)
        self.assertIn("blocked: role trainer is paused", err)

    def test_pane_capture_reads_configured_role_pane(self) -> None:
        fake_dir = self.root / "pane-bin"
        fake_dir.mkdir()
        log_path = self.root / "pane-tmux.log"
        tmux = fake_dir / "tmux"
        tmux.write_text(
            f"""#!/bin/sh
printf '%s\\n' "$*" >> {log_path}
if [ "$1" = "capture-pane" ]; then
  printf 'line one\\nline two\\n'
  exit 0
fi
exit 9
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)

        code, out, err = self.run_cli("pane", "capture", "collector", "--lines", "20", "--tmux-bin", str(tmux))

        self.assertEqual(code, 0, err)
        self.assertIn("# pane collector", out)
        self.assertIn("line one\nline two", out)
        self.assertIn("capture-pane -p -t", log_path.read_text(encoding="utf-8"))

    def test_pane_capture_supports_offset(self) -> None:
        fake_dir = self.root / "pane-offset-bin"
        fake_dir.mkdir()
        log_path = self.root / "pane-offset-tmux.log"
        tmux = fake_dir / "tmux"
        tmux.write_text(
            f"""#!/bin/sh
printf '%s\\n' "$*" >> {log_path}
if [ "$1" = "capture-pane" ]; then
  printf 'older line\\n'
  exit 0
fi
exit 9
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)

        code, out, err = self.run_cli(
            "pane",
            "capture",
            "collector",
            "--limit",
            "20",
            "--offset",
            "5",
            "--tmux-bin",
            str(tmux),
        )

        self.assertEqual(code, 0, err)
        self.assertIn("# pane collector", out)
        self.assertIn("20 lines offset 5", out)
        self.assertIn("older line", out)
        self.assertIn("capture-pane -p -t test:collector.0 -S -25 -E -6", log_path.read_text(encoding="utf-8"))

    def test_pane_capture_policy_allows_orchestrator_supervision(self) -> None:
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "pane",
            "capture",
            "orchestrator",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not authorized to run pane.capture", err)

        completed = subprocess.CompletedProcess(["tmux"], 0, stdout="orchestrator pane\n", stderr="")
        with patch("tmux_team.cli.subprocess.run", return_value=completed):
            code, out, err = self.run_main(
                "--config",
                str(self.config),
                "--actor",
                "orchestrator",
                "pane",
                "capture",
                "collector",
            )

        self.assertEqual(code, 0, err)
        self.assertIn("orchestrator pane", out)

    def test_complete_can_reply_to_sender(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "test failed",
            "--body",
            "Evidence goes here.",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        message_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("inbox", "ack", message_id, "--role", "orchestrator")
        self.assertEqual(code, 0, err)

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
            "--reply-to-sender",
            "--reply-no-notify",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("state=completed", out)
        self.assertIn("reply: msg_", out)
        self.assertIn("to=collector", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("from=orchestrator", out)
        self.assertIn("orchestrator completed: test failed", out)

    def test_completion_replies_can_be_bulk_completed_after_ack(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "test failed",
            "--body",
            "Evidence goes here.",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        original_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("inbox", "ack", original_id, "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(
            "inbox",
            "complete",
            original_id,
            "--role",
            "orchestrator",
            "--summary",
            "routed",
            "--reply-to-sender",
            "--reply-no-notify",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("inbox", "next", "--role", "collector")
        self.assertEqual(code, 0, err)
        reply_id = ""
        for line in out.splitlines():
            if line.startswith("id: "):
                reply_id = line.removeprefix("id: ")
        self.assertTrue(reply_id.startswith("msg_"))

        code, out, err = self.run_cli("inbox", "ack", reply_id, "--role", "collector")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("inbox", "list", "--role", "collector", "--verbose")
        self.assertEqual(code, 0, err)
        self.assertIn("kind=completion_notice", out)
        self.assertIn(f"related_to={original_id}", out)

        code, out, err = self.run_cli("inbox", "complete-replies", "--role", "collector")

        self.assertEqual(code, 0, err)
        self.assertIn("completed 1 completion notice(s)", out)
        self.assertIn(f"{reply_id} state=completed", out)

    def test_complete_accepts_body_detail(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "test failed",
            "--body",
            "Evidence goes here.",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        message_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("inbox", "ack", message_id, "--role", "orchestrator")
        self.assertEqual(code, 0, err)
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
            "--body",
            "detail line one\nline two",
            "--reply-to-sender",
            "--reply-no-notify",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("state=completed", out)
        self.assertIn("reply: msg_", out)

        code, out, err = self.run_cli("inbox", "next", "--role", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("Result: handled\n\ndetail line one\nline two", out)

    def test_reply_to_sender_is_message_scoped(self) -> None:
        for sender, summary in (("collector", "collector report"), ("trainer", "trainer report")):
            code, out, err = self.run_cli(
                "send",
                "--to",
                "orchestrator",
                "--from",
                sender,
                "--summary",
                summary,
                "--body",
                "body",
                "--no-notify",
            )
            self.assertEqual(code, 0, err)
            message_id = out.split()[0]
            code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
            self.assertEqual(code, 0, err)
            code, out, err = self.run_cli("inbox", "ack", message_id, "--role", "orchestrator")
            self.assertEqual(code, 0, err)
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
                "--reply-to-sender",
                "--reply-no-notify",
            )
            self.assertEqual(code, 0, err)
            self.assertIn(f"to={sender}", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "collector")
        self.assertEqual(code, 0, err)
        self.assertIn("orchestrator completed: collector report", out)
        self.assertNotIn("trainer report", out)

        code, out, err = self.run_cli("inbox", "list", "--role", "trainer")
        self.assertEqual(code, 0, err)
        self.assertIn("orchestrator completed: trainer report", out)
        self.assertNotIn("collector report", out)

    def test_reply_to_sender_skips_completion_reply_loops(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--from",
            "collector",
            "--summary",
            "collector report",
            "--body",
            "body",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)
        original_id = out.split()[0]

        code, out, err = self.run_cli("inbox", "next", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("inbox", "ack", original_id, "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(
            "inbox",
            "complete",
            original_id,
            "--role",
            "orchestrator",
            "--status",
            "done",
            "--summary",
            "routed",
            "--reply-to-sender",
            "--reply-no-notify",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("inbox", "next", "--role", "collector")
        self.assertEqual(code, 0, err)
        reply_id = ""
        for line in out.splitlines():
            if line.startswith("id: "):
                reply_id = line.removeprefix("id: ")
        self.assertTrue(reply_id.startswith("msg_"))
        code, out, err = self.run_cli("inbox", "ack", reply_id, "--role", "collector")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(
            "inbox",
            "complete",
            reply_id,
            "--role",
            "collector",
            "--status",
            "done",
            "--summary",
            "acknowledged",
            "--reply-to-sender",
            "--reply-no-notify",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("reply_skipped: message is already a completion reply", err)
        self.assertNotIn("reply: msg_", out)

    def test_init_writes_valid_toml_config(self) -> None:
        config_path = self.root / "new-project" / ".tmux-team" / "team.toml"

        code, out, err = self.run_main(
            "init",
            "--config",
            str(config_path),
            "--name",
            "demo-team",
            "--runtime-dir",
            ".tmux-team/runtime",
        )

        self.assertEqual(code, 0, err)
        self.assertIn(f"created {config_path}", out)
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["team"]["name"], "demo-team")
        self.assertTrue(data["roles"]["implementer"]["can_edit"])
        config = load_config(config_path)
        self.assertEqual(config.name, "demo-team")
        self.assertIn("orchestrator", config.roles)

    def test_authenticated_actor_defaults_send_sender_to_self(self) -> None:
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "send",
            "--to",
            "orchestrator",
            "--summary",
            "status",
            "--body",
            "collector status",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("inbox", "list", "--role", "orchestrator")
        self.assertEqual(code, 0, err)
        self.assertIn("from=collector", out)

    def test_authenticated_actor_cannot_send_as_another_role(self) -> None:
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "send",
            "--to",
            "orchestrator",
            "--from",
            "trainer",
            "--summary",
            "spoof",
            "--body",
            "bad sender",
            "--no-notify",
        )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not authorized to send as 'trainer'", err)

    def test_authenticated_actor_cannot_claim_another_inbox(self) -> None:
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--summary",
            "incoming",
            "--body",
            "task",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "inbox",
            "next",
            "--role",
            "orchestrator",
        )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not authorized to run inbox.next", err)

    def test_authenticated_actor_needs_policy_for_role_state_changes(self) -> None:
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "role",
            "pause",
            "trainer",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not authorized to change role state", err)

        self.config.write_text(
            self.config.read_text(encoding="utf-8")
            + """
[roles.collector.policy]
can_change_role_state = true
""",
            encoding="utf-8",
        )
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "role",
            "pause",
            "trainer",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("trainer state=paused", out)

    def test_authenticated_actor_needs_policy_to_notify_another_role(self) -> None:
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "notify",
            "orchestrator",
            "--method",
            "app-server-turn",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not authorized to run role.notify", err)

        self.config.write_text(
            self.config.read_text(encoding="utf-8")
            + """
[roles.collector.policy]
can_notify = ["orchestrator"]
""",
            encoding="utf-8",
        )
        code, out, err = self.run_cli(
            "send",
            "--to",
            "orchestrator",
            "--summary",
            "queued for explicit notify",
            "--body",
            "task",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "notify",
            "orchestrator",
            "--method",
            "app-server-turn",
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no app-server endpoint/thread binding", err)

    def test_authenticated_actor_cannot_use_send_keys_notify_without_breakglass_policy(self) -> None:
        self.config.write_text(
            self.config.read_text(encoding="utf-8")
            + """
[roles.collector.policy]
can_notify = ["orchestrator"]
""",
            encoding="utf-8",
        )

        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "notify",
            "orchestrator",
            "--method",
            "send-keys",
        )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not authorized to use tmux send-keys notification", err)

    def test_authenticated_actor_cannot_run_privileged_cli_actions_by_default(self) -> None:
        cases = (
            (
                ("codex", "bind", "collector", "--endpoint", "ws://127.0.0.1:4500", "--thread-id", "thread-1"),
                "not authorized to bind Codex app-server roles",
            ),
            (("stable", "approve", "abc123"), "not authorized to approve stable commits"),
            (("sleep", "--dry-run"), "not authorized to sleep the team"),
            (("resume", "--dry-run"), "not authorized to resume the team"),
        )
        for command, expected_error in cases:
            with self.subTest(command=command):
                code, out, err = self.run_main("--config", str(self.config), "--actor", "collector", *command)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(expected_error, err)

    def test_policy_mode_permissive_is_cli_breakglass(self) -> None:
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "--policy-mode",
            "permissive",
            "role",
            "pause",
            "trainer",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("trainer state=paused", out)

    def test_codex_session_context_includes_recovery_contract_and_memory(self) -> None:
        memory = self.root / ".tmux-team" / "memory" / "collector.md"
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text(
            "# collector Scratchpad\n\n## Latest Updates\n\nActive task: collect evidence.\n", encoding="utf-8"
        )
        code, out, err = self.run_cli(
            "send",
            "--to",
            "collector",
            "--summary",
            "collect evidence",
            "--body",
            "task",
            "--no-notify",
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("codex", "session-context", "--role", "collector", "--max-memory-chars", "200")

        self.assertEqual(code, 0, err)
        self.assertIn("same operating contract as the initial role startup prompt", out)
        self.assertIn("not a new task", out)
        self.assertIn("Role: collector", out)
        self.assertIn("Pending inbox messages: 1", out)
        self.assertIn("Scratchpad excerpt:", out)
        self.assertIn("Active task: collect evidence.", out)

    def test_codex_session_context_defaults_to_actor_role(self) -> None:
        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "codex",
            "session-context",
            "--max-memory-chars",
            "0",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Role: collector", out)

        code, out, err = self.run_main(
            "--config",
            str(self.config),
            "--actor",
            "collector",
            "codex",
            "session-context",
            "--role",
            "orchestrator",
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not authorized to run memory.read", err)

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

    def test_send_keys_notification_uses_blunt_prompt_when_explicitly_allowed(self) -> None:
        fake_dir, log_path = self.write_fake_tmux("0\t0\tcodex\n", allow_send_keys=True)

        with patch.dict(os.environ, {"PATH": f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
            code, out, err = self.run_cli(
                "send",
                "--to",
                "orchestrator",
                "--summary",
                "debug wake",
                "--body",
                "body",
                "--notify-method",
                "send-keys",
            )

        self.assertEqual(code, 0, err)
        self.assertIn(" queued to=orchestrator ", out)
        log = log_path.read_text(encoding="utf-8")
        self.assertIn("send-keys", log)
        self.assertIn("Wake notice only", log)
        self.assertIn("tmux-team inbox next", log)
        self.assertNotIn("tmux-team memory show", log)
        self.assertNotIn("tmux-team inbox ack", log)
        self.assertNotIn("tmux-team inbox complete", log)

    def test_role_startup_prompt_discourages_memory_spam(self) -> None:
        prompt = role_startup_prompt("collector")

        self.assertIn("tmux-team memory show --role collector", prompt)
        self.assertIn("tmux-team inbox next --role collector", prompt)
        self.assertIn("tmux-team inbox ack <message-id> --role collector", prompt)
        self.assertIn("tmux-team inbox complete <message-id> --role collector", prompt)
        self.assertIn("shared worktrees are ambiguous", prompt)
        self.assertIn("Append memory only for high-value durable changes", prompt)
        self.assertIn("Do not append routine startup/parking/status chatter", prompt)
        self.assertNotIn("missing or stale", prompt)
        self.assertNotIn("durable status update", prompt)

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
        self.assertIn("tmux new-session -d -s tt-bootstrap -n tt-control", out)
        self.assertIn("tmux new-window -t tt-bootstrap -n tt-app-server", out)
        self.assertIn("tmux new-window -t tt-bootstrap -n tt-agents", out)
        self.assertIn("tmux split-window -t tt-bootstrap:tt-agents", out)
        self.assertIn("tmux set-option -p -t tt-bootstrap:tt-agents.0 @tmux-team-role orchestrator", out)
        self.assertIn("tmux set-option -p -t tt-bootstrap:tt-agents.1 @tmux-team-role implementer", out)
        self.assertIn("tmux select-pane -t tt-bootstrap:tt-agents.0 -T tt-orchestrator", out)
        self.assertIn("tmux select-pane -t tt-bootstrap:tt-agents.1 -T tt-implementer", out)
        self.assertIn("tmux select-layout -t tt-bootstrap:tt-agents tiled", out)
        self.assertIn("codex app-server --listen ws://127.0.0.1:4500", out)
        self.assertIn(f"codex --cd {self.root.resolve()} --remote ws://127.0.0.1:4500", out)
        self.assertIn("You are the `orchestrator` role in a tmux-team managed Codex team.", out)
        self.assertIn("tmux-team memory show --role orchestrator", out)
        self.assertIn("tmux-team inbox next --role orchestrator", out)
        self.assertIn(f"TMUX_TEAM_CONFIG={generated_config}", out)
        self.assertIn("TMUX_TEAM_ROLE=orchestrator", out)
        self.assertIn("[roles.orchestrator]", out)
        self.assertIn('pane = "tt-bootstrap:tt-agents.0"', out)
        self.assertIn('scratchpad = ".tmux-team/memory/orchestrator.md"', out)
        self.assertIn('mode = "app_server_remote_tui"', out)
        self.assertIn('notify_method = "app-server-turn"', out)
        self.assertIn("session: tt-bootstrap", out)
        self.assertFalse(generated_config.exists())

    def test_bootstrap_dry_run_uses_runtime_home_env(self) -> None:
        generated_config = self.root / ".tmux-team" / "generated.toml"
        runtime = self.root / "team-state"

        with patch.dict(os.environ, {"TMUX_TEAM_HOME": str(runtime), "TMUX_TEAM_RUNTIME_DIR": ""}):
            code, out, err = self.run_main(
                "bootstrap",
                "--project-root",
                str(self.root),
                "--config",
                str(generated_config),
                "--session",
                "tt-bootstrap",
                "--endpoint",
                "ws://127.0.0.1:4500",
                "--roles",
                "orchestrator",
                "--dry-run",
            )

        self.assertEqual(code, 0, err)
        self.assertIn(f'runtime_dir = "{runtime}"', out)
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
        self.assertIn(
            f"codex --dangerously-bypass-approvals-and-sandbox --cd {self.root.resolve()} --remote ws://127.0.0.1:4500",
            out,
        )
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
        self.assertIn(f"codex --profile tmux-team-role --cd {self.root.resolve()} --remote ws://127.0.0.1:4500", out)
        self.assertIn('codex_profile = "tmux-team-role"', out)
        self.assertFalse(generated_config.exists())

    def test_bootstrap_dry_run_can_launch_roles_with_role_codex_options(self) -> None:
        generated_config = self.root / ".tmux-team" / "generated.toml"

        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(generated_config),
            "--session",
            "tt-bootstrap-options",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "orchestrator,collector",
            "--role-model",
            "orchestrator=gpt-5.5",
            "--role-reasoning-effort",
            "orchestrator=xhigh",
            "--role-codex-profile",
            "collector=collector-profile",
            "--role-codex-config",
            'collector=model_reasoning_effort="high"',
            "--dry-run",
        )

        self.assertEqual(code, 0, err)
        self.assertIn("--model gpt-5.5", out)
        self.assertIn('model_reasoning_effort="xhigh"', out)
        self.assertIn("codex --profile collector-profile", out)
        self.assertIn('model_reasoning_effort="high"', out)
        self.assertIn('codex_model = "gpt-5.5"', out)
        self.assertIn('codex_reasoning_effort = "xhigh"', out)
        self.assertIn('codex_profile = "collector-profile"', out)
        self.assertIn("codex_config = [", out)
        self.assertIn('"model_reasoning_effort=\\"high\\""', out)
        self.assertFalse(generated_config.exists())

    def test_bootstrap_dry_run_accepts_custom_role_memory(self) -> None:
        generated_config = self.root / ".tmux-team" / "generated.toml"
        memory_path = self.root / "memory" / "collector.md"

        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(generated_config),
            "--session",
            "tt-bootstrap-memory",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "collector",
            "--role-memory",
            f"collector={memory_path}",
            "--dry-run",
        )

        self.assertEqual(code, 0, err)
        self.assertIn(f'scratchpad = "{memory_path.resolve()}"', out)
        self.assertFalse(generated_config.exists())

    def test_bootstrap_dry_run_uses_per_role_worktrees(self) -> None:
        generated_config = self.root / ".tmux-team" / "generated.toml"
        collector = (self.root / "collector-wt").resolve()
        trainer = (self.root / "trainer-wt").resolve()
        collector.mkdir()
        trainer.mkdir()

        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(generated_config),
            "--session",
            "tt-bootstrap-worktrees",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "orchestrator,collector,trainer",
            "--role-worktree",
            f"collector={collector}",
            "--role-worktree",
            f"trainer={trainer}",
            "--dry-run",
        )

        self.assertEqual(code, 0, err)
        self.assertIn(f"tmux split-window -t tt-bootstrap-worktrees:tt-agents -c {collector}", out)
        self.assertIn(f"tmux split-window -t tt-bootstrap-worktrees:tt-agents -c {trainer}", out)
        self.assertIn(f"codex --cd {collector} --remote ws://127.0.0.1:4500", out)
        self.assertIn(f"codex --cd {trainer} --remote ws://127.0.0.1:4500", out)
        self.assertIn("TMUX_TEAM_ROLE=collector", out)
        self.assertIn("TMUX_TEAM_ROLE=trainer", out)
        self.assertIn(f'worktree = "{collector}"', out)
        self.assertIn(f'worktree = "{trainer}"', out)
        self.assertFalse(generated_config.exists())

    def test_bootstrap_role_worktree_validation(self) -> None:
        missing = (self.root / "missing-worktree").resolve()
        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(self.root / ".tmux-team" / "generated.toml"),
            "--session",
            "tt-bootstrap-missing",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "collector",
            "--role-worktree",
            f"collector={missing}",
            "--dry-run",
        )
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

        code, out, err = self.run_main(
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(self.root / ".tmux-team" / "generated.toml"),
            "--session",
            "tt-bootstrap-create",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "collector",
            "--role-worktree",
            f"collector={missing}",
            "--create-missing-worktrees",
            "--worktree-base-ref",
            "origin/main",
            "--dry-run",
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"git -C {self.root.resolve()} worktree add {missing} origin/main", out)

    def test_bootstrap_duplicate_explicit_worktree_requires_allow_flag(self) -> None:
        shared = self.root / "shared-wt"
        shared.mkdir()
        base_args = (
            "bootstrap",
            "--project-root",
            str(self.root),
            "--config",
            str(self.root / ".tmux-team" / "generated.toml"),
            "--session",
            "tt-bootstrap-shared",
            "--endpoint",
            "ws://127.0.0.1:4500",
            "--roles",
            "collector,trainer",
            "--role-worktree",
            f"collector={shared}",
            "--role-worktree",
            f"trainer={shared}",
        )

        code, _out, err = self.run_main(*base_args, "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("roles share worktree", err)

        code, _out, err = self.run_main(*base_args, "--allow-shared-worktree", "collector,trainer", "--dry-run")
        self.assertEqual(code, 0, err)

    def test_role_worktree_dirty_tracked_files_require_allow_flag(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(BootstrapError, "dirty tracked files"):
            prepare_role_worktrees(
                repo,
                ("worker",),
                {"worker": repo},
                create_missing_worktrees=False,
                worktree_base_ref="HEAD",
                allow_shared_worktree_groups=(),
                allow_dirty_roles=frozenset(),
                dry_run=False,
            )

        worktrees, commands = prepare_role_worktrees(
            repo,
            ("worker",),
            {"worker": repo},
            create_missing_worktrees=False,
            worktree_base_ref="HEAD",
            allow_shared_worktree_groups=(),
            allow_dirty_roles=frozenset({"worker"}),
            dry_run=False,
        )
        self.assertEqual(worktrees["worker"], repo.resolve())
        self.assertEqual(commands, [])

    def test_sleep_dry_run_plans_managed_window_teardown(self) -> None:
        self.write_remote_tui_config()

        code, out, err = self.run_cli("sleep", "--dry-run")

        self.assertEqual(code, 0, err)
        self.assertIn("snapshot: (dry-run)", out)
        self.assertIn("roles: 2", out)
        self.assertIn("roles: target=tt:tt-agents", out)
        self.assertIn("app-server: target=tt:tt-app-server", out)
        self.assertIn("tmux kill-window -t tt:tt-agents", out)
        self.assertIn("tmux kill-window -t tt:tt-app-server", out)
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

    def test_resume_dry_run_plans_codex_resume_from_sleep_snapshot(self) -> None:
        self.write_remote_tui_config()
        snapshot = self.write_sleep_snapshot()

        code, out, err = self.run_cli("resume", "--dry-run", "--no-start-app-server")

        self.assertEqual(code, 0, err)
        self.assertIn(f"snapshot: {snapshot.resolve()}", out)
        self.assertIn("session: tt", out)
        self.assertIn("endpoint: ws://127.0.0.1:4500", out)
        self.assertIn("roles: 2", out)
        self.assertIn("orchestrator: thread_id=thread-orch", out)
        self.assertIn("implementer: thread_id=thread-impl", out)
        self.assertIn("codex resume", out)
        self.assertIn("--remote ws://127.0.0.1:4500 thread-orch", out)
        self.assertIn("--remote ws://127.0.0.1:4500 thread-impl", out)
        self.assertIn("tmux new-window -t tt -n tt-agents", out)
        self.assertIn("tmux split-window -t tt:tt-agents", out)
        self.assertIn("dry-run: no tmux panes created", out)

    def test_app_server_wake_prompt_tells_role_to_drain_multiple_messages(self) -> None:
        store = Store(TeamConfig(name="test", runtime_dir=self.root / "runtime", roles={}))

        prompt = store.app_server_wake_prompt("implementer", 3)

        self.assertIn("3 pending", prompt)
        self.assertIn("Wake notice only", prompt)
        self.assertIn("loaded role loop", prompt)
        self.assertIn("drain until empty", prompt)
        self.assertNotIn("tmux-team inbox next", prompt)
        self.assertNotIn("tmux-team", prompt)
        self.assertNotIn("tmux-team inbox ack", prompt)
        self.assertNotIn("tmux-team inbox complete", prompt)
        self.assertNotIn("--reply-to-sender", prompt)
        self.assertNotIn("tmux-team memory append", prompt)
        self.assertNotIn("start-tmux-team", prompt)

    def test_app_server_wake_prompt_uses_pane_env_instead_of_config_path(self) -> None:
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

        self.assertIn("Inbox wake", prompt)
        self.assertNotIn("tmux-team", prompt)
        self.assertNotIn("--config", prompt)
        self.assertNotIn("--role implementer", prompt)
        self.assertNotIn(str(self.root), prompt)

    def test_app_server_wake_prompt_omits_absolute_config_for_role_worktree(self) -> None:
        config_path = self.root / ".tmux-team" / "team.toml"
        role_worktree = self.root / "implementer-worktree"
        store = Store(
            TeamConfig(
                name="test",
                runtime_dir=self.root / "runtime",
                roles={"implementer": RoleConfig(name="implementer", worktree=str(role_worktree))},
                config_path=config_path,
                project_root=self.root,
            )
        )

        prompt = store.app_server_wake_prompt("implementer", 1)

        self.assertIn("Wake notice only", prompt)
        self.assertNotIn(str(config_path), prompt)
        self.assertNotIn("--role implementer", prompt)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        return self.run_main("--config", str(self.config), *args)

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([*args])
        return code, stdout.getvalue(), stderr.getvalue()

    def write_fake_tmux(self, inspection_output: str, *, allow_send_keys: bool = False) -> tuple[Path, Path]:
        fake_dir = self.root / "bin"
        fake_dir.mkdir()
        log_path = self.root / "tmux.log"
        tmux = fake_dir / "tmux"
        send_keys_block = (
            "exit 0" if allow_send_keys else "printf 'send-keys should not be called in this test\\n' >&2\n  exit 9"
        )
        tmux.write_text(
            f"""#!/bin/sh
printf '%s\\n' "$*" >> {log_path}
if [ "$1" = "display-message" ] && [ "$2" = "-p" ]; then
  printf '{inspection_output}'
  exit 0
fi
if [ "$1" = "send-keys" ]; then
  {send_keys_block}
fi
exit 0
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        return fake_dir, log_path

    def write_remote_tui_config(self) -> None:
        runtime = self.root / "runtime"
        orchestrator = self.root / "orchestrator"
        implementer = self.root / "implementer"
        orchestrator.mkdir(exist_ok=True)
        implementer.mkdir(exist_ok=True)
        self.config.write_text(
            f"""[team]
name = "test-team"
runtime_dir = "{runtime}"

[roles.orchestrator]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:tt-agents.0"
worktree = "{orchestrator}"
notify_method = "app-server-turn"
app_server_endpoint = "ws://127.0.0.1:4500"
codex_thread_id = "thread-orch"

[roles.implementer]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:tt-agents.1"
worktree = "{implementer}"
notify_method = "app-server-turn"
app_server_endpoint = "ws://127.0.0.1:4500"
codex_thread_id = "thread-impl"
""",
            encoding="utf-8",
        )

    def write_sleep_snapshot(self) -> Path:
        sleep_dir = self.root / "runtime" / "sleeps"
        sleep_dir.mkdir(parents=True)
        orchestrator = self.root / "orchestrator"
        implementer = self.root / "implementer"
        snapshot = sleep_dir / "latest.toml"
        snapshot.write_text(
            f"""schema_version = 1
sleep_id = "sleep_test"
created_at = "2026-07-03T00:00:00+00:00"
dry_run = false
pause_roles = true

[team]
name = "test-team"
project_root = "{self.root}"
config_path = "{self.config}"
runtime_dir = "{self.root / "runtime"}"

[tmux]
session = "tt"
kill_session = false

[roles.orchestrator]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:tt-agents.0"
worktree = "{orchestrator}"

[roles.orchestrator.capabilities]
notify_method = "app-server-turn"

[roles.orchestrator.app_server]
endpoint = "ws://127.0.0.1:4500"
thread_id = "thread-orch"
timeout = 10.0

[roles.orchestrator.tmux]
target = "tt:tt-agents.0"
session = "tt"
window_id = "@3"
window_name = "tt-agents"
pane_id = "%10"
pane_title = "tt-orchestrator"
pane_dead = false
current_command = "codex"
live = true

[roles.implementer]
mode = "app_server_remote_tui"
state = "active"
pane = "tt:tt-agents.1"
worktree = "{implementer}"

[roles.implementer.capabilities]
notify_method = "app-server-turn"

[roles.implementer.app_server]
endpoint = "ws://127.0.0.1:4500"
thread_id = "thread-impl"
timeout = 10.0

[roles.implementer.tmux]
target = "tt:tt-agents.1"
session = "tt"
window_id = "@3"
window_name = "tt-agents"
pane_id = "%11"
pane_title = "tt-implementer"
pane_dead = false
current_command = "codex"
live = true
""",
            encoding="utf-8",
        )
        return snapshot

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
    tt:tt-agents.0) printf 'tt\\t@3\\ttt-agents\\t%%10\\ttt-orchestrator\\t0\\tbash\\n'; exit 0 ;;
    tt:tt-agents.1) printf 'tt\\t@3\\ttt-agents\\t%%11\\ttt-implementer\\t0\\tbash\\n'; exit 0 ;;
  esac
  printf 'unknown target %s\\n' "$4" >&2
  exit 1
fi
if [ "$1" = "list-windows" ]; then
  printf '@1\\ttt-control\\n@2\\ttt-app-server\\n@3\\ttt-agents\\n'
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
