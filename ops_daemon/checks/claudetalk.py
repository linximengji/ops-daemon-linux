# claudetalk checks — systemd-managed on cloud.
import subprocess, time

async def _systemd_is_active(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False

async def _systemd_get_pid(unit: str) -> int | None:
    try:
        r = subprocess.run(["systemctl", "show", "--property=MainPID", unit], capture_output=True, text=True, timeout=5)
        pid = r.stdout.strip().replace("MainPID=", "")
        return int(pid) if pid.isdigit() and int(pid) > 1 else None
    except Exception:
        return None

async def check_claudetalk(cfg: dict, store=None) -> dict:
    if not await _systemd_is_active("claudetalk"):
        if store:
            store.append_episodic({"type": "claudetalk_stopped"})
        return {"status": "stopped"}
    pid = await _systemd_get_pid("claudetalk")
    result = {"status": "up", "pid": pid}
    if pid:
        import psutil
        try:
            result["uptime_seconds"] = int(time.time() - psutil.Process(pid).create_time())
        except Exception:
            pass
    return result

async def check_mcp_server(cfg: dict, store=None) -> dict:
    import socket
    port = cfg.get("port", 9877)
    host = cfg.get("host", "127.0.0.1")
    timeout = cfg.get("timeout_seconds", 5)
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
    except (TimeoutError, ConnectionRefusedError, OSError):
        return {"status": "stopped", "error": "port %d not open" % port}
    return {"status": "up"}

async def check_feishu_bridge(cfg: dict, store=None) -> dict:
    if not await _systemd_is_active("claudetalk"):
        if store:
            store.append_episodic({"type": "feishu_bridge_stopped"})
        return {"status": "stopped", "error": "feishu bridge bundled with claudetalk"}
    pid = await _systemd_get_pid("claudetalk")
    result = {"status": "up", "pid": pid}
    if pid:
        import psutil
        try:
            result["uptime_seconds"] = int(time.time() - psutil.Process(pid).create_time())
        except Exception:
            pass
    return result
