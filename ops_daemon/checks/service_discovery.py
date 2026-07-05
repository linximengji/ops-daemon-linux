"""Service auto-discovery — detect new listening TCP ports and flag as candidates.

Phase 2 feature: scans localhost for ports not in known_ports list or
connections.yaml services. When enabled, reports new ports so they can
be added to the connections.yaml -> tunnel routing.

Enabled/disabled via connections.yaml discovery.enabled field.
"""
import yaml
from pathlib import Path

from ops_daemon._proc import get_listening_ports

CONNECTIONS_PATH = Path(__file__).resolve().parent.parent.parent.parent / "connections.yaml"


async def check_service_discovery(cfg: dict, store=None) -> dict:
    """Check for new listening TCP ports not in known_ports or connections.yaml services."""
    if not cfg.get("enabled", False):
        return {"status": "disabled"}

    known = set(cfg.get("known_ports", []))
    try:
        conn_cfg = yaml.safe_load(CONNECTIONS_PATH.read_text(encoding="utf-8"))
        for svc in conn_cfg.get("services", {}).values():
            if isinstance(svc, dict) and "port" in svc:
                known.add(svc["port"])
    except Exception:
        pass

    current = get_listening_ports(host="127.0.0.1")
    new_ports = sorted(current - known)

    if new_ports:
        return {
            "status": "candidate",
            "new_ports": new_ports,
            "message": f"发现新本地服务端口: {new_ports}；如需暴露到 tunnel 请加入 connections.yaml services",
        }
    return {"status": "ok", "checked_count": len(current)}
