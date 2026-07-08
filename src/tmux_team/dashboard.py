from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from .config import TeamConfig, role_scratchpad_path
from .store import (
    CLAIMABLE_STATES,
    STALE_CLAIMED_STATE,
    OBLIGATION_VISIBLE_STATES,
    Store,
    inspect_tmux_pane,
    parse_utc_datetime,
)


class DashboardDependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class DashboardSnapshot:
    team: str
    config_path: str
    runtime_dir: str
    collected_at: str
    roles: tuple[dict[str, object], ...]
    active_messages: tuple[dict[str, object], ...]
    obligations: tuple[dict[str, object], ...]
    watchdog_runners: tuple[dict[str, object], ...]
    milestones: tuple[dict[str, object], ...]
    memories: tuple[dict[str, object], ...]
    pane_previews: tuple[dict[str, object], ...]
    alerts: tuple[str, ...]


def collect_dashboard_snapshot(
    store: Store,
    conn,
    *,
    role_filter: str | None = None,
    include_pane_preview: bool = True,
    pane_lines: int = 8,
    tmux_bin: str = "tmux",
    active_limit: int = 5,
    milestone_limit: int = 8,
    memory_chars: int = 260,
) -> DashboardSnapshot:
    roles = [role for role in store.list_roles(conn) if role_filter is None or role["name"] == role_filter]
    if role_filter is not None and not roles:
        raise KeyError(f"Unknown role: {role_filter}")

    counts = store.active_counts(conn)
    role_rows: list[dict[str, object]] = []
    active_rows: list[dict[str, object]] = []
    obligation_rows: list[dict[str, object]] = []
    watchdog_rows: list[dict[str, object]] = []
    memory_rows: list[dict[str, object]] = []
    pane_rows: list[dict[str, object]] = []
    alerts: list[str] = []

    for role in roles:
        role_name = str(role["name"])
        active = store.list_active_messages(conn, role=role_name, limit=active_limit)
        in_progress = [row for row in active if row["state"] in ("claimed", "acknowledged")]
        todo_counts = store.open_todo_counts(conn, role=role_name, message_ids=(row["id"] for row in active))
        role_counts = counts.get(role_name, {})
        stale_claimed = role_counts.get(STALE_CLAIMED_STATE, 0)
        pending = sum(role_counts.get(state, 0) for state in CLAIMABLE_STATES) + stale_claimed
        active_summary = in_progress[0]["summary"] if in_progress else "-"

        open_todos = sum(todo_counts.values())
        role_rows.append(
            {
                "name": role_name,
                "source": "runtime-db",
                "confidence": "authoritative",
                "state": str(role["state"]),
                "mode": str(role["mode"]),
                "pane": str(role["pane"] or "-"),
                "worktree": str(role["worktree"] or "-"),
                "pending": pending,
                "stale_claimed": stale_claimed,
                "claimed": role_counts.get("claimed", 0),
                "acknowledged": role_counts.get("acknowledged", 0),
                "completed": role_counts.get("completed", 0),
                "open_todos": open_todos,
                "active_summary": str(active_summary),
            }
        )

        if pending and role["state"] != "active":
            alerts.append(f"{role_name}: {pending} pending while role is {role['state']}")
        if stale_claimed:
            alerts.append(f"{role_name}: {stale_claimed} stale claimed message(s)")

        for row in active:
            todos = tuple(
                str(todo["text"])
                for todo in store.list_todos(conn, role=role_name, message_id=row["id"], states=("open",), limit=10)
            )
            active_rows.append(
                {
                    "role": role_name,
                    "source": "runtime-db",
                    "todo_source": "todo",
                    "confidence": "authoritative",
                    "message_id": str(row["id"]),
                    "state": str(row_value(row, "display_state", row["state"])),
                    "priority": str(row["priority"]),
                    "sender": str(row["sender"]),
                    "summary": str(row["summary"]),
                    "age": format_age(str(row["created_at"])),
                    "todos": todos,
                }
            )

        for obligation in store.list_obligations(
            conn, role=role_name, states=OBLIGATION_VISIBLE_STATES, limit=active_limit
        ):
            paused = obligation["status"] == "paused"
            overdue = not paused and is_overdue(obligation["next_update_at"])
            review_due = paused and is_overdue(obligation["review_at"])
            if overdue:
                alerts.append(f"{role_name}: obligation overdue {obligation['id']} {obligation['current_summary']}")
            if review_due:
                alerts.append(
                    f"{role_name}: obligation review due "
                    f"{obligation['id']} {obligation['paused_reason'] or obligation['current_summary']}"
                )
            obligation_rows.append(
                {
                    "role": role_name,
                    "source": "runtime-db",
                    "confidence": "authoritative",
                    "obligation_id": str(obligation["id"]),
                    "state": str(obligation["status"]),
                    "summary": str(obligation["current_summary"]),
                    "updated": format_age(str(obligation["updated_at"])),
                    "next_update": str(obligation["next_update_at"] or "-"),
                    "paused_reason": str(obligation["paused_reason"] or "-"),
                    "review_at": str(obligation["review_at"] or "-"),
                    "overdue": overdue,
                    "review_due": review_due,
                }
            )

        memory_path = role_scratchpad_path(store.config, role_name)
        memory_rows.append(
            {
                "role": role_name,
                "source": "memory-excerpt",
                "confidence": "operator-authored-prose",
                "excerpt": read_excerpt(memory_path, memory_chars) or "(missing or empty)",
            }
        )

        if include_pane_preview and role["pane"]:
            pane_preview = capture_pane_preview(tmux_bin, str(role["pane"]), pane_lines)
            pane_rows.append(
                {
                    "role": role_name,
                    "pane": str(role["pane"]),
                    "source": "pane-capture",
                    "screen_source": "screen-text-heuristic",
                    "confidence": "best-effort",
                    **pane_preview,
                }
            )

    for runner in store.list_watchdog_runners(conn, limit=active_limit):
        display_state = watchdog_runner_display_state(runner, stale_grace_seconds=60)
        review_due = runner["state"] == "paused" and is_overdue(runner["review_at"])
        if display_state in ("stale", "failed"):
            alerts.append(f"watchdog {runner['name']}: {display_state} {runner['last_finding_summary'] or ''}".rstrip())
        if review_due:
            alerts.append(f"watchdog {runner['name']}: review due {runner['paused_reason'] or ''}".rstrip())
        watchdog_rows.append(
            {
                "name": str(runner["name"]),
                "source": "watchdog",
                "confidence": "runtime-db",
                "state": display_state,
                "interval": format_seconds_duration(int(runner["interval_seconds"])),
                "scope": str(runner["scope_role"] or "team"),
                "delivery": str(runner["delivery_method"]),
                "last_run": str(runner["last_run_at"] or "-"),
                "next_run": str(runner["next_run_at"] or "-"),
                "findings": int(runner["last_finding_count"]),
                "summary": str(runner["last_finding_summary"] or "-"),
                "paused_reason": str(runner["paused_reason"] or "-"),
                "review_at": str(runner["review_at"] or "-"),
                "pane": str(runner["pane"] or "-"),
                "safe_to_close": "yes" if display_state in ("stopped", "failed") else "no",
            }
        )

    milestone_rows = store.list_milestones(role=role_filter, limit=milestone_limit)
    recent_failures = conn.execute(
        """
        SELECT role, method, state, details
        FROM notifications
        WHERE state IN ('notify_failed', 'notify_deferred')
        ORDER BY id DESC
        LIMIT 8
        """
    ).fetchall()
    for row in recent_failures:
        alerts.append(f"{row['role']}: {row['state']} via {row['method']}: {row['details']}")

    return DashboardSnapshot(
        team=store.config.name,
        config_path=str(store.config.config_path or "(auto-discovered)"),
        runtime_dir=str(store.runtime_dir),
        collected_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        roles=tuple(role_rows),
        active_messages=tuple(active_rows),
        obligations=tuple(obligation_rows),
        watchdog_runners=tuple(watchdog_rows),
        milestones=tuple(milestone_rows),
        memories=tuple(memory_rows),
        pane_previews=tuple(pane_rows),
        alerts=tuple(alerts),
    )


def render_dashboard_snapshot(snapshot: DashboardSnapshot, *, provenance: bool = False) -> str:
    lines = [
        f"tmux-team dashboard  team={snapshot.team}  at={snapshot.collected_at}",
        f"config={snapshot.config_path}",
        f"runtime={snapshot.runtime_dir}",
        "",
        "Alerts [source=runtime-db/watchdog]",
    ]
    lines.extend(f"  ! {alert}" for alert in snapshot.alerts) if snapshot.alerts else lines.append("  none")
    lines.extend(["", "Roles [source=runtime-db]"])
    lines.extend(
        format_table(
            ("role", "state", "pane", "pending", "claimed", "ack", "stale", "todos", "active"),
            (
                (
                    row_text(row, "name"),
                    row_text(row, "state"),
                    row_text(row, "pane"),
                    row_text(row, "pending"),
                    row_text(row, "claimed"),
                    row_text(row, "acknowledged"),
                    row_text(row, "stale_claimed"),
                    row_text(row, "open_todos"),
                    truncate(row_text(row, "active_summary"), 54),
                )
                for row in snapshot.roles
            ),
        )
    )
    lines.extend(["", "Active Work [source=runtime-db todo]"])
    lines.extend(indent_lines(active_lines(snapshot.active_messages, provenance=provenance), "  "))
    lines.extend(["", "Obligations [source=runtime-db]"])
    lines.extend(indent_lines(obligation_lines(snapshot.obligations, provenance=provenance), "  "))
    lines.extend(["", "Watchdog Runners [source=watchdog/runtime-db]"])
    lines.extend(indent_lines(watchdog_lines(snapshot.watchdog_runners, provenance=provenance), "  "))
    lines.extend(["", "Milestones [source=milestone-jsonl]"])
    if snapshot.milestones:
        lines.extend(f"  {format_milestone_line(row, provenance=provenance)}" for row in snapshot.milestones)
    else:
        lines.append("  none")
    lines.extend(["", "Memory Excerpts [source=memory-excerpt prose]"])
    lines.extend(indent_lines(memory_lines(snapshot.memories, provenance=provenance), "  "))
    if snapshot.pane_previews:
        lines.extend(["", "Pane Preview [source=pane-capture best-effort screen-text-heuristic]"])
        lines.extend(
            indent_lines(
                format_pane_preview_lines(
                    snapshot.pane_previews, tail_count=6, truncate_at=None, provenance=provenance
                ),
                "  ",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def run_textual_dashboard(
    config: TeamConfig,
    *,
    role_filter: str | None,
    refresh: float,
    include_pane_preview: bool,
    pane_line_count: int,
    tmux_bin: str,
    provenance: bool = False,
) -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, VerticalScroll
        from textual.widgets import DataTable, Footer, Header, Static
    except ImportError as exc:
        raise DashboardDependencyError(
            "Textual dashboard support is not installed. Install with `uv tool install 'tmux-team[dashboard] @ "
            "git+https://github.com/PheelaV/tmux-team.git'` or `pipx install 'tmux-team[dashboard] @ "
            "git+https://github.com/PheelaV/tmux-team.git'`."
        ) from exc

    class DashboardApp(App):
        TITLE = "tmux-team dashboard"
        BINDINGS: ClassVar = [
            ("q", "quit", "Quit"),
            ("r", "refresh_now", "Refresh"),
            ("h", "toggle_help", "Help"),
            ("tab", "focus_next", "Next"),
            ("shift+tab", "focus_previous", "Previous"),
            ("escape", "clear_filter", "Team"),
            ("f", "filter_focused_role", "Filter role"),
            ("a", "show_section('alerts')", "Alerts"),
            ("t", "show_section('roles')", "Roles"),
            ("o", "show_section('obligations')", "Obligations"),
            ("d", "show_section('watchdogs')", "Watchdogs"),
            ("m", "show_section('milestones')", "Milestones"),
            ("p", "show_section('panes')", "Panes"),
            ("1", "filter_role_1", "Role 1"),
            ("2", "filter_role_2", "Role 2"),
            ("3", "filter_role_3", "Role 3"),
            ("4", "filter_role_4", "Role 4"),
            ("5", "filter_role_5", "Role 5"),
            ("6", "filter_role_6", "Role 6"),
            ("7", "filter_role_7", "Role 7"),
            ("8", "filter_role_8", "Role 8"),
            ("9", "filter_role_9", "Role 9"),
            ("0", "filter_role_10", "Role 10"),
        ]
        CSS = """
        Screen { layout: vertical; }
        #top { height: 8; }
        #summary, #alerts { width: 1fr; border: solid $primary; padding: 0 1; }
        DataTable { height: 10; border: solid $accent; }
        .panel { border: solid $secondary; padding: 0 1; margin: 1 0 0 0; }
        #help { height: 8; border: heavy $warning; padding: 0 1; display: none; }
        #help.visible { display: block; }
        """

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="top"):
                yield Static(id="summary")
                yield Static(id="alerts")
            yield DataTable(id="roles")
            yield Static(id="help")
            with VerticalScroll():
                yield Static(id="active", classes="panel")
                yield Static(id="obligations", classes="panel")
                yield Static(id="watchdogs", classes="panel")
                yield Static(id="milestones", classes="panel")
                yield Static(id="memory", classes="panel")
                yield Static(id="panes", classes="panel")
            yield Footer()

        def on_mount(self) -> None:
            self.role_filter = role_filter
            self.role_order = tuple(config.roles)
            self.visible_roles = self.role_order
            self.help_visible = False
            table = self.query_one("#roles", DataTable)
            table.zebra_stripes = True
            table.cursor_type = "row"
            table.add_columns("Role", "State", "Pane", "Pending", "Claimed", "Ack", "Stale", "Todos", "Active")
            self.refresh_dashboard()
            self.set_interval(refresh, self.refresh_dashboard)

        def action_refresh_now(self) -> None:
            self.refresh_dashboard()

        def action_toggle_help(self) -> None:
            self.help_visible = not self.help_visible
            help_panel = self.query_one("#help", Static)
            help_panel.set_class(self.help_visible, "visible")
            help_panel.update(help_text())

        def action_clear_filter(self) -> None:
            self.role_filter = None
            self.refresh_dashboard()

        def action_show_section(self, section_id: str) -> None:
            try:
                self.query_one(f"#{section_id}").scroll_visible()
            except Exception:
                pass

        def action_filter_focused_role(self) -> None:
            table = self.query_one("#roles", DataTable)
            row_index = getattr(table, "cursor_row", 0)
            if 0 <= row_index < len(self.visible_roles):
                self.role_filter = self.visible_roles[row_index]
                self.refresh_dashboard()

        def _filter_role_number(self, number: int) -> None:
            index = number - 1
            if 0 <= index < len(self.role_order):
                self.role_filter = self.role_order[index]
                self.refresh_dashboard()

        def action_filter_role_1(self) -> None:
            self._filter_role_number(1)

        def action_filter_role_2(self) -> None:
            self._filter_role_number(2)

        def action_filter_role_3(self) -> None:
            self._filter_role_number(3)

        def action_filter_role_4(self) -> None:
            self._filter_role_number(4)

        def action_filter_role_5(self) -> None:
            self._filter_role_number(5)

        def action_filter_role_6(self) -> None:
            self._filter_role_number(6)

        def action_filter_role_7(self) -> None:
            self._filter_role_number(7)

        def action_filter_role_8(self) -> None:
            self._filter_role_number(8)

        def action_filter_role_9(self) -> None:
            self._filter_role_number(9)

        def action_filter_role_10(self) -> None:
            self._filter_role_number(10)

        def refresh_dashboard(self) -> None:
            store = Store(config)
            with store.connect() as conn:
                snapshot = collect_dashboard_snapshot(
                    store,
                    conn,
                    role_filter=self.role_filter,
                    include_pane_preview=include_pane_preview,
                    pane_lines=pane_line_count,
                    tmux_bin=tmux_bin,
                )
            self.query_one("#summary", Static).update(summary_panel(snapshot, refresh, self.role_filter))
            self.query_one("#alerts", Static).update(alerts_panel(snapshot.alerts))
            table = self.query_one("#roles", DataTable)
            cursor_row = getattr(table, "cursor_row", 0)
            table.clear(columns=False)
            self.visible_roles = tuple(row_text(row, "name") for row in snapshot.roles)
            for row in snapshot.roles:
                table.add_row(
                    row_text(row, "name"),
                    row_text(row, "state"),
                    row_text(row, "pane"),
                    row_text(row, "pending"),
                    row_text(row, "claimed"),
                    row_text(row, "acknowledged"),
                    row_text(row, "stale_claimed"),
                    row_text(row, "open_todos"),
                    truncate(row_text(row, "active_summary"), 64),
                )
            if snapshot.roles:
                try:
                    table.move_cursor(row=min(cursor_row, len(snapshot.roles) - 1))
                except Exception:
                    pass
            self.query_one("#active", Static).update(
                section_panel(
                    "Active Work [runtime-db + todo]",
                    active_lines(snapshot.active_messages, rich=True, provenance=provenance),
                )
            )
            self.query_one("#obligations", Static).update(
                section_panel(
                    "Obligations [runtime-db]",
                    obligation_lines(snapshot.obligations, rich=True, provenance=provenance),
                )
            )
            self.query_one("#watchdogs", Static).update(
                section_panel(
                    "Watchdog Runners [watchdog/runtime-db]",
                    watchdog_lines(snapshot.watchdog_runners, rich=True, provenance=provenance),
                )
            )
            self.query_one("#milestones", Static).update(
                lines_panel(
                    "Milestones [milestone-jsonl]",
                    (format_milestone_line(row, provenance=provenance) for row in snapshot.milestones),
                    rich=True,
                )
            )
            self.query_one("#memory", Static).update(
                section_panel(
                    "Memory Excerpts [memory-excerpt prose]",
                    memory_lines(snapshot.memories, truncate_at=140, rich=True, provenance=provenance),
                )
            )
            pane_body = textual_pane_preview_body(snapshot, include_pane_preview=include_pane_preview)
            self.query_one("#panes", Static).update(section_panel("Pane Preview [best-effort pane-capture]", pane_body))

    DashboardApp().run()
    return 0


def summary_panel(snapshot: DashboardSnapshot, refresh: float, role_filter: str | None = None) -> str:
    scope = role_filter or "team"
    return "\n".join(
        [
            f"[b]team[/b] {rich_escape(snapshot.team)}",
            f"[b]scope[/b] {rich_escape(scope)}",
            f"[b]collected[/b] {rich_escape(snapshot.collected_at)}",
            f"[b]refresh[/b] {refresh:g}s",
            f"[b]runtime[/b] {rich_escape(snapshot.runtime_dir)}",
            f"[b]config[/b] {rich_escape(snapshot.config_path)}",
        ]
    )


def alerts_panel(alerts: Iterable[str]) -> str:
    rows = list(alerts)
    if not rows:
        return "[b]alerts[/b]\nnone"
    return "[b]alerts[/b]\n" + "\n".join(f"[red]![/red] {rich_escape(truncate(row, 120))}" for row in rows[:8])


def help_text() -> str:
    return "\n".join(
        [
            "[b]tmux-team dashboard keys[/b]",
            "r refresh  q quit  h help  tab/shift-tab focus  escape team overview",
            "f filter to focused role row  1-9 role rows 1-9  0 role row 10",
            "jumps: a alerts  t roles  o obligations  d watchdogs  m milestones  p panes",
            "Sources: runtime-db is authoritative; memory-excerpt is prose; pane-capture is best-effort screen text.",
        ]
    )


def section_panel(title: str, rows: Iterable[str]) -> str:
    values = list(rows)
    if not values:
        values = ["none"]
    return f"[b]{title}[/b]\n" + "\n".join(values)


def active_lines(rows: Iterable[dict[str, object]], *, rich: bool = False, provenance: bool = False) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        source = provenance_suffix(row, provenance)
        lines.append(
            f"{safe_row_text(row, 'role', rich)} {safe_row_text(row, 'message_id', rich)} "
            f"{safe_row_text(row, 'state', rich)} {safe_row_text(row, 'priority', rich)} "
            f"from={safe_row_text(row, 'sender', rich)} age={safe_row_text(row, 'age', rich)} "
            f"{safe_row_text(row, 'summary', rich)}{source}"
        )
        for todo in row_strings(row, "todos"):
            marker = r"\[ ]" if rich else "[ ]"
            lines.append(f"  {marker} {rich_escape(todo) if rich else todo}")
    if not seen:
        lines.append("none")
    return lines


def obligation_lines(rows: Iterable[dict[str, object]], *, rich: bool = False, provenance: bool = False) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        if bool(row.get("overdue")):
            marker = "[red]OVERDUE[/red]" if rich else "OVERDUE"
        elif bool(row.get("review_due")):
            marker = "[yellow]REVIEW[/yellow]" if rich else "REVIEW"
        elif row_text(row, "state") == "paused":
            marker = "[yellow]PAUSED[/yellow]" if rich else "paused"
        else:
            marker = row_text(row, "state")
        pause = ""
        if row_text(row, "state") == "paused":
            pause = (
                f" review={safe_row_text(row, 'review_at', rich)} reason={safe_row_text(row, 'paused_reason', rich)}"
            )
        lines.append(
            f"{safe_row_text(row, 'role', rich)} {safe_row_text(row, 'obligation_id', rich)} {marker} "
            f"updated={safe_row_text(row, 'updated', rich)} next={safe_row_text(row, 'next_update', rich)}{pause} "
            f"{safe_row_text(row, 'summary', rich)}{provenance_suffix(row, provenance)}"
        )
    if not seen:
        lines.append("none")
    return lines


def watchdog_lines(rows: Iterable[dict[str, object]], *, rich: bool = False, provenance: bool = False) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        state = row_text(row, "state")
        if rich and state in ("stale", "failed"):
            state = f"[red]{state}[/red]"
        elif rich and state == "paused":
            state = f"[yellow]{state}[/yellow]"
        pause = ""
        if row_text(row, "state") == "paused":
            pause = (
                f" review={safe_row_text(row, 'review_at', rich)} reason={safe_row_text(row, 'paused_reason', rich)}"
            )
        lines.append(
            f"{safe_row_text(row, 'name', rich)} {state} interval={safe_row_text(row, 'interval', rich)} "
            f"scope={safe_row_text(row, 'scope', rich)} delivery={safe_row_text(row, 'delivery', rich)} "
            f"last={safe_row_text(row, 'last_run', rich)} next={safe_row_text(row, 'next_run', rich)} "
            f"findings={safe_row_text(row, 'findings', rich)} safe_to_close={safe_row_text(row, 'safe_to_close', rich)} "
            f"pane={safe_row_text(row, 'pane', rich)}{pause} {safe_row_text(row, 'summary', rich)}"
            f"{provenance_suffix(row, provenance)}"
        )
    if not seen:
        lines.append("none")
    return lines


def lines_panel(title: str, rows: Iterable[str], *, rich: bool = False) -> str:
    values = list(rows)
    if not values:
        values = ["none"]
    rendered = [rich_escape(truncate(row, 140)) if rich else truncate(row, 140) for row in values]
    return f"[b]{title}[/b]\n" + "\n".join(rendered)


def textual_pane_preview_body(snapshot: DashboardSnapshot, *, include_pane_preview: bool) -> list[str]:
    if not include_pane_preview:
        return ["disabled"]
    return format_pane_preview_lines(snapshot.pane_previews, tail_count=5, truncate_at=140, rich=True, provenance=True)


def memory_lines(
    rows: Iterable[dict[str, object]], *, truncate_at: int | None = None, rich: bool = False, provenance: bool = False
) -> list[str]:
    lines: list[str] = []
    for row in rows:
        line = (
            f"{safe_row_text(row, 'role', rich)}: "
            f"{rich_escape(first_content_line(row_text(row, 'excerpt'))) if rich else first_content_line(row_text(row, 'excerpt'))}"
            f"{provenance_suffix(row, provenance)}"
        )
        lines.append(truncate(line, truncate_at) if truncate_at else line)
    return lines or ["none"]


def format_pane_preview_lines(
    rows: Iterable[dict[str, object]],
    *,
    tail_count: int,
    truncate_at: int | None,
    rich: bool = False,
    provenance: bool = False,
) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        meta = (
            f"command={safe_row_text(row, 'current_command', rich)} "
            f"dead={safe_row_text(row, 'dead', rich)} "
            f"copy_mode={safe_row_text(row, 'in_mode', rich)} "
            f"screen_source={safe_row_text(row, 'screen_source', rich)}"
        )
        lines.append(
            f"{safe_row_text(row, 'role', rich)} {safe_row_text(row, 'pane', rich)} "
            f"[best-effort] {meta}{provenance_suffix(row, provenance)}:"
        )
        tail = row_text(row, "text").splitlines()[-tail_count:] or ["(empty)"]
        for line in tail:
            rendered = truncate(line, truncate_at) if truncate_at else line
            lines.append(f"  {rich_escape(rendered) if rich else rendered}")
    if not seen:
        lines.append("none")
    return lines


def capture_pane_tail(tmux_bin: str, pane: str, lines: int) -> str:
    if lines <= 0:
        return ""
    result = subprocess.run(
        [tmux_bin, "capture-pane", "-p", "-t", pane, "-S", f"-{lines}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"{tmux_bin} exited {result.returncode}").strip()
        return f"(pane capture failed: {details})"
    return result.stdout.strip()


def capture_pane_preview(tmux_bin: str, pane: str, lines: int) -> dict[str, object]:
    ok, state = inspect_tmux_pane(tmux_bin, pane)
    if ok and not isinstance(state, str):
        dead = state.dead
        in_mode = state.in_mode
        current_command = state.current_command or "-"
        inspection_error = None
    else:
        dead = "-"
        in_mode = "-"
        current_command = "-"
        inspection_error = state if isinstance(state, str) else "pane inspection failed"
    return {
        "text": capture_pane_tail(tmux_bin, pane, lines),
        "dead": dead,
        "in_mode": in_mode,
        "current_command": current_command,
        "inspection_error": inspection_error,
    }


def read_excerpt(path: Path, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def first_line(value: str) -> str:
    stripped = value.strip()
    return stripped.splitlines()[0] if stripped else "-"


def first_content_line(value: str) -> str:
    for line in value.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return first_line(value)


def format_milestone_line(row: dict, *, provenance: bool = False) -> str:
    kind = row.get("kind") or "milestone"
    recorded_by = row.get("recorded_by") or row.get("actor") or "-"
    return (
        f"{row.get('created_at')} [{kind}] recorded_by={recorded_by} "
        f"subject={milestone_subject_label(row)} {row.get('summary')}"
        f"{provenance_suffix({'source': 'milestone-jsonl', 'confidence': 'operator-recorded'}, provenance)}"
    )


def milestone_subject_label(row: dict) -> str:
    if row.get("scope") == "team":
        return "team"
    subject_roles = tuple(str(role) for role in row.get("subject_roles") or ())
    if subject_roles:
        return ",".join(subject_roles)
    return str(row.get("role") or "-")


def format_table(headers: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> list[str]:
    materialized = [tuple(str(cell) for cell in row) for row in rows]
    widths = [len(header) for header in headers]
    for row in materialized:
        for index, cell in enumerate(row):
            widths[index] = min(max(widths[index], len(cell)), 64)
    lines = ["  " + "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  " + "  ".join("-" * width for width in widths))
    if not materialized:
        lines.append("  none")
        return lines
    for row in materialized:
        lines.append(
            "  " + "  ".join(truncate(cell, widths[index]).ljust(widths[index]) for index, cell in enumerate(row))
        )
    return lines


def format_age(created_at: str) -> str:
    age = datetime.now(UTC) - parse_utc_datetime(created_at)
    seconds = max(0, int(age.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def is_overdue(value: str | None) -> bool:
    if not value:
        return False
    return parse_utc_datetime(str(value)) <= datetime.now(UTC)


def watchdog_runner_display_state(row, stale_grace_seconds: int) -> str:
    if row["state"] != "running":
        return str(row["state"])
    next_run_at = row["next_run_at"]
    if not next_run_at:
        return "stale"
    stale_at = parse_utc_datetime(str(next_run_at)) + timedelta(seconds=stale_grace_seconds)
    if stale_at < datetime.now(UTC):
        return "stale"
    return "running"


def format_seconds_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def truncate(value: str, limit: int | None) -> str:
    if limit is None:
        return value
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def row_text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    return "-" if value is None else str(value)


def safe_row_text(row: dict[str, object], key: str, rich: bool) -> str:
    value = row_text(row, key)
    return rich_escape(value) if rich else value


def provenance_suffix(row: dict[str, object], enabled: bool) -> str:
    if not enabled:
        return ""
    source = row.get("source") or "-"
    confidence = row.get("confidence") or "-"
    return f" source={source} confidence={confidence}"


def rich_escape(value: object) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def row_strings(row: dict[str, object], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if value is None:
        return ()
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


def indent_lines(rows: Iterable[str], prefix: str) -> list[str]:
    return [f"{prefix}{row}" for row in rows]


def row_value(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default
