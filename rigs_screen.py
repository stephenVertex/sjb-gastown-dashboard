from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from rigs_data import fetch_rigs

RIGS_REFRESH_INTERVAL = 10


class RigsScreen(Screen):
    BINDINGS = [
        Binding("q,escape", "app.pop_screen", "Back"),
        Binding("r", "refresh_now", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._status = Static("", id="rigs-status")
        self._table = DataTable(id="rigs-table", zebra_stripes=True, cursor_type="row")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield self._status
            yield self._table
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Rigs"
        self.sub_title = f"Refresh {RIGS_REFRESH_INTERVAL}s"
        self._table.add_columns("prefix", "rig", "active?", "status", "witness", "refinery", "polecats", "crew")
        self.set_interval(RIGS_REFRESH_INTERVAL, self.refresh_rigs)
        self.refresh_rigs()

    def action_refresh_now(self) -> None:
        self.refresh_rigs()

    def refresh_rigs(self) -> None:
        snapshot = fetch_rigs()
        self._table.clear()
        if snapshot.error:
            self._status.update(f"[red]error:[/] {snapshot.error}")
            return
        active_count = sum(1 for row in snapshot.rows if row.active)
        self._status.update(
            f"[bold]{len(snapshot.rows)}[/] rigs • [green]{active_count}[/] active • "
            f"updated {snapshot.refreshed_at.strftime('%H:%M:%S')}"
        )
        for row in snapshot.rows:
            active_cell = "[green]yes[/]" if row.active else "[dim]no[/]"
            self._table.add_row(
                row.prefix,
                row.name,
                active_cell,
                row.status,
                row.witness,
                row.refinery,
                str(row.polecats),
                str(row.crew),
            )
