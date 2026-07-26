"""Cross-process file locking via atomic directory creation (Windows-compatible).

Usage:
    if not acquire_lock(lock_path, timeout=10):
        return  # could not acquire
    try:
        # critical section
    finally:
        release_lock(lock_path)
"""
import os
import time


def _pid_path(lock_path: str) -> str:
    return os.path.join(lock_path, "pid")


def _write_owner(lock_path: str):
    try:
        with open(_pid_path(lock_path), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _remove_stale(lock_path: str):
    for name in os.listdir(lock_path):
        try:
            os.unlink(os.path.join(lock_path, name))
        except FileNotFoundError:
            pass
    try:
        os.rmdir(lock_path)
    except OSError:
        pass


def acquire_lock(lock_path: str, timeout: float = 10.0) -> bool:
    """Create lock directory atomically. Returns True if acquired within timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.mkdir(lock_path)
            _write_owner(lock_path)
            return True
        except FileExistsError:
            try:
                with open(_pid_path(lock_path), encoding="utf-8") as f:
                    holder = int(f.read().strip())
                try:
                    os.kill(holder, 0)
                except (ProcessLookupError, ValueError):
                    _remove_stale(lock_path)
                    continue
                except PermissionError:
                    pass
            except FileNotFoundError:
                pass
            time.sleep(0.05)
    return False


def release_lock(lock_path: str):
    try:
        with open(_pid_path(lock_path), encoding="utf-8") as f:
            holder = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        holder = os.getpid()
    if holder != os.getpid():
        return
    _remove_stale(lock_path)
