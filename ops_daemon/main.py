"""ops-daemon entry point — monitoring-only, no process management."""
import asyncio, json, subprocess, sys, os, time, yaml, traceback
from pathlib import Path

try:
    import systemd.daemon as sd  # systemd watchdog support (python3-systemd)
    _HAS_SD = True
except ImportError:
    _HAS_SD = False


sys.stdout.reconfigure(encoding="utf-8")

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
from ops_daemon.notify import notify
from ops_daemon.llm import diagnose as llm_diagnose


def load_config() -> tuple[dict, Path]:
    root = Path(__file__).parent.parent
    cfg_path = root / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f), root


async def main():
    cfg, root = load_config()
    dc = cfg["daemon"]
    cc = cfg["checks"]
    nc = cfg.get("notify", {})
    data_dir = root / dc["data_dir"]
    store = StateStore(str(data_dir))

    # Simple PID file — no locking, no singleton enforcement
    pid_path = root / "data" / "daemon.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
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

    lcfg = cfg.get("llm", {})

    # Service status history for up→stopped detection (persisted via working)
    _prev_checks: dict = {}

    @daemon.on_check_complete
    async def on_checks(checks: dict):
        nonlocal _prev_checks
        # systemd watchdog notification — tells systemd the daemon is alive
        if _HAS_SD:
            sd.notify("WATCHDOG=1")
        # Detect service status transitions: up → stopped
        _MONITORED_SERVICES = ('feishu_bridge', 'claudetalk', 'mcp_server', 'cloudflared', 'proxy')
        for name in _MONITORED_SERVICES:
            cur = checks.get(name, {})
            prev = _prev_checks.get(name, {})
            if isinstance(cur, dict) and isinstance(prev, dict):
                cur_status = cur.get('status')
                prev_status = prev.get('status')
                if prev_status == 'up' and cur_status == 'stopped':
                    await notify('WARN', f'{name} 已停止',
                                 f'服务 {name} 从运行状态变为停止。\n'
                                 f'PID: {prev.get("pid", "?")} → 已退出\n'
                                 f'请及时检查 systemd 状态')
        _prev_checks = dict(checks)
        # LLM diagnosis — purely passive, no repair
        if lcfg.get("enabled", False) and lcfg.get("diagnosis", False):
            criticals = [(k, v) for k, v in checks.items()
                         if isinstance(v, dict) and v.get("status") in ("critical",)]
            for name, result in criticals:
                recent = store.load_episodic(days=1)
                diagnosis = await llm_diagnose(f"{name}_critical", {"result": result}, recent)
                store.append_episodic({"type": "llm_diagnosis", "check": name, "text": diagnosis})

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
        cmd = ["python3", "runner.py",
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
    # sd_notify READY=1 tells systemd the daemon has initialized fully
    if _HAS_SD:
        sd.notify("READY=1")
    await notify("INFO", f"{dc['name']} started", f"PID {os.getpid()}")

    # crash-recovery: if last exit was a crash, notify after 3 minutes of stability
    _recent = store.load_episodic(days=1)
    _was_crash = any(e.get("type") == "daemon_crash" for e in _recent[-20:])
    if _was_crash:
        async def _stabilized_check():
            await asyncio.sleep(180)
            if daemon.running:
                await notify("INFO", f"{dc['name']} recovered",
                             "Daemon has been stable for 3 minutes after crash.")
        asyncio.create_task(_stabilized_check())

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
        scheduler.stop()
        store.append_episodic({"type": "daemon_stop", "message": "shutdown"})
        try:
            await notify("INFO", f"{dc['name']} stopped", f"PID {os.getpid()}")
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
