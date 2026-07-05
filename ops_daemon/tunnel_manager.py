"""tunnel_manager.py — 统一 tunnel + services 启停管理器。

职责：从 connections.yaml 读取配置，统一 start/stop/status。
      支持 process 和 docker 两种服务类型。
      支持按服务名单独启停。
"""
import json, logging, os, subprocess, sys, time, yaml
from pathlib import Path

from ops_daemon._proc import (
    kill_port, get_listening_ports, is_pid_alive,
    kill_pid, hard_kill,
)

_LOG = logging.getLogger("tunnel_manager")

# Resolve connections.yaml relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONNECTIONS_PATH = _PROJECT_ROOT.parent / "connections.yaml"
DATA_DIR = _PROJECT_ROOT / "data"
STATE_FILE = DATA_DIR / "remote_state.json"


def _load_config() -> dict:
    with open(CONNECTIONS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_exe(raw: str) -> str:
    return os.path.expandvars(raw)


def _wait_http(url: str, timeout: int) -> bool:
    import urllib.request
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(url, timeout=2).close()
            return True
        except Exception:
            time.sleep(1)
    return False


def _wait_tunnel(exe: str, tunnel_name: str, timeout: int) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            r = subprocess.run(
                [exe, "tunnel", "info", "-o", "json", tunnel_name],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                info = json.loads(r.stdout)
                if info.get("conns"):
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _start_service(svc_name: str, svc: dict) -> bool:
    """启动单个服务，返回是否成功。"""
    stype = svc.get("type", "process")
    if stype == "docker":
        container = svc.get("container")
        compose_dir = svc.get("compose_dir")
        compose_svc = svc.get("compose_service")
        if compose_dir and compose_svc:
            r = subprocess.run(
                ["docker", "compose", "-p", "ops-daemon", "start", compose_svc],
                cwd=compose_dir, capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        elif container:
            r = subprocess.run(
                ["docker", "start", container],
                capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        return False
    else:
        kill_port(svc["port"])
        cmd = svc["command"]
        popen_kwargs = {}
        if svc.get("workdir"):
            popen_kwargs["cwd"] = svc["workdir"]
        log_path = svc.get("log_file")
        err_path = svc.get("err_file")
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            popen_kwargs["stdout"] = open(log_path, "a", encoding="utf-8")
            popen_kwargs["stderr"] = (
                subprocess.STDOUT if not err_path
                else open(err_path, "a", encoding="utf-8")
            )
        subprocess.Popen(cmd, **popen_kwargs)
        health = svc.get("health", {})
        if health.get("type") == "http":
            port = svc["port"]
            path = health.get("path", "/health")
            return _wait_http(f"http://127.0.0.1:{port}{path}", health.get("timeout", 30))
        return True


def _stop_service(svc_name: str, svc: dict) -> bool:
    """停止单个服务，返回是否成功。"""
    stype = svc.get("type", "process")
    if stype == "docker":
        container = svc.get("container")
        compose_dir = svc.get("compose_dir")
        compose_svc = svc.get("compose_service")
        if compose_dir and compose_svc:
            r = subprocess.run(
                ["docker", "compose", "-p", "ops-daemon", "stop", compose_svc],
                cwd=compose_dir, capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        elif container:
            r = subprocess.run(
                ["docker", "stop", container],
                capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        return False
    else:
        kill_port(svc["port"])
        return True


# ── Public API ──────────────────────────────────────────────────────

def start() -> dict:
    """启动 tunnel + 所有 services，写 remote_state.json。"""
    cfg = _load_config()
    tunnel_cfg = cfg["tunnel"]
    services_cfg = cfg["services"]
    cf_exe = _resolve_exe(tunnel_cfg["cloudflared"]["exe"])
    tunnel_name = tunnel_cfg["name"]
    timeout = tunnel_cfg.get("startup_timeout", 30)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 杀掉残留 cloudflared
    pid_file = DATA_DIR / "cloudflared.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            kill_pid(old_pid)
            for _ in range(5):
                if not is_pid_alive(old_pid):
                    break
                time.sleep(1)
            else:
                hard_kill(old_pid)
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
    subprocess.run(["pkill", "-f", "cloudflared"], capture_output=True, timeout=5)
    time.sleep(2)

    # 2. 启动 cloudflared
    cf_log = DATA_DIR / "cloudflared.log"
    cf_args = [cf_exe, "tunnel",
               "--pidfile", str(pid_file),
               "--loglevel", tunnel_cfg["cloudflared"].get("log_level", "info"),
               "--logfile", str(cf_log)]
    if tunnel_cfg["cloudflared"].get("no_prechecks", False):
        cf_args.append("--no-prechecks")
    cf_args += ["run", tunnel_name]
    proc = subprocess.Popen(
        cf_args,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pid_file.write_text(str(proc.pid), encoding="utf-8")

    # 3. 启动所有 services
    svc_results = {}
    for svc_name, svc in services_cfg.items():
        svc_results[svc_name] = _start_service(svc_name, svc)

    # 4. 等待 tunnel 健康
    tunnel_ok = _wait_tunnel(cf_exe, tunnel_name, timeout)

    all_services_ok = all(svc_results.values())
    status = "running" if (tunnel_ok and all_services_ok) else "partial"

    state = {
        "status": status,
        "tunnel": tunnel_ok,
        "tunnel_name": tunnel_name,
        "services": svc_results,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def stop() -> dict:
    """停止 tunnel + 所有 services，写 remote_state.json。"""
    cfg = _load_config()
    services_cfg = cfg["services"]

    # 1. 先杀 cloudflared
    pid_file = DATA_DIR / "cloudflared.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            kill_pid(old_pid)
            for _ in range(5):
                if not is_pid_alive(old_pid):
                    break
                time.sleep(1)
            else:
                hard_kill(old_pid)
            _LOG.info("stop tunnel (PID file) pid=%d", old_pid)
        except Exception as e:
            _LOG.warning("stop tunnel (PID file) exception: %s", e)
        pid_file.unlink(missing_ok=True)
    subprocess.run(["pkill", "-f", "cloudflared"], capture_output=True, timeout=5)
    time.sleep(2)

    # 2. 再停所有 services
    for svc_name, svc in services_cfg.items():
        _stop_service(svc_name, svc)

    state = {"status": "stopped", "stopped_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _LOG.info("stop complete, state=%s", json.dumps(state))
    return state


def start_service(name: str) -> dict:
    """启动单个服务（已启动则跳过），返回状态。"""
    cfg = _load_config()
    services_cfg = cfg["services"]
    if name not in services_cfg:
        return {"status": "error", "detail": f"unknown service: {name}"}

    svc = services_cfg[name]
    ok = _start_service(name, svc)
    return {"service": name, "status": "ok" if ok else "error"}


def stop_service(name: str) -> dict:
    """停止单个服务，返回状态。"""
    cfg = _load_config()
    services_cfg = cfg["services"]
    if name not in services_cfg:
        return {"status": "error", "detail": f"unknown service: {name}"}

    svc = services_cfg[name]
    ok = _stop_service(name, svc)
    return {"service": name, "status": "stopped" if ok else "error"}


def status() -> dict:
    """读取 remote_state.json，合并各服务实际进程状态。"""
    cfg = _load_config()
    services_cfg = cfg["services"]

    listening_ports = get_listening_ports(host="127.0.0.1")
    svc_status = {}
    for svc_name, svc in services_cfg.items():
        stype = svc.get("type", "process")
        if stype == "docker":
            container = svc.get("container")
            if container:
                try:
                    r = subprocess.run(
                        ["docker", "ps", "-q", "--filter", f"name={container}"],
                        capture_output=True, text=True, timeout=5)
                    svc_status[svc_name] = "running" if r.stdout.strip() else "stopped"
                except Exception:
                    svc_status[svc_name] = "unknown"
            else:
                svc_status[svc_name] = "unknown"
        else:
            port = svc.get("port")
            if not port:
                svc_status[svc_name] = "unknown"
                continue
            svc_status[svc_name] = "running" if port in listening_ports else "stopped"

    state = {"status": "running", "services": svc_status}
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state["tunnel"] = saved.get("tunnel", False)
            state["started_at"] = saved.get("started_at")
            state["stopped_at"] = saved.get("stopped_at")
        except Exception:
            pass
    return state


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "start_service":
        if len(sys.argv) < 3:
            print(json.dumps({"status": "error", "detail": "usage: tunnel_manager start_service <name>"}))
            sys.exit(1)
        result = start_service(sys.argv[2])
    elif cmd == "stop_service":
        if len(sys.argv) < 3:
            print(json.dumps({"status": "error", "detail": "usage: tunnel_manager stop_service <name>"}))
            sys.exit(1)
        result = stop_service(sys.argv[2])
    else:
        result = {"start": start, "stop": stop, "status": status}[cmd]()

    print(json.dumps(result, indent=2, ensure_ascii=False))
    exit_code = 0
    if isinstance(result, dict):
        s = result.get("status")
        if s in ("running", "stopped", "ok"):
            exit_code = 0
        elif s == "error":
            exit_code = 1
    sys.exit(exit_code)
