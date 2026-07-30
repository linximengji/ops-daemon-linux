"""pact_verify - ops-daemon check wrapper for Pact contract verification.

Runs scripts/pact_verify.py --json, parses results.
"""
import asyncio
import json
from pathlib import Path

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "pact_verify.py")
_CACHE_TTL = 3600
_cache = {"data": None, "ts": 0}


async def check_pact_verify(cfg: dict, store=None) -> dict:
    now = __import__("time").time()

    min_interval = cfg.get("min_interval", _CACHE_TTL)
    if min_interval > 0 and now - _cache["ts"] < min_interval:
        return _cache["data"] or {"status": "cached"}

    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", _SCRIPT, "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        result = json.loads(out.decode())
    except Exception as e:
        return {"status": "error", "error": str(e)}

    failures = result.get("failures", [])
    summary = {
        "status": "up" if result.get("status") == "passed" else "degraded",
        "total": result.get("total_interactions", 0),
        "passed": result.get("passed", 0),
        "failed": result.get("failed", 0),
        "skipped": result.get("skipped", 0),
        "failures": failures[:5],
        "ts": result.get("timestamp", ""),
    }

    if failures and store:
        store.append_episodic({"type": "pact_verify_failed", "failures": failures[:3]})

    _cache["data"] = summary
    _cache["ts"] = now
    return summary
