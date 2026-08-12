"""twin-entity health check — 数字分身实体常驻服务。

实体是 systemd 管理（twin-entity.service），这里探测两层：
1. systemctl is-active 判断进程是否存活
2. HTTP /health 判断 API 是否可响应
把结果并入 ops-daemon 统一输出，供日报告/告警消费。
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

SERVICE = "twin-entity.service"
_last_status: dict[str, str] = {}


def _systemctl_active() -> tuple[bool, str]:
    try:
        r = subprocess.run(["systemctl", "is-active", SERVICE],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active", r.stdout.strip()
    except Exception as e:
        return False, f"systemctl check failed: {e}"


def _heartbeat_age(heartbeat_path: str) -> int | None:
    try:
        ts = float(Path(heartbeat_path).read_text().strip())
        return int(time.time() - ts)
    except Exception:
        return None


async def check_twin_entity(cfg: dict, store=None) -> dict:
    active, state = _systemctl_active()
    issues = []

    # 进程存活（systemd）
    if not active:
        issues.append(f"service {state}")

    # API 可达 + 心跳新鲜度
    api_ok = False
    heartbeat_s = None
    try:
        port = cfg.get("port", 8790)
        host = cfg.get("host", "127.0.0.1")
        timeout = cfg.get("timeout_seconds", 3)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        # 发一个 GET /health
        writer.write(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        # HTTP/1.0 <code> ...
        parts = line.decode("latin-1").split(" ", 2)
        api_ok = len(parts) >= 2 and parts[1] == "200"
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except (asyncio.TimeoutError, TimeoutError, ConnectionRefusedError, OSError):
        issues.append("API 不可达")
    except Exception as e:
        issues.append(f"API check error: {e}")

    # 心跳新鲜度
    hb_path = cfg.get("heartbeat_path", "")
    if hb_path:
        heartbeat_s = _heartbeat_age(hb_path)
        if heartbeat_s is not None and heartbeat_s > cfg.get("heartbeat_max_s", 300):
            issues.append(f"心跳过期({heartbeat_s}s)")

    result = {
        "status": "up" if (active and api_ok) else "stopped",
        "service_active": active,
        "api_ok": api_ok,
        "heartbeat_s": heartbeat_s,
    }
    if issues:
        result["issues"] = issues
        result["status"] = "degraded" if active else "stopped"

    # 状态变化告警（幂等：仅在转非 up 时记一条）
    prev = _last_status.get("twin_entity")
    if store and prev == "up" and result["status"] != "up":
        store.append_episodic({"type": "twin_entity_degraded",
                               "service": SERVICE, "issues": issues})
    _last_status["twin_entity"] = result["status"]
    return result
