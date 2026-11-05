"""Tests for MCP Server FastAPI auto-start helpers."""

from __future__ import annotations

import socket
from unittest.mock import patch

import aiteam as _aiteam_pkg
from aiteam.mcp._autostart import _ensure_api_running, _is_port_open


def test_is_port_open_returns_false():
    """An unlistened port should return False."""
    # Use a high port extremely unlikely to be occupied
    assert _is_port_open("127.0.0.1", 59999) is False


def test_is_port_open_returns_true():
    """A listened port should return True."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _is_port_open("127.0.0.1", port) is True
    finally:
        srv.close()


@patch("aiteam.mcp._autostart._is_port_open", return_value=True)
@patch("aiteam.mcp._autostart._is_api_healthy_on_port", return_value=True)
@patch(
    "aiteam.mcp._autostart._get_running_api_version_on_port",
    # Must follow the real package version — it once hard-coded "1.3.4" and after a package upgrade was judged "version outdated"
    # and walked into the kill-the-occupier path (Popen of fuser/lsof), failing assertions falsely
    return_value=_aiteam_pkg.__version__,
)
@patch("aiteam.mcp._autostart.subprocess.Popen")
# Isolate real runtime files: _debug_log writes ~/.claude/data/ai-team-os/mcp-debug.log,
# _get_api_port reads the real api_port.txt — unit tests must not pollute/depend on them
@patch("aiteam.mcp._autostart._get_api_port", return_value=8000)
@patch("aiteam.mcp._autostart._debug_log")
def test_ensure_api_skips_when_running(
    mock_debug_log, mock_get_port, mock_popen, mock_version_on_port, mock_healthy, mock_port
):
    """Port already occupied with matching version — subprocess must not be spawned."""
    _ensure_api_running()
    mock_popen.assert_not_called()
