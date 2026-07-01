from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tmux_team.config import RoleConfig, TeamConfig
from tmux_team.store import Message, Store


class StoreInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(
            TeamConfig(
                name="test-team",
                runtime_dir=self.root / "runtime",
                roles={
                    "sender": RoleConfig(name="sender"),
                    "worker": RoleConfig(name="worker"),
                    "other": RoleConfig(name="other"),
                },
            )
        )
        self.conn = self.store.connect()

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_concurrent_claim_next_returns_one_winner_for_one_message(self) -> None:
        message = self.create_message()
        worker_count = 12
        barrier = threading.Barrier(worker_count)
        lock = threading.Lock()
        results: list[str | None] = []
        errors: list[Exception] = []

        def worker() -> None:
            conn = self.open_thread_connection()
            try:
                barrier.wait(timeout=5)
                row = self.store.claim_next(conn, "worker", 60)
                with lock:
                    results.append(row["id"] if row is not None else None)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        hanging = [thread.name for thread in threads if thread.is_alive()]
        self.assertEqual(hanging, [])
        self.assertEqual([repr(error) for error in errors], [])
        self.assertEqual(results.count(message.id), 1)
        self.assertEqual(results.count(None), worker_count - 1)

        stored = self.conn.execute("SELECT state, claimed_by FROM messages WHERE id = ?", (message.id,)).fetchone()
        self.assertEqual(stored["state"], "claimed")
        self.assertEqual(stored["claimed_by"], "worker")

    def test_claim_next_reclaims_expired_claimed_message(self) -> None:
        message = self.create_message()
        stale_time = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0).isoformat()
        self.conn.execute(
            """
            UPDATE messages
            SET state = 'claimed', claimed_by = 'worker', claim_expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (stale_time, stale_time, message.id),
        )
        self.conn.commit()

        row = self.store.claim_next(self.conn, "worker", 300)

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["id"], message.id)
        self.assertEqual(row["state"], "claimed")
        self.assertEqual(row["claimed_by"], "worker")
        self.assertGreater(row["claim_expires_at"], stale_time)

    def test_claim_next_does_not_reclaim_unexpired_claimed_message(self) -> None:
        message = self.create_message()
        future_time = (datetime.now(UTC) + timedelta(minutes=5)).replace(microsecond=0).isoformat()
        self.conn.execute(
            """
            UPDATE messages
            SET state = 'claimed', claimed_by = 'worker', claim_expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (future_time, future_time, message.id),
        )
        self.conn.commit()

        self.assertIsNone(self.store.claim_next(self.conn, "worker", 300))

    def test_ack_and_complete_require_claimed_or_acknowledged_state(self) -> None:
        queued = self.create_message()
        with self.assertRaisesRegex(ValueError, "Cannot acknowledge"):
            self.store.ack_message(self.conn, "worker", queued.id)
        with self.assertRaisesRegex(ValueError, "Cannot complete"):
            self.store.complete_message(self.conn, "worker", queued.id, "done", "not allowed")

        claimed = self.store.claim_next(self.conn, "worker", 60)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["id"], queued.id)

        acknowledged = self.store.ack_message(self.conn, "worker", queued.id)
        self.assertEqual(acknowledged["state"], "acknowledged")
        acknowledged_again = self.store.ack_message(self.conn, "worker", queued.id)
        self.assertEqual(acknowledged_again["state"], "acknowledged")

        completed = self.store.complete_message(self.conn, "worker", queued.id, "done", "finished")
        self.assertEqual(completed["state"], "completed")
        with self.assertRaisesRegex(ValueError, "Cannot acknowledge"):
            self.store.ack_message(self.conn, "worker", queued.id)
        with self.assertRaisesRegex(ValueError, "Cannot complete"):
            self.store.complete_message(self.conn, "worker", queued.id, "done", "already done")

        second = self.create_message(summary="complete directly")
        claimed_second = self.store.claim_next(self.conn, "worker", 60)
        self.assertIsNotNone(claimed_second)
        assert claimed_second is not None
        self.assertEqual(claimed_second["id"], second.id)
        completed_from_claimed = self.store.complete_message(self.conn, "worker", second.id, "done", "finished")
        self.assertEqual(completed_from_claimed["state"], "completed")

    def test_ack_and_complete_require_recipient(self) -> None:
        message = self.create_message()
        claimed = self.store.claim_next(self.conn, "worker", 60)
        self.assertIsNotNone(claimed)

        with self.assertRaises(PermissionError):
            self.store.ack_message(self.conn, "other", message.id)
        with self.assertRaises(PermissionError):
            self.store.complete_message(self.conn, "other", message.id, "done", "not yours")

    def create_message(self, summary: str = "work") -> Message:
        return self.store.create_message(
            self.conn,
            sender="sender",
            recipient="worker",
            priority="normal",
            summary=summary,
            body="body",
        )

    def open_thread_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.store.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn


if __name__ == "__main__":
    unittest.main()
