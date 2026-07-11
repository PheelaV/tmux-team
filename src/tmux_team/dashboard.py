from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from .config import TeamConfig, role_scratchpad_path
from .display import format_seconds_duration, role_capabilities, watchdog_runner_display_state
from .store import (
    OBLIGATION_VISIBLE_STATES,
    STALE_CLAIMED_STATE,
    Store,
    inspect_tmux_pane,
    parse_utc_datetime,
    pending_count_from_state_counts,
)


class DashboardDependencyError(RuntimeError):
    pass


ROLE_TABLE_HEADERS = ("role", "state", "pane", "pend", "clmd", "ack", "stale", "todo", "runtime", "active")


@dataclass(frozen=True)
class DashboardAlert:
    text: str
    created_at: str | None = None
    age: str | None = None
    stale: bool = False


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
    alerts: tuple[DashboardAlert, ...]
    alert_history: tuple[DashboardAlert, ...]


@dataclass(frozen=True)
class SemanticPalette:
    section: str = "cyan"
    label: str = "cyan"
    role: str = "magenta"
    identifier: str = "blue"
    datetime: str = "green"
    warning: str = "yellow"
    error: str = "red"
    success: str = "green"
    text: str = "white"


DEFAULT_SEMANTIC_PALETTE = SemanticPalette()
ALERTS_RECENT_LIMIT = 5
ALERTS_HISTORY_LIMIT = 50
ALERTS_RECENT_WINDOW_SECONDS = 30 * 60
DASHBOARD_PREFERENCES_FILE = "dashboard_preferences.json"


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
    alert_limit: int = ALERTS_HISTORY_LIMIT,
) -> DashboardSnapshot:
    all_roles = store.list_roles(conn)
    all_role_names = tuple(str(role["name"]) for role in all_roles)
    roles = [role for role in all_roles if role_filter is None or role["name"] == role_filter]
    if role_filter is not None and not roles:
        raise KeyError(f"Unknown role: {role_filter}")

    counts = store.active_counts(conn)
    role_rows: list[dict[str, object]] = []
    active_rows: list[dict[str, object]] = []
    obligation_rows: list[dict[str, object]] = []
    watchdog_rows: list[dict[str, object]] = []
    memory_rows: list[dict[str, object]] = []
    pane_rows: list[dict[str, object]] = []
    alerts: list[DashboardAlert] = []

    for role in roles:
        role_name = str(role["name"])
        active = store.list_active_messages(conn, role=role_name, limit=active_limit)
        in_progress = [row for row in active if row["state"] in ("claimed", "acknowledged")]
        todo_counts = store.open_todo_counts(conn, role=role_name, message_ids=(row["id"] for row in active))
        role_counts = counts.get(role_name, {})
        stale_claimed = role_counts.get(STALE_CLAIMED_STATE, 0)
        pending = pending_count_from_state_counts(role_counts)
        active_summary = in_progress[0]["summary"] if in_progress else "-"

        open_todos = sum(todo_counts.values())
        capabilities = role_capabilities(role)
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
                "runtime_settings": dashboard_runtime_chips(str(role["mode"]), capabilities),
            }
        )

        if pending and role["state"] != "active":
            alerts.append(DashboardAlert(f"{role_name}: {pending} pending while role is {role['state']}"))
        if stale_claimed:
            alerts.append(DashboardAlert(f"{role_name}: {stale_claimed} stale claimed message(s)"))

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
                alerts.append(
                    DashboardAlert(
                        f"{role_name}: obligation overdue {obligation['id']} {obligation['current_summary']}"
                    )
                )
            if review_due:
                alerts.append(
                    DashboardAlert(
                        f"{role_name}: obligation review due "
                        f"{obligation['id']} {obligation['paused_reason'] or obligation['current_summary']}"
                    )
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
                "updated": file_updated_at(memory_path),
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

    for runner in store.list_watchdog_runners(conn, limit=alert_limit):
        if role_filter is not None and not watchdog_matches_role(runner, role_filter, all_role_names=all_role_names):
            continue
        display_state = watchdog_runner_display_state(runner, stale_grace_seconds=60)
        review_due = runner["state"] == "paused" and is_overdue(runner["review_at"])
        if display_state in ("stale", "failed"):
            alerts.append(
                DashboardAlert(
                    f"watchdog {runner['name']}: {display_state} {runner['last_finding_summary'] or ''}".rstrip()
                )
            )
        if review_due:
            alerts.append(
                DashboardAlert(f"watchdog {runner['name']}: review due {runner['paused_reason'] or ''}".rstrip())
            )
        if len(watchdog_rows) >= active_limit:
            continue
        watchdog_rows.append(
            {
                "name": str(runner["name"]),
                "source": "watchdog",
                "confidence": "runtime-db",
                "state": display_state,
                "interval": format_seconds_duration(int(runner["interval_seconds"])),
                "scope": str(runner["scope_role"] or "team"),
                "description": str(runner["description"] or "-"),
                "goal": str(runner["goal"] or "-"),
                "notify_role": str(runner["notify_role"] or "-"),
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
    notification_params: tuple[object, ...]
    if role_filter is None:
        notification_where = "state IN ('notify_failed', 'notify_deferred')"
        notification_params = (alert_limit,)
    else:
        notification_where = "role = ? AND state IN ('notify_failed', 'notify_deferred')"
        notification_params = (role_filter, alert_limit)
    recent_failures = conn.execute(
        f"""
        SELECT role, method, state, details, created_at
        FROM notifications
        WHERE {notification_where}
        ORDER BY id DESC
        LIMIT ?
        """,
        notification_params,
    ).fetchall()
    notification_history: list[DashboardAlert] = []
    for row in recent_failures:
        alert = notification_alert(row)
        notification_history.append(alert)
        if not alert.stale:
            alerts.append(alert)
    alert_history = [*alerts, *(alert for alert in notification_history if alert.stale)]

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
        alert_history=tuple(alert_history),
    )


def watchdog_matches_role(runner, role: str, *, all_role_names: tuple[str, ...]) -> bool:
    return runner["scope_role"] == role or watchdog_effective_notify_role(runner, all_role_names) == role


def watchdog_effective_notify_role(runner, all_role_names: tuple[str, ...]) -> str | None:
    if runner["notify_role"]:
        return str(runner["notify_role"])
    if runner["scope_role"]:
        return str(runner["scope_role"])
    if "orchestrator" in all_role_names:
        return "orchestrator"
    return all_role_names[0] if all_role_names else None


def notification_alert(row) -> DashboardAlert:
    created_at = str(row["created_at"])
    age = format_age(created_at)
    stale = datetime.now(UTC) - parse_utc_datetime(created_at) > timedelta(seconds=ALERTS_RECENT_WINDOW_SECONDS)
    text = f"{row['role']}: {row['state']} via {row['method']}: {row['details']}"
    return DashboardAlert(text=text, created_at=created_at, age=age, stale=stale)


def role_shortcut_target(visible_roles: Iterable[str], number: int | str) -> str | None:
    index = int(number) - 1
    roles = tuple(visible_roles)
    if 0 <= index < len(roles):
        return roles[index]
    return None


def dashboard_codex_chips(capabilities: dict[str, object]) -> str:
    chips: list[str] = []
    if capabilities.get("codex_yolo") is True:
        chips.append("yolo")
    for key, label in (
        ("codex_reasoning_effort", "e"),
        ("codex_model", "m"),
        ("codex_profile", "p"),
    ):
        value = capabilities.get(key)
        if value:
            chips.append(f"{label}:{value}")
    config_overrides = capabilities.get("codex_config")
    if isinstance(config_overrides, list) and config_overrides:
        chips.append(f"cfg:{len(config_overrides)}")
    elif config_overrides:
        chips.append("cfg")
    return " ".join(chips) if chips else "-"


def dashboard_runtime_chips(mode: str, capabilities: dict[str, object]) -> str:
    if mode == "acp_tui" or capabilities.get("control_socket"):
        chips = ["acp"]
        provider = capabilities.get("acp_provider")
        if provider:
            chips.append(str(provider))
        return " ".join(chips)
    codex = dashboard_codex_chips(capabilities)
    return "codex" if codex == "-" else f"codex {codex}"


def render_dashboard_snapshot(snapshot: DashboardSnapshot, *, provenance: bool = False) -> str:
    lines = [
        f"tmux-team dashboard  team={snapshot.team}  at={snapshot.collected_at}",
        f"config={snapshot.config_path}",
        f"runtime={snapshot.runtime_dir}",
        "",
        "Alerts [source=runtime-db/watchdog]",
    ]
    if snapshot.alerts:
        lines.extend(f"  {format_plain_alert_line(alert)}" for alert in snapshot.alerts)
    else:
        lines.append("  none")
    lines.extend(["", "Roles [source=runtime-db]"])
    lines.extend(format_table(ROLE_TABLE_HEADERS, role_table_rows(snapshot, codex_limit=32, active_limit=54)))
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


def load_dashboard_preferences(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_dashboard_preferences(path: Path, preferences: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(preferences, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


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
        from rich.text import Text
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Grid, Horizontal, VerticalScroll
        from textual.screen import ModalScreen
        from textual.widgets import DataTable, Footer, Header, Static
    except ImportError as exc:
        raise DashboardDependencyError(
            "Textual dashboard support is not installed. Install with `uv tool install 'tmux-team[dashboard] @ "
            "git+https://github.com/PheelaV/tmux-team.git'` or `pipx install 'tmux-team[dashboard] @ "
            "git+https://github.com/PheelaV/tmux-team.git'`."
        ) from exc

    preferences_path = config.runtime_dir / DASHBOARD_PREFERENCES_FILE

    class HelpScreen(ModalScreen[None]):
        CSS = """
        HelpScreen { align: center middle; }
        #help-dialog {
            width: 82%;
            max-width: 110;
            height: auto;
            border: heavy $warning;
            padding: 1 2;
            background: $surface;
        }
        """
        BINDINGS: ClassVar = [("escape", "dismiss", "Close"), ("h", "dismiss", "Close")]

        def compose(self) -> ComposeResult:
            yield Static(help_text(), id="help-dialog")

        def action_dismiss(self) -> None:
            self.dismiss()

    class DashboardApp(App):
        TITLE = "tmux-team dashboard"
        BINDINGS: ClassVar = [
            ("q", "quit", "Quit"),
            ("r", "refresh_now", "Refresh"),
            ("h", "toggle_help", "Help"),
            Binding("tab", "focus_next", "Next", show=False),
            Binding("shift+tab", "focus_previous", "Previous", show=False),
            ("escape", "clear_filter", "Team"),
            ("f", "filter_focused_role", "Filter role"),
            ("v", "toggle_verbosity", "Verbose"),
            ("[", "previous_page", "Prev page"),
            ("]", "next_page", "Next page"),
            Binding("w", "show_page('supervision')", "Work page", show=False),
            Binding("c", "show_page('records')", "Context page", show=False),
            Binding("a", "show_section('alerts')", "Alerts", show=False),
            Binding("t", "show_section('roles')", "Roles", show=False),
            Binding("o", "show_section('obligations')", "Obligations", show=False),
            Binding("d", "show_section('watchdogs')", "Watchdogs", show=False),
            Binding("m", "show_section('milestones')", "Milestones", show=False),
            Binding("p", "toggle_pane_preview", "Pane preview", show=False),
        ] + [
            Binding(str(number % 10), f"filter_role({number})", f"Role {number}", show=False) for number in range(1, 11)
        ]
        CSS = """
        Screen { layout: vertical; }
        #top { height: 7; }
        #summary, #alerts-recent { width: 1fr; border: solid $primary; padding: 0 1; }
        DataTable { height: 10; border: solid $accent; }
        #sections {
            height: 1fr;
            layout: grid;
            grid-columns: 1fr 1fr;
        }
        #sections.supervision {
            grid-size: 2 2;
            grid-rows: 0.75fr 1.25fr;
        }
        #sections.records {
            grid-size: 2 2;
            grid-rows: 1fr 1fr;
        }
        #sections.records.preview-on {
            grid-size: 2 3;
            grid-rows: 0.9fr 0.9fr 1.1fr;
        }
        #active-scroll, #panes-scroll, #alerts-scroll { column-span: 2; }
        .hidden { display: none; }
        .section-scroll {
            width: 1fr;
            height: 1fr;
            border: solid $secondary;
            padding: 0 1;
            margin: 1 1 0 0;
        }
        .section-scroll:focus { border: heavy $accent; }
        .narrow #top {
            height: 12;
            layout: vertical;
        }
        .narrow #summary, .narrow #alerts-recent {
            width: 1fr;
            height: 6;
        }
        .narrow #sections {
            grid-size: 1;
            grid-columns: 1fr;
            grid-rows: 1fr;
        }
        .narrow #active-scroll, .narrow #panes-scroll, .narrow #alerts-scroll { column-span: 1; }
        """

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="top"):
                yield Static(id="summary")
                yield Static(id="alerts-recent")
            yield DataTable(id="roles")
            with Grid(id="sections"):
                with VerticalScroll(id="active-scroll", classes="section-scroll"):
                    yield Static(id="active")
                with VerticalScroll(id="obligations-scroll", classes="section-scroll"):
                    yield Static(id="obligations")
                with VerticalScroll(id="watchdogs-scroll", classes="section-scroll"):
                    yield Static(id="watchdogs")
                with VerticalScroll(id="milestones-scroll", classes="section-scroll"):
                    yield Static(id="milestones")
                with VerticalScroll(id="memory-scroll", classes="section-scroll"):
                    yield Static(id="memory")
                with VerticalScroll(id="panes-scroll", classes="section-scroll"):
                    yield Static(id="panes")
                with VerticalScroll(id="alerts-scroll", classes="section-scroll"):
                    yield Static(id="alerts-history")
            yield Footer()

        def on_mount(self) -> None:
            self.preferences_path = preferences_path
            self.saved_theme = self.theme
            self.verbose_items = False
            self.saved_verbosity = "concise"
            self.load_dashboard_preferences()
            self.role_filter = role_filter
            self.role_order = tuple(sorted(config.roles))
            self.visible_roles = self.role_order
            self.current_page = "supervision"
            self.pane_preview_enabled = False
            self.section_targets = {
                "alerts": "alerts-scroll",
                "roles": "roles",
                "obligations": "obligations-scroll",
                "watchdogs": "watchdogs-scroll",
                "milestones": "milestones-scroll",
                "panes": "panes-scroll",
            }
            self.update_responsive_layout()
            self.update_page_visibility(reset_scroll=True)
            table = self.query_one("#roles", DataTable)
            table.zebra_stripes = True
            table.cursor_type = "row"
            table.add_columns(*(header.title() for header in ROLE_TABLE_HEADERS))
            self.refresh_dashboard()
            self.set_interval(refresh, self.refresh_dashboard)

        def load_dashboard_preferences(self) -> None:
            preferences = load_dashboard_preferences(self.preferences_path)
            theme_name = str(preferences.get("theme") or "")
            if theme_name and theme_name in self.available_themes:
                self.theme = theme_name
                self.saved_theme = theme_name
            verbosity = str(preferences.get("verbosity") or "")
            if verbosity in {"concise", "verbose"}:
                self.verbose_items = verbosity == "verbose"
                self.saved_verbosity = verbosity

        def persist_dashboard_preferences(self) -> None:
            verbosity = "verbose" if self.verbose_items else "concise"
            if self.theme == self.saved_theme and verbosity == self.saved_verbosity:
                return
            save_dashboard_preferences(self.preferences_path, {"theme": self.theme, "verbosity": verbosity})
            self.saved_theme = self.theme
            self.saved_verbosity = verbosity

        def action_refresh_now(self) -> None:
            self.refresh_dashboard()

        def action_toggle_help(self) -> None:
            self.push_screen(HelpScreen())

        def action_toggle_verbosity(self) -> None:
            self.verbose_items = not self.verbose_items
            self.refresh_dashboard()

        def action_clear_filter(self) -> None:
            self.role_filter = None
            self.refresh_dashboard()

        def action_show_section(self, section_id: str) -> None:
            if section_id in {"active", "obligations", "watchdogs"}:
                self.current_page = "supervision"
            elif section_id in {"alerts", "milestones", "memory"}:
                self.current_page = "records"
            elif section_id == "panes":
                self.current_page = "records"
                self.pane_preview_enabled = True
            self.update_page_visibility(reset_scroll=True)
            if section_id == "panes":
                self.refresh_dashboard()
            target_id = self.section_targets.get(section_id, f"{section_id}-scroll")
            try:
                target = self.query_one(f"#{target_id}")
                target.focus()
                target.scroll_visible()
            except Exception:
                pass

        def action_show_page(self, page: str) -> None:
            if page in {"supervision", "records"}:
                self.current_page = page
                self.update_page_visibility(reset_scroll=True)
                self.refresh_dashboard()

        def action_previous_page(self) -> None:
            self.current_page = "supervision" if self.current_page == "records" else "records"
            self.update_page_visibility(reset_scroll=True)
            self.refresh_dashboard()

        def action_next_page(self) -> None:
            self.action_previous_page()

        def action_toggle_pane_preview(self) -> None:
            self.current_page = "records"
            self.pane_preview_enabled = not self.pane_preview_enabled
            self.update_page_visibility(reset_scroll=True)
            self.refresh_dashboard()

        def action_filter_focused_role(self) -> None:
            table = self.query_one("#roles", DataTable)
            row_index = getattr(table, "cursor_row", 0)
            if 0 <= row_index < len(self.visible_roles):
                self.role_filter = self.visible_roles[row_index]
                self.refresh_dashboard()

        def action_filter_role(self, number: int | str) -> None:
            shortcut_roles = self.visible_roles if self.role_filter is None else self.role_order
            target = role_shortcut_target(shortcut_roles, number)
            if target is not None:
                self.role_filter = target
                self.refresh_dashboard()

        def on_resize(self, event) -> None:
            self.update_responsive_layout()

        def update_responsive_layout(self) -> None:
            width = getattr(getattr(self, "screen", None), "size", None)
            columns = getattr(width, "width", 0)
            target = self.screen
            if columns and columns < 118:
                target.add_class("narrow")
            else:
                target.remove_class("narrow")

        def update_page_visibility(self, *, reset_scroll: bool = False) -> None:
            sections = self.query_one("#sections")
            sections.set_class(self.current_page == "supervision", "supervision")
            sections.set_class(self.current_page == "records", "records")
            sections.set_class(self.current_page == "records" and self.pane_preview_enabled, "preview-on")
            visibility = {
                "active-scroll": self.current_page == "supervision",
                "obligations-scroll": self.current_page == "supervision",
                "watchdogs-scroll": self.current_page == "supervision",
                "milestones-scroll": self.current_page == "records",
                "memory-scroll": self.current_page == "records",
                "alerts-scroll": self.current_page == "records",
                "panes-scroll": self.current_page == "records" and self.pane_preview_enabled,
            }
            for widget_id, visible in visibility.items():
                widget = self.query_one(f"#{widget_id}")
                widget.set_class(not visible, "hidden")
                if visible and reset_scroll:
                    try:
                        widget.scroll_home(animate=False, immediate=True)
                    except Exception:
                        pass

        def resolve_semantic_palette(self) -> SemanticPalette:
            theme = self.current_theme
            variables = {**theme.to_color_system().generate(), **theme.variables}

            def token(name: str, fallback: str) -> str:
                value = variables.get(name) or variables.get(name.replace("_", "-")) or fallback
                return str(value)

            return SemanticPalette(
                section=token("accent", "cyan"),
                label=token("primary", "cyan"),
                role=token("secondary", "magenta"),
                identifier=token("accent", "blue"),
                datetime=token("success", "green"),
                warning=token("warning", "yellow"),
                error=token("error", "red"),
                success=token("success", "green"),
                text=token("text", "white"),
            )

        def refresh_dashboard(self) -> None:
            self.persist_dashboard_preferences()
            store = Store(config)
            with closing(store.connect()) as conn:
                snapshot = collect_dashboard_snapshot(
                    store,
                    conn,
                    role_filter=self.role_filter,
                    include_pane_preview=include_pane_preview and self.pane_preview_enabled,
                    pane_lines=pane_line_count,
                    tmux_bin=tmux_bin,
                )
            palette = self.resolve_semantic_palette()
            self.update_page_visibility()
            self.query_one("#summary", Static).update(
                summary_panel(
                    snapshot,
                    refresh,
                    self.role_filter,
                    palette=palette,
                    page=self.current_page,
                    pane_preview_enabled=self.pane_preview_enabled,
                    verbose_items=self.verbose_items,
                )
            )
            self.query_one("#alerts-recent", Static).update(alerts_recent_panel(snapshot.alerts, palette=palette))
            self.query_one("#alerts-history", Static).update(
                section_panel(
                    "Alert History",
                    alert_lines(snapshot.alert_history, palette=palette),
                    palette=palette,
                )
            )
            table = self.query_one("#roles", DataTable)
            cursor_row = getattr(table, "cursor_row", 0)
            table.clear(columns=False)
            self.visible_roles = tuple(row_text(row, "name") for row in snapshot.roles)
            if self.role_filter is None:
                self.role_order = self.visible_roles
            for row in textual_role_table_rows(
                snapshot, text_cls=Text, codex_limit=40, active_limit=64, palette=palette
            ):
                table.add_row(*row)
            if snapshot.roles:
                try:
                    table.move_cursor(row=min(cursor_row, len(snapshot.roles) - 1))
                except Exception:
                    pass
            self.query_one("#active", Static).update(
                section_panel(
                    "Active Work",
                    active_lines(snapshot.active_messages, rich=True, provenance=provenance, palette=palette),
                    palette=palette,
                )
            )
            self.query_one("#obligations", Static).update(
                section_panel(
                    "Obligations",
                    obligation_lines(
                        snapshot.obligations,
                        rich=True,
                        provenance=provenance,
                        palette=palette,
                        verbose=self.verbose_items,
                    ),
                    palette=palette,
                )
            )
            self.query_one("#watchdogs", Static).update(
                section_panel(
                    "Watchdog Runners",
                    watchdog_lines(
                        snapshot.watchdog_runners,
                        rich=True,
                        provenance=provenance,
                        palette=palette,
                        verbose=self.verbose_items,
                    ),
                    palette=palette,
                )
            )
            self.query_one("#milestones", Static).update(
                section_panel(
                    "Milestones",
                    (
                        format_milestone_line(row, rich=True, provenance=provenance, palette=palette)
                        for row in snapshot.milestones
                    ),
                    palette=palette,
                )
            )
            self.query_one("#memory", Static).update(
                section_panel(
                    "Memory Excerpts",
                    memory_lines(snapshot.memories, truncate_at=140, rich=True, provenance=provenance, palette=palette),
                    palette=palette,
                )
            )
            self.query_one("#panes", Static).update(
                section_panel(
                    "Pane Preview",
                    textual_pane_preview_body(
                        snapshot,
                        include_pane_preview=include_pane_preview and self.pane_preview_enabled,
                        rich=True,
                        palette=palette,
                    ),
                    palette=palette,
                )
            )

    DashboardApp().run()
    return 0


def summary_panel(
    snapshot: DashboardSnapshot,
    refresh: float,
    role_filter: str | None = None,
    *,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
    page: str = "supervision",
    pane_preview_enabled: bool = False,
    verbose_items: bool = False,
) -> str:
    scope = role_filter or "team"
    pane_state = "on" if pane_preview_enabled else "off"
    verbosity = "verbose" if verbose_items else "concise"
    return "\n".join(
        [
            f"{label('team', palette)} {rich_escape(snapshot.team)}",
            f"{label('scope', palette)} {rich_escape(scope)}  {label('page', palette)} {rich_escape(page)}",
            f"{label('collected', palette)} {datetime_text(snapshot.collected_at, palette)}",
            f"{label('refresh', palette)} {refresh:g}s  {label('view', palette)} {rich_escape(verbosity)}  "
            f"{label('pane preview', palette)} {rich_escape(pane_state)}",
            f"{label('runtime', palette)} {rich_escape(snapshot.runtime_dir)}",
            f"{label('config', palette)} {rich_escape(snapshot.config_path)}",
        ]
    )


def alerts_recent_panel(
    alerts: Iterable[DashboardAlert | str], *, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE
) -> str:
    rows = list(alerts)
    if not rows:
        return f"{section_title('alerts', palette)}\nnone"
    visible = rows[:ALERTS_RECENT_LIMIT]
    hidden = len(rows) - len(visible)
    lines = [section_title("alerts", palette)]
    lines.extend(format_alert_line(row, truncate_at=120, palette=palette) for row in visible)
    if hidden:
        lines.append(f"[dim]+{hidden} more in alert history[/dim]")
    return "\n".join(lines)


def alert_lines(
    alerts: Iterable[DashboardAlert | str], *, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE
) -> list[str]:
    rows = list(alerts)
    if not rows:
        return ["none"]
    return [format_alert_line(row, palette=palette) for row in rows]


def help_text() -> str:
    return "\n".join(
        [
            "[b]tmux-team dashboard help[/b]",
            "The dashboard has two pages: work/supervision and context/history.",
            "Use left/right bracket to switch pages. The work page shows active work, obligations, and watchdog runners.",
            "The context page shows milestones, memory excerpts, and alert history.",
            "Use v to toggle concise/verbose item text. Pane preview is off by default; use the key menu to toggle it.",
            "Use f on a focused role row to filter to that role; escape returns to the team overview.",
            "Role table abbreviations: pend=pending claimable, clmd=claimed, ack=acknowledged, stale=expired claimed, todo=open todos.",
            "Codex chips: e=reasoning effort, m=model, p=profile, yolo=allow-all role launch.",
            "Recent alerts only show live or recent items; older notification failures move to dimmed alert history with age/timestamp.",
            "Press Ctrl-P for the full key menu, including page jumps, section jumps, pane preview, and role shortcuts.",
            "Sources: runtime-db is authoritative; memory-excerpt is prose; pane-capture is best-effort screen text.",
        ]
    )


def section_title(value: str, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    clean = str(value).replace("[", "(").replace("]", ")")
    return f"[bold underline reverse {palette.section}]{rich_escape(clean)}[/]"


def label(value: str, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    return f"[bold underline {palette.label}]{rich_escape(value)}[/]"


def role_text(value: str, rich: bool, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    if not rich:
        return value
    return f"[bold underline {palette.role}]{rich_escape(value)}[/]"


def id_text(value: str, rich: bool, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    if not rich:
        return value
    return f"[bold underline {palette.identifier}]{rich_escape(value)}[/]"


def datetime_text(value: object, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    text = "-" if value is None else str(value)
    if text in ("", "-"):
        return rich_escape(text)
    return f"[bold underline {palette.datetime}]{rich_escape(text)}[/]"


def priority_text(value: str, rich: bool, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    if not rich:
        return value
    color = palette.error if value.lower() == "urgent" else palette.warning if value.lower() == "high" else palette.text
    return f"[{color}]{rich_escape(value)}[/]"


def status_text(
    value: str, rich: bool, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE, color: str | None = None
) -> str:
    if not rich:
        return value
    normalized = value.lower()
    selected = color
    if selected is None:
        if normalized in ("stale", "failed", "overdue"):
            selected = palette.error
        elif normalized in ("paused", "review", "acknowledged", "claimed"):
            selected = palette.warning
        elif normalized in ("active", "completed", "done"):
            selected = palette.success
        else:
            selected = palette.text
    return f"[{selected}]{rich_escape(value)}[/]"


def state_text(value: str, rich: bool, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    return status_text(value, rich, palette)


def format_alert_line(
    row: DashboardAlert | str, *, truncate_at: int | None = None, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE
) -> str:
    alert = row if isinstance(row, DashboardAlert) else DashboardAlert(str(row))
    prefix = ""
    if alert.age or alert.created_at:
        bits = []
        if alert.age:
            bits.append(f"age={alert.age}")
        if alert.created_at:
            bits.append(f"at={alert.created_at}")
        prefix = " ".join(bits) + " "
    text = prefix + alert.text
    text = truncate(text, truncate_at) if truncate_at else text
    role, separator, rest = text.partition(":")
    line: str
    if separator and role:
        line = f"[{palette.error}]![/] {role_text(role, True, palette)}:{rich_escape(rest)}"
    else:
        line = f"[{palette.error}]![/] {rich_escape(text)}"
    return f"[dim]{line}[/dim]" if alert.stale else line


def format_plain_alert_line(row: DashboardAlert | str) -> str:
    alert = row if isinstance(row, DashboardAlert) else DashboardAlert(str(row))
    prefix = ""
    if alert.age or alert.created_at:
        bits = []
        if alert.age:
            bits.append(f"age={alert.age}")
        if alert.created_at:
            bits.append(f"at={alert.created_at}")
        prefix = " ".join(bits) + " "
    return f"! {prefix}{alert.text}"


def section_panel(title: str, rows: Iterable[str], *, palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE) -> str:
    values = list(rows)
    if not values:
        values = ["none"]
    return f"{section_title(title, palette)}\n" + "\n".join(values)


def role_table_rows(snapshot: DashboardSnapshot, *, codex_limit: int, active_limit: int) -> Iterable[tuple[str, ...]]:
    for row in snapshot.roles:
        yield (
            row_text(row, "name"),
            row_text(row, "state"),
            row_text(row, "pane"),
            row_text(row, "pending"),
            row_text(row, "claimed"),
            row_text(row, "acknowledged"),
            row_text(row, "stale_claimed"),
            row_text(row, "open_todos"),
            truncate(row_text(row, "runtime_settings"), codex_limit),
            truncate(row_text(row, "active_summary"), active_limit),
        )


def textual_role_table_rows(
    snapshot: DashboardSnapshot,
    *,
    text_cls,
    codex_limit: int,
    active_limit: int,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
) -> Iterable[tuple[object, ...]]:
    for row in snapshot.roles:
        yield (
            text_cls.from_markup(role_text(row_text(row, "name"), True, palette)),
            text_cls.from_markup(state_text(row_text(row, "state"), True, palette)),
            text_cls.from_markup(id_text(row_text(row, "pane"), True, palette)),
            row_text(row, "pending"),
            row_text(row, "claimed"),
            row_text(row, "acknowledged"),
            row_text(row, "stale_claimed"),
            row_text(row, "open_todos"),
            truncate(row_text(row, "runtime_settings"), codex_limit),
            truncate(row_text(row, "active_summary"), active_limit),
        )


def active_lines(
    rows: Iterable[dict[str, object]],
    *,
    rich: bool = False,
    provenance: bool = False,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        source = provenance_suffix(row, provenance)
        lines.append(
            f"{role_text(row_text(row, 'role'), rich, palette)} {id_text(row_text(row, 'message_id'), rich, palette)} "
            f"{state_text(row_text(row, 'state'), rich, palette)} {priority_text(row_text(row, 'priority'), rich, palette)} "
            f"from={role_text(row_text(row, 'sender'), rich, palette)} age={safe_row_text(row, 'age', rich)} "
            f"{safe_row_text(row, 'summary', rich)}{source}"
        )
        for todo in row_strings(row, "todos"):
            marker = r"\[ ]" if rich else "[ ]"
            lines.append(f"  {marker} {rich_escape(todo) if rich else todo}")
    if not seen:
        lines.append("none")
    return lines


def obligation_lines(
    rows: Iterable[dict[str, object]],
    *,
    rich: bool = False,
    provenance: bool = False,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
    verbose: bool = True,
) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        if bool(row.get("overdue")):
            marker = status_text("OVERDUE", rich, palette, palette.error)
        elif bool(row.get("review_due")):
            marker = status_text("REVIEW", rich, palette, palette.warning)
        elif row_text(row, "state") == "paused":
            marker = status_text("PAUSED", rich, palette, palette.warning)
        else:
            marker = state_text(row_text(row, "state"), rich, palette)
        pause = ""
        if row_text(row, "state") == "paused":
            pause = (
                f" review={datetime_text(row_text(row, 'review_at'), palette) if rich else row_text(row, 'review_at')} "
                f"reason={safe_row_text(row, 'paused_reason', rich)}"
            )
        if not verbose:
            if row_text(row, "state") == "paused":
                timing = f"review={datetime_text(row_text(row, 'review_at'), palette) if rich else row_text(row, 'review_at')}"
            else:
                timing = f"next={datetime_text(row_text(row, 'next_update'), palette) if rich else row_text(row, 'next_update')}"
            summary = truncate(row_text(row, "summary"), 110)
            summary_text = rich_escape(summary) if rich else summary
            lines.append(
                f"{role_text(row_text(row, 'role'), rich, palette)} {id_text(row_text(row, 'obligation_id'), rich, palette)} "
                f"{marker} {timing} {summary_text}{provenance_suffix(row, provenance)}"
            )
            continue
        lines.append(
            f"{role_text(row_text(row, 'role'), rich, palette)} {id_text(row_text(row, 'obligation_id'), rich, palette)} {marker} "
            f"updated={datetime_text(row_text(row, 'updated'), palette) if rich else row_text(row, 'updated')} "
            f"next={datetime_text(row_text(row, 'next_update'), palette) if rich else row_text(row, 'next_update')}{pause} "
            f"{safe_row_text(row, 'summary', rich)}{provenance_suffix(row, provenance)}"
        )
    if not seen:
        lines.append("none")
    return lines


def watchdog_lines(
    rows: Iterable[dict[str, object]],
    *,
    rich: bool = False,
    provenance: bool = False,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
    verbose: bool = True,
) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        state = row_text(row, "state")
        state = state_text(state, rich, palette)
        pause = ""
        if row_text(row, "state") == "paused":
            pause = (
                f" review={datetime_text(row_text(row, 'review_at'), palette) if rich else row_text(row, 'review_at')} "
                f"reason={safe_row_text(row, 'paused_reason', rich)}"
            )
        if not verbose:
            next_or_review = (
                f"review={datetime_text(row_text(row, 'review_at'), palette) if rich else row_text(row, 'review_at')}"
                if row_text(row, "state") == "paused"
                else f"next={datetime_text(row_text(row, 'next_run'), palette) if rich else row_text(row, 'next_run')}"
            )
            summary = truncate(row_text(row, "summary"), 100)
            summary_text = rich_escape(summary) if rich else summary
            lines.append(
                f"{id_text(row_text(row, 'name'), rich, palette)} {state} "
                f"every={safe_row_text(row, 'interval', rich)} scope={role_text(row_text(row, 'scope'), rich, palette)} "
                f"notify={role_text(row_text(row, 'notify_role'), rich, palette)} {next_or_review} "
                f"findings={safe_row_text(row, 'findings', rich)} {summary_text}{provenance_suffix(row, provenance)}"
            )
            continue
        lines.append(
            f"{id_text(row_text(row, 'name'), rich, palette)} {state} interval={safe_row_text(row, 'interval', rich)} "
            f"scope={role_text(row_text(row, 'scope'), rich, palette)} notify={role_text(row_text(row, 'notify_role'), rich, palette)} "
            f"delivery={safe_row_text(row, 'delivery', rich)} "
            f"last={datetime_text(row_text(row, 'last_run'), palette) if rich else row_text(row, 'last_run')} "
            f"next={datetime_text(row_text(row, 'next_run'), palette) if rich else row_text(row, 'next_run')} "
            f"findings={safe_row_text(row, 'findings', rich)} safe_to_close={safe_row_text(row, 'safe_to_close', rich)} "
            f"pane={safe_row_text(row, 'pane', rich)}{pause} {safe_row_text(row, 'summary', rich)} "
            f"goal={safe_row_text(row, 'goal', rich)}"
            f"{provenance_suffix(row, provenance)}"
        )
    if not seen:
        lines.append("none")
    return lines


def lines_panel(title: str, rows: Iterable[str], *, rich: bool = False, truncate_at: int | None = 140) -> str:
    values = list(rows)
    if not values:
        values = ["none"]
    rendered = []
    for row in values:
        value = truncate(row, truncate_at) if truncate_at else row
        rendered.append(rich_escape(value) if rich else value)
    return f"[b]{title}[/b]\n" + "\n".join(rendered)


def textual_pane_preview_body(
    snapshot: DashboardSnapshot,
    *,
    include_pane_preview: bool,
    rich: bool = False,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
) -> list[str]:
    if not include_pane_preview:
        return ["disabled"]
    return format_pane_preview_grid_lines(
        snapshot.pane_previews, tail_count=5, rich=rich, provenance=True, palette=palette
    )


def format_pane_preview_grid_lines(
    rows: Iterable[dict[str, object]],
    *,
    tail_count: int,
    rich: bool = False,
    provenance: bool = False,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
) -> list[str]:
    lines: list[str] = []
    seen = False
    for row in rows:
        seen = True
        state = f"cmd={row_text(row, 'current_command')} dead={row_text(row, 'dead')} copy={row_text(row, 'in_mode')}"
        source = f"source={row_text(row, 'screen_source')}{provenance_suffix(row, provenance)}"
        tail = row_text(row, "text").splitlines()[-tail_count:] or ["(empty)"]
        if rich:
            lines.append(
                f"{role_text(row_text(row, 'role'), rich, palette)} {id_text(row_text(row, 'pane'), rich, palette)} "
                f"[dim]{rich_escape(state)} {rich_escape(source)}[/dim]"
            )
            for line in tail:
                lines.append(f"  {rich_escape(line)}")
        else:
            lines.append(f"{row_text(row, 'role')} {row_text(row, 'pane')} {state} {source}")
            for line in tail:
                lines.append(f"  {line}")
        lines.append("")
    if not seen:
        lines.append("none")
    return lines


def format_fixed_row(cells: tuple[str, str, str, str], *, rich: bool = False) -> str:
    role, pane, state, tail = cells
    row = f"{role:<12}  {pane:<8}  {state:<32}  {tail}"
    return rich_escape(row) if rich else row


def memory_lines(
    rows: Iterable[dict[str, object]],
    *,
    truncate_at: int | None = None,
    rich: bool = False,
    provenance: bool = False,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
) -> list[str]:
    lines: list[str] = []
    for row in rows:
        excerpt = first_content_line(row_text(row, "excerpt"))
        if truncate_at is not None:
            role_budget = (
                len(row_text(row, "role")) + len(row_text(row, "updated")) + len(provenance_suffix(row, provenance)) + 7
            )
            excerpt = truncate(excerpt, max(12, truncate_at - role_budget))
        line = (
            f"{role_text(row_text(row, 'role'), rich, palette)} "
            f"{datetime_text(row_text(row, 'updated'), palette) if rich else row_text(row, 'updated')}: "
            f"{rich_escape(excerpt) if rich else excerpt}"
            f"{provenance_suffix(row, provenance)}"
        )
        lines.append(line)
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


def file_updated_at(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(microsecond=0).isoformat()
    except OSError:
        return "-"


def first_line(value: str) -> str:
    stripped = value.strip()
    return stripped.splitlines()[0] if stripped else "-"


def first_content_line(value: str) -> str:
    for line in value.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return first_line(value)


def format_milestone_line(
    row: dict,
    *,
    rich: bool = False,
    provenance: bool = False,
    palette: SemanticPalette = DEFAULT_SEMANTIC_PALETTE,
) -> str:
    kind = row.get("kind") or "milestone"
    recorded_by = row.get("recorded_by") or row.get("actor") or "-"
    if not rich:
        return (
            f"{row.get('created_at')} [{kind}] recorded_by={recorded_by} "
            f"subject={milestone_subject_label(row)} {row.get('summary')}"
            f"{provenance_suffix({'source': 'milestone-jsonl', 'confidence': 'operator-recorded'}, provenance)}"
        )
    return (
        f"{datetime_text(row.get('created_at'), palette)} [{palette.warning}]{rich_escape(kind)}[/] "
        f"recorded_by={role_text(str(recorded_by), True, palette)} "
        f"subject={role_text(milestone_subject_label(row), True, palette)} {rich_escape(row.get('summary'))}"
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
