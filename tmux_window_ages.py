#!/usr/bin uv
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.14"
# dependencies = ["rich", "textual"]
# ///
import json
import os
import difflib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

POLL_INTERVAL = 5
CMD_HISTORY = 8
TITLE_HISTORY = 100
RECENT_DEATHS = 20
HOME_GT = str(Path.home() / "gt")
BAR_WIDTH = 28
BEAD_DETAIL_REFRESH = 60  # seconds between bead detail fetches
GT_ROOT = Path.home() / "gt"
SIDE_PANEL_WIDTH = 54
QUEUE_LINES = 8
ACTIVITY_HISTORY = 6
PR_REFRESH_INTERVAL = 60
PR_TABLE_LINES = 12
MAX_ORPHAN_BRANCHES_PER_RIG = 3
RECENTLY_CLOSED_LINES = 8
CLOSED_LOOKBACK_DAYS = 2
SEARCH_LIMIT = 200

# -- LLM session summary settings --
LLM_SUMMARY_INTERVAL = 30  # seconds between LLM summary refreshes
FIREWORKS_API_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
FIREWORKS_MODEL = "accounts/fireworks/models/minimax-m2p5"
FIREWORKS_API_KEY = os.environ.get("FIREWORKS_API_KEY", "")
# Roles excluded from LLM summarization
_SUMMARY_SKIP_ROLES = {"witness", "boot", "deacon"}

XDG_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
STATE_DIR = XDG_STATE_DIR / "gastown"
STATE_FILE = STATE_DIR / "tmux_window_ages.json"

GT_AUTO_PLUGIN_TITLES = {
    "Plugin crash (blocks boot)",
    "Plugin stderr on startup",
    "Plugin timed out during startup",
}


def state_path(socket: str) -> Path:
    return STATE_DIR / f"tmux_window_ages_{socket}.json"


def save_state(
    socket: str,
    birth_times: dict[tuple[str, str], datetime],
    cmd_histories: dict[tuple[str, str], deque[str]],
    title_histories: dict[tuple[str, str], deque[tuple[str, str]]],
    death_log: deque[tuple[str, str, str]],
) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "birth_times": {
            f"{k[0]}:{k[1]}": v.isoformat() for k, v in birth_times.items()
        },
        "cmd_histories": {f"{k[0]}:{k[1]}": list(v) for k, v in cmd_histories.items()},
        "title_histories": {
            f"{k[0]}:{k[1]}": list(v) for k, v in title_histories.items()
        },
        "death_log": list(death_log),
    }
    tmp = state_path(socket).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(state_path(socket))


def load_state(
    socket: str,
) -> tuple[
    dict[tuple[str, str], datetime],
    dict[tuple[str, str], deque[str]],
    dict[tuple[str, str], deque[tuple[str, str]]],
    deque[tuple[str, str, str]],
]:
    p = state_path(socket)
    if not p.exists():
        return {}, {}, {}, deque(maxlen=RECENT_DEATHS)
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}, {}, {}, deque(maxlen=RECENT_DEATHS)

    birth_times = {}
    for k, v in data.get("birth_times", {}).items():
        session, idx = k.split(":", 1)
        birth_times[(session, idx)] = datetime.fromisoformat(v)

    cmd_histories = {}
    for k, v in data.get("cmd_histories", {}).items():
        session, idx = k.split(":", 1)
        cmd_histories[(session, idx)] = deque(v, maxlen=CMD_HISTORY)

    title_histories = {}
    for k, v in data.get("title_histories", {}).items():
        session, idx = k.split(":", 1)
        title_histories[(session, idx)] = deque(
            [tuple(item) for item in v], maxlen=TITLE_HISTORY
        )

    death_log = deque(
        [tuple(item) for item in data.get("death_log", [])],
        maxlen=RECENT_DEATHS,
    )

    return birth_times, cmd_histories, title_histories, death_log


def list_all_windows(
    socket: str,
) -> dict[tuple[str, str], tuple[str, str, str, str, str]]:
    fmt = "#{session_name}:#{window_index}:#{window_name}:#{pane_current_command}:#{pane_current_path}:#{pane_title}:#{pane_id}"
    result = subprocess.run(
        ["tmux", "-L", socket, "list-windows", "-a", "-F", fmt],
        capture_output=True,
        text=True,
    )
    windows = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split(":", 6)
        if len(parts) == 7:
            session, idx, name, cmd, path, title, pane_id = parts
            windows[(session, idx)] = (name, cmd, path, title, pane_id)
    return windows


def capture_pane_text(socket: str, target: str) -> str:
    result = subprocess.run(
        ["tmux", "-L", socket, "capture-pane", "-p", "-t", target],
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def fmt_age(secs: float) -> str:
    d = timedelta(seconds=int(secs))
    days = d.days
    hours, rem = divmod(d.seconds, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def age_color(secs: float) -> str:
    if secs < 60:
        return "green"
    if secs < 300:
        return "yellow"
    if secs < 3600:
        return "dark_orange"
    return "red"


def cmd_color(cmd: str) -> str:
    colors = {
        "opencode": "bright_green",
        "node": "bright_cyan",
        "zsh": "bright_yellow",
        "bash": "bright_yellow",
        "vim": "bright_magenta",
        "nvim": "bright_magenta",
        "python": "bright_blue",
        "python3": "bright_blue",
    }
    return colors.get(cmd, "white")


def shorten_path(path: str, max_len: int = 40) -> str:
    if len(path) <= max_len:
        return path
    parts = path.split("/")
    short = ".../" + "/".join(parts[-3:])
    if len(short) > max_len:
        short = "..." + path[-(max_len - 3) :]
    return short


def shorten_text(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


_rig_prefixes: dict[str, str] | None = None


def load_rig_prefixes() -> dict[str, str]:
    """Load rig_name -> prefix mapping from rigs.json (cached)."""
    global _rig_prefixes
    if _rig_prefixes is not None:
        return _rig_prefixes

    _rig_prefixes = {}
    for candidate in [GT_ROOT / "mayor" / "rigs.json", GT_ROOT / "rigs.json"]:
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text())
            for rig_name, info in data.get("rigs", {}).items():
                prefix = (info.get("beads") or {}).get("prefix", "")
                if prefix:
                    _rig_prefixes[rig_name] = prefix
            break
        except (json.JSONDecodeError, OSError):
            continue

    return _rig_prefixes


def rig_abbrev(rig_name: str) -> str:
    """Return the short prefix for a rig, falling back to the name itself."""
    prefixes = load_rig_prefixes()
    return prefixes.get(rig_name, rig_name)


def rig_name_from_path(path: str) -> str | None:
    try:
        path_obj = Path(path).resolve()
    except OSError:
        return None

    try:
        relative = path_obj.relative_to(GT_ROOT)
    except ValueError:
        return None

    if not relative.parts:
        return None
    return relative.parts[0]


def load_bead_assignments() -> dict[tuple[str, str], str]:
    assignments: dict[tuple[str, str], tuple[datetime, str]] = {}

    for interactions_path in GT_ROOT.glob("*/mayor/rig/.beads/interactions.jsonl"):
        rig_name = interactions_path.parts[-5]
        try:
            with interactions_path.open() as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if record.get("kind") != "field_change":
                        continue

                    extra = record.get("extra") or {}
                    assignee = extra.get("new_value") or ""
                    if extra.get("field") != "assignee" or "/polecats/" not in assignee:
                        continue

                    parts = assignee.split("/")
                    if len(parts) < 3:
                        continue

                    polecat_name = parts[-1]
                    issue_id = record.get("issue_id") or ""
                    created_at = record.get("created_at") or ""
                    if not issue_id or not created_at:
                        continue

                    try:
                        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    key = (rig_name, polecat_name)
                    current = assignments.get(key)
                    if current is None or ts > current[0]:
                        assignments[key] = (ts, issue_id)
        except OSError:
            continue

    return {key: value[1] for key, value in assignments.items()}


def bead_for_session(
    session: str, path: str, bead_assignments: dict[tuple[str, str], str]
) -> str:
    if "-" not in session:
        return ""

    rig_prefix, role_name = session.split("-", 1)
    rig_name = rig_name_from_path(path)

    if rig_name is not None:
        match = bead_assignments.get((rig_name, role_name))
        if match:
            return match

    prefix_matches = [
        issue_id
        for (candidate_rig, candidate_role), issue_id in bead_assignments.items()
        if candidate_role == role_name and candidate_rig.startswith(rig_prefix)
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]

    return ""


def run_bd_list(rig_path: Path, status: str) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["bd", "list", f"--status={status}"],
        capture_output=True,
        text=True,
        cwd=str(rig_path),
    )
    if result.returncode != 0:
        return []

    rows: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        if stripped.startswith("Total:") or stripped.startswith("Status:"):
            continue
        if stripped.startswith("No issues found"):
            continue

        parts = stripped.split(maxsplit=3)
        if len(parts) < 4:
            continue

        issue_id = parts[1]
        title = parts[3]
        rows.append((issue_id, title))

    return rows


def load_bead_status_tables() -> tuple[
    list[tuple[str, str, str]], list[tuple[str, str, str]]
]:
    in_progress: list[tuple[str, str, str]] = []
    queue: list[tuple[str, str, str]] = []

    for rig_dir in GT_ROOT.iterdir():
        if not rig_dir.is_dir():
            continue

        beads_root = rig_dir / "mayor" / "rig"
        if not beads_root.exists():
            continue

        rig_name = rig_dir.name
        for issue_id, title in run_bd_list(beads_root, "in_progress"):
            if include_cross_rig_bead(rig_name, issue_id, title):
                in_progress.append((rig_name, issue_id, title))
        for issue_id, title in run_bd_list(beads_root, "hooked"):
            if include_cross_rig_bead(rig_name, issue_id, title):
                in_progress.append((rig_name, issue_id, title))
        for issue_id, title in run_bd_list(beads_root, "open"):
            if include_cross_rig_bead(rig_name, issue_id, title):
                queue.append((rig_name, issue_id, title))

    deduped_in_progress: list[tuple[str, str, str]] = []
    seen_in_progress: set[tuple[str, str]] = set()
    for rig_name, issue_id, title in in_progress:
        key = (rig_name, issue_id)
        if key in seen_in_progress:
            continue
        seen_in_progress.add(key)
        deduped_in_progress.append((rig_name, issue_id, title))

    return deduped_in_progress, queue


def build_bead_status_table(
    title: str,
    rows: list[tuple[str, str, str]],
    empty_message: str,
    bead_to_session: dict[tuple[str, str], str] | None = None,
) -> Table:
    show_session = title == "Worked Now" and bead_to_session is not None

    table = Table(
        title=None,
        expand=True,
        box=None,
        show_edge=False,
        padding=(0, 1),
        collapse_padding=True,
        show_header=True,
        header_style="bold blue",
    )
    if show_session:
        table.add_column("SESSION", width=16, style="cyan")
    table.add_column("RIG", width=5, style="magenta")
    table.add_column("BEAD", width=18, style="yellow")
    table.add_column("TITLE", ratio=1, min_width=18, style="white", no_wrap=True)

    if not rows:
        if show_session:
            table.add_row("", "", "", f"[dim]{empty_message}[/]")
        else:
            table.add_row("", "", f"[dim]{empty_message}[/]")
        return table

    limit = 6 if title == "Worked Now" else QUEUE_LINES
    for rig_name, issue_id, issue_title in rows[:limit]:
        if show_session:
            session = bead_to_session.get((rig_name, issue_id), "")
            table.add_row(session, rig_abbrev(rig_name), issue_id, issue_title)
        else:
            table.add_row(rig_abbrev(rig_name), issue_id, issue_title)

    return table


_closed_cache: list[tuple[str, str, str]] = []
_closed_cache_time: float = 0


def load_recently_closed_beads() -> list[tuple[str, str, str]]:
    """Scan all rigs for recently closed beads (last CLOSED_LOOKBACK_DAYS days).

    Returns (rig_name, issue_id, title) tuples, most-recently-closed first.
    Excludes infrastructure beads (wisp-*).
    Cached for PR_REFRESH_INTERVAL seconds (same cadence as PR scanning).
    """
    global _closed_cache, _closed_cache_time

    now = time.time()
    if now - _closed_cache_time < PR_REFRESH_INTERVAL:
        return _closed_cache

    cutoff = (datetime.now() - timedelta(days=CLOSED_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d"
    )
    results: list[tuple[str, str, str]] = []

    for rig_dir in sorted(GT_ROOT.iterdir()):
        if not rig_dir.is_dir():
            continue
        beads_root = rig_dir / "mayor" / "rig"
        if not beads_root.exists():
            continue

        rig_name = rig_dir.name
        try:
            result = subprocess.run(
                [
                    "bd",
                    "list",
                    "--status=closed",
                    "--sort=closed",
                    f"--closed-after={cutoff}",
                    "--limit=10",
                    "--flat",
                ],
                capture_output=True,
                text=True,
                cwd=str(beads_root),
                timeout=10,
            )
            if result.returncode != 0:
                continue
        except (subprocess.TimeoutExpired, OSError):
            continue

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith("Showing")
                or stripped.startswith("No ")
            ):
                continue

            parts = stripped.split(maxsplit=3)
            if len(parts) < 4:
                continue

            issue_id = parts[1]
            title = parts[3]

            # Skip infrastructure beads (wisp molecules, merge-requests)
            if "-wisp-" in issue_id:
                continue

            if not include_cross_rig_bead(rig_name, issue_id, title):
                continue

            results.append((rig_name, issue_id, title))

    _closed_cache = results
    _closed_cache_time = now
    return results


def build_recently_closed_table(rows: list[tuple[str, str, str]]) -> Table:
    table = Table(
        title=None,
        expand=True,
        box=None,
        show_edge=False,
        padding=(0, 1),
        collapse_padding=True,
        show_header=True,
        header_style="bold blue",
    )
    table.add_column("RIG", width=5, style="magenta")
    table.add_column("BEAD", width=10, style="yellow")
    table.add_column("TITLE", ratio=1, min_width=18, style="dim white", no_wrap=True)

    if not rows:
        table.add_row("", "", "[dim]None recently[/]")
        return table

    for rig_name, issue_id, title in rows[:RECENTLY_CLOSED_LINES]:
        table.add_row(rig_abbrev(rig_name), issue_id, title)

    if len(rows) > RECENTLY_CLOSED_LINES:
        remaining = len(rows) - RECENTLY_CLOSED_LINES
        table.add_row("", "", f"[dim]+{remaining} more[/]")

    return table


# -- Bead detail rotation for the bottom-left panel --

_bead_detail_cache: list[dict] = []
_bead_detail_cache_time: float = 0
_bead_detail_rotation: int = 0


def load_bead_details() -> list[dict]:
    """Fetch full details for all in-progress / hooked beads across rigs.

    Returns a list of dicts (from ``bd show --json``), one per bead,
    de-duplicated by issue ID.  Cached for PR_REFRESH_INTERVAL seconds.
    """
    global _bead_detail_cache, _bead_detail_cache_time

    now = time.time()
    if now - _bead_detail_cache_time < BEAD_DETAIL_REFRESH:
        return _bead_detail_cache

    seen: set[str] = set()
    details: list[dict] = []

    for rig_dir in sorted(GT_ROOT.iterdir()):
        if not rig_dir.is_dir():
            continue
        beads_root = rig_dir / "mayor" / "rig"
        if not beads_root.exists():
            continue

        rig_name = rig_dir.name

        # Gather in_progress + hooked issue IDs
        ids: list[str] = []
        for status in ("in_progress", "hooked"):
            for issue_id, _title in run_bd_list(beads_root, status):
                if issue_id not in seen and "-wisp-" not in issue_id:
                    ids.append(issue_id)
                    seen.add(issue_id)

        for issue_id in ids:
            try:
                result = subprocess.run(
                    ["bd", "show", issue_id, "--json"],
                    capture_output=True,
                    text=True,
                    cwd=str(beads_root),
                    timeout=10,
                )
                if result.returncode != 0:
                    continue
                records = json.loads(result.stdout)
                if records and isinstance(records, list):
                    rec = records[0]
                    if not include_cross_rig_bead_record(rig_name, rec):
                        continue
                    rec["_rig"] = rig_name
                    details.append(rec)
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
                continue

    _bead_detail_cache = details
    _bead_detail_cache_time = now
    return details


def include_cross_rig_bead(rig_name: str, issue_id: str, title: str) -> bool:
    if not issue_id.startswith("gt-"):
        return True
    lowered_title = title.lower()
    if title in GT_AUTO_PLUGIN_TITLES:
        return False
    if lowered_title.startswith("ci failure:"):
        return False
    if lowered_title.startswith("gt:rig ") or lowered_title.startswith("gt:agent "):
        return False
    return True


def include_cross_rig_bead_record(rig_name: str, record: dict) -> bool:
    issue_id = str(record.get("id", ""))
    title = str(record.get("title", ""))
    if not include_cross_rig_bead(rig_name, issue_id, title):
        return False
    labels = {str(label).lower() for label in record.get("labels") or []}
    if issue_id.startswith("gt-") and "ci-failure" in labels:
        return False
    return True


def build_bead_detail_content(
    details: list[dict],
    bead_to_session: dict[tuple[str, str], str],
) -> Text:
    """Render one bead's details for the rotating panel.

    Advances the rotation counter each call so successive renders show
    the next bead.
    """
    global _bead_detail_rotation

    if not details:
        return Text.from_markup("[dim]No in-progress beads[/]")

    idx = _bead_detail_rotation % len(details)
    _bead_detail_rotation += 1
    bead = details[idx]

    rig = bead.get("_rig", "")
    bead_id = bead.get("id", "?")
    title = bead.get("title", "")
    desc = bead.get("description", "")
    priority = bead.get("priority", "")
    status = bead.get("status", "")
    bead_type = bead.get("issue_type", "")
    created = bead.get("created_at", "")
    assignee = bead.get("assignee", "")
    dep_count = bead.get("dependency_count", 0)
    dependent_count = bead.get("dependent_count", 0)

    session = bead_to_session.get((rig, bead_id), "")

    # Truncate description to ~4 lines worth
    desc_lines = desc.strip().splitlines()[:4]
    short_desc = "\n".join(desc_lines)
    if len(desc.strip().splitlines()) > 4:
        short_desc += "\n..."

    # Format created_at as relative age
    age_str = ""
    if created:
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
            delta = datetime.now() - ts
            hours = int(delta.total_seconds() // 3600)
            if hours < 24:
                age_str = f"{hours}h ago"
            else:
                age_str = f"{hours // 24}d ago"
        except ValueError:
            age_str = created[:10]

    lines: list[str] = []
    lines.append(f"[bold yellow]{bead_id}[/]  [bold white]{title}[/]")

    meta_parts = []
    if rig:
        meta_parts.append(f"[magenta]{rig_abbrev(rig)}[/]")
    if session:
        meta_parts.append(f"[cyan]{session}[/]")
    if priority:
        meta_parts.append(f"P{priority}")
    if bead_type:
        meta_parts.append(bead_type)
    if status:
        meta_parts.append(f"[green]{status}[/]")
    if age_str:
        meta_parts.append(f"[dim]{age_str}[/]")
    if dep_count:
        meta_parts.append(f"[dim]{dep_count} deps[/]")
    if dependent_count:
        meta_parts.append(f"[dim]{dependent_count} dependents[/]")
    lines.append("  ".join(meta_parts))

    if short_desc:
        lines.append("")
        lines.append(f"[dim]{short_desc}[/]")

    pager = f"[dim]({idx + 1}/{len(details)})[/]"
    lines.append("")
    lines.append(pager)

    return Text("\n").join([Text.from_markup(l) for l in lines])


_pr_cache: list[tuple[str, str, str]] = []
_pr_cache_time: float = 0


def parse_github_repo(remote_url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL (SSH or HTTPS)."""
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote_url)
    return m.group(1) if m else None


def load_pending_prs() -> list[tuple[str, str, str]]:
    """Scan all rigs for open PRs and orphan polecat branches.

    Returns (rig_name, identifier, title) tuples.
    identifier is "#N" for PRs, "branch" for pushed polecat branches without a PR.
    Results are cached for PR_REFRESH_INTERVAL seconds.
    PRs are listed first, then up to MAX_ORPHAN_BRANCHES_PER_RIG orphan branches per rig.
    """
    global _pr_cache, _pr_cache_time

    now = time.time()
    if now - _pr_cache_time < PR_REFRESH_INTERVAL:
        return _pr_cache

    pr_rows: list[tuple[str, str, str]] = []
    branch_rows: list[tuple[str, str, str]] = []

    for rig_dir in sorted(GT_ROOT.iterdir()):
        if not rig_dir.is_dir():
            continue
        repo_git = rig_dir / ".repo.git"
        if not repo_git.exists():
            continue

        rig_name = rig_dir.name

        # Resolve the GitHub repo slug from the remote
        url_result = subprocess.run(
            ["git", "--git-dir", str(repo_git), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
        if url_result.returncode != 0:
            continue
        repo_slug = parse_github_repo(url_result.stdout.strip())
        if not repo_slug:
            continue

        # Open PRs via gh
        pr_branches: set[str] = set()
        try:
            pr_result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo_slug,
                    "--json",
                    "number,title,headRefName",
                    "--state",
                    "open",
                    "--limit",
                    "20",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if pr_result.returncode == 0:
                for pr in json.loads(pr_result.stdout):
                    number = pr.get("number", 0)
                    title = pr.get("title", "")
                    head = pr.get("headRefName", "")
                    pr_branches.add(head)
                    pr_rows.append((rig_name, f"#{number}", title))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass

        # Remote polecat branches that don't have an open PR (capped per rig)
        rig_branch_count = 0
        try:
            branch_result = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(repo_git),
                    "branch",
                    "-r",
                    "--list",
                    "origin/polecat/*",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if branch_result.returncode == 0:
                for line in branch_result.stdout.splitlines():
                    branch = line.strip().removeprefix("origin/")
                    if not branch or branch in pr_branches:
                        continue
                    if rig_branch_count >= MAX_ORPHAN_BRANCHES_PER_RIG:
                        break
                    rig_branch_count += 1
                    # polecat/<name>/<issue>@<ts> → "<name> (<issue>)"
                    parts = branch.removeprefix("polecat/").split("/")
                    if len(parts) >= 2:
                        polecat = parts[0]
                        rest = parts[1].split("@")[0]
                        label = f"{polecat} ({rest})"
                    else:
                        label = parts[0] if parts else branch
                    branch_rows.append((rig_name, "branch", label))
        except (subprocess.TimeoutExpired, OSError):
            pass

    # PRs first, then orphan branches
    results = pr_rows + branch_rows
    _pr_cache = results
    _pr_cache_time = now
    return results


def build_pending_prs_table(rows: list[tuple[str, str, str]]) -> Table:
    table = Table(
        title=None,
        expand=True,
        box=None,
        show_edge=False,
        padding=(0, 1),
        collapse_padding=True,
        show_header=True,
        header_style="bold blue",
    )
    table.add_column("RIG", width=5, style="magenta")
    table.add_column("PR", width=10, style="cyan")
    table.add_column("TITLE", ratio=1, min_width=24, style="white", no_wrap=True)

    if not rows:
        table.add_row("", "", "[dim]No pending PRs[/]")
        return table

    for rig_name, identifier, title in rows[:PR_TABLE_LINES]:
        id_style = "cyan" if identifier.startswith("#") else "dim yellow"
        table.add_row(rig_abbrev(rig_name), f"[{id_style}]{identifier}[/]", title)

    if len(rows) > PR_TABLE_LINES:
        remaining = len(rows) - PR_TABLE_LINES
        table.add_row("", "", f"[dim]+{remaining} more[/]")

    return table


def make_group_separator() -> tuple[Text, str, str, str, str, str, str, Text]:
    return (
        Text("─" * 18, style="dim"),
        "─" * 4,
        "─" * 10,
        "─" * 7,
        "─" * 10,
        "─" * 84,
        "─" * 10,
        Text("─" * BAR_WIDTH, style="dim"),
    )


def measure_activity(previous: str, current: str) -> int:
    if not previous:
        return 0
    if previous == current:
        return 0

    prev_lines = previous.splitlines()
    curr_lines = current.splitlines()
    line_delta = sum(1 for a, b in zip(prev_lines, curr_lines) if a != b)
    line_delta += abs(len(prev_lines) - len(curr_lines))

    matcher = difflib.SequenceMatcher(a=previous[-2000:], b=current[-2000:])
    char_delta = int((1.0 - matcher.ratio()) * 100)

    return line_delta * 3 + char_delta


def activity_label(score: int) -> tuple[str, str]:
    if score == 0:
        return ("idle", "dim")
    if score < 20:
        return ("low", "green")
    if score < 60:
        return ("med", "yellow")
    if score < 140:
        return ("high", "orange3")
    return ("zoom", "red")


def session_status(session: str, score: int) -> tuple[str, str]:
    role = session.split("-", 1)[1] if "-" in session else session
    lowered = role.lower()
    if "zombie" in lowered or "dead" in lowered:
        return ("dead/zombie", "red")
    if any(word in lowered for word in ("done", "complete", "completed")):
        return ("completed", "dim")
    if score == 0:
        return ("idle", "yellow")
    return ("active", "green")


def markup_to_plain(value: str) -> str:
    return re.sub(r"\[[^\]]+\]", "", value or "")


def title_history_text(history: deque[tuple[str, str]]) -> str:
    if not history:
        return "No title changes captured."
    return "\n".join(f"{stamp}  {title or '(empty)'}" for stamp, title in list(history)[-8:])


def command_history_text(history: deque[str]) -> str:
    if not history:
        return "No command history captured."
    return "\n".join(list(history)[-CMD_HISTORY:])


def activity_timeline_text(history: deque[int]) -> str:
    if not history:
        return "No activity yet."
    labels = [activity_label(score)[0] for score in history]
    return "  ".join(labels)


def detail_text(
    socket: str,
    session: str,
    idx: str,
    window_name: str,
    cmd: str,
    path: str,
    title: str,
    bead_id: str,
    age_str: str,
    status_label: str,
    llm_summary: str,
    pane_snapshot: str,
    cmd_history: deque[str],
    title_history: deque[tuple[str, str]],
    activity_history: deque[int],
) -> str:
    lines = [
        f"Session: {session}",
        f"Rig: {session.split('-', 1)[0] if '-' in session else session}",
        f"Window: {idx} ({window_name})",
        f"Command: {cmd}",
        f"Path: {path}",
        f"Title: {title}",
        f"Bead: {bead_id or '-'}",
        f"Age: {age_str}",
        f"Status: {status_label}",
        f"LLM Summary: {llm_summary or '-'}",
        "",
        "Pane Snapshot",
        pane_snapshot.strip() or "(empty pane)",
        "",
        "Command History",
        command_history_text(cmd_history),
        "",
        "Title History",
        title_history_text(title_history),
        "",
        "Activity Timeline",
        activity_timeline_text(activity_history),
        "",
        f"tmux target: {socket}:{session}:{idx}",
    ]
    return "\n".join(lines)


def row_key(session: str, idx: str) -> str:
    return f"{session}:{idx}"


class SearchScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        yield Vertical(
            Input(placeholder="Search session, rig, bead, or title", id="search-input"),
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
            return self.rows[:SEARCH_LIMIT]
        return [row for row in self.rows if query in row[1].lower() or query in row[2].lower()][
            :SEARCH_LIMIT
        ]

    def _update_results(self, query: str) -> None:
        matches = self._matches(query.strip().lower())
        body = "\n".join(row[2] for row in matches) if matches else "No matches"
        self.query_one("#search-results", Static).update(body)


class SessionsTableApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #sessions {
        height: 1fr;
    }

    #detail {
        width: 42;
        padding: 1;
        overflow-y: auto;
        border: round $accent;
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

    BINDINGS = [
        Binding("enter", "open_selected", "Open"),
        Binding("s", "open_search", "Search"),
        Binding("slash", "open_search", "Search"),
        Binding("r", "refresh_rows", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, socket: str, interval: int, wrap_title: bool = False) -> None:
        super().__init__()
        self.socket = socket
        self.interval = interval
        self.wrap_title = wrap_title
        self.birth_times, self.cmd_histories, self.title_histories, self.death_log = load_state(socket)
        self.pane_snapshots: dict[str, str] = {}
        self.activity_histories: dict[tuple[str, str], deque[int]] = {}
        self.row_to_detail: dict[str, str] = {}
        self.search_rows: list[tuple[str, str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(
            DataTable(id="sessions", zebra_stripes=True, cursor_type="row"),
            Static("Loading sessions...", id="detail"),
            id="body",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.add_columns("SESSION", "RIG", "WINDOW", "AGE", "STATUS", "BEAD", "CMD", "TITLE", "LLM SUMMARY")
        self.set_interval(self.interval, self.refresh_rows)
        self.refresh_rows()

    def refresh_rows(self) -> None:
        now = datetime.now()
        windows = list_all_windows(self.socket)

        for key, (_name, cmd, _path, title, pane_id) in windows.items():
            if key not in self.birth_times:
                self.birth_times[key] = now
            hist = self.cmd_histories.setdefault(key, deque(maxlen=CMD_HISTORY))
            if not hist or hist[-1] != cmd:
                hist.append(cmd)
            title_hist = self.title_histories.setdefault(key, deque(maxlen=TITLE_HISTORY))
            if not title_hist or title_hist[-1][1] != title:
                title_hist.append((now.strftime("%H:%M:%S"), title))
            current_snapshot = capture_pane_text(self.socket, f"{key[0]}:{key[1]}")
            previous_snapshot = self.pane_snapshots.get(pane_id, "")
            score = measure_activity(previous_snapshot, current_snapshot)
            self.pane_snapshots[pane_id] = current_snapshot
            self.activity_histories.setdefault(key, deque(maxlen=ACTIVITY_HISTORY)).append(score)

        for key in set(self.birth_times) - set(windows):
            age_secs = (now - self.birth_times[key]).total_seconds()
            self.death_log.append((now.strftime("%H:%M:%S"), row_key(*key), fmt_age(age_secs)))
            del self.birth_times[key]
            self.cmd_histories.pop(key, None)
            self.title_histories.pop(key, None)
            self.activity_histories.pop(key, None)

        bead_assignments = load_bead_assignments()
        in_progress_beads, queued_beads = load_bead_status_tables()
        active_bead_ids = {issue_id for _, issue_id, _ in in_progress_beads + queued_beads}

        table = self.query_one("#sessions", DataTable)
        table.clear(columns=False)
        self.row_to_detail.clear()
        self.search_rows.clear()

        current_prefix = None
        for session, idx in sorted(self.birth_times, key=lambda k: (k[0], self.birth_times[k])):
            prefix = session.split("-")[0] if "-" in session else session
            if current_prefix is not None and prefix != current_prefix:
                separator = f"{prefix} separator"
                table.add_row("", "", "", "", "", "", "", separator, "", key=f"sep-{prefix}-{idx}", height=1)
            current_prefix = prefix

            name, cmd, path, title, pane_id = windows.get((session, idx), ("???", "???", "???", "???", ""))
            age_secs = (now - self.birth_times[(session, idx)]).total_seconds()
            age_str = fmt_age(age_secs)
            activity_scores = self.activity_histories.get((session, idx), deque())
            avg_activity = int(sum(activity_scores) / len(activity_scores)) if activity_scores else 0
            status_label, _status_color = session_status(session, avg_activity)
            bead_id = markup_to_plain(bead_for_session(session, path, bead_assignments))
            if bead_id and bead_id not in active_bead_ids:
                bead_id = f"{bead_id} (stale)"
            llm_summary = get_session_summary(session)
            display_name = "claude" if name == "node" else name
            display_title = title if self.wrap_title else shorten_text(title or "", 84)
            if path and not path.startswith(HOME_GT):
                display_title = shorten_path(path, 60)

            key_value = row_key(session, idx)
            table.add_row(
                session,
                prefix,
                f"{idx} {display_name}",
                age_str,
                status_label,
                bead_id,
                cmd,
                display_title,
                llm_summary,
                key=key_value,
            )

            detail = detail_text(
                self.socket,
                session,
                idx,
                name,
                cmd,
                path,
                title,
                bead_id,
                age_str,
                status_label,
                llm_summary,
                self.pane_snapshots.get(pane_id, ""),
                self.cmd_histories.get((session, idx), deque()),
                self.title_histories.get((session, idx), deque()),
                self.activity_histories.get((session, idx), deque()),
            )
            self.row_to_detail[key_value] = detail
            self.search_rows.append(
                (
                    key_value,
                    " ".join([session, prefix, bead_id, title or "", llm_summary or ""]),
                    f"{session:<20} {prefix:<8} {bead_id:<14} {title or '-'}",
                )
            )

        if table.row_count:
            first_key = next(iter(self.row_to_detail), None)
            if first_key:
                table.move_cursor(row=0)
                self.query_one("#detail", Static).update(self.row_to_detail[first_key])
        else:
            self.query_one("#detail", Static).update("No tmux windows found.")

        save_state(self.socket, self.birth_times, self.cmd_histories, self.title_histories, self.death_log)

    def action_refresh_rows(self) -> None:
        self.refresh_rows()

    def action_open_selected(self) -> None:
        table = self.query_one("#sessions", DataTable)
        if table.cursor_row is None:
            return
        row_key_value = table.get_row_key(table.cursor_row)
        if row_key_value is not None and row_key_value.value in self.row_to_detail:
            self.query_one("#detail", Static).update(self.row_to_detail[row_key_value.value])

    async def action_open_search(self) -> None:
        result = await self.push_screen_wait(SearchScreen(self.search_rows))
        if not result:
            return
        table = self.query_one("#sessions", DataTable)
        for row_index in range(table.row_count):
            row_key_value = table.get_row_key(row_index)
            if row_key_value is not None and row_key_value.value == result:
                table.move_cursor(row=row_index)
                self.query_one("#detail", Static).update(self.row_to_detail[result])
                break

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_key_value = event.row_key.value
        if row_key_value in self.row_to_detail:
            self.query_one("#detail", Static).update(self.row_to_detail[row_key_value])


def build_table(
    socket: str,
    interval: int,
    birth_times: dict[tuple[str, str], datetime],
    cmd_histories: dict[tuple[str, str], deque[str]],
    title_histories: dict[tuple[str, str], deque[tuple[str, str]]],
    death_log: deque[tuple[str, str, str]],
    bead_assignments: dict[tuple[str, str], str],
    pane_snapshots: dict[str, str],
    activity_histories: dict[tuple[str, str], deque[int]],
    active_bead_ids: set[str] | None = None,
    wrap_title: bool = False,
) -> Table:
    now = datetime.now()
    windows = list_all_windows(socket)

    for key, (name, cmd, path, title, pane_id) in windows.items():
        if key not in birth_times:
            birth_times[key] = now
        hist = cmd_histories.setdefault(key, deque(maxlen=CMD_HISTORY))
        if not hist or hist[-1] != cmd:
            hist.append(cmd)
        title_hist = title_histories.setdefault(key, deque(maxlen=TITLE_HISTORY))
        if not title_hist or title_hist[-1][1] != title:
            title_hist.append((now.strftime("%H:%M:%S"), title))

        current_snapshot = capture_pane_text(socket, f"{key[0]}:{key[1]}")
        previous_snapshot = pane_snapshots.get(pane_id, "")
        score = measure_activity(previous_snapshot, current_snapshot)
        pane_snapshots[pane_id] = current_snapshot
        activity_histories.setdefault(key, deque(maxlen=ACTIVITY_HISTORY)).append(score)

    for key in set(birth_times) - set(windows):
        session, idx = key
        age_secs = (now - birth_times[key]).total_seconds()
        display_name = (
            cmd_histories.get(key, deque(["???"]))[-1]
            if key in cmd_histories
            else "???"
        )
        death_log.append(
            (now.strftime("%H:%M:%S"), f"{session}:{idx}", fmt_age(age_secs))
        )
        del birth_times[key]
        cmd_histories.pop(key, None)
        title_histories.pop(key, None)
        activity_histories.pop(key, None)

    table = Table(
        title=None,
        show_header=True,
        header_style="bold magenta",
        border_style="dim blue",
        expand=False,
        padding=(0, 1),
        collapse_padding=True,
        show_lines=False,
        show_edge=False,
        box=None,
    )
    table.add_column("SESSION", style="magenta", width=18)
    table.add_column("IDX", justify="right", style="dim", width=4)
    table.add_column("WINDOW", style="cyan", width=10)
    table.add_column("ACT", width=7)
    table.add_column("BEAD", style="yellow", width=10)
    if wrap_title:
        table.add_column("TITLE", style="bright_white", min_width=30, no_wrap=False)
    else:
        table.add_column("TITLE", style="bright_white", width=84, no_wrap=True)
    table.add_column("AGE", justify="right", width=10)
    table.add_column("BAR", width=BAR_WIDTH)

    max_age = (
        max(((now - bt).total_seconds() for bt in birth_times.values()), default=1) or 1
    )

    current_prefix = None

    for session, idx in sorted(birth_times, key=lambda k: (k[0], birth_times[k])):
        prefix = session.split("-")[0] if "-" in session else session
        is_first = prefix != current_prefix
        if current_prefix is not None and is_first:
            table.add_row(*make_group_separator(), end_section=False)
        current_prefix = prefix

        name, cmd, path, title, pane_id = windows.get(
            (session, idx), ("???", "???", "???", "???", "")
        )
        age_secs = (now - birth_times[(session, idx)]).total_seconds()
        color = age_color(age_secs)
        age_str = fmt_age(age_secs)
        bar_len = min(BAR_WIDTH, int(BAR_WIDTH * age_secs / max_age))
        bar = Text("█" * bar_len + "░" * (BAR_WIDTH - bar_len), style=color)

        title_cell = title if title else ""
        if not wrap_title:
            title_cell = shorten_text(title_cell, 84)
        if path and not path.startswith(HOME_GT):
            path_str = (
                shorten_path(path, 80) if not wrap_title else shorten_path(path, 60)
            )
            title_cell = f"[bold red]!![/] [red]{path_str}[/]"
        llm_summary = get_session_summary(session)
        if llm_summary:
            summary_str = llm_summary if wrap_title else shorten_text(llm_summary, 84)
            title_cell = f"[italic bright_cyan]{summary_str}[/]"
        display_name = "claude" if name == "node" else name
        bead_cell = bead_for_session(session, path, bead_assignments)
        if bead_cell:
            if active_bead_ids and bead_cell not in active_bead_ids:
                bead_cell = f"[dim strikethrough]{bead_cell}[/]"
            else:
                bead_cell = f"[yellow]{bead_cell}[/]"
        activity_scores = activity_histories.get((session, idx), deque())
        avg_activity = (
            int(sum(activity_scores) / len(activity_scores)) if activity_scores else 0
        )
        act_text, act_color = activity_label(avg_activity)
        act_cell = f"[{act_color}]{act_text}[/]"

        session_cell = f"[bold]{session}[/]" if is_first else session

        table.add_row(
            session_cell,
            f" {idx} ",
            display_name,
            act_cell,
            bead_cell,
            title_cell,
            f"[{color}]{age_str}[/]",
            bar,
        )

    if not birth_times:
        table.add_row("", "", "[dim](no windows)[/]", "", "", "", "", "")

    return table


# ── LLM session summary engine ──────────────────────────────────────

_summary_lock = threading.Lock()
_session_summaries: dict[str, str] = {}  # session_name -> one-line summary
_summary_thread: threading.Thread | None = None
_summary_stop = threading.Event()
_summary_working = False  # True while the background thread is actively calling the LLM
_summary_last_run: float = 0  # timestamp of last completed summary cycle
_SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]


def _is_summarizable_session(
    session: str, bead_assignments: dict[tuple[str, str], str]
) -> bool:
    """Return True for sessions that should get LLM summaries.

    Everything except witness, boot, and deacon gets summarized.
    This covers polecats, refinery, and mayor.
    """
    if "-" not in session:
        return False
    _prefix, role = session.split("-", 1)
    return role not in _SUMMARY_SKIP_ROLES


def _call_fireworks(pane_text: str) -> str:
    """Call Fireworks API to summarize a pane's visible text into one line."""
    if not FIREWORKS_API_KEY:
        return ""

    # Take the last ~3000 chars to stay well within token limits
    trimmed = pane_text[-3000:] if len(pane_text) > 3000 else pane_text

    payload = json.dumps(
        {
            "model": FIREWORKS_MODEL,
            "max_tokens": 80,
            "temperature": 0.3,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "What is the person DOING in this terminal? "
                        "Answer with ONLY a 7-10 word summary of the activity. "
                        "BAD answers (never output these): "
                        "'The user is showing me a terminal', 'I can see a screen that', "
                        "'The terminal displays', 'This appears to be'. "
                        "GOOD answers: "
                        "'Editing auth.py to fix login bug', "
                        "'Running pytest suite, 3 tests failing', "
                        "'Rebasing feature branch onto main', "
                        "'Reading codebase and planning approach', "
                        "'Idle, waiting for new task'. "
                        "No quotes, no reasoning, no description of what is displayed."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Terminal screen content:\n\n{trimmed}",
                },
            ],
        }
    ).encode()

    req = urllib.request.Request(
        FIREWORKS_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {FIREWORKS_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                msg = message.get("content") or ""
                # Strip thinking tags some reasoning models leak into content
                thinking_pattern = r"<think>.*?</think>"
                msg = re.sub(thinking_pattern, "", msg, flags=re.DOTALL).strip()
                msg = msg.strip().strip("'\"")
                # Reasoning models put chain-of-thought before the answer.
                # If the response is long, keep only the last sentence.
                if len(msg.split()) > 12:
                    sentences = re.split(r"(?<=[.!?])\s+", msg)
                    if len(sentences) > 1:
                        msg = sentences[-1].strip()
                # Strip lingering meta-commentary prefixes
                for prefix in (
                    "The user is ",
                    "I can see ",
                    "I see ",
                    "The terminal is ",
                    "This is ",
                    "Summarizing: ",
                    "Summary: ",
                ):
                    if msg.lower().startswith(prefix.lower()):
                        msg = msg[len(prefix) :]
                        break
                return msg.strip()
    except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError):
        pass
    return ""


def _summary_worker(
    socket: str,
    pane_snapshots: dict[str, str],
    birth_times: dict[tuple[str, str], datetime],
    bead_assignments_ref: list,  # mutable container holding latest assignments
):
    """Background thread: refreshes LLM summaries every LLM_SUMMARY_INTERVAL seconds."""
    global _session_summaries, _summary_working, _summary_last_run

    while not _summary_stop.wait(LLM_SUMMARY_INTERVAL):
        _summary_working = True
        assignments = bead_assignments_ref[0] if bead_assignments_ref else {}
        windows = list_all_windows(socket)

        new_summaries: dict[str, str] = {}
        for (session, idx), (name, cmd, path, title, pane_id) in windows.items():
            if not _is_summarizable_session(session, assignments):
                continue
            # Only summarize the first window per session (idx "0" typically)
            if session in new_summaries:
                continue
            pane_text = pane_snapshots.get(pane_id, "")
            if not pane_text.strip():
                new_summaries[session] = "No output"
                continue
            summary = _call_fireworks(pane_text)
            if summary:
                new_summaries[session] = summary

        with _summary_lock:
            _session_summaries.update(new_summaries)
        _summary_last_run = time.time()
        _summary_working = False


def force_summary_refresh() -> None:
    """Force the summary worker to run again immediately."""
    global _summary_last_run
    _summary_last_run = 0
    _summary_stop.set()
    _summary_stop.clear()


def get_session_summary(session: str) -> str:
    """Thread-safe read of the latest LLM summary for a session."""
    with _summary_lock:
        return _session_summaries.get(session, "")


def start_summary_thread(
    socket: str,
    pane_snapshots: dict[str, str],
    birth_times: dict[tuple[str, str], datetime],
    bead_assignments_ref: list,
) -> None:
    """Launch the background summary thread (if API key is set)."""
    global _summary_thread
    if not FIREWORKS_API_KEY:
        return
    _summary_stop.clear()
    _summary_thread = threading.Thread(
        target=_summary_worker,
        args=(socket, pane_snapshots, birth_times, bead_assignments_ref),
        daemon=True,
    )
    _summary_thread.start()


def stop_summary_thread() -> None:
    _summary_stop.set()
    if _summary_thread is not None:
        _summary_thread.join(timeout=5)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="tmux window tracker")
    parser.add_argument("socket", help="tmux socket name (e.g. gt-be7f79)")
    parser.add_argument("poll_interval", type=int, nargs="?", default=POLL_INTERVAL)
    parser.add_argument(
        "--wrap", action="store_true", help="wrap TITLE column to show full text"
    )
    parser.add_argument(
        "--textual",
        action="store_true",
        help="run the sessions panel as a Textual DataTable app",
    )
    args = parser.parse_args()

    socket = args.socket
    interval = args.poll_interval

    if args.textual:
        SessionsTableApp(socket, interval, wrap_title=args.wrap).run()
        return

    birth_times, cmd_histories, title_histories, death_log = load_state(socket)
    pane_snapshots: dict[str, str] = {}
    activity_histories: dict[tuple[str, str], deque[int]] = {}
    # Mutable container so background thread always sees latest assignments
    bead_assignments_ref: list[dict] = [{}]

    initial_windows = list_all_windows(socket)
    for key in set(birth_times) - set(initial_windows):
        session, idx = key
        age_secs = (datetime.now() - birth_times[key]).total_seconds()
        death_log.append(
            (datetime.now().strftime("%H:%M:%S"), f"{session}:{idx}", fmt_age(age_secs))
        )
        del birth_times[key]
        cmd_histories.pop(key, None)
        title_histories.pop(key, None)

    console = Console()

    # Start background LLM summary thread
    start_summary_thread(socket, pane_snapshots, birth_times, bead_assignments_ref)

    def render():
        now = datetime.now()
        bead_assignments = load_bead_assignments()
        # Update shared ref so summary thread sees latest assignments
        bead_assignments_ref[0] = bead_assignments
        in_progress_beads, queued_beads = load_bead_status_tables()
        recently_closed = load_recently_closed_beads()
        pending_prs = load_pending_prs()
        # Build reverse lookup: (rig_name, issue_id) -> session name
        bead_to_session: dict[tuple[str, str], str] = {}
        prefixes = load_rig_prefixes()
        for (rig_name, polecat_name), issue_id in bead_assignments.items():
            prefix = prefixes.get(rig_name, rig_name)
            session_name = f"{prefix}-{polecat_name}"
            bead_to_session[(rig_name, issue_id)] = session_name

        # Build set of active bead IDs to detect stale assignments in BEAD column
        active_bead_ids: set[str] = set()
        for _, issue_id, _ in in_progress_beads:
            active_bead_ids.add(issue_id)
        for _, issue_id, _ in queued_beads:
            active_bead_ids.add(issue_id)

        table = build_table(
            socket,
            interval,
            birth_times,
            cmd_histories,
            title_histories,
            death_log,
            bead_assignments,
            pane_snapshots,
            activity_histories,
            active_bead_ids,
            wrap_title=args.wrap,
        )

        parts = [table]

        # Worked Now / Queue / Recently Closed row
        worked_panel = Panel(
            build_bead_status_table(
                "Worked Now",
                in_progress_beads,
                "No in-progress beads",
                bead_to_session=bead_to_session,
            ),
            title="[bold magenta]Worked Now[/]",
            border_style="dim blue",
            padding=(0, 1),
            expand=True,
        )
        queue_panel = Panel(
            build_bead_status_table("Queue", queued_beads, "No queued beads"),
            title="[bold magenta]Queue[/]",
            border_style="dim blue",
            padding=(0, 1),
            expand=True,
        )
        closed_panel = Panel(
            build_recently_closed_table(recently_closed),
            title="[bold magenta]Recently Closed[/]",
            border_style="dim blue",
            padding=(0, 1),
            expand=True,
        )
        parts.append(
            Columns(
                [worked_panel, queue_panel, closed_panel],
                expand=True,
                equal=True,
            )
        )

        # Bead Detail (rotating) / Pending PRs row
        bead_details = load_bead_details()
        detail_panel = Panel(
            build_bead_detail_content(bead_details, bead_to_session),
            title="[bold magenta]Bead Detail[/]",
            border_style="dim blue",
            padding=(0, 1),
            expand=True,
        )
        prs_panel = Panel(
            build_pending_prs_table(pending_prs),
            title="[bold magenta]Pending PRs[/]",
            border_style="dim blue",
            padding=(0, 1),
            expand=True,
        )
        bottom_row = Table(
            show_header=False, box=None, expand=True, padding=0, show_edge=False
        )
        bottom_row.add_column(ratio=1)
        bottom_row.add_column(ratio=2)
        bottom_row.add_row(detail_panel, prs_panel)
        parts.append(bottom_row)

        llm_status = ""
        if FIREWORKS_API_KEY:
            n_summaries = len(_session_summaries)
            if _summary_working:
                frame = _SPINNER_FRAMES[int(time.time() * 2) % len(_SPINNER_FRAMES)]
                llm_status = f"  ·  [bold cyan]{frame}[/] LLM summarizing..."
            elif _summary_last_run:
                ago = int(time.time() - _summary_last_run)
                llm_status = f"  ·  LLM: [cyan]{n_summaries}[/] sessions ({ago}s ago)"
            else:
                llm_status = f"  ·  LLM: [dim]waiting for first run[/]"
        subtitle = Text.from_markup(
            f"polled [bold]{now.strftime('%H:%M:%S')}[/]  ·  interval {interval}s{llm_status}  ·  [bold]r[/]esummarize  ·  Ctrl-C to stop"
        )
        return Panel(
            Group(*parts),
            title=f"[bold blue]tmux window tracker[/] — [cyan]{socket}[/]",
            subtitle=subtitle,
            border_style="blue",
            padding=0,
        )

    import select
    import tty
    import termios

    interactive = sys.stdin.isatty()
    old_term = termios.tcgetattr(sys.stdin) if interactive else None
    try:
        if interactive:
            tty.setcbreak(sys.stdin.fileno())
        with Live(render(), console=console, refresh_per_second=1, screen=True) as live:
            try:
                while True:
                    waited = 0
                    while waited < interval:
                        step = min(0.5, interval - waited)
                        if interactive and select.select([sys.stdin], [], [], step)[0]:
                            ch = sys.stdin.read(1)
                            if ch == "r":
                                force_summary_refresh()
                        else:
                            time.sleep(step)
                        waited += step
                    live.update(render())
                    save_state(
                        socket, birth_times, cmd_histories, title_histories, death_log
                    )
            except KeyboardInterrupt:
                stop_summary_thread()
                save_state(
                    socket, birth_times, cmd_histories, title_histories, death_log
                )
    finally:
        if interactive and old_term is not None:
            termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, old_term)

    console.print(f"[dim]State saved to {state_path(socket)}[/]")


if __name__ == "__main__":
    main()
