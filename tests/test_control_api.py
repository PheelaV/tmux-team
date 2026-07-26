from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest import mock

from tmux_team.config import load_config
from tmux_team.control_api import (
    API_VERSION,
    MAX_PAGE_SIZE,
    ControlAPIError,
    ReadOnlyControl,
    call_operation,
    list_operations,
    serve_control_protocol,
)
from tmux_team.store import Store


class ReadOnlyControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.config_path = self.root / ".tmux-team" / "team.toml"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            f"""[team]
name = "control-test"
runtime_dir = "{self.runtime}"

[operator]
pane = "%99"
codex_thread_id = "thread-operator-secret"

[roles.orchestrator]
mode = "app_server_remote_tui"
state = "active"
pane = "private-session:tt-agents.0"
worktree = "/private/worktrees/orchestrator"
app_server_endpoint = "ws://127.0.0.1:9999"
codex_thread_id = "thread-orchestrator-secret"

[roles.implementer]
mode = "app_server_remote_tui"
state = "paused"
pane = "private-session:tt-agents.1"
worktree = "/private/worktrees/implementer"
""",
            encoding="utf-8",
        )
        self.config = load_config(self.config_path)
        self.store = Store(self.config)
        with closing(self.store.connect()) as conn:
            self.store.create_message(
                conn,
                sender="operator",
                recipient="orchestrator",
                priority="urgent",
                summary="bounded work",
                body="private body",
            )
            self.store.record_event(
                conn,
                "test.sensitive",
                "operator",
                "safe-ref",
                {
                    "endpoint": "ws://127.0.0.1:9999",
                    "thread_id": "thread-orchestrator-secret",
                    "body_path": "/private/messages/body.md",
                    "pane": "%99",
                    "worktree": "/private/worktrees/orchestrator",
                },
            )
            conn.commit()
        self.control = ReadOnlyControl(self.config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_read_only_connection_rejects_mutation(self) -> None:
        with closing(self.control.connect()) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                conn.execute(
                    "INSERT INTO events(created_at, type, actor, ref_id, payload_json) VALUES (?, ?, ?, ?, ?)",
                    ("2026-07-26T00:00:00+00:00", "bad.write", "test", None, "{}"),
                )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)

    def test_open_does_not_create_or_initialize_missing_database(self) -> None:
        missing_runtime = self.root / "missing-runtime"
        config = load_config(self.config_path, missing_runtime)
        control = ReadOnlyControl(config)

        with self.assertRaisesRegex(ControlAPIError, "does not exist"):
            control.connect()

        self.assertFalse(missing_runtime.exists())

    def test_open_does_not_migrate_schema(self) -> None:
        writer = sqlite3.connect(self.store.db_path)
        writer.execute("PRAGMA user_version = 8")
        writer.close()

        with self.assertRaisesRegex(ControlAPIError, "schema version 8"):
            self.control.connect()

        check = sqlite3.connect(self.store.db_path)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 8)
        finally:
            check.close()

    def test_open_rejects_symlinked_database(self) -> None:
        real_database = self.runtime / "elsewhere.sqlite"
        self.store.db_path.rename(real_database)
        self.store.db_path.symlink_to(real_database)

        with self.assertRaisesRegex(ControlAPIError, "refusing symlinked"):
            self.control.connect()

    def test_status_is_bounded_structured_and_field_allowlisted(self) -> None:
        with closing(self.control.connect()) as conn:
            status = self.control.status(conn)

        self.assertEqual(status["api_version"], API_VERSION)
        self.assertEqual(status["kind"], "status")
        self.assertEqual(status["team"], {"name": "control-test"})
        roles = {role["name"]: role for role in status["roles"]}
        self.assertEqual(roles["orchestrator"]["counts"]["pending"], 1)
        self.assertEqual(roles["orchestrator"]["counts"]["urgent"], 1)
        self.assertEqual(roles["implementer"]["state"], "paused")
        self.assert_no_private_bindings(status)

    def test_status_state_and_event_cursor_share_one_snapshot(self) -> None:
        setup = sqlite3.connect(self.store.db_path)
        try:
            self.assertEqual(setup.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        finally:
            setup.close()
        writer = sqlite3.connect(self.store.db_path)
        original_counts = self.control._message_counts
        committed = False

        def commit_between_reads(
            conn: sqlite3.Connection,
            roles: list[str],
            observed_at: str,
        ) -> dict[str, dict[str, int]]:
            nonlocal committed
            if not committed:
                writer.execute("UPDATE roles SET state = 'paused' WHERE name = 'orchestrator'")
                self.store.record_event(
                    writer,
                    "role.paused",
                    "operator",
                    "orchestrator",
                    {},
                )
                writer.commit()
                committed = True
            return original_counts(conn, roles, observed_at)

        try:
            with closing(self.control.connect()) as conn:
                before_cursor = int(conn.execute("SELECT MAX(id) FROM events").fetchone()[0])
                with mock.patch.object(self.control, "_message_counts", side_effect=commit_between_reads):
                    status = self.control.status(conn)
            with closing(self.control.connect()) as conn:
                events = self.control.events_since(conn, {"cursor": status["snapshot"]["event_cursor"]})
        finally:
            writer.close()

        roles = {role["name"]: role for role in status["roles"]}
        self.assertEqual(roles["orchestrator"]["state"], "active")
        self.assertEqual(status["snapshot"]["event_cursor"], before_cursor)
        self.assertEqual([event["type"] for event in events["events"]], ["role.paused"])

    def test_events_since_uses_cursor_pagination_and_omits_payload(self) -> None:
        with closing(self.store.connect()) as writer:
            for index in range(5):
                self.store.record_event(
                    writer,
                    f"test.page_{index}",
                    "orchestrator",
                    f"ref-{index}",
                    {"summary": f"event {index}", "worktree": f"/private/{index}"},
                )
            writer.commit()

        with closing(self.control.connect()) as conn:
            first = self.control.events_since(conn, {"cursor": 0, "limit": 3})
            second = self.control.events_since(conn, {"cursor": first["cursor"]["next"], "limit": MAX_PAGE_SIZE})

        self.assertEqual(first["api_version"], API_VERSION)
        self.assertEqual(len(first["events"]), 3)
        self.assertTrue(first["cursor"]["has_more"])
        self.assertGreater(first["cursor"]["next"], first["cursor"]["requested"])
        self.assertTrue(second["events"])
        self.assertFalse(second["cursor"]["has_more"])
        first_cursors = [event["cursor"] for event in first["events"]]
        second_cursors = [event["cursor"] for event in second["events"]]
        self.assertEqual(first_cursors, sorted(first_cursors))
        self.assertTrue(set(first_cursors).isdisjoint(second_cursors))
        self.assert_no_private_bindings({"first": first, "second": second})

    def test_events_since_high_cursor_requires_reset(self) -> None:
        with closing(self.control.connect()) as conn:
            page = self.control.events_since(conn, {"cursor": 999_999, "limit": 1})

        self.assertEqual(page["events"], [])
        self.assertEqual(page["cursor"]["requested"], 999_999)
        self.assertTrue(page["cursor"]["reset_required"])
        self.assertEqual(page["cursor"]["next"], page["cursor"]["upper_bound"])

    def test_inputs_are_strict_and_bounded(self) -> None:
        with closing(self.control.connect()) as conn:
            invalid_calls = (
                lambda: self.control.status(conn, {"verbose": True}),
                lambda: self.control.events_since(conn, {"limit": 0}),
                lambda: self.control.events_since(conn, {"limit": MAX_PAGE_SIZE + 1}),
                lambda: self.control.events_since(conn, {"cursor": True}),
                lambda: self.control.milestones({"scope": "all"}),
                lambda: self.control.milestones({"subject_role": "x" * 129}),
                lambda: self.control.milestones({"since": "2026-07-26"}),
                lambda: self.control.obligations(conn, {"status": "unknown"}),
            )
            for invalid_call in invalid_calls:
                with self.subTest(call=invalid_call), self.assertRaises(ControlAPIError):
                    invalid_call()

    def test_milestones_query_filters_and_field_allowlists(self) -> None:
        with closing(self.store.connect()) as writer:
            self.store.record_milestone(
                writer,
                actor="orchestrator",
                summary="Verification passed",
                body="secret detail in /private/worktrees/orchestrator",
                subject_roles=("implementer",),
                kind="result",
                ref_id="commit:abc123",
                tags=("verified",),
                metadata={"worktree": "/private/worktrees/orchestrator"},
            )
            self.store.record_milestone(
                writer,
                actor="orchestrator",
                summary="Team checkpoint",
                body="another secret",
                scope="team",
                kind="routing",
            )
            writer.commit()

        result = self.control.milestones({"subject_role": "implementer", "kind": "result", "limit": 1})

        self.assertEqual(result["api_version"], API_VERSION)
        self.assertEqual(len(result["milestones"]), 1)
        milestone = result["milestones"][0]
        self.assertEqual(milestone["summary"], "Verification passed")
        self.assertEqual(milestone["subject_roles"], ["implementer"])
        self.assertEqual(milestone["reference"], "commit:abc123")
        self.assertNotIn("body", milestone)
        self.assertNotIn("metadata", milestone)
        self.assert_no_private_bindings(result)

    def test_milestones_reject_symlinked_log(self) -> None:
        external_log = self.root / "external-milestones.jsonl"
        external_log.write_text(
            json.dumps(
                {
                    "created_at": "2026-07-26T00:00:00+00:00",
                    "kind": "result",
                    "summary": "must not be read",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.store.milestones_path.symlink_to(external_log)

        with self.assertRaisesRegex(ControlAPIError, "safely open"):
            self.control.milestones()

    def test_milestones_reject_non_regular_log(self) -> None:
        os.mkfifo(self.store.milestones_path)

        with self.assertRaisesRegex(ControlAPIError, "not a regular file"):
            self.control.milestones()

    def test_obligations_are_filtered_bounded_and_field_allowlisted(self) -> None:
        with closing(self.store.connect()) as writer:
            obligation = self.store.start_obligation(
                writer,
                role="orchestrator",
                summary="Investigate safely",
                goal="private goal /private/worktrees/orchestrator",
                created_by="operator",
                next_update_at="2026-07-27T00:00:00+00:00",
            )
            self.store.update_obligation(
                writer,
                role="orchestrator",
                obligation_id=str(obligation["id"]),
                summary="Blocked on evidence",
                status="blocked",
                next_update_at="2026-07-27T01:00:00+00:00",
            )

        with closing(self.control.connect()) as conn:
            result = self.control.obligations(
                conn,
                {"role": "orchestrator", "status": "blocked", "limit": 1},
            )

        self.assertEqual(result["api_version"], API_VERSION)
        self.assertEqual(result["kind"], "obligations")
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["obligations"]), 1)
        row = result["obligations"][0]
        self.assertEqual(row["role"], "orchestrator")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["current_summary"], "Blocked on evidence")
        self.assertNotIn("goal", row)
        self.assertNotIn("created_by", row)
        self.assert_no_private_bindings(result)

    def test_tool_catalog_contains_only_read_only_control_tools(self) -> None:
        tools = list_operations()

        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "team_control_status",
                "team_control_events_since",
                "team_control_milestones",
                "team_control_obligations",
            },
        )
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])

    def test_control_protocol_does_not_expose_existing_mutation_tools(self) -> None:
        input_stream = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "control/list", "params": {}})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "control/call",
                    "params": {"name": "team_control_status", "arguments": {}},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "control/call",
                    "params": {"name": "team_send", "arguments": {"to": "orchestrator", "summary": "mutate"}},
                }
            )
            + "\n"
        )
        output_stream = StringIO()

        with closing(self.control.connect()) as conn:
            before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            serve_control_protocol(self.control, conn, input_stream, output_stream)
            after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(responses[0]["result"]["api_version"], API_VERSION)
        self.assertEqual(responses[1]["result"]["kind"], "status")
        self.assertEqual(responses[2]["error"]["message"], "unknown read-only control operation")
        self.assertEqual(after, before)

    def test_direct_tool_dispatch_rejects_non_object_arguments(self) -> None:
        with closing(self.control.connect()) as conn, self.assertRaisesRegex(ControlAPIError, "must be an object"):
            call_operation(self.control, conn, "team_control_status", [])  # type: ignore[arg-type]

    def assert_no_private_bindings(self, value: object) -> None:
        serialized = json.dumps(value, sort_keys=True)
        for forbidden in (
            "endpoint",
            "thread_id",
            "body_path",
            '"pane"',
            "worktree",
            "ws://127.0.0.1:9999",
            "thread-orchestrator-secret",
            "/private/worktrees/orchestrator",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
