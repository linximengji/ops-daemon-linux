"""Trip activator — start pending trips whose start time has arrived.

Runs as a systemd --user timer (trip-activate.timer), NOT part of the daemon.
Owns the trip lifecycle transition pending -> active. Runs in the user session,
so `systemctl --user start trip@<id>` works without env injection.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    _tz = ZoneInfo(os.environ.get("TZ") or Path("/etc/timezone").read_text(encoding="utf-8").strip())
except Exception:
    _tz = ZoneInfo("Asia/Shanghai")

_DAEMON_DIR = Path(__file__).resolve().parent.parent
_TRIP_DIRS = [
    _DAEMON_DIR / "data" / "trips",
    _DAEMON_DIR.parent / "claudetalk" / "data" / "trips",
]


def _parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts).replace(tzinfo=_tz)


def _is_running(trip_id: str) -> bool:
    r = subprocess.run(
        ["systemctl", "--user", "is-active", f"trip@{trip_id}.service"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() == "active"


def main() -> None:
    seen_ids: set[str] = set()
    trip_files: list[Path] = []
    for d in _TRIP_DIRS:
        if d.exists():
            for p in sorted(d.glob("*.json")):
                if p.stem not in seen_ids:
                    seen_ids.add(p.stem)
                    trip_files.append(p)

    now = time.time()
    for p in trip_files:
        try:
            trip = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[trip_activate] skip {p.name}: {e}", file=sys.stderr)
            continue
        if trip.get("status") != "pending":
            continue
        start = trip.get("created_at")
        if not start or _parse_time(start).timestamp() > now:
            continue

        trip_id = trip.get("trip_id", p.stem)
        # idempotent: service already up -> just reconcile status
        if _is_running(trip_id):
            trip["status"] = "active"
            p.write_text(json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")
            continue
        result = subprocess.run(
            ["systemctl", "--user", "start", f"trip@{trip_id}.service"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            trip["status"] = "active"
            p.write_text(json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[trip_activate] activated {trip_id}")
        else:
            print(f"[trip_activate] failed {trip_id}: {result.stderr.strip()}", file=sys.stderr)


if __name__ == "__main__":
    main()
