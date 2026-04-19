from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

POLL_INTERVAL = 5
CMD_HISTORY = 8
TITLE_HISTORY = 100
RECENT_DEATHS = 20
HOME_GT = str(Path.home() / "gt")
BAR_WIDTH = 28
BEAD_DETAIL_REFRESH = 60
QUEUE_LINES = 8
ACTIVITY_HISTORY = 6
PR_REFRESH_INTERVAL = 60
PR_TABLE_LINES = 12
MAX_ORPHAN_BRANCHES_PER_RIG = 3
RECENTLY_CLOSED_LINES = 8
CLOSED_LOOKBACK_DAYS = 2
LLM_SUMMARY_INTERVAL = 30
FIREWORKS_API_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
FIREWORKS_MODEL = "accounts/fireworks/models/minimax-m2p5"
FIREWORKS_API_KEY = os.environ.get("FIREWORKS_API_KEY", "")
_SUMMARY_SKIP_ROLES = {"witness", "boot", "deacon"}

GT_ROOT = Path.home() / "gt"
XDG_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
STATE_DIR = XDG_STATE_DIR / "gastown"

GT_AUTO_PLUGIN_TITLES = {
    "Plugin crash (blocks boot)",
    "Plugin stderr on startup",
    "Plugin timed out during startup",
}

WindowKey = tuple[str, str]
WindowInfo = tuple[str, str, str, str, str]


@dataclass
class DashboardSnapshot:
    socket: str
    refreshed_at: datetime
    windows: dict[WindowKey, WindowInfo]
    birth_times: dict[WindowKey, datetime]
    cmd_histories: dict[WindowKey, deque[str]]
    title_histories: dict[WindowKey, deque[tuple[str, str]]]
    death_log: deque[tuple[str, str, str]]
    pane_snapshots: dict[str, str]
    activity_histories: dict[WindowKey, deque[int]]
    bead_assignments: dict[tuple[str, str], str]
    in_progress_beads: list[tuple[str, str, str]]
    queued_beads: list[tuple[str, str, str]]
    recently_closed: list[tuple[str, str, str]]
    pending_prs: list[tuple[str, str, str]]
    bead_details: list[dict]
    bead_to_session: dict[tuple[str, str], str]
    active_bead_ids: set[str]
    session_summaries: dict[str, str]
    llm_working: bool
    llm_last_run: float


def state_path(socket: str) -> Path:
    return STATE_DIR / f"tmux_window_ages_{socket}.json"


def save_state(
    socket: str,
    birth_times: dict[WindowKey, datetime],
    cmd_histories: dict[WindowKey, deque[str]],
    title_histories: dict[WindowKey, deque[tuple[str, str]]],
    death_log: deque[tuple[str, str, str]],
) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "birth_times": {f"{k[0]}:{k[1]}": v.isoformat() for k, v in birth_times.items()},
        "cmd_histories": {f"{k[0]}:{k[1]}": list(v) for k, v in cmd_histories.items()},
        "title_histories": {f"{k[0]}:{k[1]}": list(v) for k, v in title_histories.items()},
        "death_log": list(death_log),
    }
    tmp = state_path(socket).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(state_path(socket))


def load_state(
    socket: str,
) -> tuple[
    dict[WindowKey, datetime],
    dict[WindowKey, deque[str]],
    dict[WindowKey, deque[tuple[str, str]]],
    deque[tuple[str, str, str]],
]:
    path = state_path(socket)
    if not path.exists():
        return {}, {}, {}, deque(maxlen=RECENT_DEATHS)
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}, {}, {}, deque(maxlen=RECENT_DEATHS)

    birth_times: dict[WindowKey, datetime] = {}
    for key, value in data.get("birth_times", {}).items():
        session, idx = key.split(":", 1)
        birth_times[(session, idx)] = datetime.fromisoformat(value)

    cmd_histories: dict[WindowKey, deque[str]] = {}
    for key, value in data.get("cmd_histories", {}).items():
        session, idx = key.split(":", 1)
        cmd_histories[(session, idx)] = deque(value, maxlen=CMD_HISTORY)

    title_histories: dict[WindowKey, deque[tuple[str, str]]] = {}
    for key, value in data.get("title_histories", {}).items():
        session, idx = key.split(":", 1)
        title_histories[(session, idx)] = deque([tuple(item) for item in value], maxlen=TITLE_HISTORY)

    death_log = deque([tuple(item) for item in data.get("death_log", [])], maxlen=RECENT_DEATHS)
    return birth_times, cmd_histories, title_histories, death_log


def list_all_windows(socket: str) -> dict[WindowKey, WindowInfo]:
    fmt = "#{session_name}:#{window_index}:#{window_name}:#{pane_current_command}:#{pane_current_path}:#{pane_title}:#{pane_id}"
    result = subprocess.run(
        ["tmux", "-L", socket, "list-windows", "-a", "-F", fmt],
        capture_output=True,
        text=True,
    )
    windows: dict[WindowKey, WindowInfo] = {}
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
    delta = timedelta(seconds=int(secs))
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
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
    return load_rig_prefixes().get(rig_name, rig_name)


def rig_name_from_path(path: str) -> str | None:
    try:
        path_obj = Path(path).resolve()
    except OSError:
        return None
    try:
        relative = path_obj.relative_to(GT_ROOT)
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


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


def bead_for_session(session: str, path: str, bead_assignments: dict[tuple[str, str], str]) -> str:
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
    return prefix_matches[0] if len(prefix_matches) == 1 else ""


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
        rows.append((parts[1], parts[3]))
    return rows


def load_bead_status_tables() -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
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
    seen: set[tuple[str, str]] = set()
    for rig_name, issue_id, title in in_progress:
        key = (rig_name, issue_id)
        if key in seen:
            continue
        seen.add(key)
        deduped_in_progress.append((rig_name, issue_id, title))
    return deduped_in_progress, queue


_closed_cache: list[tuple[str, str, str]] = []
_closed_cache_time: float = 0


def load_recently_closed_beads() -> list[tuple[str, str, str]]:
    global _closed_cache, _closed_cache_time
    now = time.time()
    if now - _closed_cache_time < PR_REFRESH_INTERVAL:
        return _closed_cache
    cutoff = (datetime.now() - timedelta(days=CLOSED_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
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
            if not stripped or stripped.startswith("Showing") or stripped.startswith("No "):
                continue
            parts = stripped.split(maxsplit=3)
            if len(parts) < 4:
                continue
            issue_id = parts[1]
            if "-wisp-" in issue_id:
                continue
            title = parts[3]
            if include_cross_rig_bead(rig_name, issue_id, title):
                results.append((rig_name, issue_id, title))
    _closed_cache = results
    _closed_cache_time = now
    return results


_bead_detail_cache: list[dict] = []
_bead_detail_cache_time: float = 0


def load_bead_details() -> list[dict]:
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


def parse_github_repo(remote_url: str) -> str | None:
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote_url)
    return match.group(1) if match else None


_pr_cache: list[tuple[str, str, str]] = []
_pr_cache_time: float = 0


def load_pending_prs() -> list[tuple[str, str, str]]:
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
                    pr_branches.add(pr.get("headRefName", ""))
                    pr_rows.append((rig_name, f"#{pr.get('number', 0)}", pr.get("title", "")))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass
        rig_branch_count = 0
        try:
            branch_result = subprocess.run(
                ["git", "--git-dir", str(repo_git), "branch", "-r", "--list", "origin/polecat/*"],
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
                    parts = branch.removeprefix("polecat/").split("/")
                    if len(parts) >= 2:
                        label = f"{parts[0]} ({parts[1].split('@')[0]})"
                    else:
                        label = parts[0] if parts else branch
                    branch_rows.append((rig_name, "branch", label))
        except (subprocess.TimeoutExpired, OSError):
            pass
    _pr_cache = pr_rows + branch_rows
    _pr_cache_time = now
    return _pr_cache


def measure_activity(previous: str, current: str) -> int:
    if not previous or previous == current:
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


_summary_lock = threading.Lock()
_session_summaries: dict[str, str] = {}
_summary_thread: threading.Thread | None = None
_summary_stop = threading.Event()
_summary_working = False
_summary_last_run: float = 0


def _is_summarizable_session(session: str, bead_assignments: dict[tuple[str, str], str]) -> bool:
    del bead_assignments
    if "-" not in session:
        return False
    _prefix, role = session.split("-", 1)
    return role not in _SUMMARY_SKIP_ROLES


def _call_fireworks(pane_text: str) -> str:
    if not FIREWORKS_API_KEY:
        return ""
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
                {"role": "user", "content": f"Terminal screen content:\n\n{trimmed}"},
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
            if not choices:
                return ""
            message = choices[0].get("message", {})
            msg = (message.get("content") or "").strip()
            msg = re.sub(r"<think>.*?</think>", "", msg, flags=re.DOTALL).strip().strip("'\"")
            if len(msg.split()) > 12:
                sentences = re.split(r"(?<=[.!?])\s+", msg)
                if len(sentences) > 1:
                    msg = sentences[-1].strip()
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
        return ""


def _summary_worker(
    socket: str,
    pane_snapshots: dict[str, str],
    bead_assignments_ref: list[dict[tuple[str, str], str]],
) -> None:
    global _summary_working, _summary_last_run
    while not _summary_stop.wait(LLM_SUMMARY_INTERVAL):
        _summary_working = True
        assignments = bead_assignments_ref[0] if bead_assignments_ref else {}
        windows = list_all_windows(socket)
        new_summaries: dict[str, str] = {}
        for (session, _idx), (_name, _cmd, _path, _title, pane_id) in windows.items():
            if not _is_summarizable_session(session, assignments):
                continue
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


def get_session_summary(session: str) -> str:
    with _summary_lock:
        return _session_summaries.get(session, "")


def start_summary_thread(
    socket: str,
    pane_snapshots: dict[str, str],
    bead_assignments_ref: list[dict[tuple[str, str], str]],
) -> None:
    global _summary_thread
    if not FIREWORKS_API_KEY or _summary_thread is not None:
        return
    _summary_stop.clear()
    _summary_thread = threading.Thread(
        target=_summary_worker,
        args=(socket, pane_snapshots, bead_assignments_ref),
        daemon=True,
    )
    _summary_thread.start()


def stop_summary_thread() -> None:
    global _summary_thread
    _summary_stop.set()
    if _summary_thread is not None:
        _summary_thread.join(timeout=5)
        _summary_thread = None


class DashboardDataStore:
    def __init__(self, socket: str) -> None:
        self.socket = socket
        self.birth_times, self.cmd_histories, self.title_histories, self.death_log = load_state(socket)
        self.pane_snapshots: dict[str, str] = {}
        self.activity_histories: dict[WindowKey, deque[int]] = {}
        self.bead_assignments_ref: list[dict[tuple[str, str], str]] = [{}]
        self._reconcile_existing_windows()
        start_summary_thread(self.socket, self.pane_snapshots, self.bead_assignments_ref)

    def _reconcile_existing_windows(self) -> None:
        initial_windows = list_all_windows(self.socket)
        for key in set(self.birth_times) - set(initial_windows):
            age_secs = (datetime.now() - self.birth_times[key]).total_seconds()
            self.death_log.append((datetime.now().strftime("%H:%M:%S"), f"{key[0]}:{key[1]}", fmt_age(age_secs)))
            del self.birth_times[key]
            self.cmd_histories.pop(key, None)
            self.title_histories.pop(key, None)

    def close(self) -> None:
        stop_summary_thread()
        save_state(self.socket, self.birth_times, self.cmd_histories, self.title_histories, self.death_log)

    def refresh(self) -> DashboardSnapshot:
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
            self.death_log.append((now.strftime("%H:%M:%S"), f"{key[0]}:{key[1]}", fmt_age(age_secs)))
            del self.birth_times[key]
            self.cmd_histories.pop(key, None)
            self.title_histories.pop(key, None)
            self.activity_histories.pop(key, None)

        bead_assignments = load_bead_assignments()
        self.bead_assignments_ref[0] = bead_assignments
        in_progress_beads, queued_beads = load_bead_status_tables()
        recently_closed = load_recently_closed_beads()
        pending_prs = load_pending_prs()
        bead_details = load_bead_details()

        bead_to_session: dict[tuple[str, str], str] = {}
        prefixes = load_rig_prefixes()
        for (rig_name, polecat_name), issue_id in bead_assignments.items():
            prefix = prefixes.get(rig_name, rig_name)
            bead_to_session[(rig_name, issue_id)] = f"{prefix}-{polecat_name}"

        active_bead_ids = {issue_id for _, issue_id, _ in in_progress_beads}
        active_bead_ids.update(issue_id for _, issue_id, _ in queued_beads)

        with _summary_lock:
            session_summaries = dict(_session_summaries)

        save_state(self.socket, self.birth_times, self.cmd_histories, self.title_histories, self.death_log)

        return DashboardSnapshot(
            socket=self.socket,
            refreshed_at=now,
            windows=windows,
            birth_times=dict(self.birth_times),
            cmd_histories=self.cmd_histories,
            title_histories=self.title_histories,
            death_log=self.death_log,
            pane_snapshots=dict(self.pane_snapshots),
            activity_histories=self.activity_histories,
            bead_assignments=bead_assignments,
            in_progress_beads=in_progress_beads,
            queued_beads=queued_beads,
            recently_closed=recently_closed,
            pending_prs=pending_prs,
            bead_details=bead_details,
            bead_to_session=bead_to_session,
            active_bead_ids=active_bead_ids,
            session_summaries=session_summaries,
            llm_working=_summary_working,
            llm_last_run=_summary_last_run,
        )
