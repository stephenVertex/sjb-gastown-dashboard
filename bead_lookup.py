#!/usr/bin/env uv
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.14"
# dependencies = ["textual"]
# ///
"""Bead lookup companion — search and view bead details.

Run in a tmux pane alongside the dashboard:
    uv run scripts/bead_lookup.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

GT_ROOT = Path.home() / "gt"


# ── helpers ──────────────────────────────────────────────────────────


def load_all_beads() -> list[dict]:
    """Return minimal info for every non-wisp bead across all rigs."""
    beads: list[dict] = []
    seen: set[str] = set()

    for rig_dir in sorted(GT_ROOT.iterdir()):
        if not rig_dir.is_dir():
            continue
        beads_root = rig_dir / "mayor" / "rig"
        if not beads_root.exists():
            continue

        rig_name = rig_dir.name
        try:
            result = subprocess.run(
                ["bd", "list", "--json"],
                capture_output=True,
                text=True,
                cwd=str(beads_root),
                timeout=15,
            )
            if result.returncode != 0:
                continue
            for rec in json.loads(result.stdout):
                bead_id = rec.get("id", "")
                if bead_id in seen or "-wisp-" in bead_id:
                    continue
                seen.add(bead_id)
                rec["_rig"] = rig_name
                beads.append(rec)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            continue

    return beads


def fetch_bead_detail(bead_id: str) -> str:
    """Run ``bd show <id> --long`` and return the human-readable output."""
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
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            continue
    return f"Bead {bead_id!r} not found in any rig."


def format_suggestion(rec: dict) -> str:
    status = rec.get("status", "")
    priority = rec.get("priority", "")
    bead_id = rec.get("id", "")
    title = rec.get("title", "")
    rig = rec.get("_rig", "")
    return f"{bead_id:<20s}  P{priority}  {status:<12s}  {rig:<8s}  {title}"


# ── app ──────────────────────────────────────────────────────────────


class BeadLookup(App):
    CSS = """
    #search {
        dock: top;
        margin: 0 1;
    }
    #suggestions {
        height: auto;
        max-height: 12;
        margin: 0 1;
        color: $text-muted;
    }
    #detail {
        margin: 1 1;
    }
    """

    BINDINGS = [
        ("escape", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.all_beads: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type bead ID or title to search...", id="search")
        yield Static("Loading beads...", id="suggestions")
        yield Static("", id="detail")

    def on_mount(self) -> None:
        self.all_beads = load_all_beads()
        self._update_suggestions("")

    @on(Input.Changed, "#search")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._update_suggestions(event.value)

    @on(Input.Submitted, "#search")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return

        # If the query matches a bead ID exactly, show it
        # Otherwise pick the first suggestion
        target = None
        for rec in self.all_beads:
            if rec.get("id", "") == query:
                target = query
                break

        if target is None:
            # Pick best match
            matches = self._filter(query)
            if matches:
                target = matches[0].get("id", "")

        if target:
            detail_widget = self.query_one("#detail", Static)
            detail_widget.update(f"[dim]Loading {target}...[/]")
            self.run_worker(self._load_detail(target), exclusive=True)

    async def _load_detail(self, bead_id: str) -> None:
        import asyncio

        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, fetch_bead_detail, bead_id)
        self.query_one("#detail", Static).update(text)

    def _filter(self, query: str) -> list[dict]:
        if not query:
            return self.all_beads[:20]
        q = query.lower()
        scored: list[tuple[int, dict]] = []
        for rec in self.all_beads:
            bead_id = rec.get("id", "").lower()
            title = rec.get("title", "").lower()
            if q in bead_id:
                scored.append((0, rec))  # ID match first
            elif q in title:
                scored.append((1, rec))
        scored.sort(key=lambda t: t[0])
        return [rec for _, rec in scored[:20]]

    def _update_suggestions(self, query: str) -> None:
        matches = self._filter(query)
        if not matches:
            self.query_one("#suggestions", Static).update("[dim]No matches[/]")
            return
        lines = [format_suggestion(rec) for rec in matches]
        self.query_one("#suggestions", Static).update("\n".join(lines))


if __name__ == "__main__":
    BeadLookup().run()
