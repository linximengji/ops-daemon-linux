"""Trip lifecycle management — create, start, stop, list trips."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "trips"
ARCHIVE_DIR = DATA_DIR / "archive"
_tz = timezone(datetime.now().astimezone().utcoffset())


def create(trip_id: str, title: str, location: dict, schedule: list[dict]) -> dict:
    """Create a new trip JSON and return it. Does NOT start the trip."""
    p = DATA_DIR / f"{trip_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    trip = {
        "trip_id": trip_id,
        "title": title,
        "status": "pending",
        "created_at": datetime.now(_tz).isoformat(),
        "location": location,
        "preferences": {"hotel_budget": None, "food_style": None, "transport": "驾车"},
        "schedule": schedule,
    }
    p.write_text(json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[trip_mgr] created: {trip_id} ({title})")
    return trip


def start(trip_id: str) -> bool:
    """Start a trip via systemd template unit."""
    unit = f"trip@{trip_id}.service"
    result = subprocess.run(
        ["systemctl", "--user", "start", unit],
        capture_output=True, text=True, timeout=10,
    )
    ok = result.returncode == 0
    if ok:
        print(f"[trip_mgr] started: {unit}")
    else:
        print(f"[trip_mgr] start failed: {result.stderr.strip()}", file=sys.stderr)
    return ok


def stop(trip_id: str) -> bool:
    """Stop a running trip."""
    unit = f"trip@{trip_id}.service"
    result = subprocess.run(
        ["systemctl", "--user", "stop", unit],
        capture_output=True, text=True, timeout=10,
    )
    ok = result.returncode == 0
    if ok:
        print(f"[trip_mgr] stopped: {unit}")
    return ok


def list_active() -> list[str]:
    """List active trip IDs."""
    trips = []
    for p in sorted(DATA_DIR.glob("*.json")):
        trip = json.loads(p.read_text(encoding="utf-8"))
        if trip.get("status") in ("pending", "active"):
            trips.append(trip["trip_id"])
    return trips


def cancel(trip_id: str):
    """Cancel a trip — stop service + archive."""
    stop(trip_id)
    p = DATA_DIR / f"{trip_id}.json"
    if p.exists():
        trip = json.loads(p.read_text(encoding="utf-8"))
        trip["status"] = "cancelled"
        p.unlink(missing_ok=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        (ARCHIVE_DIR / p.name).write_text(
            json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[trip_mgr] cancelled + archived: {trip_id}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "create":
        # sys.argv[2]=trip_id, [3]=title, stdin=schedule JSON
        trip_id = sys.argv[2]
        title = sys.argv[3]
        location = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        schedule = json.loads(sys.stdin.read())
        create(trip_id, title, location, schedule)
    elif cmd == "start":
        start(sys.argv[2])
    elif cmd == "stop":
        stop(sys.argv[2])
    elif cmd == "cancel":
        cancel(sys.argv[2])
    elif cmd == "list":
        for tid in list_active():
            print(tid)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
