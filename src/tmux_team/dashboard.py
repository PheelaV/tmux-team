from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from .config import TeamConfig, role_scratchpad_path
from .store import CLAIMABLE_STATES, STALE_CLAIMED_STATE, Store, parse_utc_datetime


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
    watches: tuple[dict[str, object], ...]
    watchdog_runners: tuple[dict[str, object], ...]
    milestones: tuple[str, ...]
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
    watch_rows: list[dict[str, object]] = []
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
                    "message_id": str(row["id"]),
                    "state": str(row_value(row, "display_state", row["state"])),
                    "priority": str(row["priority"]),
                    "sender": str(row["sender"]),
                    "summary": str(row["summary"]),
                    "age": format_age(str(row["created_at"])),
                    "todos": todos,
                }
            )

        for watch in store.list_watches(conn, role=role_name, states=("active", "blocked"), limit=active_limit):
            overdue = is_overdue(watch["next_update_at"])
            if overdue:
                alerts.append(f"{role_name}: watch overdue {watch['id']} {watch['current_summary']}")
            watch_rows.append(
                {
                    "role": role_name,
                    "watch_id": str(watch["id"]),
                    "state": str(watch["status"]),
                    "summary": str(watch["current_summary"]),
                    "updated": format_age(str(watch["updated_at"])),
                    "next_update": str(watch["next_update_at"] or "-"),
                    "overdue": overdue,
                }
            )

        memory_path = role_scratchpad_path(store.config, role_name)
        memory_rows.append(
            {
                "role": role_name,
                "excerpt": read_excerpt(memory_path, memory_chars) or "(missing or empty)",
            }
        )

        if include_pane_preview and role["pane"]:
            pane_rows.append(
                {
                    "role": role_name,
                    "pane": str(role["pane"]),
                    "text": capture_pane_tail(tmux_bin, str(role["pane"]), pane_lines),
                }
            )

    for runner in store.list_watchdog_runners(conn, limit=active_limit):
        display_state = watchdog_runner_display_state(runner, stale_grace_seconds=60)
        if display_state in ("stale", "failed"):
            alerts.append(f"watchdog {runner['name']}: {display_state} {runner['last_finding_summary'] or ''}".rstrip())
        watchdog_rows.append(
            {
                "name": str(runner["name"]),
                "state": display_state,
                "interval": format_seconds_duration(int(runner["interval_seconds"])),
                "scope": str(runner["scope_role"] or "team"),
                "delivery": str(runner["delivery_method"]),
                "last_run": str(runner["last_run_at"] or "-"),
                "next_run": str(runner["next_run_at"] or "-"),
                "findings": int(runner["last_finding_count"]),
                "summary": str(runner["last_finding_summary"] or "-"),
                "pane": str(runner["pane"] or "-"),
                "safe_to_close": "yes" if display_state in ("stopped", "failed") else "no",
            }
        )

    milestone_lines = tuple(format_milestone_line(row) for row in store.list_milestones(limit=milestone_limit))
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
        watches=tuple(watch_rows),
        watchdog_runners=tuple(watchdog_rows),
        milestones=milestone_lines,
        memories=tuple(memory_rows),
        pane_previews=tuple(pane_rows),
        alerts=tuple(alerts),
    )


def render_dashboard_snapshot(snapshot: DashboardSnapshot) -> str:
    lines = [
        f"tmux-team dashboard  team={snapshot.team}  at={snapshot.collected_at}",
        f"config={snapshot.config_path}",
        f"runtime={snapshot.runtime_dir}",
        "",
        "Alerts",
    ]
    lines.extend(f"  ! {alert}" for alert in snapshot.alerts) if snapshot.alerts else lines.append("  none")
    lines.extend(["", "Roles"])
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
    lines.extend(["", "Active Work"])
    lines.extend(indent_lines(active_lines(snapshot.active_messages), "  "))
    lines.extend(["", "Watches"])
    lines.extend(indent_lines(watch_lines(snapshot.watches), "  "))
    lines.extend(["", "Watchdog Runners"])
    lines.extend(indent_lines(watchdog_lines(snapshot.watchdog_runners), "  "))
    lines.extend(["", "Milestones"])
    lines.extend(f"  {line}" for line in snapshot.milestones) if snapshot.milestones else lines.append("  none")
    lines.extend(["", "Memory"])
    lines.extend(indent_lines(memory_lines(snapshot.memories), "  "))
    if snapshot.pane_previews:
        lines.extend(["", "Pane Preview"])
        lines.extend(indent_lines(pane_lines(snapshot.pane_previews, tail_count=6, truncate_at=None), "  "))
    return "\n".join(lines).rstrip() + "\n"


def run_textual_dashboard(
    config: TeamConfig,
    *,
    role_filter: str | None,
    refresh: float,
    include_pane_preview: bool,
    pane_lines: int,
    tmux_bin: str,
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
        BINDINGS: ClassVar = [("q", "quit", "Quit"), ("r", "refresh_now", "Refresh")]
        CSS = """
        Screen { layout: vertical; }
        #top { height: 8; }
        #summary, #alerts { width: 1fr; border: solid $primary; padding: 0 1; }
        DataTable { height: 10; border: solid $accent; }
        .panel { border: solid $secondary; padding: 0 1; margin: 1 0 0 0; }
        """

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="top"):
                yield Static(id="summary")
                yield Static(id="alerts")
            yield DataTable(id="roles")
            with VerticalScroll():
                yield Static(id="active", classes="panel")
                yield Static(id="watches", classes="panel")
                yield Static(id="watchdogs", classes="panel")
                yield Static(id="milestones", classes="panel")
                yield Static(id="memory", classes="panel")
                yield Static(id="panes", classes="panel")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#roles", DataTable)
            table.zebra_stripes = True
            table.cursor_type = "row"
            table.add_columns("Role", "State", "Pane", "Pending", "Claimed", "Ack", "Stale", "Todos", "Active")
            self.refresh_dashboard()
            self.set_interval(refresh, self.refresh_dashboard)

        def action_refresh_now(self) -> None:
            self.refresh_dashboard()

        def refresh_dashboard(self) -> None:
            store = Store(config)
            with store.connect() as conn:
                snapshot = collect_dashboard_snapshot(
                    store,
                    conn,
                    role_filter=role_filter,
                    include_pane_preview=include_pane_preview,
                    pane_lines=pane_lines,
                    tmux_bin=tmux_bin,
                )
            self.query_one("#summary", Static).update(summary_panel(snapshot, refresh))
            self.query_one("#alerts", Static).update(alerts_panel(snapshot.alerts))
            table = self.query_one("#roles", DataTable)
            table.clear(columns=False)
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
            self.query_one("#active", Static).update(
                section_panel("Active Work", active_lines(snapshot.active_messages))
            )
            self.query_one("#watches", Static).update(
                section_panel("Watches", watch_lines(snapshot.watches, rich=True))
            )
            self.query_one("#watchdogs", Static).update(
                section_panel("Watchdog Runners", watchdog_lines(snapshot.watchdog_runners, rich=True))
            )
            self.query_one("#milestones", Static).update(lines_panel("Milestones", snapshot.milestones))
            self.query_one("#memory", Static).update(
                section_panel("Memory", memory_lines(snapshot.memories, truncate_at=140))
            )
            pane_body = (
                pane_lines(snapshot.pane_previews, tail_count=5, truncate_at=140)
                if include_pane_preview
                else ["disabled"]
            )
            self.query_one("#panes", Static).update(section_panel("Pane Preview", pane_body))

    DashboardApp().run()
    return 0


def summary_panel(snapshot: DashboardSnapshot, refresh: float) -> str:
    return "\n".join(
        [
            f"[b]team[/b] {snapshot.team}",
            f"[b]collected[/b] {snapshot.collected_at}",
            f"[b]refresh[/b] {refresh:g}s",
            f"[b]runtime[/b] {snapshot.runtime_dir}",
            f"[b]config[/b] {snapshot.config_path}",
        ]
    )


def alerts_panel(alerts: Iterable[str]) -> str:
    rows = list(alerts)
    if not rows:
        return "[b]alerts[/b]\nnone"
    return "[b]alerts[/b]\n" + "\n".join(f"[red]![/red] {truncate(row, 120)}" for row in rows[:8])


def section_panel(title: str, rows: Iterable[str]) -> str:
    values = list(rows)
    if not values:
        values = ["none"]
    return f"[b]{title}[/b]\n" + "\n".join(values)


def active_lines(rows: Iterable[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        lines.append(
            f"{row_text(row, 'role')} {row_text(row, 'message_id')} {row_text(row, 'state')} "
            f"{row_text(row, 'priority')} from={row_text(row, 'sender')} age={row_text(row, 'age')} "
            f"{row_text(row, 'summary')}"
        )
        for todo in row_strings(row, "todos"):
            lines.append(f"  [ ] {todo}")
    if not seen:
        lines.append("none")
    return lines


def watch_lines(rows: Iterable[dict[str, object]], *, rich: bool = False) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        marker = "[red]OVERDUE[/red]" if rich and bool(row.get("overdue")) else "OVERDUE"
        marker = marker if bool(row.get("overdue")) else row_text(row, "state")
        lines.append(
            f"{row_text(row, 'role')} {row_text(row, 'watch_id')} {marker} "
            f"updated={row_text(row, 'updated')} next={row_text(row, 'next_update')} {row_text(row, 'summary')}"
        )
    if not seen:
        lines.append("none")
    return lines


def watchdog_lines(rows: Iterable[dict[str, object]], *, rich: bool = False) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        state = row_text(row, "state")
        if rich and state in ("stale", "failed"):
            state = f"[red]{state}[/red]"
        lines.append(
            f"{row_text(row, 'name')} {state} interval={row_text(row, 'interval')} "
            f"scope={row_text(row, 'scope')} delivery={row_text(row, 'delivery')} "
            f"last={row_text(row, 'last_run')} next={row_text(row, 'next_run')} "
            f"findings={row_text(row, 'findings')} safe_to_close={row_text(row, 'safe_to_close')} "
            f"pane={row_text(row, 'pane')} {row_text(row, 'summary')}"
        )
    if not seen:
        lines.append("none")
    return lines


def lines_panel(title: str, rows: Iterable[str]) -> str:
    values = list(rows)
    if not values:
        values = ["none"]
    return f"[b]{title}[/b]\n" + "\n".join(truncate(row, 140) for row in values)


def memory_lines(rows: Iterable[dict[str, object]], *, truncate_at: int | None = None) -> list[str]:
    lines: list[str] = []
    for row in rows:
        line = f"{row_text(row, 'role')}: {first_content_line(row_text(row, 'excerpt'))}"
        lines.append(truncate(line, truncate_at) if truncate_at else line)
    return lines or ["none"]


def pane_lines(
    rows: Iterable[dict[str, object]],
    *,
    tail_count: int,
    truncate_at: int | None,
) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        lines.append(f"{row_text(row, 'role')} {row_text(row, 'pane')}:")
        tail = row_text(row, "text").splitlines()[-tail_count:] or ["(empty)"]
        for line in tail:
            rendered = truncate(line, truncate_at) if truncate_at else line
            lines.append(f"  {rendered}")
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


def format_milestone_line(row: dict) -> str:
    role = row.get("role") or "-"
    kind = row.get("kind") or "milestone"
    return f"{row.get('created_at')} [{kind}] role={role} {row.get('summary')}"


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
