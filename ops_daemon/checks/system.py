import psutil


def _disk_key(mountpoint: str) -> str:
    return mountpoint.replace(":", "").replace("\\", "")


async def check_system(cfg: dict, store, baseline) -> dict:
    disk_warn = cfg.get("disk_warn_pct", 85)
    disk_critical = cfg.get("disk_critical_pct", 90)
    cpu_warn = cfg.get("cpu_warn_pct", 80)
    mem_warn = cfg.get("memory_warn_pct", 85)

    result = {}

    # disk
    result["disk"] = {}
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            pct = usage.percent
            result["disk"][part.mountpoint] = {
                "pct": pct, "free_gb": round(usage.free / 2 ** 30, 1)
            }
            if pct >= disk_critical:
                store.append_episodic({
                    "type": "disk_critical", "mount": part.mountpoint, "pct": pct
                })
            elif pct >= disk_warn:
                store.append_episodic({
                    "type": "disk_warn", "mount": part.mountpoint, "pct": pct
                })
            metric = f"disk_{_disk_key(part.mountpoint)}"
            try:
                baseline.record(metric, pct)
            except Exception:
                pass
        except PermissionError:
            pass

    # cpu
    cpu = psutil.cpu_percent(interval=1)
    result["cpu"] = {"pct": cpu}
    if cpu >= cpu_warn:
        store.append_episodic({"type": "cpu_high", "pct": cpu})
    baseline.record("cpu", cpu)

    # memory
    mem = psutil.virtual_memory()
    mem_pct = mem.percent
    result["memory"] = {"pct": mem_pct, "available_gb": round(mem.available / 2 ** 30, 1)}
    if mem_pct >= mem_warn:
        store.append_episodic({"type": "memory_high", "pct": mem_pct})
    baseline.record("memory", mem_pct)

    # boot uptime
    try:
        boot_ts = psutil.boot_time()
        uptime_seconds = int(__import__("time").time() - boot_ts)
        result["boot"] = {"uptime_seconds": uptime_seconds}
    except Exception:
        pass

    return result
