"""Pact contract verification check — runs verify.py against local proxy."""
import asyncio, json, os, subprocess, sys, time
from pathlib import Path

VERIFY_SCRIPT = Path(__file__).resolve().parent.parent.parent.parent / "specs" / "pact" / "verify.py"


async def check_pact_verify(cfg: dict, store=None, alerts=None) -> dict:
    if not VERIFY_SCRIPT.exists():
        return {"status": "skipped", "detail": "verify.py not found"}

    min_interval = cfg.get("min_interval_seconds", 3600)
    last_run_key = "_last_pact_verify_ts"

    if store:
        working = store.load_working()
        last_ts = working.get(last_run_key, 0)
        if last_ts and time.time() - last_ts < min_interval:
            return {"status": "skipped", "detail": f"last run {int(time.time() - last_ts)}s ago"}

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                sys.executable, "-Xutf8", str(VERIFY_SCRIPT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=cfg.get("timeout_seconds", 30),
        )
        stdout, stderr = await proc.communicate()
        ok = proc.returncode == 0

        if store:
            store.update_working_field(last_run_key, time.time())

        detail = stdout.decode("utf-8", errors="replace").strip().split("\n")[-1]
        if ok:
            if alerts:
                alerts.fire("INFO", "pact_verify", "all contracts passed")
            return {"status": "up", "detail": detail}
        else:
            if alerts:
                alerts.fire("WARN", "pact_verify", f"contract drift detected: {detail}")
            return {
                "status": "degraded",
                "detail": detail or stderr.decode("utf-8", errors="replace").strip(),
            }
    except asyncio.TimeoutError:
        if alerts:
            alerts.fire("WARN", "pact_verify", "verify.py timed out")
        return {"status": "degraded", "detail": "verify.py timed out"}
    except Exception as e:
        if alerts:
            alerts.fire("WARN", "pact_verify", f"verify error: {e}")
        return {"status": "error", "detail": str(e)}
