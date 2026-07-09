"""Generic service check driven by service-registry.yaml.

Traverses the registry, probes each service according to its manager type,
and returns services[] array for inclusion in latest.json.

Service registry YAML lives one directory up from checks/.
"""
import subprocess
import socket
import asyncio
import yaml
from pathlib import Path
from ._probe import get_pid_by_port

def _registry_path() -> Path:
    # service-registry.yaml lives one dir up from ops_daemon/checks/
    return Path(__file__).resolve().parent.parent.parent / "service-registry.yaml"

def load_registry(path: Path | None = None) -> dict:
    p = path or _registry_path()
    if not p.exists():
        return {"services": {}}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"services": {}}

async def _check_systemd_unit(unit: str) -> dict:
    """Return status dict for a systemd-managed service (system-level)."""
    try:
        r = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", unit,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(r.communicate(), timeout=5)
        status = out.decode("utf-8", errors="replace").strip()
        if status == "active":
            return {"status": "up"}
        return {"status": "stopped"}
    except (subprocess.TimeoutExpired, asyncio.TimeoutError):
        return {"status": "unknown", "error": "systemctl is-active timed out"}
    except FileNotFoundError:
        return {"status": "unknown", "error": "systemctl not found"}

async def _check_orphan(name: str, svc: dict) -> dict:
    """Return status for an orphan (non-systemd) service."""
    port = svc.get("port")
    if not port:
        return {"status": "unknown"}
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.close()
        result = {"status": "up"}
        pid = get_pid_by_port(port)
        if pid:
            result["pid"] = pid
        return result
    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        return {"status": "stopped", "error": str(e)}

async def check_services() -> list[dict]:
    """Build services[] array from registry entries."""
    reg = load_registry()
    svcs = reg.get("services", {})
    results: list[dict] = []

    for name, svc in svcs.items():
        entry: dict = {
            "name": name,
            "group": svc.get("display_group", "other"),
        }

        mgr = svc.get("manager", "orphan")
        if mgr == "systemd":
            unit = svc.get("unit", f"{name}.service")
            status = await _check_systemd_unit(unit)
            entry.update(status)
        elif mgr == "orphan":
            status = await _check_orphan(name, svc)
            entry.update(status)
        else:
            entry["status"] = "unknown"
            entry["error"] = f"unknown manager type: {mgr}"

        # Attach optional port
        if svc.get("port"):
            entry["port"] = svc["port"]

        results.append(entry)

    return results
