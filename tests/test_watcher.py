"""Tests for ops_daemon.main._restart_watcher — timeout isolation and marker processing."""
import asyncio
from unittest.mock import patch
import pytest
from ops_daemon.main import _restart_watcher


class TestRestartWatcher:

    @pytest.mark.asyncio
    async def test_processes_marker_and_unlinks(self, tmp_path):
        """Happy path: marker processed, handler called, marker unlinked."""
        marker = tmp_path / ".restart-proxy-4002"
        marker.write_text("")

        with patch("ops_daemon.main.spawn_proxy"):
            watcher = asyncio.create_task(_restart_watcher(tmp_path, tmp_path))
            await asyncio.sleep(2.5)
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher

        assert not marker.exists(), "marker should be unlinked after processing"

    @pytest.mark.asyncio
    async def test_unknown_marker_is_unlinked(self, tmp_path):
        """Unknown marker types are unlinked with a warning, not stuck."""
        marker = tmp_path / ".restart-unknown-thing"
        marker.write_text("")

        watcher = asyncio.create_task(_restart_watcher(tmp_path, tmp_path))
        await asyncio.sleep(2.5)
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher

        assert not marker.exists(), "unknown marker should be unlinked"

    @pytest.mark.asyncio
    async def test_daemon_marker_skipped(self, tmp_path):
        """.restart-daemon is skipped (handled via .stop + finally)."""
        marker = tmp_path / ".restart-daemon"
        marker.write_text("")

        watcher = asyncio.create_task(_restart_watcher(tmp_path, tmp_path))
        await asyncio.sleep(2.5)
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher

        assert not marker.exists(), ".restart-daemon marker should be unlinked"

    @pytest.mark.asyncio
    async def test_timeout_isolated_unlinks_marker(self, tmp_path):
        """When wait_for times out, marker is unlinked and loop continues."""
        marker = tmp_path / ".restart-proxy-4000"
        marker.write_text("")

        call_count = [0]
        orig_wait_for = asyncio.wait_for

        async def _timeout_first(coro, timeout=30):
            call_count[0] += 1
            if call_count[0] == 1:
                raise asyncio.TimeoutError()
            return await orig_wait_for(coro, timeout=5)

        with patch("ops_daemon.main.asyncio.wait_for", side_effect=_timeout_first):
            with patch("ops_daemon.main.spawn_proxy") as mock_spawn:
                watcher = asyncio.create_task(_restart_watcher(tmp_path, tmp_path))
                await asyncio.sleep(2.5)
                watcher.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await watcher

        assert not marker.exists(), "marker should be unlinked even on timeout"
        # spawn_proxy never called because wait_for raised before it
        mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_stop_loop(self, tmp_path):
        """Exception in handler is caught, marker unlinked, loop continues."""
        marker = tmp_path / ".restart-proxy-4002"
        marker.write_text("")

        with patch("ops_daemon.main.spawn_proxy",
                   side_effect=RuntimeError("boom!")):
            watcher = asyncio.create_task(_restart_watcher(tmp_path, tmp_path))
            await asyncio.sleep(2.5)
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher

        assert not marker.exists(), "marker should be unlinked on exception"

    @pytest.mark.asyncio
    async def test_multiple_markers_processed_sequentially(self, tmp_path):
        """Multiple markers are all processed in one cycle."""
        m1 = tmp_path / ".restart-proxy-4002"
        m2 = tmp_path / ".restart-mcp"
        m1.write_text("")
        m2.write_text("")

        with patch("ops_daemon.main.spawn_proxy") as mock_spawn:
            with patch("ops_daemon.main.spawn_mcp_server") as mock_mcp:
                watcher = asyncio.create_task(_restart_watcher(tmp_path, tmp_path))
                await asyncio.sleep(2.5)
                watcher.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await watcher

        assert not m1.exists()
        assert not m2.exists()
        mock_spawn.assert_called_once_with(4002)
        mock_mcp.assert_called_once()
