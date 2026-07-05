"""Repair actions registry — spawns processes directly via process_manager."""
import asyncio, subprocess, re
from .process_manager import spawn_proxy, spawn_claudetalk, spawn_mcp_server, spawn_feishu_bridge, CLAUDETALK_PID_FILE, clear_crash_marker, has_crash_marker


REPAIR_REGISTRY: dict[str, callable] = {}


DISABLED_REPAIRS = {
    "claudetalk", "feishu_bridge", "mcp_server",
}

def register_repair(name: str):
    if name in DISABLED_REPAIRS:
        return lambda fn: fn  # no-op decorator, function still importable

    def wrapper(fn):
        REPAIR_REGISTRY[name] = fn
        return fn
    return wrapper


async def _check_port(host: str = "127.0.0.1", port: int = 4000, timeout: float = 5) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False


@register_repair("proxy")
async def repair_proxy() -> dict:
    """Repair both proxy instances — main (4000) and backup (4002).
    Proactively switches CC to backup before restarting main to avoid connectivity drop.
    If main fails to recover, stays on backup."""
    # Proactively switch CC to healthy backup before restarting main
    from ops_daemon.proxy_manager import ensure_backup_before_main_restart
    ensure_backup_before_main_restart()
    spawn_proxy(4000, kill_first=True)
    main_ok = False
    for i in range(15):
        await asyncio.sleep(2)
        if await _check_port("127.0.0.1", 4000):
            main_ok = True
            break
    # Also ensure backup proxy is running
    if not await _check_port("127.0.0.1", 4002):
        spawn_proxy(4002, kill_first=True)
        for j in range(15):
            await asyncio.sleep(2)
            if await _check_port("127.0.0.1", 4002):
                break
    if main_ok:
        return {"status": "restored", "restart_time_s": (i + 1) * 2}
    # Main still down — backup was already switched proactively above.
    # Verify backup survived (it should, since we only restarted main).
    if await _check_port("127.0.0.1", 4002, timeout=2):
        return {"status": "degraded", "active": "backup",
                "error": "main 4000 not responding, degraded to 4002"}
    return {"status": "failed", "error": "proxy not responding after 30s"}


@register_repair("proxy_backup")
async def repair_proxy_backup() -> dict:
    """Repair only the backup proxy (4002) — used when main is healthy but backup died."""
    spawn_proxy(4002, kill_first=True)
    for i in range(15):
        await asyncio.sleep(2)
        if await _check_port("127.0.0.1", 4002):
            return {"status": "restored", "restart_time_s": (i + 1) * 2}
    return {"status": "failed", "error": "proxy backup not responding after 30s"}


@register_repair("claudetalk")
async def repair_claudetalk() -> dict:
    import signal, os
    # Phase 1: send SIGTERM for graceful drain (claudetalk has drainThenExit handler)
    pid = None
    try:
        with open(CLAUDETALK_PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        pass

    # Wait up to 10s for graceful shutdown (check if PID dies)
    if pid:
        for _ in range(10):
            await asyncio.sleep(1)
            r = subprocess.run(["ps", "-p", str(pid), "-o", "pid="],
                               capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                break  # process terminated gracefully

    # Phase 2: spawn_claudetalk handles force kill if needed + starts new process
    spawn_claudetalk(kill_first=True)
    # Defensive: _watch_exit thread from killed process may have written crash marker
    # after spawn_claudetalk's internal clear_crash_marker() (TOCTOU race).
    clear_crash_marker()
    # cli.js does not listen on any port (MCP server is a separate process on 9877).
    # Verify the new process is alive by checking its PID file content matches tasklist.
    for i in range(15):
        await asyncio.sleep(2)
        try:
            with open(CLAUDETALK_PID_FILE) as f:
                new_pid = f.read().strip()
            r = subprocess.run(["ps", "-p", str(new_pid), "-o", "pid="],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                # PID confirmed alive — also verify no stale crash marker
                if not has_crash_marker(max_age_s=60):
                    return {"status": "restored", "restart_time_s": (i + 1) * 2}
        except (FileNotFoundError, ValueError):
            pass
    return {"status": "failed", "error": "claudetalk not restored after 30s"}


@register_repair("mcp_server")
async def repair_mcp() -> dict:
    spawn_mcp_server(kill_first=True)
    for i in range(15):
        await asyncio.sleep(2)
        if await _check_port("127.0.0.1", 9877):
            return {"status": "restored", "restart_time_s": (i + 1) * 2}
    return {"status": "failed", "error": "MCP server not restored after 30s"}


@register_repair("feishu_bridge")
async def repair_feishu_bridge() -> dict:
    spawn_feishu_bridge(kill_first=True)
    for i in range(15):
        await asyncio.sleep(2)
        if await _check_port("127.0.0.1", 9878):
            return {"status": "restored", "restart_time_s": (i + 1) * 2}
    return {"status": "failed", "error": "feishu-bridge not restored after 30s"}
