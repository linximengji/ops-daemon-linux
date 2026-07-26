"""Trip scanner — read-only observation of trip state.

Read-only check: counts pending / active / overdue trips and surfaces them for
the dashboard and MCP. Activation (pending -> active via systemd) is owned by
the user-level trip-activate.timer, NOT the daemon — the daemon only observes.

Scans two locations (ops-daemon dir takes priority on id conflict):
  - ops-daemon/data/trips/   (trip_runner's directory)
  - claudetalk/data/trips/   (trip.md agent writes here)
"""
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    _tz = ZoneInfo(os.environ.get("TZ") or Path("/etc/timezone").read_text(encoding="utf-8").strip())
except Exception:
    _tz = ZoneInfo("Asia/Shanghai")

_DAEMON_DIR = Path(__file__).resolve().parent.parent.parent  # ops-daemon/
_TRIP_DIRS = [
    _DAEMON_DIR / "data" / "trips",
    _DAEMON_DIR.parent / "claudetalk" / "data" / "trips",
]


def _parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts).replace(tzinfo=_tz)


async def check_trip_scanner(cfg: dict, store=None) -> dict:
    """Observe trip state: count pending/active and flag overdue pending trips."""
    seen_ids: set[str] = set()
    trip_files: list[Path] = []
    for d in _TRIP_DIRS:
        if d.exists():
            for p in sorted(d.glob("*.json")):
                if p.stem not in seen_ids:
                    seen_ids.add(p.stem)
                    trip_files.append(p)

    now = time.time()
    pending_count = 0
    active_count = 0
    overdue: list[str] = []  # pending trips past their start time (awaiting activator)
    errors: list[str] = []
    loop = asyncio.get_running_loop()

    for p in trip_files:
        try:
            trip = json.loads(await loop.run_in_executor(
                None, lambda pp=p: pp.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            errors.append(f"{p.name}: {e}")
            continue

        status = trip.get("status", "")
        trip_id = trip.get("trip_id", p.stem)
        if status == "pending":
            pending_count += 1
            start = trip.get("created_at")
            if start and _parse_time(start).timestamp() <= now:
                overdue.append(trip_id)
        elif status == "active":
            active_count += 1

    result = {
        "status": "ok",
        "pending": pending_count,
        "active": active_count,
        "overdue_pending": overdue,
    }
    if errors:
        result["errors"] = errors
        result["status"] = "degraded"
    return result
