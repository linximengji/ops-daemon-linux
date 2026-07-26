import psutil

_last_disk_state: dict[str, str] = {}
_last_cpu_state = None
_last_mem_state = None


def _disk_key(mountpoint: str) -> str:
    return mountpoint.replace(":", "").replace("\\", "")


async def check_system(cfg: dict, store, baseline) -> dict:
    global _last_cpu_state, _last_mem_state
    disk_warn = cfg.get("disk_warn_pct", 85)
    disk_critical = cfg.get("disk_critical_pct", 90)
    cpu_warn = cfg.get("cpu_warn_pct", 80)
    mem_warn = cfg.get("memory_warn_pct", 85)

    result = {}

    # disk
    result["disk"] = {}
    for part in psutil.disk_partitions():
        try:
            # Skip snap loop mounts — always 100% full, not actionable
            if part.mountpoint.startswith("/snap/"):
                continue
            usage = psutil.disk_usage(part.mountpoint)
            pct = usage.percent
            result["disk"][part.mountpoint] = {
                "pct": pct, "free_gb": round(usage.free / 2 ** 30, 1)
            }
            if pct >= disk_critical:
                disk_state = "disk_critical"
            elif pct >= disk_warn:
                disk_state = "disk_warn"
            else:
                disk_state = "ok"
            if disk_state != "ok" and _last_disk_state.get(part.mountpoint) != disk_state:
                store.append_episodic({
                    "type": disk_state, "mount": part.mountpoint, "pct": pct
                })
            _last_disk_state[part.mountpoint] = disk_state
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
    cpu_state = "cpu_high" if cpu >= cpu_warn else "ok"
    if cpu_state != "ok" and _last_cpu_state != cpu_state:
        store.append_episodic({"type": "cpu_high", "pct": cpu})
    _last_cpu_state = cpu_state
    baseline.record("cpu", cpu)

    # memory
    mem = psutil.virtual_memory()
    mem_pct = mem.percent
    result["memory"] = {"pct": mem_pct, "available_gb": round(mem.available / 2 ** 30, 1)}
    mem_state = "memory_high" if mem_pct >= mem_warn else "ok"
    if mem_state != "ok" and _last_mem_state != mem_state:
        store.append_episodic({"type": "memory_high", "pct": mem_pct})
    _last_mem_state = mem_state
    baseline.record("memory", mem_pct)

    # boot uptime
    try:
        boot_ts = psutil.boot_time()
        uptime_seconds = int(__import__("time").time() - boot_ts)
        result["boot"] = {"uptime_seconds": uptime_seconds}
    except Exception:
        pass

    return result
