"""Check if the daily ops report was sent today. Replaces the old scheduled_tasks check."""
import os
from datetime import datetime
from pathlib import Path


_DEFAULT_WORKING = str(Path.home() / "projects" / "ops-daemon" / "data" / "working")


async def check_report(cfg: dict, store=None) -> dict:
    now = datetime.now()
    check_after = cfg.get("check_after_hour", 9)
    if now.hour < check_after:
        return {"status": "skipped", "reason": f"before check_after_hour ({check_after})"}

    working_dir = Path(os.path.expanduser(cfg.get("working_dir", _DEFAULT_WORKING)))
    today = now.strftime("%Y-%m-%d")
    sentinel = working_dir / f"report-sent-{today}.sentinel"

    if sentinel.exists():
        return {"status": "ok", "sentinel": str(sentinel)}

    result = {"status": "missing", "error": f"report-sent-{today}.sentinel not found (report not sent)"}
    if store:
        store.append_episodic({"type": "report_missing", "date": today, "error": result["error"]})
    return result
