"""SSL certificate expiry check."""
import ssl, socket, datetime


def _issuer_name(cert: dict) -> str:
    issuer = cert.get("issuer", [])
    if isinstance(issuer, list):
        for pair in issuer:
            if isinstance(pair, tuple) and len(pair) >= 2 and pair[0] == "organizationName":
                return str(pair[1])
    return ""


# only log ssl_{status} event once per days_remaining value
_last_logged: dict[str, int] = {}


async def check_ssl(cfg: dict, store=None) -> dict:
    targets = cfg.get("targets", [])
    warn_days = cfg.get("warn_days", 30)
    critical_days = cfg.get("critical_days", 7)
    timeout = cfg.get("timeout_seconds", 5)
    results = {}

    for t in targets:
        name = t.get("name", f"{t['host']}:{t['port']}")
        host = t["host"]
        port = t["port"]

        # Skip localhost — plain HTTP, no SSL
        if host in ("127.0.0.1", "localhost", "::1"):
            results[name] = {"status": "skipped", "reason": "localhost"}
            continue

        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
            not_after = cert.get("notAfter", "")
            if not not_after:
                results[name] = {"status": "unknown", "error": "no notAfter in cert"}
                continue
            expiry = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
            now = datetime.datetime.utcnow()
            remaining = (expiry - now).days
            status = "ok"
            if remaining <= critical_days:
                status = "critical"
            elif remaining <= warn_days:
                status = "warn"
            results[name] = {
                "status": status, "expires": not_after,
                "days_remaining": remaining, "issuer": _issuer_name(cert),
            }
            if status != "ok" and store:
                prev = _last_logged.get(name)
                if prev is None or prev != remaining:
                    _last_logged[name] = remaining
                    store.append_episodic({
                        "type": f"ssl_{status}",
                        "target": name, "days_remaining": remaining,
                    })
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)}

    return results
