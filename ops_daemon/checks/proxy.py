"""Proxy health check — probes main (4000) and backup (4002) ports + HTTP layer."""
import socket, time

from ._probe import http_probe, get_pid_by_port, get_process_uptime


async def check_proxy(cfg: dict, store, baseline) -> dict:
    host = cfg.get("host", "127.0.0.1")
    ports = cfg.get("ports", [4000, 4002])
    main_port = ports[0]
    timeout = cfg.get("timeout_seconds", 3)

    results = {"ports": {}, "status": "down", "active_port": None}
    for port in ports:
        start = time.time()
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
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
        except (TimeoutError, ConnectionRefusedError, OSError) as e:
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

    if main.get("status") == "down" or main.get("status") == "degraded":
        store.append_episodic({
            "type": "proxy_down",
            "port": main_port,
            "error": main.get("error", "unknown"),
            "backup_status": results["ports"].get(ports[1], {}).get("status"),
        })
    return results
