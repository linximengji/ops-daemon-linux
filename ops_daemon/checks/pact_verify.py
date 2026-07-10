"""pact_verify — ops-daemon check wrapper for Pact contract verification.

Runs scripts/pact_verify.py --json, parses results.
"""
import json
import subprocess
from pathlib import Path

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "pact_verify.py")
_CACHE_TTL = 3600
_cache = {"data": None, "ts": 0}


async def check_pact_verify(cfg: dict, store=None) -> dict:
    now = __import__("time").time()

    if cfg.get("min_interval", _CACHE_TTL) > 0 and now - _cache["ts"] < cfg["min_interval"]:
        return _cache["data"] or {"status": "cached"}

    try:
        r = subprocess.run(
            ["python3", _SCRIPT, "--json"],
            capture_output=True, text=True, timeout=30,
        )
        result = json.loads(r.stdout)
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
