"""Tests for ops_daemon.file_lock — cross-process lock utility."""
import os
import threading


from ops_daemon.file_lock import acquire_lock, release_lock


class TestFileLock:

    def test_acquire_and_release(self, tmp_path):
        lock_path = os.path.join(str(tmp_path), ".test.lock")
        assert acquire_lock(lock_path)
        assert os.path.isdir(lock_path)
        release_lock(lock_path)
        assert not os.path.exists(lock_path)

    def test_contention_blocks(self, tmp_path):
        """Second acquire on held lock should fail fast."""
        lock_path = os.path.join(str(tmp_path), ".test.lock")
        assert acquire_lock(lock_path, timeout=5)
        assert not acquire_lock(lock_path, timeout=1), "should not acquire held lock"
        release_lock(lock_path)

    def test_release_nonexistent(self, tmp_path):
        """release_lock on missing lock dir should not raise."""
        lock_path = os.path.join(str(tmp_path), ".nonexistent.lock")
        release_lock(lock_path)  # no exception

    def test_double_release(self, tmp_path):
        lock_path = os.path.join(str(tmp_path), ".test.lock")
        assert acquire_lock(lock_path)
        release_lock(lock_path)
        release_lock(lock_path)  # second release should be a no-op

    def test_serializes_concurrent_access(self, tmp_path):
        """Two threads using lock should not corrupt the file."""
        data_file = os.path.join(str(tmp_path), "data.json")
        lock_path = os.path.join(str(tmp_path), ".data.lock")
        # Seed the file
        import json
        with open(data_file, "w") as f:
            json.dump({"counter": 0}, f)

        errors = []

        def worker(delta):
            for _ in range(20):
                if not acquire_lock(lock_path, timeout=10):
                    errors.append("lock timeout")
                    return
                try:
                    with open(data_file, "r") as f:
                        d = json.load(f)
                    d["counter"] += delta
                    with open(data_file, "w") as f:
                        json.dump(d, f)
                finally:
                    release_lock(lock_path)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(-1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"lock acquisition errors: {errors}"
        with open(data_file, "r") as f:
            result = json.load(f)
        # net effect: 20 * (+1) + 20 * (-1) = 0
        assert result["counter"] == 0, f"expected 0, got {result['counter']}"
