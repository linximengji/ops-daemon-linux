"""Pact Broker health check — HTTP GET / (index always 200 when running)."""
import asyncio, urllib.request, urllib.error

PACT_BROKER_URL = "http://localhost:9292"
HEALTH_PATH = "/"


async def check_pact_broker(cfg: dict, store=None) -> dict:
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 9292)
    timeout = cfg.get("timeout_seconds", 5)
    url = f"http://{host}:{port}{HEALTH_PATH}"

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
    except (TimeoutError, ConnectionRefusedError, OSError):
        return {"status": "down", "error": "tcp refused"}

    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        if r.status != 200:
            return {"status": "degraded", "error": f"HTTP {r.status}"}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return {"status": "degraded", "error": str(e)}

    return {"status": "up", "url": url}
