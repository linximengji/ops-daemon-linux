"""Cross-platform process/port utilities — single authority for PID & port operations."""
import os
import signal
import socket
import time
import psutil


def get_pid_by_port(port: int) -> int | None:
    """Return PID listening on a TCP port, or None if not found."""
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port:
                return conn.pid
    except (psutil.AccessDenied, PermissionError):
        pass
    # Fallback: read /proc/net/tcp when psutil lacks permissions
    return _proc_net_tcp_pid(port)


def _proc_net_tcp_pid(port: int) -> int | None:
    """Fallback PID lookup via /proc/net/tcp + /proc/<pid>/fd/ socket inode matching."""
    hex_port = f"{port:04x}"
    inode_map: dict[str, int] = {}
    try:
        import pathlib
        for pd in pathlib.Path("/proc").iterdir():
            if not pd.name.isdigit():
                continue
            try:
                fd_dir = pd / "fd"
                if not fd_dir.is_dir():
                    continue
                for fd in fd_dir.iterdir():
                    try:
                        link = os.readlink(str(fd))
                        if link.startswith("socket:["):
                            inode = link[8:-1]
                            inode_map[inode] = int(pd.name)
                    except (OSError, ValueError):
                        pass
            except PermissionError:
                continue
    except (FileNotFoundError, PermissionError):
        return None
    if not inode_map:
        return None
    try:
        with open("/proc/net/tcp") as f:
            for line in f:
                cols = line.strip().split()
                if len(cols) < 10:
                    continue
                local_part = cols[1]
                if ":" not in local_part:
                    continue
                _, local_port = local_part.rsplit(":", 1)
                if local_port != hex_port:
                    continue
                state = cols[3]
                if state != "0A":  # TCP_LISTEN
                    continue
                inode = cols[9]
                return inode_map.get(inode)
    except (FileNotFoundError, OSError, IndexError):
        return None
    return None


def get_process_uptime(pid: int) -> int | None:
    """Return process uptime in seconds, or None on failure."""
    try:
        p = psutil.Process(pid)
        return int(time.time() - p.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError):
        return None


def get_listening_ports(host: str | None = None) -> set[int]:
    """Return set of all TCP listening ports. Optionally filter by laddr IP."""
    ports: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN:
                if host is None or conn.laddr.ip == host:
                    ports.add(conn.laddr.port)
    except (psutil.AccessDenied, PermissionError):
        pass
    return ports


def is_pid_alive(pid: int) -> bool:
    """Check if a PID is running by sending signal 0."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def kill_pid(pid: int) -> bool:
    """Send SIGTERM to a PID; returns True if successful, False if already dead."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def hard_kill(pid: int) -> bool:
    """Send SIGKILL to a PID."""
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_port(port: int, exclude_self: bool = True) -> list[int]:
    """Kill all processes LISTENING on a TCP port.
    Returns list of killed PIDs. Does NOT kill established connections."""
    killed = []
    pid = get_pid_by_port(port)
    if pid is None:
        return killed
    if exclude_self and pid == os.getpid():
        return killed
    if kill_pid(pid):
        killed.append(pid)
    # Wait for graceful shutdown (up to 3s)
    for _ in range(6):
        if not is_pid_alive(pid):
            return killed
        time.sleep(0.5)
    # Force kill if still alive
    hard_kill(pid)
    wait_port_free(port, timeout=10)
    return killed


def wait_port_free(port: int, host: str = "127.0.0.1", timeout: float = 20) -> bool:
    """Poll until the port accepts no more connections. Returns True if freed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            time.sleep(0.5)
        except (ConnectionRefusedError, TimeoutError, OSError):
            return True
    return False


def is_pid_alive_from_pidfile(path: str) -> bool:
    """Read PID file and check if the process is alive."""
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        return is_pid_alive(pid)
    except (FileNotFoundError, ValueError, OSError):
        return False
