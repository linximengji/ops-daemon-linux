"""Log file age/size check."""
import os, datetime


async def check_logs(cfg: dict, store=None) -> dict:
    max_age_hours = cfg.get("max_age_hours", 48)
    paths = cfg.get("paths", [])
    results = {}

    for raw in paths:
        expanded = os.path.expanduser(raw)
        try:
            st = os.stat(expanded)
        except (FileNotFoundError, PermissionError, OSError):
            results[raw] = {"status": "not_found"}
            continue

        age_hours = (datetime.datetime.now().timestamp() - st.st_mtime) / 3600
        size_mb = st.st_size / (1024 * 1024)
        status = "ok"
        if age_hours > max_age_hours:
            status = "stale"

        results[raw] = {"status": status, "age_hours": round(age_hours, 1),
                        "size_mb": round(size_mb, 1), "last_modified": str(datetime.datetime.fromtimestamp(st.st_mtime))}

        if status != "ok" and store:
            store.append_episodic({
                "type": "log_stale",
                "path": raw, "age_hours": round(age_hours, 1),
            })

    return results
