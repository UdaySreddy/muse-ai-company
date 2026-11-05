"""Subprocess tests for scripts/os-watch.sh (wake system v2 §7.4).

Uses a curl stub (prepended to PATH) + HOME override to verify exit-code semantics and the armed file lifecycle.
Does not depend on a real API/network. Skipped when bash is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "os-watch.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not SCRIPT.is_file(),
    reason="bash or os-watch.sh unavailable",
)


def _make_curl_stub(bindir: Path, body: str, exit_code: int = 0) -> None:
    """Place a curl stub in bindir: ignores arguments, outputs body, exits with exit_code."""
    stub = bindir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'JSON'\n{body}\nJSON\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)


def _run(bindir: Path, home: Path, env_extra: dict | None = None, timeout: int = 15):
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(home)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT), "sess-1", "team-1"],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def test_actionable_exits_zero(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, '{"actionable":true,"busy_agents":1,"watermark":"2026-07-14T12:00:00"}')
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "30"})
    assert r.returncode == 0
    assert "ACTIONABLE" in r.stdout


def test_api_unreachable_exits_two(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, "", exit_code=7)  # curl connection failure code
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "30"})
    assert r.returncode == 2
    assert "WATCHER_API_UNREACHABLE" in r.stdout


def test_benign_then_hard_timeout_exits_three(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, '{"actionable":false,"watermark":"2026-07-14T12:00:00"}')
    r = _run(bindir, home, {"OS_WATCH_POLL": "1", "OS_WATCH_MAX": "1"})
    assert r.returncode == 3
    assert "WATCHER_TIMEOUT" in r.stdout
    assert "STATUS benign" in r.stdout  # The benign signal was absorbed for at least one round


def test_armed_file_lifecycle(tmp_path):
    """Writes the armed heartbeat file while running; clears it on exit (trap)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _make_curl_stub(bindir, '{"actionable":false,"watermark":"2026-07-14T12:00:00"}')
    armed = home / ".claude" / "data" / "ai-team-os" / "wake-state" / "sess-1.armed"

    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(home)
    env["OS_WATCH_POLL"] = "1"
    env["OS_WATCH_MAX"] = "30"
    proc = subprocess.Popen(["bash", str(SCRIPT), "sess-1", "team-1"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Wait for it to arm
        deadline = time.time() + 6
        while time.time() < deadline and not armed.exists():
            time.sleep(0.2)
        assert armed.exists(), "Should write the armed heartbeat file while running"
        armed_until = float(armed.read_text().strip())
        assert armed_until > time.time()  # Not expired
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # trap cleanup: the armed file should be removed after exit
    assert not armed.exists(), "The trap should clear the armed file after exit"
