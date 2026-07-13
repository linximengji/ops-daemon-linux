"""Trip scanner — auto-activate pending trips when their start time arrives.

Scans data/trips/*.json across all known project directories. For pending
trips whose start time has passed: marks them active and starts trip@.service
via systemd.

Scans two locations:
  - ops-daemon/data/trips/   (trip_runner's directory)
  - claudetalk/data/trips/   (trip.md agent writes here)
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_tz = timezone(datetime.now().astimezone().utcoffset())

_DAEMON_DIR = Path(__file__).resolve().parent.parent.parent  # ops-daemon/
_TRIP_DIRS = [
    _DAEMON_DIR / "data" / "trips",
    _DAEMON_DIR.parent / "claudetalk" / "data" / "trips",
]

# daemon runs as system service without user session env; these are needed
# for systemctl --user to reach the user's systemd instance
_USER_SYSTEMD_ENV = {
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
}


def _parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts).replace(tzinfo=_tz)


async def check_trip_scanner(cfg: dict, store=None) -> dict:
    """Check for pending trips that should be activated."""
    # Collect all trip files across all dirs (ops-daemon dir takes priority on id conflict)
    seen_ids: set[str] = set()
    trip_files: list[Path] = []
    for d in _TRIP_DIRS:
        if d.exists():
            for p in sorted(d.glob("*.json")):
                tid = p.stem
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    trip_files.append(p)
    now = time.time()
    activated = 0
    pending_count = 0
    active_count = 0
    errors: list[str] = []

    for p in trip_files:
        try:
            trip = json.loads(p.read_text(encoding="utf-8"))
            status = trip.get("status", "")
            trip_id = trip.get("trip_id", p.stem)

            if status == "pending":
                pending_count += 1
                start_time_str = trip.get("started_at") or trip.get("created_at")
                if not start_time_str:
                    continue
                start_time = _parse_time(start_time_str)
                if start_time.timestamp() <= now:
                    # Activate: mark status + start systemd unit
                    trip["status"] = "active"
                    p.write_text(json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")
                    result = subprocess.run(
                        ["systemctl", "--user", "start", f"trip@{trip_id}.service"],
                        capture_output=True, text=True, timeout=10,
                        env={**os.environ, **_USER_SYSTEMD_ENV},
                    )
                    if result.returncode == 0:
                        activated += 1
                        if store:
                            store.append_episodic({
                                "type": "trip_activated",
                                "trip_id": trip_id,
                                "title": trip.get("title", ""),
                            })
                    else:
                        errors.append(f"trip@{trip_id}: {result.stderr.strip()}")
            elif status == "active":
                active_count += 1
        except (json.JSONDecodeError, KeyError, OSError) as e:
            errors.append(f"{p.name}: {e}")

    result = {
        "status": "ok",
        "pending": pending_count,
        "active": active_count,
        "activated": activated,
    }
    if errors:
        result["errors"] = errors
        result["status"] = "degraded" if activated > 0 else "error"
    return result
