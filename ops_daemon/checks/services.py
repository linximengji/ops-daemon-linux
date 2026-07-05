import time
import psutil


async def check_processes(cfg: dict) -> dict:
    all_procs = {p.pid: p.info for p in psutil.process_iter(attrs=["pid", "name", "create_time"])}

    results = {}
    for key, names in cfg.items():
        if not isinstance(names, list):
            continue
        results[key] = {}
        for name in names:
            matches = {pid: p for pid, p in all_procs.items()
                       if name.lower() in p.get("name", "").lower()}
            if matches:
                pid = list(matches.keys())[0]
                uptime = int(time.time() - matches[pid]["create_time"])
                results[key][name] = {"status": "running", "pid": pid, "uptime_seconds": uptime}
            else:
                results[key][name] = {"status": "stopped"}

    return results
