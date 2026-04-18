#!/usr/bin/env uv
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.14"
# dependencies = ["textual"]
# ///

from __future__ import annotations

import argparse

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Provider, Hits, Hit
from textual.containers import Container, Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from dashboard_data import DashboardDataStore, DashboardSnapshot, POLL_INTERVAL, fmt_age
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
        Binding("1", "focus_panel('windows')", "Windows"),
        Binding("2", "focus_panel('worked')", "Worked"),
        Binding("3", "focus_panel('queue')", "Queue"),
        Binding("4", "focus_panel('closed')", "Closed"),
        Binding("5", "focus_panel('detail')", "Detail"),
        Binding("6", "focus_panel('prs')", "PRs"),
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
                with VerticalScroll(id="worked", classes="panel", can_focus=True):
                    yield Static("Loading...", id="worked-content")
                with VerticalScroll(id="queue", classes="panel", can_focus=True):
                    yield Static("Loading...", id="queue-content")
                with VerticalScroll(id="closed", classes="panel", can_focus=True):
                    yield Static("Loading...", id="closed-content")
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
        self.query_one(f"#{panel_id}", VerticalScroll).focus()

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
        self.query_one("#worked-content", Static).update(format_rows(snapshot.in_progress_beads, "No in-progress beads", 6))
        self.query_one("#queue-content", Static).update(format_rows(snapshot.queued_beads, "No queued beads", 8))
        self.query_one("#closed-content", Static).update(format_rows(snapshot.recently_closed, "Nothing recently closed", 8))
        self.query_one("#detail-content", Static).update(build_detail_text(snapshot))
        self.query_one("#prs-content", Static).update(format_rows(snapshot.pending_prs, "No pending PRs", 12))
        for panel_id, title in (("windows", "Windows"), ("worked", "Worked Now"), ("queue", "Queue"), ("closed", "Recently Closed"), ("detail", "Bead Detail"), ("prs", "Pending PRs")):
            panel = self.query_one(f"#{panel_id}", VerticalScroll)
            panel.border_title = title


def main() -> None:
    parser = argparse.ArgumentParser(description="Textual tmux dashboard")
    parser.add_argument("socket", help="tmux socket name (e.g. gt-be7f79)")
    parser.add_argument("poll_interval", type=int, nargs="?", default=POLL_INTERVAL)
    args = parser.parse_args()
    GastownDashboard(socket=args.socket, refresh_interval=args.poll_interval).run()


if __name__ == "__main__":
    main()
