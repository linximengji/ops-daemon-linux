"""Tests for ops_daemon.repair — covers all four quadrants across every repair function."""
import asyncio
import signal
import subprocess
from unittest.mock import patch, AsyncMock, MagicMock, call, mock_open

import pytest

from ops_daemon.repair import (
    _check_port,
    register_repair,
    REPAIR_REGISTRY,
    repair_claudetalk,
    repair_proxy,
    repair_proxy_backup,
    repair_mcp,
)
from ops_daemon.process_manager import CLAUDETALK_PID_FILE, CRASH_MARKER


# ── helpers ──────────────────────────────────────────────────────────────────

class FakeWriter:
    async def wait_closed(self):
        pass

    def close(self):
        pass


# ══════════════════════════════════════════════════════════════════════════════
# _check_port
# ══════════════════════════════════════════════════════════════════════════════


class TestCheckPort:

    @pytest.mark.asyncio
    async def test_happy_path_port_open(self):
        with patch("ops_daemon.repair.asyncio.open_connection",
                   AsyncMock(return_value=(AsyncMock(), FakeWriter()))):
            result = await _check_port("127.0.0.1", 4000)
            assert result is True

    @pytest.mark.asyncio
    async def test_happy_path_connection_refused(self):
        with patch("ops_daemon.repair.asyncio.open_connection",
                   AsyncMock(side_effect=ConnectionRefusedError)):
            result = await _check_port("127.0.0.1", 4000)
            assert result is False

    @pytest.mark.asyncio
    async def test_happy_path_os_error(self):
        with patch("ops_daemon.repair.asyncio.open_connection",
                   AsyncMock(side_effect=OSError)):
            result = await _check_port("127.0.0.1", 4000)
            assert result is False

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        with patch("ops_daemon.repair.asyncio.open_connection",
                   AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await _check_port("127.0.0.1", 4000)
            assert result is False

    @pytest.mark.asyncio
    async def test_boundary_zero_timeout(self):
        with patch("ops_daemon.repair.asyncio.open_connection",
                   AsyncMock(side_effect=ConnectionRefusedError)):
            result = await _check_port(timeout=0)
            assert result is False

    @pytest.mark.asyncio
    async def test_boundary_default_params(self):
        with patch("ops_daemon.repair.asyncio.open_connection",
                   AsyncMock(return_value=(AsyncMock(), FakeWriter()))) as mock_conn:
            await _check_port()
            mock_conn.assert_called_once_with("127.0.0.1", 4000)


# ══════════════════════════════════════════════════════════════════════════════
# register_repair
# ══════════════════════════════════════════════════════════════════════════════


class TestRegisterRepair:

    def test_happy_path_registers_function(self):
        old = dict(REPAIR_REGISTRY)
        REPAIR_REGISTRY.clear()

        @register_repair("test_repair")
        async def dummy():
            return {"status": "ok"}

        assert "test_repair" in REPAIR_REGISTRY
        assert REPAIR_REGISTRY["test_repair"] is dummy

        REPAIR_REGISTRY.clear()
        REPAIR_REGISTRY.update(old)


# ══════════════════════════════════════════════════════════════════════════════
# repair_claudetalk
# ══════════════════════════════════════════════════════════════════════════════


def _mock_pid_alive(*alive_pids, dead_after=0):
    """Create a is_pid_alive mock.
    PIDs in alive_pids are considered alive (return True).
    After dead_after calls, all PIDs become dead.
    PID 0 always returns False (invalid PID sentinel).
    """
    call_count = [0]

    def side(pid):
        call_count[0] += 1
        if pid <= 0:
            return False
        if dead_after > 0 and call_count[0] > dead_after:
            return False
        return pid in alive_pids
    return side


def _mock_kill_pid(*killable_pids):
    """Create a kill_pid mock. PIDs in killable_pids are successfully killed."""
    def side(pid):
        return pid in killable_pids
    return side


class TestRepairClaudetalk:

    # ── Phase-aware PID file helpers (mock subprocess.run returns Linux ps format) ──

    @staticmethod
    def _open_two_phase(old_pid, new_pid):
        """Phase 1 → old_pid; Phase 2 (after spawn) → new_pid."""
        call_count = [0]

        def _side(path, *a, **kw):
            if path == CLAUDETALK_PID_FILE or path == CRASH_MARKER:
                call_count[0] += 1
                pid = str(old_pid) if call_count[0] == 1 else str(new_pid)
                return mock_open(read_data=pid).return_value
            return open(path, *a, **kw)

        return _side

    @staticmethod
    def _open_phase1_skip(new_pid):
        """Phase 1 raises FileNotFoundError; Phase 2 returns new_pid."""
        call_count = [0]

        def _side(path, *a, **kw):
            if path == CLAUDETALK_PID_FILE or path == CRASH_MARKER:
                call_count[0] += 1
                if call_count[0] == 1:
                    raise FileNotFoundError
                return mock_open(read_data=str(new_pid)).return_value
            return open(path, *a, **kw)

        return _side

    @staticmethod
    def _mock_subprocess_run(alive_pid=None, dead_after=0):
        """Mock subprocess.run for ps calls.
        Phase 1 (old pid) → returncode=1 (dead immediately, break loop).
        Phase 2 (new/alive pid) → returncode=0 after dead_after iterations.
        If alive_pid is None, phase1 gets returncode=1, phase2 gets returncode=0.
        """
        call_count = [0]
        pid_count = [0]

        def _run(cmd, **kw):
            proc = MagicMock()
            proc.returncode = 0
            # Phase 1: checking old PID — should be dead
            if "--phase1" in str(cmd):
                proc.returncode = 1
            elif alive_pid is None:
                # Default: phase1 dead, phase2 alive
                proc.stdout = ""
                proc.returncode = 1
                pid_count[0] += 1
                if pid_count[0] > 1:
                    proc.returncode = 0
                    proc.stdout = "54321\n"
            else:
                call_count[0] += 1
                if dead_after > 0 and call_count[0] > dead_after:
                    proc.returncode = 1
                else:
                    proc.stdout = f"{alive_pid}\n"
            return proc
        return _run

    # ── Happy path ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_full_repair_on_first_check(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(54321, dead_after=5)):
                with patch("ops_daemon.process_manager.is_pid_alive_from_pidfile",
                           side_effect=_mock_pid_alive(54321, dead_after=5)):
                    with patch("ops_daemon.process_manager.kill_pid",
                               side_effect=_mock_kill_pid(12345)):
                        with patch("ops_daemon.repair.spawn_claudetalk") as mock_spawn:
                            with patch("builtins.open", self._open_two_phase(12345, 54321)):
                                with patch("ops_daemon.repair.subprocess.run",
                                           side_effect=self._mock_subprocess_run()):
                                    result = await repair_claudetalk()

            assert result == {"status": "restored", "restart_time_s": 2}
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_pid_file_skip_phase1(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(99999)):
                with patch("ops_daemon.process_manager.is_pid_alive_from_pidfile",
                           side_effect=_mock_pid_alive(99999)):
                    with patch("ops_daemon.repair.spawn_claudetalk") as mock_spawn:
                        with patch("builtins.open", self._open_phase1_skip(99999)):
                            with patch("ops_daemon.repair.subprocess.run",
                                       side_effect=self._mock_subprocess_run()):
                                result = await repair_claudetalk()

            assert result == {"status": "restored", "restart_time_s": 4}
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_pid_file_skip_phase1(self):
        call_count = [0]

        def open_side(path, *a, **kw):
            if path == CLAUDETALK_PID_FILE or path == CRASH_MARKER:
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock_open(read_data="not-a-number\n").return_value
                return mock_open(read_data="99999").return_value
            return open(path, *a, **kw)

        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(99999)):
                with patch("ops_daemon.repair.spawn_claudetalk") as mock_spawn:
                    with patch("builtins.open", side_effect=open_side):
                        with patch("ops_daemon.repair.subprocess.run",
                                   side_effect=self._mock_subprocess_run()):
                            result = await repair_claudetalk()

            assert result == {"status": "restored", "restart_time_s": 4}
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_lookup_error_on_kill(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(99999)):
                with patch("ops_daemon.repair.spawn_claudetalk") as mock_spawn:
                    with patch("builtins.open", self._open_two_phase(12345, 99999)):
                        with patch("ops_daemon.repair.subprocess.run",
                                   side_effect=self._mock_subprocess_run()):
                            result = await repair_claudetalk()

            assert result == {"status": "restored", "restart_time_s": 2}
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_oserror_on_kill_caught(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(99999)):
                with patch("ops_daemon.repair.spawn_claudetalk") as mock_spawn:
                    with patch("builtins.open", self._open_two_phase(12345, 99999)):
                        with patch("ops_daemon.repair.subprocess.run",
                                   side_effect=self._mock_subprocess_run()):
                            result = await repair_claudetalk()

            assert result == {"status": "restored", "restart_time_s": 2}
            mock_spawn.assert_called_once()

    # ── Boundary ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_phase1_multiple_iterations_until_death(self):
        phase1_count = [0]
        phase2_count = [0]

        def _run_until_dead(cmd, **kw):
            proc = MagicMock()
            proc.stdout = ""
            # Phase 1: old PID 12345
            if "12345" in str(cmd):
                phase1_count[0] += 1
                proc.returncode = 0 if phase1_count[0] <= 3 else 1
            else:
                # Phase 2: new PID 99999 passes immediately
                proc.returncode = 0
            return proc

        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(12345, 99999, dead_after=4)):
                with patch("ops_daemon.repair.spawn_claudetalk"):
                    with patch("builtins.open", self._open_two_phase(12345, 99999)):
                        with patch("ops_daemon.repair.subprocess.run",
                                   side_effect=_run_until_dead):
                            result = await repair_claudetalk()

            assert result["status"] == "restored"

    @pytest.mark.asyncio
    async def test_new_process_takes_multiple_iterations(self):
        phase2_count = [0]

        def _run(cmd, **kw):
            proc = MagicMock()
            proc.stdout = ""
            # Phase 1: old PID 12345 — process dies immediately
            if "12345" in str(cmd):
                proc.returncode = 1
            else:
                # Phase 2: new PID takes 3 iterations to verify
                phase2_count[0] += 1
                if phase2_count[0] >= 3:
                    proc.returncode = 0
                    proc.stdout = "54321\n"
                else:
                    proc.returncode = 1
            return proc

        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(12345, dead_after=3)):
                with patch("ops_daemon.process_manager.kill_pid",
                           side_effect=_mock_kill_pid(12345)):
                    with patch("ops_daemon.repair.spawn_claudetalk"):
                        with patch("builtins.open", self._open_two_phase(12345, 99999)):
                            with patch("ops_daemon.repair.subprocess.run",
                                       side_effect=_run):
                                result = await repair_claudetalk()

            assert result == {"status": "restored", "restart_time_s": 6}

    @pytest.mark.asyncio
    async def test_phase1_skipped_when_no_pid(self):
        with patch("ops_daemon.repair.asyncio.sleep") as mock_sleep:
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(99999)):
                with patch("ops_daemon.repair.spawn_claudetalk"):
                    with patch("builtins.open", self._open_phase1_skip(99999)):
                        await repair_claudetalk()

            assert call(2) in mock_sleep.call_args_list
            assert call(1) not in mock_sleep.call_args_list

    # ── Exception ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_new_process_never_verified(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(12345, dead_after=3)):
                with patch("ops_daemon.process_manager.kill_pid",
                           side_effect=_mock_kill_pid(12345)):
                    with patch("ops_daemon.repair.spawn_claudetalk"):
                        with patch("builtins.open", self._open_two_phase(12345, 99999)):
                            def _run(cmd, **kw):
                                proc = MagicMock()
                                proc.returncode = 1
                                proc.stdout = ""
                                return proc
                            with patch("ops_daemon.repair.subprocess.run",
                                       side_effect=_run):
                                result = await repair_claudetalk()

            assert result == {"status": "failed",
                              "error": "claudetalk not restored after 30s"}

    @pytest.mark.asyncio
    async def test_pid_file_disappears_during_verification(self):
        call_count = [0]
        verify_count = [0]

        def open_flaky(path, *a, **kw):
            if path == CLAUDETALK_PID_FILE or path == CRASH_MARKER:
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock_open(read_data="12345").return_value
                if call_count[0] == 2:
                    return mock_open(read_data="99999").return_value
                if call_count[0] in (3, 4):
                    raise FileNotFoundError
                return mock_open(read_data="99999").return_value
            return open(path, *a, **kw)

        def _run(cmd, **kw):
            proc = MagicMock()
            proc.returncode = 0
            if "12345" in str(cmd):
                proc.stdout = ""
                return proc
            verify_count[0] += 1
            if verify_count[0] <= 3:
                proc.stdout = ""
            else:
                proc.stdout = "54321\n"
            return proc

        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(12345, dead_after=3)):
                with patch("ops_daemon.process_manager.kill_pid",
                           side_effect=_mock_kill_pid(12345)):
                    with patch("ops_daemon.repair.spawn_claudetalk"):
                        with patch("builtins.open", side_effect=open_flaky):
                            with patch("ops_daemon.repair.subprocess.run",
                                       side_effect=_run):
                                result = await repair_claudetalk()

            assert result["status"] == "restored"

    @pytest.mark.asyncio
    async def test_subprocess_timeout_propagates(self):
        """TimeoutExpired not caught by except (FileNotFoundError, ValueError)."""
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(12345, dead_after=3)):
                with patch("ops_daemon.process_manager.kill_pid",
                           side_effect=_mock_kill_pid(12345)):
                    with patch("ops_daemon.repair.spawn_claudetalk"):
                        with patch("builtins.open", self._open_two_phase(12345, 99999)):
                            with patch("ops_daemon.repair.subprocess.run",
                                       side_effect=subprocess.TimeoutExpired(cmd="x", timeout=3)):
                                with pytest.raises(subprocess.TimeoutExpired):
                                    await repair_claudetalk()

    # ── Timing ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_sleep_intervals_1s_phase1_2s_phase2(self):
        phase1_count = [0]

        with patch("ops_daemon.repair.asyncio.sleep") as mock_sleep:
            with patch("ops_daemon.process_manager.is_pid_alive",
                       side_effect=_mock_pid_alive(12345, 99999, dead_after=3)):
                with patch("ops_daemon.process_manager.kill_pid",
                           side_effect=_mock_kill_pid(12345)):
                    with patch("ops_daemon.repair.spawn_claudetalk"):
                        with patch("builtins.open", self._open_two_phase(12345, 99999)):
                            await repair_claudetalk()

            assert call(1) in mock_sleep.call_args_list
            assert call(2) in mock_sleep.call_args_list


# ══════════════════════════════════════════════════════════════════════════════
# repair_proxy
# ══════════════════════════════════════════════════════════════════════════════


class TestRepairProxy:

    @pytest.mark.asyncio
    async def test_main_comes_up_immediately(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy") as mock_spawn:
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [True, True]
                    result = await repair_proxy()

            assert result == {"status": "restored", "restart_time_s": 2}
            mock_spawn.assert_called_once_with(4000, kill_first=True)

    @pytest.mark.asyncio
    async def test_both_need_repair(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy") as mock_spawn:
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [True, False, True]
                    result = await repair_proxy()

            assert result == {"status": "restored", "restart_time_s": 2}
            mock_spawn.assert_any_call(4000, kill_first=True)
            mock_spawn.assert_any_call(4002, kill_first=True)

    @pytest.mark.asyncio
    async def test_main_fails_returns_failed(self):
        """Both 4000 and 4002 down → returns failed (degrade unavailable)."""
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [False] * 34
                    result = await repair_proxy()

            assert result == {"status": "failed",
                              "error": "proxy not responding after 30s"}

    @pytest.mark.asyncio
    async def test_main_fails_degrade_to_backup(self):
        """4000 fails but 4002 healthy → degrade to backup proxy."""
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [False] * 15 + [True] * 4
                    with patch("ops_daemon.proxy_manager._set_proxy_url") as mock_set:
                        result = await repair_proxy()

            assert result["status"] == "degraded"
            assert result["active"] == "backup"

    @pytest.mark.asyncio
    async def test_main_fails_only_backport_available(self):
        """4000 fails, 4002 down then comes up after spawn_proxy(4002)."""
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy") as mock_spawn:
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [False] * 16 + [True] * 4
                    result = await repair_proxy()

            assert result["status"] == "degraded"
            assert result["active"] == "backup"
            mock_spawn.assert_any_call(4000, kill_first=True)
            mock_spawn.assert_any_call(4002, kill_first=True)

    @pytest.mark.asyncio
    async def test_main_fails_both_down_returns_failed(self):
        """4000 and 4002 both down → still returns failed."""
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [False] * 34
                    result = await repair_proxy()

            assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_main_takes_3_iterations(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [False, False, True, True]
                    result = await repair_proxy()

            assert result == {"status": "restored", "restart_time_s": 6}


# ══════════════════════════════════════════════════════════════════════════════
# repair_proxy_backup
# ══════════════════════════════════════════════════════════════════════════════


class TestRepairProxyBackup:

    @pytest.mark.asyncio
    async def test_backup_comes_up_immediately(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy") as mock_spawn:
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.return_value = True
                    result = await repair_proxy_backup()

            assert result == {"status": "restored", "restart_time_s": 2}
            mock_spawn.assert_called_once_with(4002, kill_first=True)
            mock_port.assert_called_once_with("127.0.0.1", 4002)

    @pytest.mark.asyncio
    async def test_backup_never_starts(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.return_value = False
                    result = await repair_proxy_backup()

            assert result == {"status": "failed",
                              "error": "proxy backup not responding after 30s"}

    @pytest.mark.asyncio
    async def test_backup_takes_3_iterations(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_proxy"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [False, False, True]
                    result = await repair_proxy_backup()

            assert result == {"status": "restored", "restart_time_s": 6}


# ══════════════════════════════════════════════════════════════════════════════
# repair_mcp
# ══════════════════════════════════════════════════════════════════════════════


class TestRepairMcp:

    @pytest.mark.asyncio
    async def test_mcp_comes_up_immediately(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_mcp_server") as mock_spawn:
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.return_value = True
                    result = await repair_mcp()

            assert result == {"status": "restored", "restart_time_s": 2}
            mock_spawn.assert_called_once()
            mock_port.assert_called_once_with("127.0.0.1", 9877)

    @pytest.mark.asyncio
    async def test_mcp_never_starts(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_mcp_server"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.return_value = False
                    result = await repair_mcp()

            assert result == {"status": "failed",
                              "error": "MCP server not restored after 30s"}

    @pytest.mark.asyncio
    async def test_mcp_takes_multiple_iterations(self):
        with patch("ops_daemon.repair.asyncio.sleep"):
            with patch("ops_daemon.repair.spawn_mcp_server"):
                with patch("ops_daemon.repair._check_port", new_callable=AsyncMock) as mock_port:
                    mock_port.side_effect = [False] * 4 + [True]
                    result = await repair_mcp()

            assert result == {"status": "restored", "restart_time_s": 10}
