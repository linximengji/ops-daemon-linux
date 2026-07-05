"""ops-daemon entry point."""
import asyncio, json, subprocess, sys, os, time, yaml, psutil, traceback
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")

_INIT_MARKER = "initialized"

def _write_init_marker(data_dir: Path):
    (data_dir / _INIT_MARKER).write_text(str(time.time()))

def _remove_init_marker(data_dir: Path):
    (data_dir / _INIT_MARKER).unlink(missing_ok=True)

# Load global .env at startup
_env_path = Path(os.path.expanduser("~/.claude/.env"))
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("\"'")
        if k not in os.environ:
            os.environ[k] = v

import sys as _ag_sys
_ag_path = str(Path(__file__).resolve().parent.parent.parent / "agent_core")
if _ag_path not in _ag_sys.path:
    _ag_sys.path.insert(0, _ag_path)
from agent_core import BaseDaemon, StateStore, BaselineEngine, AlertManager, Scheduler
from ops_daemon.checks.proxy import check_proxy
from ops_daemon.checks.cloudflared import check_cloudflared
from ops_daemon.checks.claudetalk import check_claudetalk, check_mcp_server, check_feishu_bridge
from ops_daemon.checks.services import check_processes
from ops_daemon.checks.system import check_system
from ops_daemon.checks.ssl import check_ssl
from ops_daemon.checks.logs import check_logs
from ops_daemon.checks.zombies import check_zombies
from ops_daemon.checks.report_check import check_report
from ops_daemon.checks.service_discovery import check_service_discovery
from ops_daemon.checks.pact_verify import check_pact_verify
from ops_daemon.checks.compose_up import check_compose_up
from ops_daemon.checks.defense_layers import check_defense_layers
from ops_daemon.checks.github_ci import check_github_ci
from ops_daemon.repair import REPAIR_REGISTRY
from ops_daemon.repair_coordinator import RepairCoordinator

# Module-level lock preventing concurrent spawn_claudetalk from watcher + repair
_SPAWN_LOCK = __import__("threading").Lock()

# ── OpenTelemetry init ─────────────────────────────────────────────────────
from opentelemetry import trace as _otel_trace
from opentelemetry.sdk.trace import TracerProvider as _TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as _OTLPSpanExporter
from opentelemetry.sdk.resources import Resource as _Resource
_otel_provider = _TracerProvider(resource=_Resource.create({"service.name": "ops-daemon"}))
_otel_provider.add_span_processor(_BatchSpanProcessor(_OTLPSpanExporter(
    endpoint="http://localhost:4317", insecure=True)))
_otel_trace.set_tracer_provider(_otel_provider)
try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    HTTPXClientInstrumentor().instrument()
except Exception:
    pass  # httpx instrumentor not critical
from ops_daemon.notify import notify
from ops_daemon.llm import diagnose as llm_diagnose
from ops_daemon.proxy_manager import ProxySwitch
from ops_daemon.process_manager import spawn_proxy, spawn_claudetalk, spawn_mcp_server, spawn_feishu_bridge
from ops_daemon._proc import get_pid_by_port as _get_pid_by_port, is_pid_alive as _proc_is_alive
# daily_report migrated to independent project: D:/ClaudeProjects/daily_report/runner.py


def load_config() -> tuple[dict, Path]:
    root = Path(__file__).parent.parent
    cfg_path = root / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f), root


def _read_pid_file(path: Path) -> int | None:
    try:
        raw = path.read_bytes().strip()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        return int(raw)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _systemd_pid(unit: str) -> int | None:
    import subprocess
    try:
        r = subprocess.run(["systemctl", "show", "--property=MainPID", unit], capture_output=True, text=True, timeout=5)
        pid = r.stdout.strip().replace("MainPID=", "")
        return int(pid) if pid.isdigit() and int(pid) > 1 else None
    except Exception:
        return None


def _find_pid_by_port(port: int) -> int | None:
    """Discover PID listening on a TCP port (cross-platform via _proc)."""
    return _get_pid_by_port(port)


def _write_protected_pids(data_dir: Path):
    """Write current PIDs to protected_pids.json for PreToolUse hook."""
    pid_sources = {
        "ops-daemon": {"pid": os.getpid(), "name": "ops-daemon"},
        "proxy-4000": {"pid": _find_pid_by_port(4000), "name": "model_proxy", "port": 4000},
        "proxy-4002": {"pid": _find_pid_by_port(4002), "name": "proxy_backup", "port": 4002},
        "claudetalk": {"pid": _systemd_pid("claudetalk"), "name": "claudetalk"},
        "feishu-bridge": {"pid": _systemd_pid("feishu-bridge"), "name": "feishu-bridge"},
    }
    # Omit services whose PID is unknown (avoids null entries)
    services = {k: v for k, v in pid_sources.items() if v["pid"] is not None}
    path = data_dir / "protected_pids.json"
    path.write_text(json.dumps({
        "services": services,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2))


async def _handle_tunnel_marker(action: str):
    """Handle .restart-tunnel / .stop-tunnel markers via tunnel_manager."""
    from ops_daemon.tunnel_manager import start, stop
    try:
        if action == "start":
            result = await asyncio.to_thread(start)
            print(f"[restart_watcher] .restart-tunnel done: {result.get('status')}", flush=True)
        else:
            result = await asyncio.to_thread(stop)
            print(f"[restart_watcher] .stop-tunnel done: {result.get('status')}", flush=True)
    except Exception as e:
        print(f"[restart_watcher] tunnel {action} failed: {e}", flush=True)

async def _restart_watcher(data_dir: Path, root: Path):
    """Poll .restart-* markers every 2s and execute restarts via process_manager."""
    import glob as _glob

    async def _spawn_claudetalk_safe():
        """Wrap spawn_claudetalk with spawn verification — returns True on success."""
        try:
            lock_ok = _SPAWN_LOCK.acquire(blocking=False)
        except NameError:
            print("[restart_watcher] _SPAWN_LOCK not defined (module-level NameError) — race window", flush=True)
            result = await asyncio.to_thread(lambda: spawn_claudetalk(kill_first=True))
            return result is not None
        if not lock_ok:
            print("[restart_watcher] spawn_claudetalk already in progress by another path, skipping", flush=True)
            return False
        try:
            result = await asyncio.to_thread(lambda: spawn_claudetalk(kill_first=True))
            return result is not None
        finally:
            _SPAWN_LOCK.release()

    async def _spawn_feishu_bridge_safe():
        result = await asyncio.to_thread(lambda: spawn_feishu_bridge(kill_first=True))
        return result is not None

    HANDLERS = {
        ".restart-proxy-4002": lambda: asyncio.to_thread(spawn_proxy, 4002),
        ".restart-mcp": lambda: asyncio.to_thread(spawn_mcp_server),
        ".restart-claudetalk": lambda: _spawn_claudetalk_safe(),
        ".restart-feishu-bridge": lambda: _spawn_feishu_bridge_safe(),
        ".restart-tunnel": lambda: _handle_tunnel_marker("start"),
        ".stop-tunnel": lambda: _handle_tunnel_marker("stop"),
    }

    while True:
        await asyncio.sleep(2)
        pattern = str(data_dir / ".restart-*"), str(data_dir / ".stop-*")
        found = sorted(_glob.glob(pattern[0]) + _glob.glob(pattern[1]))
        for marker in found:
            mpath = Path(marker)
            name = mpath.name
            if name == ".restart-daemon":
                mpath.unlink(missing_ok=True)
                print(f"[restart_watcher] .restart-daemon — touching .stop + marker", flush=True)
                # Pre-write heartbeat: check-daemon.ps1 sees fresh heartbeat and skips
                # kill+spawn during the window between old daemon exit and new daemon's
                # first heartbeat (at most 60s into its first check cycle).
                (data_dir / "heartbeat").write_text(str(time.time()))
                (root / ".stop").touch()
                (data_dir / ".trigger-restart").touch()  # 标记给 finally 用
                continue
            try:
                if name == ".restart-proxy-4000":
                    # Switch CC to backup first so main restart doesn't drop connectivity
                    from ops_daemon.proxy_manager import ensure_backup_before_main_restart
                    if ensure_backup_before_main_restart():
                        print(f"[restart_watcher] switched CC to backup before restarting proxy-4000", flush=True)
                    else:
                        print(f"[restart_watcher] backup 4002 not available (TCP or HTTP failed), restarting main directly", flush=True)
                    await asyncio.wait_for(
                        asyncio.to_thread(spawn_proxy, 4000), timeout=30)
                    mpath.unlink(missing_ok=True)
                    print(f"[restart_watcher] proxy-4000 restarted OK", flush=True)
                else:
                    handler = HANDLERS.get(name)
                    if handler is None:
                        print(f"[restart_watcher] unknown marker: {name}", flush=True)
                        mpath.unlink(missing_ok=True)
                        continue
                    # For claudetalk/feishu-bridge: await spawn result, only unlink on success
                    if name in (".restart-claudetalk", ".restart-feishu-bridge"):
                        success = await asyncio.wait_for(handler(), timeout=30)
                        if success:
                            mpath.unlink(missing_ok=True)
                            print(f"[restart_watcher] {name} restarted OK", flush=True)
                        else:
                            print(f"[restart_watcher] {name} spawn failed — keeping marker for retry", flush=True)
                    else:
                        await asyncio.wait_for(handler(), timeout=30)
                        mpath.unlink(missing_ok=True)
                        print(f"[restart_watcher] {name} restarted OK", flush=True)
            except asyncio.TimeoutError:
                print(f"[restart_watcher] TIMEOUT processing {name} (>30s) — unlinked marker", flush=True)
                mpath.unlink(missing_ok=True)
            except Exception as exc:
                print(f"[restart_watcher] {name} failed: {exc}", flush=True)
                mpath.unlink(missing_ok=True)


async def main():
    cfg, root = load_config()
    dc = cfg["daemon"]
    cc = cfg["checks"]
    rc = cfg.get("repair", {})
    nc = cfg.get("notify", {})
    data_dir = root / dc["data_dir"]
    store = StateStore(str(data_dir))

    # ── Daemon singleton lock (platform-specific) ──
    # LINUX_PATCHED: flock on Linux, Named Mutex + msvcrt on Windows
    pid_path = root / "data" / "daemon.pid"
    _close_mutex = lambda h: None
    _close_fallback_lock = lambda fd: None
    _win_mutex = None
    _fallback_lock_fd = None
    _lock_fd = None
    if sys.platform == "win32":
        _MUTEX_NAME = "Global\ClaudeProjects_OpsDaemon"
        try:
            import ctypes
            _win_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
            if ctypes.windll.kernel32.GetLastError() == 183:
                msg = "[main] another daemon already running (Named Mutex)"
                print(msg, flush=True)
                (data_dir / "daemon-boot-error.txt").write_text(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
                return
        except Exception as exc:
            print(f"[main] Named Mutex failed ({exc}), falling back to msvcrt file lock", flush=True)
            try:
                import msvcrt
                fh = open(pid_path, "r+b")
                _fallback_lock_fd = fh.fileno()
                msvcrt.locking(_fallback_lock_fd, msvcrt.LK_NBLCK, 1)
                fh.seek(0); fh.truncate()
                fh.write(str(os.getpid()).encode())
                fh.flush(); os.fsync(_fallback_lock_fd)
            except (PermissionError, OSError):
                msg = "[main] another daemon holds the PID lock — exiting"
                print(msg, flush=True)
                (data_dir / "daemon-boot-error.txt").write_text(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
                return
        def _close_mutex(handle):
            if handle is not None:
                try:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
        def _close_fallback_lock(fd):
            if fd is not None:
                try:
                    import msvcrt as _ms
                    _ms.locking(fd, _ms.LK_UNLCK, 1)
                    os.close(fd)
                except Exception:
                    pass
    else:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
            _lock_fd = os.open(pid_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(_lock_fd, 0)
            os.write(_lock_fd, str(os.getpid()).encode())
            os.fsync(_lock_fd)
        except (IOError, BlockingIOError):
            msg = "[main] another daemon holds the PID lock — exiting"
            print(msg, flush=True)
            (data_dir / "daemon-boot-error.txt").write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
            return
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    # Write PID file for PS script compatibility (no lock semantics)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    if not store.load_working():
        store.update_working({"daemon": dc["name"], "status": "starting"})

    baseline = BaselineEngine(store)
    alerts = AlertManager(store, cooldown=cfg["alerts"].get("cooldown_seconds", 1800))

    daemon = BaseDaemon(dc["name"], dc, store, heartbeat_path=str(data_dir / "heartbeat"))

    if cc["proxy"]["enabled"]:
        daemon.register_check("proxy", lambda: check_proxy(cc["proxy"], store, baseline))

    if cc["services"]["enabled"]:
        daemon.register_check("services", lambda: check_processes(cc["services"]))

    if cc["system"]["enabled"]:
        daemon.register_check("system", lambda: check_system(cc["system"], store, baseline))

    if cc.get("ssl", {}).get("enabled", False):
        daemon.register_check("ssl", lambda: check_ssl(cc["ssl"], store))

    if cc.get("logs", {}).get("enabled", False):
        daemon.register_check("logs", lambda: check_logs(cc["logs"], store))

    if cc.get("zombies", {}).get("enabled", False):
        daemon.register_check("zombies", lambda: check_zombies(cc["zombies"]))

    if cc.get("report_check", {}).get("enabled", False):
        daemon.register_check("report_check", lambda: check_report(cc["report_check"], store))

    if cc.get("claudetalk", {}).get("enabled", False):
        daemon.register_check("claudetalk", lambda: check_claudetalk(cc["claudetalk"], store))

    if cc.get("mcp_server", {}).get("enabled", cc.get("claudetalk", {}).get("enabled", False)):
        daemon.register_check("mcp_server", lambda cfg=cc["mcp_server"]: check_mcp_server(cfg, store))

    if cc.get("feishu_bridge", {}).get("enabled", True):
        daemon.register_check("feishu_bridge", lambda: check_feishu_bridge(cc.get("feishu_bridge", {}), store))

    if cc.get("cloudflared", {}).get("enabled", False):
        daemon.register_check("cloudflared", lambda: check_cloudflared(cc["cloudflared"], store))

    if cc.get("service_discovery", {}).get("enabled", False):
        daemon.register_check("service_discovery", lambda: check_service_discovery(cc["service_discovery"], store))

    if cc.get("pact_verify", {}).get("enabled", False):
        daemon.register_check("pact_verify", lambda: check_pact_verify(cc["pact_verify"], store, alerts))

    if cc.get("docker_compose", {}).get("enabled", False):
        daemon.register_check("docker_compose", lambda: check_compose_up(cc["docker_compose"], store))

    if cc.get("defense_layers", {}).get("enabled", False):
        daemon.register_check("defense_layers", lambda: check_defense_layers(cc["defense_layers"], store))

    if cc.get("github_ci", {}).get("enabled", False):
        daemon.register_check("github_ci", lambda: check_github_ci(cc["github_ci"], store))

    pxc = cfg.get("proxy_switch", {})
    proxy_mgr = ProxySwitch(store) if pxc.get("enabled", True) else None

    lcfg = cfg.get("llm", {})

    coordinator = RepairCoordinator(
        max_attempts=rc.get("max_attempts", 2),
        cooldown_s=rc.get("cooldown_seconds", 120),
        exhaust_timeout=rc.get("exhaust_timeout_seconds", 3600),
    )

    @daemon.on_check_complete
    async def _proxy_auto_switch(checks: dict):
        if proxy_mgr:
            await proxy_mgr.evaluate(checks)

    # ── cloudflared degraded tracking (idle_timeout from connections.yaml) ──
    try:
        _connections_cfg = yaml.safe_load(
            Path(__file__).resolve().parent.parent.parent.parent / "connections.yaml")
        _cf_timeout = _connections_cfg["tunnel"].get("idle_timeout", 600)
    except Exception:
        _cf_timeout = 600

    @daemon.on_check_complete
    async def on_checks(checks: dict):
        # LLM diagnosis for system/cert/log anomalies
        if lcfg.get("enabled", False) and lcfg.get("diagnosis", False):
            criticals = [(k, v) for k, v in checks.items()
                         if isinstance(v, dict) and v.get("status") in ("critical",)]
            for name, result in criticals:
                recent = store.load_episodic(days=1)
                diagnosis = await llm_diagnose(f"{name}_critical", {"result": result}, recent)
                store.append_episodic({"type": "llm_diagnosis", "check": name, "text": diagnosis})

        if not rc.get("enabled", True):
            return
        now = __import__("time").time()
        for name, result in checks.items():
            if not isinstance(result, dict):
                continue
            is_fail = result.get("status") in ("down", "stopped", "degraded") or result.get("error")
            if not is_fail:
                coordinator.on_success(name)
                continue

            # check has repair handler?
            repair_fn = REPAIR_REGISTRY.get(name)
            if not repair_fn:
                continue

            was_exhausted = coordinator.is_exhausted(name)
            decision = coordinator.decide(name, now)

            if decision == 'skip':
                continue

            if decision == 'exhausted':
                _was_notified = coordinator.was_notified(name)
                _attempts = coordinator.get_attempts(name)
                print(f"[debug] exhausted: name={name} attempts={_attempts} was_notified={_was_notified} notified_set={list(coordinator._notified)}", flush=True)
                if _was_notified:
                    print(f"[debug] BLOCKED notify — was_notified=True for {name}", flush=True)
                if not _was_notified:
                    alerts.fire("CRITICAL", name, f"repair failed after {_attempts} attempts")
                    if nc.get("enabled", True):
                        await notify("CRITICAL", f"ops-daemon: {name} repair failed",
                                    f"{name} is down, {_attempts} repair attempts exhausted.")
                if lcfg.get("enabled", False) and lcfg.get("diagnosis", False):
                    recent = store.load_episodic(days=1)
                    diagnosis = await llm_diagnose("repair_failed", {"check": name, "result": result}, recent)
                    store.append_episodic({"type": "llm_diagnosis", "check": name, "text": diagnosis})
                continue

            # decision == 'repair'
            if was_exhausted:
                tier_timeout = coordinator.get_current_tier_timeout(name)
                alerts.fire("INFO", name, f"repair exhaust timeout ({tier_timeout}s) elapsed, retrying")

            attempt_num = coordinator.record_attempt(name, now)
            alerts.fire("WARN", name, f"autorepair attempt {attempt_num}/{coordinator.max_attempts}")
            inner = await repair_fn()

            if inner.get("status") == "restored":
                coordinator.on_success(name)
                store.append_episodic({
                    "type": f"{name}_restored",
                    "repair_time_s": inner.get("restart_time_s"),
                })
                if nc.get("enabled", True):
                    await notify("INFO", f"ops-daemon: {name} restored",
                                f"{name} restored in {inner.get('restart_time_s')}s")
            else:
                store.append_episodic({
                    "type": f"{name}_repair_failed",
                    "attempts": attempt_num,
                    "error": inner.get("error"),
                })

    # ── claudetalk rapid crash protection ──
    _ct_prev_status = ""
    _ct_rapid_crashes = 0
    _ct_last_up = 0.0
    _ct_backoff_tier = 0  # 0=normal, >0=in stepped backoff (sentinel for recovery notification)
    # _SPAWN_LOCK defined at module level — prevents concurrent spawn from watcher + repair

    @daemon.on_check_complete
    async def _claudetalk_lifecycle(checks: dict):
        nonlocal _ct_prev_status, _ct_rapid_crashes, _ct_last_up, _ct_backoff_tier
        ct = checks.get("claudetalk", {})
        if not isinstance(ct, dict):
            return
        status = ct.get("status", "")
        RAPID_WINDOW = 15
        MAX_RAPID = 5

        if status == "up":
            if _ct_backoff_tier > 0:
                _ct_backoff_tier = 0
                if nc.get("enabled", True):
                    await notify("INFO", "claudetalk 已恢复",
                                "claudetalk 快速崩溃退避已结束，现已自动恢复运行")
            _ct_last_up = time.time()
            _ct_prev_status = "up"

        elif status in ("stopped", "zombie") and _ct_prev_status in ("", "up"):
            now = time.time()
            runtime = now - _ct_last_up if _ct_last_up > 0 else 0
            if runtime < RAPID_WINDOW:
                _ct_rapid_crashes += 1
                store.append_episodic({
                    "type": "claudetalk_rapid_crash",
                    "crash_count": _ct_rapid_crashes,
                    "runtime_s": round(runtime, 1),
                })
            else:
                _ct_rapid_crashes = 0  # survived beyond RAPID_WINDOW → reset counter

            if _ct_rapid_crashes >= MAX_RAPID:
                _ct_backoff_tier += 1
                _CT_BACKOFF_TIERS = [300, 900, 3600]
                tier_idx = min(_ct_backoff_tier - 1, len(_CT_BACKOFF_TIERS) - 1)
                tier_s = _CT_BACKOFF_TIERS[tier_idx]
                last_crash_count = _ct_rapid_crashes
                _ct_rapid_crashes = 0
                store.append_episodic({
                    "type": "claudetalk_rapid_crash_exhausted",
                    "crashes": last_crash_count,
                    "backoff_s": tier_s,
                })
                alerts.fire("CRITICAL", "claudetalk",
                            f"快速崩溃超过阈值 ({MAX_RAPID} 次 in {RAPID_WINDOW}s)，退避 {tier_s}s")
                if nc.get("enabled", True):
                    await notify("CRITICAL", "claudetalk 快速崩溃超过阈值",
                                f"claudetalk 已在 {RAPID_WINDOW}s 内崩溃 {MAX_RAPID} 次，"
                                f"退避 {tier_s}s 后自动重试")
                coordinator.set_backoff_tier("claudetalk", _CT_BACKOFF_TIERS, tier_idx)
                coordinator.mark_exhausted("claudetalk", time.time())

            _ct_prev_status = status

    @daemon.on_check_complete
    async def _repair_proxy_backup(checks: dict):
        """Detect when backup proxy 4002 is down while main is healthy — repair independently."""
        proxy_result = checks.get("proxy", {})
        if not proxy_result.get("ports"):
            return
        backup_port = proxy_result["ports"].get(4002, {})
        main_port = proxy_result["ports"].get(4000, {})
        if main_port.get("status") == "up" and backup_port.get("status") in ("down", None):
            now = __import__("time").time()
            last_bk = coordinator.get_last_cooldown("proxy_backup")
            if now - last_bk >= coordinator.cooldown_s:
                attempt_num = coordinator.record_attempt("proxy_backup", now)
                if attempt_num <= coordinator.max_attempts:
                    spawn_proxy(4002, kill_first=True)
                    store.append_episodic({"type": "proxy_backup_restarted", "source": "backup_detected"})
                else:
                    coordinator.mark_exhausted("proxy_backup", now)
                    alerts.fire("CRITICAL", "proxy_backup", f"repair exhausted after {coordinator.max_attempts} attempts")
            # Reset cooldown when healthy
        elif backup_port.get("status") == "up":
            coordinator.on_success("proxy_backup")

    @daemon.on_check_complete
    async def _write_pids(checks: dict):
        _write_protected_pids(data_dir)

    @daemon.on_check_complete
    async def _cloudflared_auto_stop(checks: dict):
        cf = checks.get("cloudflared", {})
        if not isinstance(cf, dict):
            return
        now = time.time()
        deg_since = cf.get("degraded_since")
        if cf.get("status") == "degraded" and deg_since is not None:
            elapsed = now - deg_since
            store.update_working_field("cloudflared_degraded_since", deg_since)
            if elapsed > _cf_timeout:
                store.append_episodic({
                    "type": "cloudflared_autostop",
                    "degraded_seconds": round(elapsed, 1),
                    "timeout": _cf_timeout,
                })
                from ops_daemon.tunnel_manager import stop as _cf_stop
                _cf_stop()
                store.update_working_field("cloudflared_degraded_since", None)
        elif cf.get("status") == "up":
            store.update_working_field("cloudflared_degraded_since", None)

    scheduler = Scheduler(persist_path=str(root / "data" / "tasks.json"))

    tcfg = cfg.get("tasks", {})

    _sentinel_dir = root / "data" / "working"

    def _write_report_sentinel():
        today = time.strftime("%Y-%m-%d")
        sentinel_path = _sentinel_dir / f"report-sent-{today}.sentinel"
        sentinel_path.write_text("sent")
        print(f"[scheduler] sentinel written: {sentinel_path}")

    _report_count = 0

    def run_report():
        nonlocal _report_count
        _report_count += 1
        print(f"[scheduler] run_daily_report called #{_report_count}")
        email_to = tcfg.get("daily_report", {}).get("email_to", "")
        cmd = ["python", "runner.py",
               "--data-dir", str(root / "data"),
               "--send-email"]
        if email_to:
            cmd += ["--to", email_to]
        sec_cfg = tcfg.get("daily_report", {}).get("sections", {})
        if sec_cfg:
            cmd += ["--section-config", json.dumps(sec_cfg)]
        _dr_env = {**__import__("os").environ}
        _dr_p = str(root.parent)
        if _dr_p not in _dr_env.get("PYTHONPATH", ""):
            _dr_env["PYTHONPATH"] = _dr_p + ":" + _dr_env.get("PYTHONPATH", "")
        result = subprocess.run(cmd, cwd=str(root.parent / "daily_report"), env=_dr_env, capture_output=True, timeout=120)
        print(f"[scheduler] run_daily_report #{_report_count} exit code={result.returncode}")
        if result.stdout:
            print(f"[scheduler] stdout:\n{result.stdout.decode('utf-8', errors='replace')}")
        if result.stderr:
            print(f"[scheduler] stderr:\n{result.stderr.decode('utf-8', errors='replace')}")
        if result.returncode == 0:
            _write_report_sentinel()

    if tcfg.get("daily_report", {}).get("enabled", True):
        scheduler.add_task("daily_report", tcfg["daily_report"]["schedule"], run_report)

    _semi_count = 0

    def run_semi_report():
        """Run semi-report pipeline via node."""
        nonlocal _semi_count
        _semi_count += 1
        print(f"[scheduler] run_semi_report called #{_semi_count}")
        result = subprocess.run(
            ["npx", "tsx", "src/pipeline.ts"],
            cwd=str(root.parent / "semi-report"),
            capture_output=True, timeout=600,
        )
        print(f"[scheduler] run_semi_report #{_semi_count} exit code={result.returncode}")
        if result.stdout:
            print(f"[scheduler] stdout:\n{result.stdout.decode('utf-8', errors='replace')}")
        if result.returncode == 0:
            _write_report_sentinel()

    if tcfg.get("semi_report", {}).get("enabled", True):
        scheduler.add_task("semi_report", tcfg["semi_report"]["schedule"], run_semi_report)

    scheduler.start()
    store.append_episodic({"type": "daemon_start", "message": f"{dc['name']} started"})
    await notify("INFO", f"{dc['name']} started", f"PID {os.getpid()}")

    # ── crash-recovery: 如果上次退出是 crash, 3 分钟后发稳定通知 ──
    _recent = store.load_episodic(days=1)
    _was_crash = any(e.get("type") == "daemon_crash" for e in _recent[-20:])
    if _was_crash:
        async def _stabilized_check():
            await asyncio.sleep(180)
            if daemon.running:
                await notify("INFO", f"{dc['name']} recovered",
                             "Daemon has been stable for 3 minutes after crash.")
        asyncio.create_task(_stabilized_check())

    restart_task = asyncio.create_task(_restart_watcher(data_dir, root))

    # Initialization complete — write marker so check-daemon.ps1 can verify
    _write_init_marker(data_dir)

    # Boot-time child process spawning. Each spawn_* function checks PID file
    # (via _is_pid_alive) before killing — daemon restart does NOT restart
    # already-healthy services, avoiding brief unavailability.
    # Failures are non-fatal — the check-repair cycle (every 60s) catches any
    # service that didn't start.
    _boot_spawns = [
        ("proxy-4000", lambda: spawn_proxy(4000)),
        ("proxy-4002", lambda: spawn_proxy(4002)),
        # ("claudetalk", lambda: spawn_claudetalk()),  # REMOVED: systemd-managed
        # ("mcp-server", lambda: spawn_mcp_server()),  # REMOVED: systemd-managed
        # ("feishu-bridge", lambda: spawn_feishu_bridge()),  # REMOVED: systemd-managed
    ]
    for name, fn in _boot_spawns:
        try:
            await asyncio.to_thread(fn)
            print(f"[main] boot-spawn: {name} OK", flush=True)
        except Exception as exc:
            print(f"[main] boot-spawn: {name} FAILED ({exc})", flush=True)

    try:
        await daemon.run()
    except KeyboardInterrupt:
        pass
    except BaseException:
        _tb = traceback.format_exc()
        print(f"[main] UNEXPECTED CRASH: {_tb}", flush=True)
        store.append_episodic({"type": "daemon_crash", "traceback": _tb})
        await notify("CRITICAL", f"{dc['name']} crashed",
                     f"```\n{_tb[-2000:]}\n```")
    finally:
        _remove_init_marker(data_dir)
        restart_task.cancel()
        scheduler.stop()

        store.append_episodic({"type": "daemon_stop", "message": "shutdown"})
        try:
            await notify("INFO", f"{dc['name']} stopped", f"PID {os.getpid()}")
        except Exception:
            pass

        # ── .restart-daemon: refresh heartbeat then spawn new instance ──
                # ── Platform-specific lock cleanup ──
        # LINUX_PATCHED FINALLY_BLOCK
        if sys.platform == "win32":
            if (data_dir / ".trigger-restart").exists():
                (data_dir / ".trigger-restart").unlink()
                (data_dir / "heartbeat").write_text(str(time.time()))
                print("[main] .restart-daemon: heartbeat refreshed, cleaning up...", flush=True)
                self_pid = os.getpid()
                try:
                    for p in psutil.process_iter(["pid", "cmdline"]):
                        try:
                            cmd = " ".join(p.info.get("cmdline") or [])
                            if "ops_daemon.main" not in cmd: continue
                            if p.info["pid"] == self_pid: continue
                            subprocess.run(["taskkill", "/F", "/PID", str(p.info["pid"])], capture_output=True, timeout=5)
                        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
                except Exception as exc:
                    print(f"[main] orphan daemon cleanup error: {exc}", flush=True)
                _close_mutex(_win_mutex)
                _close_fallback_lock(_fallback_lock_fd)
                pid_path.unlink(missing_ok=True)
                import ctypes as _ctypes, time as _time
                for _ in range(12):
                    _test_m = _ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
                    if _ctypes.windll.kernel32.GetLastError() != 183:
                        _ctypes.windll.kernel32.ReleaseMutex(_test_m)
                        _ctypes.windll.kernel32.CloseHandle(_test_m)
                        break
                    _ctypes.windll.kernel32.CloseHandle(_test_m)
                    _time.sleep(0.5)
                import subprocess as _sp, sys as _sys
                _sp.Popen([_sys.executable, "-m", "ops_daemon.main"],
                          creationflags=_POPEN_NO_WINDOW)
                return
            else:
                _close_mutex(_win_mutex)
                _close_fallback_lock(_fallback_lock_fd)
                pid_path.unlink(missing_ok=True)
        else:
            # LINUX: handle .trigger-restart (same pattern as Windows above)
            if (data_dir / ".trigger-restart").exists():
                (data_dir / ".trigger-restart").unlink()
                (data_dir / "heartbeat").write_text(str(time.time()))
                print("[main] .restart-daemon: heartbeat refreshed on Linux, cleaning up...", flush=True)
                self_pid = os.getpid()
                try:
                    for p in psutil.process_iter(["pid", "cmdline"]):
                        try:
                            cmd = " ".join(p.info.get("cmdline") or [])
                            if "ops_daemon.main" not in cmd:
                                continue
                            if p.info["pid"] == self_pid:
                                continue
                            p.terminate()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                except Exception as exc:
                    print(f"[main] orphan daemon cleanup error: {exc}", flush=True)
                if _lock_fd is not None:
                    try:
                        import fcntl
                        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                        os.close(_lock_fd)
                    except Exception:
                        pass
                pid_path.unlink(missing_ok=True)
                import subprocess as _sp, sys as _sys
                _sp.Popen([_sys.executable, "-m", "ops_daemon.main"])
                return
            else:
                if _lock_fd is not None:
                    try:
                        import fcntl
                        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                        os.close(_lock_fd)
                    except Exception:
                        pass
                pid_path.unlink(missing_ok=True)
if __name__ == "__main__":
    # Redirect stderr → daemon_stderr.log for process-level crashes
    # (segfault, OOM kill, C extension error) that bypass Python exception handling.
    _err_log = Path(__file__).parent.parent / "data" / "daemon_stderr.log"
    try:
        sys.stderr = open(_err_log, "a", encoding="utf-8", buffering=1)
    except Exception:
        pass
    # Also redirect stdout so all print() output is captured for post-mortem.
    _out_log = Path(__file__).parent.parent / "data" / "daemon_stdout.log"
    try:
        sys.stdout = open(_out_log, "a", encoding="utf-8", buffering=1)
    except Exception:
        pass
    # Global exception guard: catches anything main()'s internal except misses.
    try:
        asyncio.run(main())
    except BaseException:
        import traceback as _tb_outer
        _crash = Path(__file__).parent.parent / "data" / "daemon-boot-error.txt"
        _crash.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} UNCAUGHT: {_tb_outer.format_exc()}")
