"""Cloudflare Tunnel status check — cloud version."""
import json
import time
import psutil
import subprocess

TUNNEL_NAME = "remote-terminal"


def _get_process() -> psutil.Process | None:
    for p in psutil.process_iter(["pid", "name", "create_time"]):
        if "cloudflared" in (p.info.get("name") or "").lower():
            return p
    return None


def _get_connections() -> int:
    try:
        r = subprocess.run(
            ["cloudflared", "tunnel", "info", "--output", "json", TUNNEL_NAME],
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return 0
        info = json.loads(r.stdout)
        connectors = info.get("conns", [])
        return sum(len(c.get("conns", [])) for c in connectors)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, ValueError):
        return 0


async def check_cloudflared(cfg: dict, store=None) -> dict:
    proc = _get_process()
    if not proc:
        return {"status": "stopped", "connections": 0}

    conns = _get_connections()
    status = "up" if conns > 0 else "degraded"

    result = {"status": status, "connections": conns, "pid": proc.pid}

    if store:
        working = store.load_working()
        if isinstance(working, dict):
            degraded_since = working.get("cloudflared_degraded_since")
            if status == "degraded":
                if degraded_since is None:
                    degraded_since = time.time()
                    result["_first_degraded"] = True
                result["degraded_since"] = degraded_since
            else:
                result["degraded_since"] = None
        else:
            result["degraded_since"] = None

    return result
