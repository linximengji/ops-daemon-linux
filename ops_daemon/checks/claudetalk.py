"""claudetalk / feishu-bridge / MCP server health checks.
Uses process name matching so it works without systemd."""
import asyncio
import time
import psutil

CLAUDETALK_PROC_NAME = "claudetalk-default"
FEISHU_BRIDGE_PROC_NAME = "feishu-bridge"
MCP_SERVER_PROC_NAME = "claudetalk-mcp"

_last_status: dict[str, str] = {}


def _find_pid_by_name(name: str) -> int | None:
    for proc in psutil.process_iter(["name", "cmdline", "pid", "create_time"]):
        try:
            # Match by cmdline containing the name (e.g. "claudetalk-default")
            cmdline = proc.info.get("cmdline") or []
            if any(name in part for part in cmdline):
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _process_info(name: str) -> dict:
    pid = _find_pid_by_name(name)
    if pid is None:
        return {"status": "stopped"}
    try:
        uptime = int(time.time() - psutil.Process(pid).create_time())
    except Exception:
        uptime = None
    return {"status": "up", "pid": pid, "uptime_seconds": uptime}


async def check_claudetalk(cfg: dict, store=None) -> dict:
    result = _process_info(CLAUDETALK_PROC_NAME)
    if store and result["status"] == "stopped" and _last_status.get(CLAUDETALK_PROC_NAME) != "stopped":
        store.append_episodic({"type": "claudetalk_stopped"})
    _last_status[CLAUDETALK_PROC_NAME] = result["status"]
    return result


async def check_feishu_bridge(cfg: dict, store=None) -> dict:
    result = _process_info(FEISHU_BRIDGE_PROC_NAME)
    if store and result["status"] == "stopped" and _last_status.get(FEISHU_BRIDGE_PROC_NAME) != "stopped":
        store.append_episodic({"type": "feishu_bridge_stopped"})
    _last_status[FEISHU_BRIDGE_PROC_NAME] = result["status"]
    return result


async def check_mcp_server(cfg: dict, store=None) -> dict:
    # 优先用进程名匹配
    by_name = _process_info(MCP_SERVER_PROC_NAME)
    if by_name["status"] == "up":
        return by_name

    # 回退到端口检查
    port = cfg.get("port", 9877)
    host = cfg.get("host", "127.0.0.1")
    timeout = cfg.get("timeout_seconds", 5)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
    except (asyncio.TimeoutError, TimeoutError, ConnectionRefusedError, OSError):
        return {"status": "stopped", "error": "port %d not open" % port}
    return {"status": "up"}
