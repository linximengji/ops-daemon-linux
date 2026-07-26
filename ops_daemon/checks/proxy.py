"""Proxy health check — probes main (4000) and backup (4002) ports + HTTP layer."""
import asyncio
import time

from ._probe import http_probe, get_pid_by_port, get_process_uptime

_last_main_status = None


async def check_proxy(cfg: dict, store, baseline) -> dict:
    global _last_main_status
    host = cfg.get("host", "127.0.0.1")
    ports = cfg.get("ports", [4000, 4002])
    main_port = ports[0]
    timeout = cfg.get("timeout_seconds", 3)

    results = {"ports": {}, "status": "down", "active_port": None}
    for port in ports:
        start = time.time()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout)
            writer.close()
            await writer.wait_closed()
            latency = (time.time() - start) * 1000
            baseline.record(f"proxy_latency_ms_{port}", round(latency, 1))

            port_info = {"latency_ms": round(latency, 1)}
            pid = get_pid_by_port(port)
            if pid is not None:
                port_info["pid"] = pid
                uptime = get_process_uptime(pid)
                if uptime is not None:
                    port_info["uptime_seconds"] = uptime

            # HTTP layer probe
            http_ok = await http_probe(host, port, "/v1/models", timeout)
            if http_ok:
                port_info["status"] = "up"
            else:
                port_info["status"] = "degraded"
                port_info["error"] = f"port {port} open but HTTP /v1/models not responding"
            results["ports"][port] = port_info
        except (asyncio.TimeoutError, TimeoutError, ConnectionRefusedError, OSError) as e:
            results["ports"][port] = {"status": "down", "error": str(e)}

    main = results["ports"].get(main_port, {})
    if main.get("status") == "up":
        results["status"] = "up"
        results["active_port"] = main_port
    elif main.get("status") == "degraded":
        results["status"] = "degraded"
        results["active_port"] = main_port
    elif results["ports"].get(ports[1], {}).get("status") in ("up", "degraded"):
        results["status"] = results["ports"][ports[1]]["status"]
        results["active_port"] = ports[1]
    else:
        results["status"] = "down"

    main_status = main.get("status")
    if main_status in ("down", "degraded") and _last_main_status not in ("down", "degraded"):
        store.append_episodic({
            "type": "proxy_down",
            "port": main_port,
            "error": main.get("error", "unknown"),
            "backup_status": results["ports"].get(ports[1], {}).get("status"),
        })
    _last_main_status = main_status
    return results
