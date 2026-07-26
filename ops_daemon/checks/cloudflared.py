"""Cloudflare Tunnel status check — cloud version."""
import asyncio
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


async def _get_connections() -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel", "info", "--output", "json", TUNNEL_NAME,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return 0
        info = json.loads(out.decode("utf-8", errors="replace"))
        connectors = info.get("conns", [])
        return sum(len(c.get("conns", [])) for c in connectors)
    except (subprocess.TimeoutExpired, asyncio.TimeoutError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return 0


async def check_cloudflared(cfg: dict, store=None) -> dict:
    proc = _get_process()
    if not proc:
        return {"status": "stopped", "connections": 0}

    conns = await _get_connections()
    status = "up" if conns > 0 else "degraded"

    result = {"status": status, "connections": conns, "pid": proc.pid}

    if store:
        working = store.load_working()
        # latest.json nests this check's result under the "cloudflared" key
        prev = working.get("cloudflared", {}) if isinstance(working, dict) else {}
        degraded_since = prev.get("degraded_since") if isinstance(prev, dict) else None
        if status == "degraded":
            if degraded_since is None:
                degraded_since = time.time()
                result["_first_degraded"] = True
            result["degraded_since"] = degraded_since
        else:
            result["degraded_since"] = None

    return result
