"""Spawn/kill proxy and claudetalk processes with targeted port control."""
import os, sys, subprocess, time, re, threading, json
import socket
import psutil
from datetime import datetime, timezone

from ops_daemon._proc import (
    kill_port, is_pid_alive_from_pidfile, is_pid_alive,
    kill_pid, hard_kill, wait_port_free,
)

NODE = "node"

PROXY_SCRIPT = os.path.join(os.path.expanduser("~"), "projects", "proxy", "model_proxy.py")
BACKUP_SCRIPT = os.path.join(os.path.expanduser("~"), "projects", "proxy", "proxy_backup.py")
PROXY_LOG = os.path.join(os.path.expanduser("~"), "projects", "proxy", "proxy.log")
BACKUP_LOG = os.path.join(os.path.expanduser("~"), "projects", "proxy", "proxy_backup.log")

CLAUDETALK_DIR = os.path.join(os.path.expanduser("~"), "projects", "claudetalk")
CLAUDETALK_WORKDIR = os.path.join(os.path.expanduser("~"), "projects")
CLAUDETALK_CLI = os.path.join(CLAUDETALK_DIR, "dist", "cli.js")
MCP_SERVER_CLI = os.path.join(CLAUDETALK_DIR, "dist", "mcp-standalone.js")
CLAUDETALK_LOG = os.path.join(os.path.expanduser("~"), "projects", ".claudetalk", "claudetalk.log")
FEISHU_BRIDGE_CLI = os.path.join(CLAUDETALK_DIR, "dist", "feishu-bridge.js")
FEISHU_BRIDGE_PID_FILE = os.path.join(os.path.expanduser("~"), "projects", ".claudetalk", "feishu-bridge.pid")

PROXY_PID_FILE = os.path.join(os.path.expanduser("~"), ".claude", "watchdog_proxy.pid")
BACKUP_PID_FILE = os.path.join(os.path.expanduser("~"), ".claude", "watchdog_backup.pid")
CLAUDETALK_PID_FILE = os.path.join(os.path.expanduser("~"), "projects", ".claudetalk", "claudetalk-default.pid")
CLAUDETALK_STOP_FLAG = os.path.join(os.path.expanduser("~"), "projects", ".claudetalk", ".stop-intent")


def _wait_port_free(port: int, host: str = "127.0.0.1", timeout: float = 20) -> bool:
    """Poll until the port accepts no more connections (process fully dead + OS released)."""
    return wait_port_free(port, host, timeout)


def _kill_pid(pid_str: str, expected_name=None):
    """Kill a PID safely. If expected_name given, verify process name first."""
    pid = int(pid_str)
    if expected_name:
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            if not re.search(rf'\b{re.escape(expected_name)}\b', name, re.IGNORECASE):
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        return
    except (PermissionError, OSError):
        hard_kill(pid)


FEISHU_BRIDGE_HTTP_QUIT = "http://127.0.0.1:9878/quit"


def _hard_kill(pid: int):
    """Try graceful HTTP shutdown first, then SIGKILL."""
    try:
        import urllib.request
        req = urllib.request.Request(FEISHU_BRIDGE_HTTP_QUIT, method='POST', data=b'{}')
        urllib.request.urlopen(req, timeout=3)
        for _ in range(10):
            time.sleep(0.5)
            if not is_pid_alive(pid):
                return
    except Exception:
        pass
    hard_kill(pid)


def _is_pid_alive(path: str) -> bool:
    """Check if the process in the PID file is still running."""
    return is_pid_alive_from_pidfile(path)


def _kill_by_pid_file(path: str, expected_name: str | None = None):
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        _kill_pid(str(pid), expected_name=expected_name)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        pass


def _rotate_log(log_path: str, keep: int = 5):
    """Rotate proxy.log to a timestamped archive, keeping last N generations.

    Strategy:
    - Rename the active log to proxy.{YYYYMMDD-HHMMSS}.log before spawning.
      The dying process keeps its file handle and continues writing to the renamed
      archive — its remaining buffered output goes there, never into the new file.
    - The new process opens a fresh proxy.log — zero interleaving.
    - Prunes the oldest archives beyond `keep`, so /var/log doesn't fill.
    """
    if not os.path.isfile(log_path):
        return
    try:
        size = os.path.getsize(log_path)
        if size < 100:
            return
    except OSError:
        return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rotated = f"{log_path}.{ts}"
    try:
        os.rename(log_path, rotated)
    except OSError:
        return

    base_dir = os.path.dirname(log_path) or "."
    base_name = os.path.basename(log_path)
    pattern = re.compile(re.escape(base_name) + r"\.\d{8}-\d{6}$")
    candidates = sorted(
        (os.path.join(base_dir, f) for f in os.listdir(base_dir) if pattern.match(f)),
        key=os.path.getmtime,
        reverse=True,
    )
    for old in candidates[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass


def spawn_proxy(port: int = 4000, kill_first: bool = False):
    """Start a single proxy instance on given port.
    Args:
        port: 4000 for main (model_proxy.py), 4002 for backup (proxy_backup.py).
        kill_first: If False, skip restart when existing process is healthy.
    """
    if port == 4000 and not kill_first and _is_pid_alive(PROXY_PID_FILE):
        return
    if port == 4002 and not kill_first and _is_pid_alive(BACKUP_PID_FILE):
        return
    kill_port(port)

    py = sys.executable
    if port == 4000:
        script = PROXY_SCRIPT
        log_path = PROXY_LOG
    else:
        script = BACKUP_SCRIPT
        log_path = BACKUP_LOG

    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    _rotate_log(log_path)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n--- spawning {script} on {port} ---\n")
    proc = subprocess.Popen(
        [py, script, str(port), "-v"],
        stdout=open(log_path, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )

    if port == 4000:
        with open(PROXY_PID_FILE, "w") as f:
            f.write(str(proc.pid))
    elif port == 4002:
        with open(BACKUP_PID_FILE, "w") as f:
            f.write(str(proc.pid))
    return proc


def stop_proxy(port: int = 4000):
    kill_port(port)


def _with_tools_in_path() -> dict[str, str]:
    """Return a copy of environ with PATH augmented for node-based tool resolution.
    On Linux, node and npx are normally in PATH already; this is a best-effort
    augmentation for non-standard installs."""
    env = os.environ.copy()
    extra = []
    node_bin = os.path.join(CLAUDETALK_DIR, "node_modules", ".bin")
    if os.path.isdir(node_bin):
        extra.append(node_bin)
    if extra:
        env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "")
    return env


def spawn_claudetalk(kill_first: bool = False):
    """Start claudetalk — returns proc on success, None if spawn failed."""
    if not kill_first and _is_pid_alive(CLAUDETALK_PID_FILE):
        return

    stop_claudetalk()
    os.makedirs(os.path.dirname(CLAUDETALK_LOG), exist_ok=True)

    for attempt in range(3):
        proc = subprocess.Popen(
            [NODE, "--unhandled-rejections=warn", CLAUDETALK_CLI, "--profile", "default"],
            cwd=CLAUDETALK_WORKDIR,
            env=_with_tools_in_path(),
            stdout=open(CLAUDETALK_LOG, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        with open(CLAUDETALK_PID_FILE, "w") as f:
            f.write(str(proc.pid))
        clear_crash_marker()

        alive = True
        for _ in range(5):
            time.sleep(0.2)
            if not _is_pid_alive(CLAUDETALK_PID_FILE):
                alive = False
                break
        if alive:
            break
        print(f"[spawn_claudetalk] attempt {attempt + 1} died quickly, retrying...", flush=True)
    else:
        print("[spawn_claudetalk] All 3 attempts failed", flush=True)
        return None

    def _watch_exit(pid=proc.pid):
        proc.wait()
        if proc.returncode != 0:
            if os.path.exists(CLAUDETALK_STOP_FLAG):
                try:
                    os.remove(CLAUDETALK_STOP_FLAG)
                except OSError:
                    pass
                return
            try:
                with open(CLAUDETALK_PID_FILE) as f:
                    current_pid = int(f.read().strip())
                if current_pid != pid:
                    return
            except (FileNotFoundError, ValueError):
                pass
            write_crash_marker()
            write_crash_detail(pid, proc.returncode)
    threading.Thread(target=_watch_exit, daemon=True).start()
    return proc


CRASH_MARKER = os.path.join(os.path.dirname(CLAUDETALK_PID_FILE), "crash.marker")
CRASH_DETAIL_FILE = os.path.join(os.path.dirname(CLAUDETALK_PID_FILE), "last-crash-detail.json")


def _tail_log(path, n=20):
    """Read last n lines from a file efficiently."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf = bytearray()
            lines = []
            pos = size
            while pos > 0 and len(lines) < n:
                chunk_size = min(4096, pos)
                pos -= chunk_size
                f.seek(pos)
                chunk = f.read(chunk_size)
                buf = bytearray(chunk) + buf
                lines = buf.decode("utf-8", errors="replace").splitlines()
                if pos == 0:
                    break
            return lines[-n:]
    except (FileNotFoundError, OSError):
        return []


def write_crash_marker():
    try:
        with open(CRASH_MARKER, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def write_crash_detail(pid, exit_code):
    """Write detailed crash info alongside the marker."""
    try:
        last_log = _tail_log(CLAUDETALK_LOG, 20)
        detail = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": pid,
            "exit_code": exit_code,
            "exit_code_hex": f"0x{exit_code & 0xFFFFFFFF:08X}" if exit_code < 0 else hex(exit_code),
            "last_log_lines": last_log,
        }
        with open(CRASH_DETAIL_FILE, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def has_crash_marker(max_age_s: float = 30) -> bool:
    try:
        if os.path.exists(CRASH_MARKER):
            age = time.time() - float(open(CRASH_MARKER).read().strip())
            return age < max_age_s
    except (OSError, ValueError):
        pass
    return False


def _write_stop_flag():
    """Write stop-intent flag so _watch_exit skips crash marker on intentional kill."""
    try:
        with open(CLAUDETALK_STOP_FLAG, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _kill_claudetalk_orphans():
    """Kill all node processes running claudetalk cli.js except the PID file owner."""
    known_pid = None
    try:
        with open(CLAUDETALK_PID_FILE) as f:
            known_pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass
    try:
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                if "cli.js" not in cmd:
                    continue
                pid = p.info["pid"]
                if pid == known_pid or pid == os.getpid():
                    continue
                hard_kill(pid)
                print(f"[stop_claudetalk] killed orphan claudetalk PID {pid}", flush=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
    except ImportError:
        pass
    except Exception as exc:
        print(f"[stop_claudetalk] orphan cleanup error: {exc}", flush=True)


def stop_claudetalk():
    """Stop claudetalk. Kills orphan instances first (cmdline match), then the
    PID-file owner."""
    _write_stop_flag()
    _kill_claudetalk_orphans()
    clear_crash_marker()
    old_pid = None
    try:
        with open(CLAUDETALK_PID_FILE) as f:
            old_pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass
    if old_pid:
        kill_pid(old_pid)
        for _ in range(15):
            if not is_pid_alive(old_pid):
                break
            time.sleep(1)
        else:
            hard_kill(old_pid)


def clear_crash_marker():
    try:
        if os.path.exists(CRASH_MARKER):
            os.remove(CRASH_MARKER)
        if os.path.exists(CLAUDETALK_STOP_FLAG):
            os.remove(CLAUDETALK_STOP_FLAG)
    except OSError:
        pass


MCP_PID_FILE = os.path.join(os.path.expanduser("~"), "projects", ".claudetalk", "mcp-server.pid")


def spawn_mcp_server(kill_first: bool = False):
    """Start standalone MCP server on port 9877 — skips kill+restart if already running."""
    if not kill_first and _is_pid_alive(MCP_PID_FILE):
        return
    kill_port(9877)
    os.makedirs(os.path.dirname(CLAUDETALK_LOG), exist_ok=True)

    proc = subprocess.Popen(
        [NODE, MCP_SERVER_CLI, "--work-dir", CLAUDETALK_WORKDIR],
        cwd=CLAUDETALK_WORKDIR,
        stdout=open(CLAUDETALK_LOG, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    with open(MCP_PID_FILE, "w") as f:
        f.write(str(proc.pid))


def stop_mcp_server():
    kill_port(9877)


FEISHU_BRIDGE_PORT = 9878


def spawn_feishu_bridge(kill_first: bool = False):
    """Start feishu-bridge — returns proc on success, None if spawn failed."""
    if not kill_first and _is_pid_alive(FEISHU_BRIDGE_PID_FILE):
        return
    kill_port(FEISHU_BRIDGE_PORT)
    os.makedirs(os.path.dirname(CLAUDETALK_LOG), exist_ok=True)
    env = os.environ.copy()
    env["FEISHU_BRIDGE_WORK_DIR"] = CLAUDETALK_WORKDIR

    sock_check = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock_check.connect(("127.0.0.1", FEISHU_BRIDGE_PORT))
        sock_check.close()
        print("[spawn_feishu_bridge] Port occupied, signaling old bridge via PID file", flush=True)
        with open(FEISHU_BRIDGE_PID_FILE, "w") as f:
            f.write("0")
        for _ in range(60):
            time.sleep(0.5)
            try:
                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s2.connect(("127.0.0.1", FEISHU_BRIDGE_PORT))
                s2.close()
            except (ConnectionRefusedError, OSError):
                break
        else:
            print("[spawn_feishu_bridge] Old bridge did not exit within 30s", flush=True)
    except (ConnectionRefusedError, OSError):
        pass

    for attempt in range(3):
        proc = subprocess.Popen(
            [NODE, FEISHU_BRIDGE_CLI],
            cwd=CLAUDETALK_WORKDIR,
            env=env,
            stdout=open(CLAUDETALK_LOG, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        with open(FEISHU_BRIDGE_PID_FILE, "w") as f:
            f.write(str(proc.pid))

        alive = True
        for _ in range(5):
            time.sleep(0.2)
            if not _is_pid_alive(FEISHU_BRIDGE_PID_FILE):
                alive = False
                break
        if alive:
            break
        print(f"[spawn_feishu_bridge] attempt {attempt + 1} died quickly, retrying...", flush=True)
    else:
        print("[spawn_feishu_bridge] All 3 attempts failed", flush=True)
        return None
    return proc


def stop_feishu_bridge():
    kill_port(FEISHU_BRIDGE_PORT)


def spawn_cloudflared():
    """Start cloudflared tunnel via tunnel_manager."""
    from ops_daemon.tunnel_manager import start
    return start()


def stop_cloudflared():
    """Stop cloudflared tunnel via tunnel_manager."""
    from ops_daemon.tunnel_manager import stop
    return stop()


def spawn_daemon():
    """Start a new ops-daemon instance."""
    py = sys.executable
    subprocess.Popen([py, "-m", "ops_daemon.main"])
