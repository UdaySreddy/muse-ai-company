"""Directory fingerprint temp buckets — pure functions + hook consistency + unregistered briefing minimalism unit tests.

Corresponds to three of the product-layer changes in the 2026-07-21 cross-wiring incident root fix:
- the pure-function semantics of aiteam.memory.scoping.dir_bucket_scope_id;
- under an unregistered directory, both hooks encode the same cwd into the same X-Project-Dir header (the fingerprint
authority lives only on
  the API side; hooks only pass cwd — this test pins both hooks' arguments to be identical, preventing silent drift);
- the session_bootstrap unregistered branch outputs only the registration guide + legitimate global memories, without
pouring in workbench content.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from aiteam.memory.scoping import DIR_BUCKET_PREFIX, dir_bucket_scope_id

# ============================================================
# dir_bucket_scope_id pure function
# ============================================================


def test_empty_cwd_returns_empty() -> None:
    assert dir_bucket_scope_id("") == ""
    assert dir_bucket_scope_id("   ") == ""


def test_prefix_and_length() -> None:
    bucket = dir_bucket_scope_id("/tmp/some-unregistered-dir")
    assert bucket.startswith(DIR_BUCKET_PREFIX)
    # "dir:" + 16 hex chars
    assert len(bucket) == len(DIR_BUCKET_PREFIX) + 16
    hexpart = bucket[len(DIR_BUCKET_PREFIX):]
    assert all(c in "0123456789abcdef" for c in hexpart)


def test_deterministic() -> None:
    p = "/tmp/aiteam-determinism"
    assert dir_bucket_scope_id(p) == dir_bucket_scope_id(p)


def test_idempotent_under_realpath() -> None:
    """Passing the raw cwd and passing its realpath result agree (realpath is idempotent, so the write and read sides
    do not diverge over normalization)."""
    p = "/tmp/aiteam-realpath-x"
    assert dir_bucket_scope_id(p) == dir_bucket_scope_id(os.path.realpath(p))


def test_different_dirs_different_buckets() -> None:
    assert dir_bucket_scope_id("/tmp/dir-one") != dir_bucket_scope_id("/tmp/dir-two")


# ============================================================
# X-Project-Dir encoding consistency of the two hooks (pinned consistency)
# ============================================================


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _make_capturing_urlopen(captured: list):
    def _fake(req, timeout=None):  # noqa: ANN001, ANN202
        captured.append(req)
        return _FakeResp({"data": []})

    return _fake


def test_both_hooks_encode_same_cwd_identically(monkeypatch) -> None:
    """session_bootstrap and inject_subagent_context emit the same X-Project-Dir header for the same cwd."""
    from aiteam.hooks import inject_subagent_context as inj
    from aiteam.hooks import session_bootstrap as sb

    cwd = "/tmp/aiteam-consistency-dir"
    captured: list = []
    monkeypatch.setattr(urllib.request, "urlopen", _make_capturing_urlopen(captured))

    # session_bootstrap unregistered branch: project_dir=cwd (no project_id → sends X-Project-Dir)
    sb._fetch_direction_memories(project_dir=cwd)
    # inject_subagent_context: X-Project-Dir comes from CLAUDE_PROJECT_DIR or cwd
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", cwd)
    inj._fetch_direction_memories()

    assert len(captured) == 2
    # urllib stores headers via key.capitalize(): X-Project-Dir → "X-project-dir"
    h_sb = captured[0].headers.get("X-project-dir")
    h_inj = captured[1].headers.get("X-project-dir")
    assert h_sb is not None
    assert h_sb == h_inj


def test_session_bootstrap_uses_project_id_when_registered(monkeypatch) -> None:
    """Registered projects use X-Project-Id (no X-Project-Dir) — the registration path is unaffected by the dir
    branch."""
    from aiteam.hooks import session_bootstrap as sb

    captured: list = []
    monkeypatch.setattr(urllib.request, "urlopen", _make_capturing_urlopen(captured))
    sb._fetch_direction_memories(project_id="proj-abc")

    assert len(captured) == 1
    assert captured[0].headers.get("X-project-id") == "proj-abc"
    assert captured[0].headers.get("X-project-dir") is None


# ============================================================
# Unregistered briefing minimalism
# ============================================================

# Pinpoint section headers/lines unique to the full briefing, to avoid harming explanatory mentions of features in the
# registration guide body
# (e.g. "task wall / meetings / reports available after registration" is legitimate guide copy, not a workbench
# section).
_WORKBENCH_MARKERS = [
    "=== Leader Core Rules",
    "Task Wall Top 5",
    " active",  # team status line "Team: N active, M completed"
    "=== Auto Wake",
    "=== Available Skills",
    "=== Available Agent Templates",
    "=== In-Progress Tasks",
]


def test_unregistered_briefing_is_minimal(monkeypatch) -> None:
    """The unregistered briefing contains the registration guide and no workbench content such as team lists / Leader
    rules / task wall / wake / skills."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(sb, "_fetch_direction_memories", lambda **kw: [])
    out = sb._build_unregistered_briefing("/tmp/aiteam-unreg", is_dismissed=False)

    # Contains the registration/ignore guide
    assert "project_create(" in out
    assert "dismiss_project_registration(" in out
    # Contains no workbench section
    for marker in _WORKBENCH_MARKERS:
        assert marker not in out, f"The minimal briefing should not contain a workbench section: {marker}"


def test_unregistered_briefing_dismissed_suppresses_prompt(monkeypatch) -> None:
    """A directory whose registration was ignored: no longer shows the registration guide, still stays minimal (no
    workbench poured in)."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(sb, "_fetch_direction_memories", lambda **kw: [])
    out = sb._build_unregistered_briefing("/tmp/aiteam-unreg", is_dismissed=True)

    assert "project_create(" not in out
    for marker in _WORKBENCH_MARKERS:
        assert marker not in out


def test_unregistered_briefing_renders_global_memories(monkeypatch) -> None:
    """The unregistered briefing renders the retrieved global/dir bucket directional memories (legitimate global
    discipline is inherited as usual)."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(
        sb,
        "_fetch_direction_memories",
        lambda **kw: [{"content": "All output in Chinese", "kind": "constraint"}],
    )
    out = sb._build_unregistered_briefing("/tmp/aiteam-unreg", is_dismissed=False)
    assert "All output in Chinese" in out


def test_build_briefing_routes_unregistered_to_minimal(monkeypatch) -> None:
    """_build_briefing routes to the minimal briefing when unregistered (produces no Leader workbench)."""
    from aiteam.hooks import session_bootstrap as sb

    monkeypatch.setattr(sb, "_api_get", lambda *a, **kw: None)
    monkeypatch.setattr(
        sb, "_check_project_registration", lambda *a, **kw: (False, False, {})
    )
    monkeypatch.setattr(sb, "_fetch_direction_memories", lambda **kw: [])

    out = sb._build_briefing()
    assert "Minimal briefing" in out
    assert "Leader Core Rules" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
