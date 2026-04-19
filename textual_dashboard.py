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
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static

from dashboard_data import DashboardDataStore, DashboardSnapshot, POLL_INTERVAL, fmt_age
from rigs_screen import RigsScreen


PANEL_ORDER = ["windows", "worked", "queue", "closed", "detail", "prs"]
PANEL_TITLES = {
    "windows": "Sessions",
    "worked": "Worked Now",
    "queue": "Queue",
    "closed": "Recently Closed",
    "detail": "Bead Detail",
    "prs": "Pending PRs",
}
COMMAND_MAP = {
    "ss": "windows",
    "wn": "worked",
    "q": "queue",
    "rc": "closed",
    "bd": "detail",
    "pr": "prs",
}

HELP_TEXT = (
    "[b]Navigation[/]  (press [b]/[/], type command, Enter)\n\n"
    "  /ss   Sessions table\n"
    "  /wn   Worked Now\n"
    "  /q    Queue\n"
    "  /rc   Recently Closed\n"
    "  /bd   Bead Detail\n"
    "  /pr   Pending PRs\n"
    "  /s    Fuzzy search focused panel\n"
    "  /h    This help\n\n"
    "[b]Keys[/]\n\n"
    "  /          Open command bar\n"
    "  Tab        Next panel\n"
    "  Shift+Tab  Previous panel\n"
    "  Escape     Close overlay / clear search\n"
    "  q          Quit\n"
    "  r          Rigs screen\n\n"
    "[dim]Escape to close.[/]"
)


def filter_bead_rows(rows: list[tuple[str, str, str]], query: str) -> list[tuple[str, str, str]]:
    if not query:
        return rows
    q = query.lower()
    return [r for r in rows if q in " ".join(r).lower()]


def format_bead_rows(
    rows: list[tuple[str, str, str]], empty: str, limit: int, query: str = ""
) -> str:
    filtered = filter_bead_rows(rows, query)
    if not filtered:
        if query:
            return f"[dim]No matches for '{query}'[/]"
        return empty
    rendered = [f"{rig:<8} {bead:<18} {title}" for rig, bead, title in filtered[:limit]]
    if len(filtered) > limit:
        rendered.append(f"+{len(filtered) - limit} more")
    return "\n".join(rendered)


def build_windows_lines(snapshot: DashboardSnapshot) -> list[str]:
    if not snapshot.birth_times:
        return []
    lines: list[str] = []
    for session, idx in sorted(
        snapshot.birth_times, key=lambda item: (item[0], snapshot.birth_times[item])
    ):
        name, cmd, path, title, _pane_id = snapshot.windows.get(
            (session, idx), ("?", "?", "?", "", "")
        )
        age_secs = (
            snapshot.refreshed_at - snapshot.birth_times[(session, idx)]
        ).total_seconds()
        label = "-"
        for (rig_name, issue_id), session_name in snapshot.bead_to_session.items():
            if session_name == session:
                label = issue_id
                break
        title_text = title or path or ""
        lines.append(
            f"{session:<18} {idx:>2} {name:<10} {cmd:<10} {label:<12} "
            f"{fmt_age(age_secs):>10}  {title_text}"
        )
    return lines


def build_windows_text(snapshot: DashboardSnapshot, query: str = "") -> str:
    lines = build_windows_lines(snapshot)
    if not lines:
        return "No tmux windows"
    if query:
        q = query.lower()
        lines = [line for line in lines if q in line.lower()]
        if not lines:
            return f"[dim]No matches for '{query}'[/]"
    return "\n".join(lines)


def build_detail_text(snapshot: DashboardSnapshot, query: str = "") -> str:
    if not snapshot.bead_details:
        return "No in-progress bead details"
    bead = snapshot.bead_details[0]
    lines = [f"{bead.get('id', '?')}  {bead.get('title', '')}"]
    lines.append(
        f"rig: {bead.get('_rig', '')}  status: {bead.get('status', '')}  "
        f"priority: P{bead.get('priority', '')}"
    )
    description = (bead.get("description", "") or "").strip()
    if description:
        lines.append("")
        lines.extend(description.splitlines()[:6])
    text = "\n".join(lines)
    if query and query.lower() not in text.lower():
        return f"[dim]No match for '{query}'[/]"
    return text


class DashboardCommands(Provider):
    async def search(self, query: str) -> Hits:
        app = self.app
        hits = Hits()
        items = [
            ("windows", "Focus sessions panel"),
            ("worked", "Focus worked-now panel"),
            ("queue", "Focus queue panel"),
            ("closed", "Focus recently closed panel"),
            ("detail", "Focus bead detail panel"),
            ("prs", "Focus pending PRs panel"),
        ]
        for panel_id, help_text in items:
            if query and query.lower() not in panel_id:
                continue
            hits.add(
                Hit(
                    panel_id,
                    help_text,
                    lambda panel_id=panel_id: app.action_focus_panel(panel_id),
                )
            )
        if not query or query.lower() in "rigs":
            hits.add(Hit("rigs", "Open rigs screen", lambda: app.action_show_rigs()))
        return hits


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "Close")]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: 60;
        height: auto;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Container(Static(HELP_TEXT, id="help-content"), id="help-box")


class BarInput(Input):
    """Input that cancels back to the app on Escape."""

    BINDINGS = [Binding("escape", "cancel_bar", "Cancel", show=False)]

    def action_cancel_bar(self) -> None:
        app = self.app
        handler = getattr(app, "hide_bar", None)
        if handler is not None:
            handler(self.id or "")


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

    .panel:focus-within {
        border: round $success;
    }

    #windows {
        height: 2fr;
    }

    .bar {
        height: 3;
        dock: bottom;
        margin: 0 1;
        display: none;
    }

    .bar.-visible {
        display: block;
    }
    """

    COMMANDS = App.COMMANDS | {DashboardCommands}
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "open_command_bar", "Cmd"),
        Binding("question_mark", "show_help", "Help"),
        Binding("tab", "cycle_panel(1)", "Next panel", show=False, priority=True),
        Binding("shift+tab", "cycle_panel(-1)", "Prev panel", show=False, priority=True),
        Binding("escape", "escape", "Back", show=False),
        Binding("1", "focus_panel('windows')", "Sessions"),
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
        self._active_panel: str = "windows"
        self._search_queries: dict[str, str] = {}

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
        yield BarInput(
            id="command-bar",
            classes="bar",
            placeholder="Command: ss wn q rc bd pr s h (Enter to run, Esc to cancel)",
        )
        yield BarInput(
            id="search-bar",
            classes="bar",
            placeholder="Search focused panel... (Esc to clear)",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Textual Dashboard [{self.socket}]"
        self.sub_title = f"Refresh {self.refresh_interval}s"
        self.set_interval(self.refresh_interval, self.refresh_data)
        self.refresh_data()
        self.query_one("#windows", VerticalScroll).focus()

    def on_unmount(self) -> None:
        self.store.close()

    # ----- panel focus / navigation -----

    def action_focus_panel(self, panel_id: str) -> None:
        if panel_id not in PANEL_TITLES:
            return
        self._active_panel = panel_id
        self.query_one(f"#{panel_id}", VerticalScroll).focus()

    def action_cycle_panel(self, direction: int) -> None:
        try:
            idx = PANEL_ORDER.index(self._active_panel)
        except ValueError:
            idx = 0
        new_idx = (idx + int(direction)) % len(PANEL_ORDER)
        self.action_focus_panel(PANEL_ORDER[new_idx])

    def action_show_rigs(self) -> None:
        self.push_screen(RigsScreen())

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    # ----- command bar -----

    def action_open_command_bar(self) -> None:
        self._hide_bar("search-bar")
        bar = self.query_one("#command-bar", BarInput)
        bar.value = ""
        self._show_bar("command-bar")
        bar.focus()

    def _open_search_bar(self) -> None:
        self._hide_bar("command-bar")
        bar = self.query_one("#search-bar", BarInput)
        bar.value = self._search_queries.get(self._active_panel, "")
        bar.placeholder = f"Search {PANEL_TITLES[self._active_panel]}... (Esc to clear)"
        self._show_bar("search-bar")
        bar.focus()

    def _show_bar(self, bar_id: str) -> None:
        bar = self.query_one(f"#{bar_id}", BarInput)
        bar.add_class("-visible")

    def _hide_bar(self, bar_id: str) -> None:
        try:
            bar = self.query_one(f"#{bar_id}", BarInput)
        except Exception:
            return
        bar.remove_class("-visible")
        bar.value = ""

    def hide_bar(self, bar_id: str) -> None:
        """Called by BarInput when Escape is pressed."""
        if bar_id == "search-bar":
            self._search_queries.pop(self._active_panel, None)
            self._hide_bar("search-bar")
            self._render_snapshot()
        else:
            self._hide_bar(bar_id)
        self.query_one(f"#{self._active_panel}", VerticalScroll).focus()

    def action_escape(self) -> None:
        # Fallback when no bar/modal handled the key.
        if self._search_queries.get(self._active_panel):
            self._search_queries.pop(self._active_panel, None)
            self._render_snapshot()

    def _handle_command(self, raw: str) -> None:
        cmd = raw.strip().lstrip("/").lower()
        if not cmd:
            return
        if cmd == "h":
            self.action_show_help()
            return
        if cmd == "s":
            self._open_search_bar()
            return
        panel = COMMAND_MAP.get(cmd)
        if panel:
            self.action_focus_panel(panel)
            return
        self.notify(f"Unknown command: /{cmd}", severity="warning")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "command-bar":
            value = event.value
            self._hide_bar("command-bar")
            self._handle_command(value)
        elif event.input.id == "search-bar":
            self._search_queries[self._active_panel] = event.value
            self._hide_bar("search-bar")
            self.query_one(f"#{self._active_panel}", VerticalScroll).focus()
            self._render_snapshot()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-bar":
            self._search_queries[self._active_panel] = event.value
            self._render_snapshot()

    # ----- data refresh -----

    def refresh_data(self) -> None:
        self.snapshot = self.store.refresh()
        self._render_snapshot()

    def _query_for(self, panel_id: str) -> str:
        return self._search_queries.get(panel_id, "")

    def _render_snapshot(self) -> None:
        if self.snapshot is None:
            return
        snapshot = self.snapshot
        self.query_one("#windows-content", Static).update(
            build_windows_text(snapshot, self._query_for("windows"))
        )
        self.query_one("#worked-content", Static).update(
            format_bead_rows(
                snapshot.in_progress_beads, "No in-progress beads", 6, self._query_for("worked")
            )
        )
        self.query_one("#queue-content", Static).update(
            format_bead_rows(
                snapshot.queued_beads, "No queued beads", 8, self._query_for("queue")
            )
        )
        self.query_one("#closed-content", Static).update(
            format_bead_rows(
                snapshot.recently_closed,
                "Nothing recently closed",
                8,
                self._query_for("closed"),
            )
        )
        self.query_one("#detail-content", Static).update(
            build_detail_text(snapshot, self._query_for("detail"))
        )
        self.query_one("#prs-content", Static).update(
            format_bead_rows(
                snapshot.pending_prs, "No pending PRs", 12, self._query_for("prs")
            )
        )
        for panel_id, title in PANEL_TITLES.items():
            panel = self.query_one(f"#{panel_id}", VerticalScroll)
            query = self._query_for(panel_id)
            panel.border_title = f"{title}  [dim]/{query}[/]" if query else title


def main() -> None:
    parser = argparse.ArgumentParser(description="Textual tmux dashboard")
    parser.add_argument("socket", help="tmux socket name (e.g. gt-be7f79)")
    parser.add_argument("poll_interval", type=int, nargs="?", default=POLL_INTERVAL)
    args = parser.parse_args()
    GastownDashboard(socket=args.socket, refresh_interval=args.poll_interval).run()


if __name__ == "__main__":
    main()
