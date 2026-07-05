"""Shared async HTTP probe helpers for application-layer health checks.

Platform-specific PID/uptime lookups delegated to ops_daemon._proc."""
import asyncio
from datetime import datetime, timezone

from ops_daemon._proc import get_pid_by_port, get_process_uptime


async def http_probe(host: str, port: int, path: str = "/", timeout: float = 3) -> bool:
    """Async HTTP GET probe — returns True if server responds with 2xx/3xx."""
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        req = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        w.write(req.encode())
        await w.drain()
        header = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout)
        w.close()
        first_line = header.split(b"\r\n")[0].decode("utf-8", errors="replace")
        return first_line.startswith("HTTP/1.") and len(first_line) > 9 and first_line[9] in ("2", "3")
    except Exception:
        return False


def get_port_uptime(port: int) -> int | None:
    """Convenience: find PID by port then return its uptime."""
    pid = get_pid_by_port(port)
    if pid is None:
        return None
    return get_process_uptime(pid)
