"""Trip runner — standalone process for trip@.service template units.

Each trip instance reads its JSON, monitors schedule nodes, and pushes
feishu notifications with rich cards. Exits when the trip completes or is cancelled.
"""
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

# travel-mcp is a sibling project, import its functions directly
_travel_mcp = Path(__file__).resolve().parent.parent.parent / "travel-mcp"
if str(_travel_mcp) not in sys.path:
    sys.path.insert(0, str(_travel_mcp))

from weather import get_weather  # noqa: E402
from poi import search_poi  # noqa: E402

DATA_DIR = Path(__file__).parent.parent / "data" / "trips"
# Trip bot credentials (TRIP_FEISHU_* take priority, fall back to generic FEISHU_*)
FEISHU_APP_ID = os.environ.get("TRIP_FEISHU_APP_ID") or os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("TRIP_FEISHU_APP_SECRET") or os.environ.get("FEISHU_APP_SECRET")
RECEIVE_ID = os.environ.get("TRIP_FEISHU_RECEIVE_ID") or os.environ.get("FEISHU_RECEIVE_ID", "ou_6a6b52dc63d4051834ae522a3a6e7775")

_tz = timezone(datetime.now().astimezone().utcoffset())
_executor = ThreadPoolExecutor(max_workers=2)
_last_weather_check = 0
WEATHER_CHECK_INTERVAL = 300  # 5 minutes


def _load_trip(trip_id: str) -> dict | None:
    p = DATA_DIR / f"{trip_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save_trip(trip: dict):
    p = DATA_DIR / f"{trip['trip_id']}.json"
    p.write_text(json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")


async def _get_token() -> str | None:
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return None
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        return r.json().get("tenant_access_token")


async def _send_card(payload: dict, receive_id: str = "", receive_id_type: str = "open_id") -> bool:
    token = await _get_token()
    if not token:
        print("[trip_runner] no feishu token, skip card")
        return False
    target = receive_id or RECEIVE_ID
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": target,
                "msg_type": "interactive",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        )
        ok = r.status_code == 200
        if not ok:
            print(f"[trip_runner] card send failed: {r.status_code} {r.text[:200]}")
        return ok


def _parse_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts).replace(tzinfo=_tz)


# ========== Card Builders ==========

def _location_str(loc: dict) -> str:
    """Format a location dict to readable string."""
    name = loc.get("name", "")
    city = loc.get("city", "")
    if city and city not in name:
        return f"{city}·{name}" if city else name
    return name


def _build_start_card(trip: dict, pending: list[dict]) -> dict:
    lines = [f"共 {len(pending)} 个节点"]
    for n in pending[:8]:
        lines.append(f"- {n['time'][-8:-3]} {n['title']}")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"行程开始: {trip['title']}"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "各节点开始前会提前提醒你"}]},
        ],
    }


def _build_departure_card(node: dict, trip: dict) -> dict:
    """First pending node — show route + weather."""
    origin = trip.get("location", {})
    dest = node.get("location", {})
    route = trip.get("route_data", {})

    lines = [f"出发: {_location_str(origin)} → {_location_str(dest)}"]

    if route:
        dist = route.get("total_distance_km", "")
        dur = route.get("total_duration_h", "")
        tolls = route.get("total_tolls", "")
        parts = []
        if dist:
            parts.append(f"约 {dist}km")
        if dur:
            parts.append(f"预计 {dur}h")
        if tolls is not None:
            parts.append(f"收费 ¥{tolls}")
        if parts:
            lines.append(" · ".join(parts))

    # Weather
    dest_loc = dest if dest else {}
    weather_str = _get_weather_text(dest_loc)
    if weather_str:
        lines.append(f"\n{weather_str}")

    lines.append(f"\n出发时间: {node['time'][-8:-3]}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "出发提醒"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": node.get("description", "准备出发")}]},
        ],
    }


def _build_arrival_card(node: dict, _trip: dict) -> dict:
    """Last pending node — show nearby parking + food."""
    dest = node.get("location", {})
    coords = f"{dest.get('lng', '')},{dest.get('lat', '')}" if dest.get("lng") and dest.get("lat") else ""
    lines = []

    if coords:
        parking = _get_poi_text(coords, "parking", "附近停车场", count=3)
        if parking:
            lines.append(parking)
        food = _get_poi_text(coords, "food", "附近美食", count=3)
        if food:
            lines.append(f"\n{food}")

    if not lines:
        lines.append(f"目的地: {_location_str(dest)}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "即将到达"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": node.get("description", "")}]},
        ],
    }


def _build_node_card(node: dict) -> dict:
    """Generic node reminder."""
    loc = _location_str(node.get("location", {}))
    desc = node.get("description", "")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"提醒: {node['title']}"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": f"{desc}\n\n位置: {loc}"},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "如需调整，直接回复即可"}]},
        ],
    }


def _build_weather_alert_card(trip: dict, current: dict, saved: dict) -> dict | None:
    """Build a weather change alert card. Returns None if no significant change."""
    changes = _weather_changes(current, saved)
    if not changes:
        return None

    dest = trip.get("schedule", [{}])[-1].get("location", {}) if trip.get("schedule") else {}
    loc_name = _location_str(dest) if dest else trip.get("title", "")

    lines = [f"{loc_name}天气有变化："]
    for c in changes:
        lines.append(f"· {c}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "天气变化提醒"}, "template": "yellow"},
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "建议确认行程是否需要调整"}]},
        ],
    }


def _build_end_card(trip: dict) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"行程结束: {trip['title']}"}, "template": "green"},
        "elements": [
            {"tag": "markdown", "content": "所有节点已完成，感谢使用。"},
        ],
    }


# ========== Data Helpers (sync, run in thread pool) ==========

def _get_weather_text(loc: dict) -> str:
    """Get formatted weather text for a location dict."""
    if not loc:
        return ""
    try:
        city = loc.get("city", "")
        coords = f"{loc.get('lng', '')},{loc.get('lat', '')}" if loc.get("lng") and loc.get("lat") else ""
        w = get_weather(location=coords, city=city)
        if w.get("error"):
            return ""
        live = w.get("live", {})
        if live:
            weather_parts = [
                live.get("weather", "?"),
                f"{live.get('temperature', '?')}°C",
                live.get("wind", ""),
                live.get("wind_power", ""),
            ]
            return "  ".join(p for p in weather_parts if p)
        forecast = w.get("forecast", [])
        if forecast:
            f0 = forecast[0]
            return f"{f0.get('day_weather', '?')} {f0.get('day_temp', '?')}°C~{f0.get('night_temp', '?')}°C"
    except Exception as e:
        print(f"[trip_runner] weather fetch failed: {e}")
    return ""


def _get_poi_text(coords: str, poi_type: str, label: str, count: int = 3) -> str:
    """Get formatted POI text."""
    try:
        results = search_poi(location=coords, poi_type=poi_type, radius=3000)
        if not results:
            return ""
        lines = [f"{label}:"]
        for r in results[:count]:
            dist = f"{r['distance_m']}m" if r['distance_m'] < 1000 else f"{r['distance_m'] / 1000:.1f}km"
            extra = f"  ¥{r['cost']}/人" if r.get("cost") else ""
            lines.append(f"· {r['name']}  {dist}{extra}")
        return "\n".join(lines)
    except Exception as e:
        print(f"[trip_runner] poi fetch failed: {e}")
    return ""


def _weather_changes(current: dict, saved: dict) -> list[str]:
    """Compare current vs saved weather, return list of significant change descriptions."""
    if not current or not saved:
        return []
    cur_live = current.get("live", {})
    sav_live = saved.get("live", {})
    changes = []

    cur_w = cur_live.get("weather", "")
    sav_w = sav_live.get("weather", "")
    rain_keywords = ["雨", "雪", "雷", "storm", "rain", "snow"]
    cur_rain = any(k in cur_w for k in rain_keywords)
    sav_rain = any(k in sav_w for k in rain_keywords)

    if cur_rain and not sav_rain:
        changes.append(f"天气转为{cur_w}，注意带伞和行车安全")
    elif not cur_rain and sav_rain:
        changes.append(f"天气从{sav_w}转好，祝旅途愉快")

    cur_temp = cur_live.get("temperature", 0)
    sav_temp = sav_live.get("temperature", 0)
    if cur_temp and sav_temp and abs(cur_temp - sav_temp) > 5:
        direction = "下降" if cur_temp < sav_temp else "上升"
        changes.append(f"气温{direction}{abs(cur_temp - sav_temp)}°C (当前{cur_temp}°C)")

    cur_wind = cur_live.get("wind_power", "")
    sav_wind = sav_live.get("wind_power", "")
    strong_wind = ["5", "6", "7", "8", "9", "10", "11", "12"]
    if cur_wind in strong_wind and sav_wind not in strong_wind:
        changes.append(f"风力增强至{cur_wind}级 ({cur_live.get('wind', '')})")

    return changes


# ========== Main Loop ==========

async def run_trip(trip_id: str):
    global _last_weather_check
    trip = _load_trip(trip_id)
    if not trip:
        print(f"[trip_runner] trip not found: {trip_id}", file=sys.stderr)
        return

    print(f"[trip_runner] starting trip: {trip['title']} ({trip_id})")
    trip["status"] = "active"
    trip["started_at"] = datetime.now(_tz).isoformat()
    _last_weather_check = 0
    _save_trip(trip)

    notified: set[str] = set()
    pending = [n for n in trip["schedule"] if _parse_time(n["time"]) > datetime.now(_tz)]

    if not pending:
        print("[trip_runner] no pending nodes, marking completed")
        _archive(trip)
        return

    trip_chat_id = trip.get("chat_id", "")
    _send_kwargs = {"receive_id": trip_chat_id, "receive_id_type": "chat_id"} if trip_chat_id else {}
    await _send_card(_build_start_card(trip, pending), **_send_kwargs)

    # Snapshot weather at start for change detection
    dest_loc = pending[-1].get("location", {})
    coords = (
        f"{dest_loc['lng']},{dest_loc['lat']}"
        if dest_loc.get("lng") and dest_loc.get("lat")
        else ""
    )
    city = dest_loc.get("city", "")
    loop = asyncio.get_running_loop()

    def _fetch_w():
        return get_weather(location=coords, city=city)

    if coords or city:
        try:
            saved_w = await loop.run_in_executor(_executor, _fetch_w)
            trip["last_weather"] = saved_w
            _save_trip(trip)
        except Exception as e:
            print(f"[trip_runner] initial weather fetch failed: {e}")

    while pending:
        now = datetime.now(_tz)
        now_ts = now.timestamp()

        # Periodic weather check
        if trip.get("last_weather") and now_ts - _last_weather_check >= WEATHER_CHECK_INTERVAL:
            _last_weather_check = now_ts
            try:
                current_w = await loop.run_in_executor(_executor, _fetch_w)
                alert_card = _build_weather_alert_card(trip, current_w, trip["last_weather"])
                if alert_card:
                    await _send_card(alert_card, **_send_kwargs)
                trip["last_weather"] = current_w
                _save_trip(trip)
            except Exception as e:
                print(f"[trip_runner] weather check failed: {e}")

        for node in pending[:]:
            node_time = _parse_time(node["time"])
            notify_at = node_time.timestamp() - node.get("notify_before_min", 15) * 60
            node_key = f"{node['time']}_{node['title']}"

            if now.timestamp() >= notify_at and node_key not in notified:
                is_first = node is pending[0]
                is_last = node is pending[-1] and len(pending) > 1

                if is_first:
                    card = _build_departure_card(node, trip)
                elif is_last:
                    card = _build_arrival_card(node, trip)
                else:
                    card = _build_node_card(node)

                await _send_card(card, **_send_kwargs)
                notified.add(node_key)

            if now >= node_time:
                trip = _load_trip(trip_id)
                if trip:
                    current_node = next(
                        (n for n in trip["schedule"] if n["time"] == node["time"] and n["title"] == node["title"]),
                        None,
                    )
                    if current_node and _parse_time(current_node["time"]) > now:
                        idx = pending.index(node)
                        pending[idx]["time"] = current_node["time"]
                        continue

                pending.remove(node)
                _trip = _load_trip(trip_id)
                if _trip:
                    _trip.setdefault("completed", []).append(node["title"])
                    _save_trip(_trip)

        await asyncio.sleep(15)

    trip = _load_trip(trip_id)
    if trip:
        await _send_card(_build_end_card(trip), **_send_kwargs)
        _archive(trip)


def _archive(trip: dict):
    trip["status"] = "completed"
    trip["completed_at"] = datetime.now(_tz).isoformat()
    src = DATA_DIR / f"{trip['trip_id']}.json"
    dst = DATA_DIR / "archive" / f"{trip['trip_id']}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")
    src.unlink(missing_ok=True)
    print(f"[trip_runner] archived: {trip['trip_id']}")


def main():
    if len(sys.argv) < 2:
        print("usage: python3 -m ops_daemon.trip_runner <trip_id>", file=sys.stderr)
        sys.exit(1)
    trip_id = sys.argv[1]
    asyncio.run(run_trip(trip_id))


if __name__ == "__main__":
    main()
