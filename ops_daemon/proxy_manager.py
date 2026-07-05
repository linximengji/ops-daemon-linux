"""Proxy auto-switch: health-driven main ↔ backup."""
import json, os, socket, time

from ops_daemon.file_lock import acquire_lock, release_lock

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
SETTINGS_LOCK = os.path.join(os.path.dirname(SETTINGS_PATH), ".settings.lock")
STATS_URL = "http://127.0.0.1:{port}/v1/stats"
HEALTH_URL = "http://127.0.0.1:{port}/health"

ERROR_RATE_THRESHOLD = 0.5
ERROR_ABSOLUTE_MIN = 10
MIN_RECOVERY_WAIT = 60
RECOVERY_STABLE_CHECKS = 2


def _set_proxy_url(target: str):
    if not acquire_lock(SETTINGS_LOCK):
        pass  # fall through — best-effort
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
            s = json.load(f)
        url = "http://localhost:4000" if target == "main" else "http://localhost:4002"
        s.setdefault("env", {})["ANTHROPIC_BASE_URL"] = url
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
            f.write("\n")
    finally:
        release_lock(SETTINGS_LOCK)


def _tcp_check(host: str, port: int, timeout: float = 2) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def _health_check(host: str, port: int, timeout: float = 3) -> bool:
    """Check /health endpoint returns {'status': 'ok'}."""
    try:
        import httpx
        r = httpx.get(f"http://{host}:{port}/health", timeout=timeout)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


def ensure_backup_before_main_restart() -> bool:
    """Verify backup proxy (4002) is healthy (TCP + HTTP), then switch CC to it.

    Returns True if switch succeeded (CC is now on backup 4002),
    False if backup is unavailable (CC stays on main 4000).
    """
    if not _tcp_check("127.0.0.1", 4002):
        return False
    if not _health_check("127.0.0.1", 4002):
        return False
    _set_proxy_url("backup")
    return True


def _current_proxy() -> str:
    with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
        s = json.load(f)
    url = s.get("env", {}).get("ANTHROPIC_BASE_URL", "")
    return "backup" if "4002" in url else "main"


class ProxySwitch:

    def __init__(self, store=None):
        self.on_backup = _current_proxy() == "backup"
        self.switched_at = 0.0
        self.recovery_healthy_count = 0
        self.last_stats_time = 0.0
        self.prev_stats = None
        self.store = store

    async def _fetch_json(self, url: str, timeout: float = 5):
        from httpx import AsyncClient
        try:
            async with AsyncClient(timeout=timeout) as c:
                r = await c.get(url)
                return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def _get_proxy_status(self, checks: dict) -> dict:
        """Extract port statuses from multi-port check result."""
        px = checks.get("proxy", {})
        if not isinstance(px, dict):
            return {}
        return px.get("ports", {})

    def _switch(self, target: str, reason: str):
        try:
            _set_proxy_url(target)
            log_msg = f"proxy_switch to {target}: {reason}"
        except Exception as e:
            log_msg = f"proxy_switch to {target} FAILED: {e}"
        if self.store:
            self.store.append_episodic({"type": "proxy_switch", "target": target, "reason": reason, "message": log_msg})

    async def evaluate(self, checks: dict):
        """Run once per check cycle. Call from on_check_complete."""
        now = time.time()
        ports_status = self._get_proxy_status(checks)

        main_ok = ports_status.get(4000, {}).get("status") == "up"
        backup_ok = ports_status.get(4002, {}).get("status") == "up"

        # ── On main: check if we should switch to backup ─────────────
        if not self.on_backup:
            if not main_ok:
                if backup_ok:
                    self._switch("backup", "main:4000 down, backup:4002 up")
                    self.on_backup = True
                    self.switched_at = now
                    self.recovery_healthy_count = 0
                else:
                    # Both down — log but don't switch (pointing to dead 4002 is worse)
                    log_msg = "main:4000 DOWN, backup:4002 also DOWN — no switch"
                    if self.store:
                        self.store.append_episodic({"type": "proxy_switch", "target": "none", "reason": log_msg})
                return

            # Error rate from /v1/stats
            if now - self.last_stats_time >= 30:
                self.last_stats_time = now
                stats = await self._fetch_json(STATS_URL.format(port=4000))
                if stats and self.prev_stats:
                    de = stats.get("errors_total", 0) - self.prev_stats.get("errors_total", 0)
                    dr = stats.get("requests_total", 0) - self.prev_stats.get("requests_total", 0)
                    if dr >= 5 and de >= ERROR_ABSOLUTE_MIN and de / dr >= ERROR_RATE_THRESHOLD:
                        if backup_ok:
                            self._switch("backup", f"error_rate: {de/dr:.0%} ({de}/{dr})")
                            self.on_backup = True
                            self.switched_at = now
                            self.recovery_healthy_count = 0
                self.prev_stats = stats
            return

        # ── On backup: auto-recover to main ─────────────────────────
        if now - self.switched_at < MIN_RECOVERY_WAIT:
            return
        main_health = await self._fetch_json(HEALTH_URL.format(port=4000))
        if main_health and main_health.get("status") == "ok":
            self.recovery_healthy_count += 1
            if self.recovery_healthy_count >= RECOVERY_STABLE_CHECKS:
                self._switch("main", "auto-recovery (stable)")
                self.on_backup = False
                self.recovery_healthy_count = 0
                self.prev_stats = None
        else:
            self.recovery_healthy_count = 0
