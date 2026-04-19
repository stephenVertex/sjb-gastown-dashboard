#!/usr/bin/env uv
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.14"
# dependencies = ["textual"]
# ///

from __future__ import annotations

import argparse
import asyncio
import subprocess
import time
from datetime import datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Provider, Hits, Hit
from textual.containers import Container, Vertical, Horizontal, VerticalScroll
from textual.events import Key
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static

from dashboard_data import DashboardDataStore, DashboardSnapshot, GT_ROOT, POLL_INTERVAL, fmt_age, rig_abbrev
from rigs_screen import RigsScreen


def _row_table(columns: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 1), expand=True)
    for _, justify in columns:
        table.add_column(justify=justify, overflow="ellipsis", no_wrap=True)
    return table


def format_rows(
    rows: list[tuple[str, str, str]], empty: str, limit: int
) -> RenderableType:
    if not rows:
        return Text(empty, style="dim")
    table = _row_table([("rig", "left"), ("bead", "left"), ("title", "left")])
    for rig, bead, title in rows[:limit]:
        table.add_row(Text(rig_abbrev(rig), style="cyan"), Text(bead, style="bold"), title)
    if len(rows) > limit:
        return Group(table, Text(f"+{len(rows) - limit} more", style="dim"))
    return table


def build_windows_text(snapshot: DashboardSnapshot) -> RenderableType:
    if not snapshot.birth_times:
        return Text("No tmux windows", style="dim")

    session_for_bead = {session_name: issue_id for (_rig, issue_id), session_name in snapshot.bead_to_session.items()}

    table = _row_table(
        [
            ("session", "left"),
            ("idx", "right"),
            ("name", "left"),
            ("cmd", "left"),
            ("bead", "left"),
            ("age", "right"),
            ("title", "left"),
        ]
    )
    for session, idx in sorted(snapshot.birth_times, key=lambda item: (item[0], snapshot.birth_times[item])):
        name, cmd, path, title, _pane_id = snapshot.windows.get((session, idx), ("?", "?", "?", "", ""))
        age_secs = (snapshot.refreshed_at - snapshot.birth_times[(session, idx)]).total_seconds()
        label = session_for_bead.get(session, "-")
        title_text = title or path or ""
        table.add_row(
            Text(session, style="cyan"),
            str(idx),
            name,
            cmd,
            Text(label, style="bold" if label != "-" else "dim"),
            fmt_age(age_secs),
            title_text,
        )
    return table


def _format_relative_age(value: str) -> str:
    if not value:
        return ""
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
        delta = datetime.now(created.tzinfo) - created
    except ValueError:
        return value[:10]
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _format_dependency_lines(items: list[dict], label: str) -> list[str]:
    if not items:
        return []
    lines = [f"{label}:"]
    for item in items:
        dep_id = item.get("id", "?")
        title = item.get("title", "")
        status = item.get("status", "")
        piece = f"- {dep_id}"
        if title:
            piece += f"  {title}"
        if status:
            piece += f" [{status}]"
        lines.append(piece)
    return lines


def render_bead_summary(bead: dict, session_name: str = "") -> str:
    bead_id = bead.get("id", "?")
    title = bead.get("title", "")
    rig = bead.get("_rig", "")
    status = bead.get("status", "")
    priority = bead.get("priority", "")
    bead_type = bead.get("issue_type", "")
    assignee = bead.get("assignee", "")
    owner = bead.get("owner", "")
    created = _format_relative_age(bead.get("created_at", ""))
    updated = _format_relative_age(bead.get("updated_at", ""))
    description = (bead.get("description", "") or "").strip()
    labels = bead.get("labels") or []
    comments = bead.get("comments") or []
    dependencies = bead.get("dependencies") or []
    dependents = bead.get("dependents") or []

    lines = [f"{bead_id}  {title}".rstrip()]
    meta = []
    if rig:
        meta.append(f"rig: {rig_abbrev(rig)}")
    if session_name:
        meta.append(f"session: {session_name}")
    if status:
        meta.append(f"status: {status}")
    if priority != "":
        meta.append(f"priority: P{priority}")
    if bead_type:
        meta.append(f"type: {bead_type}")
    if meta:
        lines.append("  ".join(meta))

    people = []
    if assignee:
        people.append(f"assignee: {assignee}")
    if owner:
        people.append(f"owner: {owner}")
    if people:
        lines.append("  ".join(people))

    timing = []
    if created:
        timing.append(f"created: {created}")
    if updated:
        timing.append(f"updated: {updated}")
    if timing:
        lines.append("  ".join(timing))

    if labels:
        lines.append("")
        lines.append("labels: " + ", ".join(str(label) for label in labels))

    if description:
        lines.append("")
        lines.append("description:")
        lines.extend(description.splitlines())

    dep_lines = _format_dependency_lines(dependencies, "dependencies")
    dependent_lines = _format_dependency_lines(dependents, "dependents")
    if dep_lines:
        lines.append("")
        lines.extend(dep_lines)
    if dependent_lines:
        lines.append("")
        lines.extend(dependent_lines)

    if comments:
        lines.append("")
        lines.append("comments:")
        for comment in comments:
            author = comment.get("author") or comment.get("created_by") or "unknown"
            body = (comment.get("body") or comment.get("comment") or "").strip()
            created_at = _format_relative_age(comment.get("created_at", ""))
            header = f"- {author}"
            if created_at:
                header += f" ({created_at})"
            lines.append(header)
            if body:
                lines.extend(f"  {line}" for line in body.splitlines())

    return "\n".join(lines)


def _bead_search_rows(snapshot: DashboardSnapshot) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for bead in snapshot.bead_details:
        bead_id = bead.get("id", "")
        if not bead_id or bead_id in seen:
            continue
        seen.add(bead_id)
        rig = bead.get("_rig", "")
        title = bead.get("title", "")
        status = bead.get("status", "")
        display = f"{bead_id:<18} {status:<10} {rig_abbrev(rig):<8} {title}".rstrip()
        rows.append((bead_id, title, display))
    return rows


def fetch_bead_long_detail(bead_id: str) -> str:
    for rig_dir in sorted(GT_ROOT.iterdir()):
        if not rig_dir.is_dir():
            continue
        beads_root = rig_dir / "mayor" / "rig"
        if not beads_root.exists():
            continue
        try:
            result = subprocess.run(
                ["bd", "show", bead_id, "--long"],
                capture_output=True,
                text=True,
                cwd=str(beads_root),
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return f"Bead {bead_id!r} not found in any rig."


class BeadSearchScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        yield Vertical(
            Input(placeholder="Search bead ID or title", id="search-input"),
            Static(id="search-results"),
            id="search-modal",
        )

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()
        self._update_results("")

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._update_results(event.value)

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip().lower()
        matches = self._matches(query)
        self.dismiss(matches[0][0] if matches else None)

    def _matches(self, query: str) -> list[tuple[str, str, str]]:
        if not query:
            return self.rows[:20]
        return [row for row in self.rows if query in row[0].lower() or query in row[1].lower()][:20]

    def _update_results(self, query: str) -> None:
        matches = self._matches(query.strip().lower())
        body = "\n".join(row[2] for row in matches) if matches else "No matches"
        self.query_one("#search-results", Static).update(body)


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
            ("bd", "Focus bead detail panel"),
        ]
        for panel_id, help_text in items:
            if query and query.lower() not in panel_id:
                continue
            target = "detail" if panel_id == "bd" else panel_id
            hits.add(Hit(panel_id, help_text, lambda panel_id=target: app.action_focus_panel(panel_id)))
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

    #search-modal {
        width: 90;
        height: 24;
        padding: 1;
        border: round $accent;
        background: $surface;
    }

    #search-results {
        height: 1fr;
        overflow-y: auto;
        padding-top: 1;
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
        Binding("s", "detail_search", "Search Beads"),
        Binding("r", "show_rigs", "Rigs"),
    ]

    snapshot: reactive[DashboardSnapshot | None] = reactive(None)

    def __init__(self, socket: str, refresh_interval: int = POLL_INTERVAL) -> None:
        super().__init__()
        self.socket = socket
        self.refresh_interval = refresh_interval
        self.store = DashboardDataStore(socket)
        self._detail_rotation = 0
        self._last_detail_switch = 0.0
        self._detail_pinned_id: str | None = None
        self._detail_pinned_text = ""

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

    def action_detail_search(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        rows = _bead_search_rows(snapshot)
        if not rows:
            return
        self.push_screen(BeadSearchScreen(rows), self._pin_detail_bead)

    def action_show_rigs(self) -> None:
        self.push_screen(RigsScreen())

    def on_key(self, event: Key) -> None:
        if event.key == "escape" and self._detail_pinned_id and self.screen is self.screen_stack[0]:
            self._detail_pinned_id = None
            self._detail_pinned_text = ""
            self._render_snapshot()
            event.stop()

    def refresh_data(self) -> None:
        self.snapshot = self.store.refresh()
        self._render_snapshot()

    def _pin_detail_bead(self, bead_id: str | None) -> None:
        if not bead_id:
            return
        self._detail_pinned_id = bead_id
        self._detail_pinned_text = f"Loading {bead_id}..."
        self._render_snapshot()
        self.run_worker(self._load_pinned_detail(bead_id), exclusive=True)

    async def _load_pinned_detail(self, bead_id: str) -> None:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, fetch_bead_long_detail, bead_id)
        if self._detail_pinned_id != bead_id:
            return
        self._detail_pinned_text = text
        self._render_snapshot()

    def _build_rotating_detail_text(self, snapshot: DashboardSnapshot) -> str:
        details = snapshot.bead_details
        if not details:
            return "No in-progress bead details"
        now = time.time()
        if now - self._last_detail_switch >= self.refresh_interval:
            self._last_detail_switch = now
            if self._detail_rotation >= len(details):
                self._detail_rotation = 0
            bead = details[self._detail_rotation % len(details)]
            self._detail_rotation += 1
        else:
            current_idx = (self._detail_rotation - 1) % len(details) if self._detail_rotation else 0
            bead = details[current_idx]
        bead_id = bead.get("id", "")
        session_name = snapshot.bead_to_session.get((bead.get("_rig", ""), bead_id), "")
        body = render_bead_summary(bead, session_name)
        current_idx = (self._detail_rotation - 1) % len(details) if self._detail_rotation else 0
        return body + f"\n\n[{current_idx + 1}/{len(details)}] auto-rotate • /s to pin • Esc to unpin"

    def _build_detail_panel_text(self, snapshot: DashboardSnapshot) -> str:
        if self._detail_pinned_id:
            return self._detail_pinned_text or f"Loading {self._detail_pinned_id}..."
        return self._build_rotating_detail_text(snapshot)

    def _render_snapshot(self) -> None:
        if self.snapshot is None:
            return
        snapshot = self.snapshot
        self.query_one("#windows-content", Static).update(build_windows_text(snapshot))
        self.query_one("#worked-content", Static).update(format_rows(snapshot.in_progress_beads, "No in-progress beads", 6))
        self.query_one("#queue-content", Static).update(format_rows(snapshot.queued_beads, "No queued beads", 8))
        self.query_one("#closed-content", Static).update(format_rows(snapshot.recently_closed, "Nothing recently closed", 8))
        self.query_one("#detail-content", Static).update(self._build_detail_panel_text(snapshot))
        self.query_one("#prs-content", Static).update(format_rows(snapshot.pending_prs, "No pending PRs", 12))
        detail_title = "Bead Detail"
        if self._detail_pinned_id:
            detail_title = f"Bead Detail [pinned: {self._detail_pinned_id}]"
        for panel_id, title in (("windows", "Windows"), ("worked", "Worked Now"), ("queue", "Queue"), ("closed", "Recently Closed"), ("detail", detail_title), ("prs", "Pending PRs")):
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
