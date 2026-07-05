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


def acquire_lock(lock_path: str, timeout: float = 10.0) -> bool:
    """Create lock directory atomically. Returns True if acquired within timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.mkdir(lock_path)
            return True
        except FileExistsError:
            time.sleep(0.05)
    return False


def release_lock(lock_path: str):
    try:
        os.rmdir(lock_path)
    except FileNotFoundError:
        pass
