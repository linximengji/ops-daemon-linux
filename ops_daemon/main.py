"""ops-daemon entry point — state aggregator + task scheduler."""
# ruff: noqa: E402 — agent-core path insertion must happen before those imports
import asyncio
import json
import subprocess
import sys
import os
import time
import yaml
import traceback
from pathlib import Path

try:
    import systemd.daemon as sd
    _HAS_SD = True
except ImportError:
    _HAS_SD = False

sys.stdout.reconfigure(encoding="utf-8")

# ── Single-instance lock ──
_LOCK_PATH = Path(__file__).resolve().parent.parent / "data" / ".daemon.lock"
try:
    import fcntl
    _lock_fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[main] Another instance is already running (lock held at {_LOCK_PATH}). Exiting.")
        sys.exit(1)
except ImportError:
    pass  # Windows — no fcntl, skip lock

import sys as _ag_sys
_ag_path = str(Path(__file__).resolve().parent.parent.parent / "agent_core")
if _ag_path not in _ag_sys.path:
    _ag_sys.path.insert(0, _ag_path)
from agent_core import BaseDaemon, StateStore, BaselineEngine, AlertManager, Scheduler

from ops_daemon.checks.proxy import check_proxy
from ops_daemon.checks.cloudflared import check_cloudflared
from ops_daemon.checks.claudetalk import check_claudetalk, check_mcp_server, check_feishu_bridge
from ops_daemon.checks.system import check_system
from ops_daemon.checks.service_registry import check_services
from ops_daemon.checks.trip_scanner import check_trip_scanner

from ops_daemon.notify import notify
from ops_daemon.llm import diagnose as llm_diagnose

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


def load_config() -> tuple[dict, Path]:
    root = Path(__file__).parent.parent
    cfg_path = root / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f), root


async def main():
    cfg, root = load_config()
    dc = cfg["daemon"]
    cc = cfg["checks"]
    data_dir = root / dc["data_dir"]

    # OpenTelemetry init — agent-core checks will export spans to Jaeger
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        _provider = TracerProvider(resource=Resource.create({"service.name": "ops-daemon"}))
        _provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint="http://localhost:4317", insecure=True)))
        trace.set_tracer_provider(_provider)
        HTTPXClientInstrumentor().instrument()
    except Exception as _otel_err:
        print(f"[main] OTel init failed (non-fatal): {_otel_err}")

    store = StateStore(str(data_dir))

    # Rotate stdout/stderr on each start so they don't grow unbounded
    _log_dir = root / "data"
    for lname in ("daemon_stdout.log", "daemon_stderr.log"):
        lp = _log_dir / lname
        if lp.exists() and lp.stat().st_size > 5 * 1024 * 1024:
            rotated = _log_dir / f"{lname}.old"
            rotated.unlink(missing_ok=True)
            lp.rename(rotated)

    store.cleanup_episodic(keep_days=30)

    if not store.load_working():
        store.update_working({"daemon": dc["name"], "status": "starting"})

    baseline = BaselineEngine(store)
    # AlertManager is kept for future alert integration; remove if unused after refactor
    _alerts = AlertManager(store, cooldown=cfg["alerts"].get("cooldown_seconds", 1800))

    daemon = BaseDaemon(dc["name"], dc, store, heartbeat_path=str(data_dir / "heartbeat"))
    lcfg = cfg.get("llm", {})

    # ── Retained core checks (special probe logic) ──
    if cc["proxy"]["enabled"]:
        daemon.register_check("proxy", lambda: check_proxy(cc["proxy"], store, baseline))

    if cc["system"]["enabled"]:
        daemon.register_check("system", lambda: check_system(cc["system"], store, baseline))

    if cc.get("claudetalk", {}).get("enabled", False):
        daemon.register_check("claudetalk", lambda: check_claudetalk(cc["claudetalk"], store))

    if cc.get("mcp_server", {}).get("enabled", cc.get("claudetalk", {}).get("enabled", False)):
        daemon.register_check("mcp_server", lambda cfg=cc["mcp_server"]: check_mcp_server(cfg, store))

    if cc.get("feishu_bridge", {}).get("enabled", True):
        daemon.register_check("feishu_bridge", lambda: check_feishu_bridge(cc.get("feishu_bridge", {}), store))

    if cc.get("cloudflared", {}).get("enabled", False):
        daemon.register_check("cloudflared", lambda: check_cloudflared(cc["cloudflared"], store))

    # ── Managed check — deprecated status aggregator, will be replaced when all services
    #     migrate to systemd. Currently a pass-through for abandoned-format consumers.

    # ── Generic registry check — probes all services listed in service-registry.yaml ──
    daemon.register_check("services", check_services)

    # ── Defense layers check — aggregate L1 (Pact Broker) + L2 (Jaeger) + L3 (Git diff) ──
    if cc.get("defense_layers", {}).get("enabled", False):
        from ops_daemon.checks.defense_layers import check_defense_layers
        daemon.register_check("defense_layers", lambda: check_defense_layers(cc.get("defense_layers", {})))

    if cc.get("trip_scanner", {}).get("enabled", False):
        daemon.register_check("trip_scanner", lambda: check_trip_scanner(cc["trip_scanner"], store))

    if cc.get("pact_verify", {}).get("enabled", False):
        from ops_daemon.checks.pact_verify import check_pact_verify
        daemon.register_check("pact_verify", lambda: check_pact_verify(cc.get("pact_verify", {}), store))

    # ── on_check_complete: build unified output ──
    _prev_checks: dict = {}
    _stopped_count: dict = {}
    # Persist report_status to disk so daemon restart doesn't lose it
    _report_status_path = root / "data" / "working" / "_report_status.json"

    def _write_report_status(task_type: str, status: str):
        data = {"semi_report": {"status": "pending"}, "daily_report": {"status": "pending"}}
        if _report_status_path.exists():
            try:
                data = json.loads(_report_status_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        data[task_type] = {"status": status, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _report_status_path.write_text(json.dumps(data, ensure_ascii=False))

    def _load_report_status() -> dict:
        if not _report_status_path.exists():
            return {"semi_report": {"status": "pending"}, "daily_report": {"status": "pending"}}
        try:
            return json.loads(_report_status_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"semi_report": {"status": "pending"}, "daily_report": {"status": "pending"}}

    @daemon.on_check_complete
    async def on_checks(checks: dict):
        nonlocal _prev_checks, _stopped_count
        if _HAS_SD:
            sd.notify("WATCHDOG=1")

        # Re-inject report_status into latest.json (BaseDaemon.run() wrote without it)
        checks["report_status"] = _load_report_status()
        # Manually flush to disk so the current cycle includes report_status
        store.update_working(checks)

        # Service transition alerts (2 consecutive stopped rounds to debounce)
        _MONITORED_SERVICES = ('feishu_bridge', 'claudetalk', 'mcp_server', 'cloudflared', 'proxy')
        for name in _MONITORED_SERVICES:
            cur = checks.get(name, {})
            prev = _prev_checks.get(name, {})
            if isinstance(cur, dict) and isinstance(prev, dict):
                cur_status = cur.get('status')
                prev_status = prev.get('status')
                if cur_status == 'stopped':
                    c = _stopped_count.get(name, 0) + 1
                    _stopped_count[name] = c
                    if c >= 2 and prev_status == 'up':
                        await notify('WARN', f'{name} 已停止',
                                     f'服务 {name} 从运行状态变为停止。\n'
                                     f'PID: {prev.get("pid", "?")} → 已退出\n'
                                     f'请及时检查 systemd 状态')
                else:
                    _stopped_count[name] = 0

        _prev_checks = dict(checks)

        # LLM diagnosis — purely passive
        if lcfg.get("enabled", False) and lcfg.get("diagnosis", False):
            criticals = [(k, v) for k, v in checks.items()
                         if isinstance(v, dict) and v.get("status") in ("critical",)]
            for name, result in criticals:
                recent = store.load_episodic(days=1)
                diagnosis = await llm_diagnose(f"{name}_critical", {"result": result}, recent)
                store.append_episodic({"type": "llm_diagnosis", "check": name, "text": diagnosis})

    # ── Scheduler ──
    scheduler = Scheduler(persist_path=str(root / "data" / "tasks.json"))
    tcfg = cfg.get("tasks", {})
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
        _dr_env = {**os.environ}
        _dr_p = str(root.parent)
        if _dr_p not in _dr_env.get("PYTHONPATH", ""):
            _dr_env["PYTHONPATH"] = _dr_p + ":" + _dr_env.get("PYTHONPATH", "")
        try:
            _dr_cwd = str(root.parent / "daily_report")
            result = subprocess.run(cmd, cwd=_dr_cwd, env=_dr_env, capture_output=True, timeout=300)
            ok = result.returncode == 0
            _write_report_status("daily_report", "done" if ok else "failed")
            print(f"[scheduler] run_daily_report #{_report_count} exit code={result.returncode}")
            if result.stdout:
                print(f"[scheduler] stdout:\n{result.stdout.decode('utf-8', errors='replace')}")
            if result.stderr:
                print(f"[scheduler] stderr:\n{result.stderr.decode('utf-8', errors='replace')}")
        except Exception as e:
            _write_report_status("daily_report", "failed")
            print(f"[scheduler] run_daily_report #{_report_count} exception: {e}")

    if tcfg.get("daily_report", {}).get("enabled", True):
        scheduler.add_task("daily_report", tcfg["daily_report"]["schedule"], run_report)

    _semi_count = 0

    def run_semi_report():
        nonlocal _semi_count
        _semi_count += 1
        print(f"[scheduler] run_semi_report called #{_semi_count}")
        try:
            env = os.environ.copy()
            if tcfg.get("semi_report", {}).get("miniflux", False):
                env["MINIFLUX_ENABLED"] = "1"
            result = subprocess.run(
                ["npx", "tsx", "src/pipeline.ts"],
                cwd=str(root.parent / "semi-report"),
                capture_output=True, timeout=600, env=env,
            )
            ok = result.returncode == 0
            _write_report_status("semi_report", "done" if ok else "failed")
            print(f"[scheduler] run_semi_report #{_semi_count} exit code={result.returncode}")
            if result.stdout:
                print(f"[scheduler] stdout:\n{result.stdout.decode('utf-8', errors='replace')}")
        except Exception as e:
            _write_report_status("semi_report", "failed")
            print(f"[scheduler] run_semi_report #{_semi_count} exception: {e}")

    if tcfg.get("semi_report", {}).get("enabled", True):
        scheduler.add_task("semi_report", tcfg["semi_report"]["schedule"], run_semi_report)

    def run_twin_gap_push():
        print("[scheduler] run_twin_gap_push called")
        try:
            # Inject twin bot credentials so gap_detector pushes via twin bot
            twin_env = os.environ.copy()
            twin_env["FEISHU_APP_ID"] = os.environ.get("TWIN_FEISHU_APP_ID", os.environ.get("FEISHU_APP_ID", ""))
            twin_env["FEISHU_APP_SECRET"] = os.environ.get("TWIN_FEISHU_APP_SECRET", os.environ.get("FEISHU_APP_SECRET", ""))
            twin_env["FEISHU_RECEIVE_ID"] = os.environ.get("TWIN_FEISHU_RECEIVE_ID", os.environ.get("FEISHU_RECEIVE_ID", ""))
            result = subprocess.run(
                ["python3", "-c",
                 "import sys; sys.path.insert(0, '/home/ubuntu/projects/digital-clone')\n"
                 "from twin.gap_detector import detect_and_push_gaps\n"
                 "import json\n"
                 "print(json.dumps(detect_and_push_gaps(max_count=3), ensure_ascii=False))"],
                capture_output=True, timeout=120, env=twin_env,
            )
            ok = result.returncode == 0
            print(f"[scheduler] twin_gap_push exit={result.returncode} ok={ok}")
            if result.stdout:
                print(f"[scheduler] stdout: {result.stdout.decode('utf-8', errors='replace').strip()}")
            if result.stderr:
                print(f"[scheduler] stderr: {result.stderr.decode('utf-8', errors='replace')[:500]}")
        except Exception as e:
            print(f"[scheduler] twin_gap_push exception: {e}")

    if tcfg.get("twin_gap_push", {}).get("enabled", False):
        scheduler.add_task("twin_gap_push", tcfg["twin_gap_push"]["schedule"], run_twin_gap_push)

    scheduler.start()
    store.append_episodic({"type": "daemon_start", "message": f"{dc['name']} started"})
    if _HAS_SD:
        sd.notify("READY=1")
    await notify("INFO", f"{dc['name']} started", f"PID {os.getpid()}")

    # crash-recovery detection
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


if __name__ == "__main__":
    _err_log = Path(__file__).parent.parent / "data" / "daemon_stderr.log"
    try:
        sys.stderr = open(_err_log, "a", encoding="utf-8", buffering=1)
    except Exception:
        pass
    _out_log = Path(__file__).parent.parent / "data" / "daemon_stdout.log"
    try:
        sys.stdout = open(_out_log, "a", encoding="utf-8", buffering=1)
    except Exception:
        pass
    try:
        asyncio.run(main())
    except BaseException:
        import traceback as _tb_outer
        _crash = Path(__file__).parent.parent / "data" / "daemon-boot-error.txt"
        _crash.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} UNCAUGHT: {_tb_outer.format_exc()}")
