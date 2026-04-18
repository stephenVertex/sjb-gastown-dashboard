from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime


@dataclass
class RigRow:
    prefix: str
    name: str
    status: str  # operational / parked / docked / ...
    witness: str
    refinery: str
    polecats: int
    crew: int

    @property
    def active(self) -> bool:
        return self.status == "operational"


@dataclass
class RigsSnapshot:
    refreshed_at: datetime
    rows: list[RigRow]
    error: str | None = None


def _coerce_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0


def fetch_rigs() -> RigsSnapshot:
    now = datetime.now()
    try:
        result = subprocess.run(
            ["gt", "rig", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return RigsSnapshot(refreshed_at=now, rows=[], error=str(exc))
    if result.returncode != 0:
        return RigsSnapshot(refreshed_at=now, rows=[], error=result.stderr.strip() or "gt rig list failed")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return RigsSnapshot(refreshed_at=now, rows=[], error=f"json parse: {exc}")

    rows: list[RigRow] = []
    for entry in data:
        rows.append(
            RigRow(
                prefix=entry.get("beads_prefix") or "",
                name=entry.get("name") or "",
                status=entry.get("status") or "",
                witness=(entry.get("witness") or {}).get("state", "") if isinstance(entry.get("witness"), dict) else str(entry.get("witness") or ""),
                refinery=(entry.get("refinery") or {}).get("state", "") if isinstance(entry.get("refinery"), dict) else str(entry.get("refinery") or ""),
                polecats=_coerce_int(entry.get("polecats")),
                crew=_coerce_int(entry.get("crew")),
            )
        )
    rows.sort(key=lambda r: (not r.active, r.name))
    return RigsSnapshot(refreshed_at=now, rows=rows)
