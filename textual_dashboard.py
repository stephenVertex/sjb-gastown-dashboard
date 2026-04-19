#!/usr/bin/env uv
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.14"
# dependencies = ["textual"]
# ///

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Provider, Hits, Hit
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, Static

from dashboard_data import DashboardDataStore, DashboardSnapshot, GT_ROOT, POLL_INTERVAL, fmt_age
from rigs_screen import RigsScreen


def format_rows(rows: list[tuple[str, str, str]], empty: str, limit: int) -> str:
    if not rows:
        return empty
    rendered = [f"{rig:<8} {bead:<18} {title}" for rig, bead, title in rows[:limit]]
    if len(rows) > limit:
        rendered.append(f"+{len(rows) - limit} more")
    return "\n".join(rendered)


def build_windows_text(snapshot: DashboardSnapshot) -> str:
    if not snapshot.birth_times:
        return "No tmux windows"

    lines: list[str] = []
    for session, idx in sorted(snapshot.birth_times, key=lambda item: (item[0], snapshot.birth_times[item])):
        name, cmd, path, title, _pane_id = snapshot.windows.get((session, idx), ("?", "?", "?", "", ""))
        age_secs = (snapshot.refreshed_at - snapshot.birth_times[(session, idx)]).total_seconds()
        label = "-"
        for (rig_name, issue_id), session_name in snapshot.bead_to_session.items():
            if session_name == session:
                label = issue_id
                break
        title_text = title or path or ""
        lines.append(f"{session:<18} {idx:>2} {name:<10} {cmd:<10} {label:<12} {fmt_age(age_secs):>10}  {title_text}")
    return "\n".join(lines)


def build_detail_text(snapshot: DashboardSnapshot) -> str:
    if not snapshot.bead_details:
        return "No in-progress bead details"
    bead = snapshot.bead_details[0]
    lines = [f"{bead.get('id', '?')}  {bead.get('title', '')}"]
    lines.append(f"rig: {bead.get('_rig', '')}  status: {bead.get('status', '')}  priority: P{bead.get('priority', '')}")
    description = (bead.get("description", "") or "").strip()
    if description:
        lines.append("")
        lines.extend(description.splitlines()[:6])
    return "\n".join(lines)


def format_bead_detail(record: dict) -> str:
    lines = [f"{record.get('id', '?')}  {record.get('title', '')}"]
    lines.append(
        "  ".join(
            part
            for part in (
                f"rig: {record.get('_rig', '')}" if record.get("_rig") else "",
                f"status: {record.get('status', '')}" if record.get("status") else "",
                f"priority: P{record.get('priority', '')}" if record.get("priority") not in (None, "") else "",
                f"assignee: {record.get('assignee', '')}" if record.get("assignee") else "",
            )
            if part
        )
    )
    description = (record.get("description", "") or "").strip()
    if description:
        lines.append("")
        lines.extend(description.splitlines())
    labels = record.get("labels") or []
    if labels:
        lines.append("")
        lines.append("labels: " + ", ".join(labels))
    deps = record.get("dependencies") or []
    if deps:
        lines.append("")
        lines.append("dependencies:")
        lines.extend(f"- {dep}" for dep in deps)
    history = record.get("history") or []
    if history:
        lines.append("")
        lines.append("history:")
        for item in history[:12]:
            if isinstance(item, dict):
                summary = item.get("summary") or item.get("description") or json.dumps(item, sort_keys=True)
                lines.append(f"- {summary}")
            else:
                lines.append(f"- {item}")
    return "\n".join(line for line in lines if line is not None)


def fetch_bead_detail(bead_id: str) -> str:
    for rig_dir in sorted(GT_ROOT.iterdir()):
        if not rig_dir.is_dir():
            continue
        beads_root = rig_dir / "mayor" / "rig"
        if not beads_root.exists():
            continue
        try:
            result = subprocess.run(
                ["bd", "show", bead_id, "--json"],
                capture_output=True,
                text=True,
                cwd=str(beads_root),
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            records = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if not records:
            continue
        record = records[0]
        record["_rig"] = rig_dir.name
        return format_bead_detail(record)
    return f"Bead {bead_id!r} not found."


def fuzzy_filter_rows(rows: list[tuple[str, ...]], query: str) -> list[tuple[str, ...]]:
    q = query.strip().lower()
    if not q:
        return rows
    ranked: list[tuple[tuple[int, int], tuple[str, ...]]] = []
    for row in rows:
        haystack = " ".join(str(part) for part in row).lower()
        if q in haystack:
            ranked.append(((0, haystack.index(q)), row))
            continue
        pos = -1
        for char in q:
            pos = haystack.find(char, pos + 1)
            if pos == -1:
                break
        else:
            ranked.append(((1, pos), row))
    ranked.sort(key=lambda item: item[0])
    return [row for _, row in ranked]


class PanelSearchScreen(ModalScreen[str | None]):
    CSS = """
    #panel-search {
        width: 90;
        height: 24;
        padding: 1;
        border: round $accent;
        background: $surface;
    }

    #panel-search-results {
        height: 1fr;
        overflow-y: auto;
        padding-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, rows: list[tuple[str, ...]]) -> None:
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        yield Vertical(
            Input(placeholder="Search bead rows", id="panel-search-input"),
            Static(id="panel-search-results"),
            id="panel-search",
        )

    def on_mount(self) -> None:
        self.query_one("#panel-search-input", Input).focus()
        self._update_results("")

    @on(Input.Changed, "#panel-search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._update_results(event.value)

    @on(Input.Submitted, "#panel-search-input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        matches = fuzzy_filter_rows(self.rows, event.value)
        self.dismiss(matches[0][1] if matches else None)

    def _update_results(self, query: str) -> None:
        matches = fuzzy_filter_rows(self.rows, query)[:12]
        body = "\n".join("  ".join(str(part) for part in row) for row in matches) if matches else "No matches"
        self.query_one("#panel-search-results", Static).update(body)


class BeadPanel(DataTable):
    def __init__(self, panel_id: str, columns: tuple[str, ...], *, empty_message: str) -> None:
        super().__init__(id=panel_id, zebra_stripes=True, cursor_type="row")
        self.panel_id = panel_id
        self.empty_message = empty_message
        self.columns_spec = columns
        self.rows_data: list[tuple[str, ...]] = []
        self.bead_ids_by_key: dict[str, str | None] = {}

    def on_mount(self) -> None:
        if len(self.columns) > 0:
            return
        self.add_columns(*self.columns_spec)

    def update_rows(self, rows: list[tuple[str, ...]], bead_id_index: int) -> None:
        self.rows_data = rows
        self.clear(columns=False)
        self.bead_ids_by_key.clear()
        if not rows:
            key = f"{self.panel_id}-empty"
            values = [""] * (len(self.columns_spec) - 1) + [self.empty_message]
            self.add_row(*values, key=key)
            self.bead_ids_by_key[key] = None
            self.cursor_type = "none"
            return
        self.cursor_type = "row"
        for idx, row in enumerate(rows):
            key = f"{self.panel_id}-{idx}"
            self.add_row(*row, key=key)
            self.bead_ids_by_key[key] = row[bead_id_index]

    def bead_id_for_row(self, row_key: str | None) -> str | None:
        if row_key is None:
            return None
        return self.bead_ids_by_key.get(row_key)


class DashboardCommands(Provider):
    async def search(self, query: str) -> Hits:
        app = self.app
        hits = Hits()
        items = [
            ("windows", "Focus windows panel"),
            ("worked", "Focus worked-now panel"),
            ("queue", "Focus queue panel"),
            ("closed", "Focus recently closed panel"),
            ("detail", "Focus bead detail panel"),
            ("prs", "Focus pending PRs panel"),
        ]
        for panel_id, help_text in items:
            if query and query.lower() not in panel_id:
                continue
            hits.add(Hit(panel_id, help_text, lambda panel_id=panel_id: app.action_focus_panel(panel_id)))
        if not query or query.lower() in "rigs":
            hits.add(Hit("rigs", "Open rigs screen", lambda: app.action_show_rigs()))
        return hits


class GastownDashboard(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #grid {
        height: 1fr;
        layout: vertical;
    }

    .row {
        height: 1fr;
    }

    .panel {
        border: round $accent;
        width: 1fr;
        margin: 0 1 1 1;
        padding: 0 1;
    }

    #windows {
        height: 2fr;
    }
    """

    COMMANDS = App.COMMANDS | {DashboardCommands}
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "command_palette", "Commands"),
        Binding("tab", "focus_next_panel", "Next Panel"),
        Binding("1", "focus_panel('windows')", "Windows"),
        Binding("2", "focus_panel('worked')", "Worked"),
        Binding("3", "focus_panel('queue')", "Queue"),
        Binding("4", "focus_panel('closed')", "Closed"),
        Binding("5", "focus_panel('detail')", "Detail"),
        Binding("6", "focus_panel('prs')", "PRs"),
        Binding("w", "focus_panel('worked')", "Worked"),
        Binding("n", "focus_panel('queue')", "Queue"),
        Binding("c", "focus_panel('closed')", "Closed"),
        Binding("s", "search_active_panel", "Search Panel"),
        Binding("r", "show_rigs", "Rigs"),
    ]

    snapshot: reactive[DashboardSnapshot | None] = reactive(None)

    def __init__(self, socket: str, refresh_interval: int = POLL_INTERVAL) -> None:
        super().__init__()
        self.socket = socket
        self.refresh_interval = refresh_interval
        self.store = DashboardDataStore(socket)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="grid"):
            with VerticalScroll(id="windows", classes="panel", can_focus=True):
                yield Static("Loading dashboard...", id="windows-content")
            with Horizontal(classes="row"):
                with Vertical(id="worked-shell", classes="panel"):
                    yield BeadPanel("worked", ("SESSION", "RIG", "BEAD", "TITLE"), empty_message="No in-progress beads")
                with Vertical(id="queue-shell", classes="panel"):
                    yield BeadPanel("queue", ("RIG", "BEAD", "TITLE"), empty_message="No queued beads")
                with Vertical(id="closed-shell", classes="panel"):
                    yield BeadPanel("closed", ("RIG", "BEAD", "CLOSED", "TITLE"), empty_message="Nothing recently closed")
            with Horizontal(classes="row"):
                with VerticalScroll(id="detail", classes="panel", can_focus=True):
                    yield Static("Loading...", id="detail-content")
                with VerticalScroll(id="prs", classes="panel", can_focus=True):
                    yield Static("Loading...", id="prs-content")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Textual Dashboard [{self.socket}]"
        self.sub_title = f"Refresh {self.refresh_interval}s"
        self.set_interval(self.refresh_interval, self.refresh_data)
        self.refresh_data()

    def on_unmount(self) -> None:
        self.store.close()

    def action_focus_panel(self, panel_id: str) -> None:
        if panel_id in {"worked", "queue", "closed"}:
            self.query_one(f"#{panel_id}", BeadPanel).focus()
            return
        self.query_one(f"#{panel_id}", VerticalScroll).focus()

    def action_focus_next_panel(self) -> None:
        order = ["worked", "queue", "closed"]
        focused = self.focused
        if isinstance(focused, BeadPanel) and focused.panel_id in order:
            index = order.index(focused.panel_id)
            self.action_focus_panel(order[(index + 1) % len(order)])
            return
        self.action_focus_panel(order[0])

    async def action_search_active_panel(self) -> None:
        focused = self.focused
        if not isinstance(focused, BeadPanel):
            return
        result = await self.push_screen_wait(PanelSearchScreen(focused.rows_data))
        if not result:
            return
        for row_index in range(focused.row_count):
            row_key = focused.get_row_key(row_index)
            if row_key is None:
                continue
            bead_id = focused.bead_id_for_row(row_key.value)
            if bead_id == result:
                focused.move_cursor(row=row_index)
                self.run_worker(self._load_bead_detail(bead_id), exclusive=True)
                break

    def action_show_rigs(self) -> None:
        self.push_screen(RigsScreen())

    def refresh_data(self) -> None:
        self.snapshot = self.store.refresh()
        self._render_snapshot()

    def _render_snapshot(self) -> None:
        if self.snapshot is None:
            return
        snapshot = self.snapshot
        self.query_one("#windows-content", Static).update(build_windows_text(snapshot))
        worked_rows = [
            (
                snapshot.bead_to_session.get((rig_name, issue_id), ""),
                rig_name,
                issue_id,
                title,
            )
            for rig_name, issue_id, title in snapshot.in_progress_beads
        ]
        queue_rows = [(rig_name, issue_id, title) for rig_name, issue_id, title in snapshot.queued_beads]
        closed_rows = [(rig_name, issue_id, closed_at, title) for rig_name, issue_id, closed_at, title in snapshot.recently_closed]
        self.query_one("#worked", BeadPanel).update_rows(worked_rows, bead_id_index=2)
        self.query_one("#queue", BeadPanel).update_rows(queue_rows, bead_id_index=1)
        self.query_one("#closed", BeadPanel).update_rows(closed_rows, bead_id_index=1)
        self.query_one("#detail-content", Static).update(build_detail_text(snapshot))
        self.query_one("#prs-content", Static).update(format_rows(snapshot.pending_prs, "No pending PRs", 12))
        for panel_id, title in (("windows", "Windows"), ("worked-shell", "Worked Now"), ("queue-shell", "Queue"), ("closed-shell", "Recently Closed"), ("detail", "Bead Detail"), ("prs", "Pending PRs")):
            panel = self.query_one(f"#{panel_id}")
            panel.border_title = title

    async def _load_bead_detail(self, bead_id: str) -> None:
        self.query_one("#detail-content", Static).update(f"Loading {bead_id}...")
        loop = asyncio.get_event_loop()
        detail = await loop.run_in_executor(None, fetch_bead_detail, bead_id)
        self.query_one("#detail-content", Static).update(detail)

    @on(DataTable.RowSelected, "#worked")
    @on(DataTable.RowSelected, "#queue")
    @on(DataTable.RowSelected, "#closed")
    def on_bead_row_selected(self, event: DataTable.RowSelected) -> None:
        panel = event.data_table
        if not isinstance(panel, BeadPanel):
            return
        bead_id = panel.bead_id_for_row(event.row_key.value)
        if bead_id:
            self.run_worker(self._load_bead_detail(bead_id), exclusive=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Textual tmux dashboard")
    parser.add_argument("socket", help="tmux socket name (e.g. gt-be7f79)")
    parser.add_argument("poll_interval", type=int, nargs="?", default=POLL_INTERVAL)
    args = parser.parse_args()
    GastownDashboard(socket=args.socket, refresh_interval=args.poll_interval).run()


if __name__ == "__main__":
    main()
