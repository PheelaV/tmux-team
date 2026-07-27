from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tmux_team.config import load_config
from tmux_team.service import NotificationResult
from tmux_team.store import Store
from tmux_team.wake_relay import main, relay_once


class WakeRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.config_path = self.root / ".tmux-team" / "team.toml"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            f"""[team]
name = "wake-relay-test"
runtime_dir = "{self.runtime}"

[roles.collector]
mode = "app_server_remote_tui"
state = "active"
notify_method = "app-server-turn"

[roles.paused]
mode = "paused"
state = "paused"
notify_method = "app-server-turn"
""",
            encoding="utf-8",
        )
        self.store = Store(load_config(self.config_path))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _queue(self, conn, recipient: str, summary: str) -> None:
        self.store.create_message(
            conn,
            sender="orchestrator",
            recipient=recipient,
            priority="normal",
            summary=summary,
            body="durable body",
            state="queued",
        )

    def test_relay_coalesces_queued_work_and_skips_paused_roles(self) -> None:
        with closing(self.store.connect()) as conn:
            self._queue(conn, "collector", "first")
            self._queue(conn, "collector", "second")
            self._queue(conn, "paused", "held")

            def notify(_service, relay_conn, role, method, *, actor=None):
                self.assertEqual(role, "collector")
                self.assertEqual(method, "app-server-turn")
                self.assertEqual(actor, "wake-relay")
                relay_conn.execute(
                    "UPDATE messages SET state = 'notified' WHERE recipient = ? AND state = 'queued'",
                    (role,),
                )
                relay_conn.commit()
                return NotificationResult(True, "submitted", method)

            with patch("tmux_team.wake_relay.TeamService.notify_role", new=notify):
                delivered, failed = relay_once(self.store, conn)

            self.assertEqual((delivered, failed), (1, 0))
            states = conn.execute(
                "SELECT recipient, state FROM messages ORDER BY recipient, created_at"
            ).fetchall()
            self.assertEqual(
                [(row["recipient"], row["state"]) for row in states],
                [
                    ("collector", "notified"),
                    ("collector", "notified"),
                    ("paused", "queued"),
                ],
            )

    def test_failed_delivery_remains_queued_for_retry(self) -> None:
        with closing(self.store.connect()) as conn:
            self._queue(conn, "collector", "retry me")
            with patch(
                "tmux_team.wake_relay.TeamService.notify_role",
                return_value=NotificationResult(
                    False,
                    "app-server turn submission failed",
                    "app-server-turn",
                ),
            ):
                delivered, failed = relay_once(self.store, conn)
            state = conn.execute(
                "SELECT state FROM messages WHERE recipient = 'collector'"
            ).fetchone()["state"]

        self.assertEqual((delivered, failed), (0, 1))
        self.assertEqual(state, "queued")

    def test_cli_rejects_unsafe_poll_intervals(self) -> None:
        self.assertEqual(
            main(["--config", str(self.config_path), "--interval", "0.01", "--once"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
