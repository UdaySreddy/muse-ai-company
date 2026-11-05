"""Tool group switches + read-only tier unit tests (tool progressive loading P2).

Coverage: AITEAM_TOOLSETS parsing (all/default/list/unknown group name warning/mixed incremental),
the default group hard cap of ≤50, AITEAM_READONLY removing write tools while keeping read tools, and the default full set of 112 unchanged.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from aiteam.mcp.tools import register_all
from aiteam.mcp.tools.toolsets import (
    ALL_TOOLSETS,
    DEFAULT_TOOLSETS,
    WRITE_TOOLS,
    resolve_readonly,
    resolve_toolsets,
)

# Full tool count baseline — changing this number must sync docs/CHANGELOG (red line I6 only recognizes the tool count).
TOTAL_TOOLS = 113
DEFAULT_HARD_CAP = 50


def _registered_names(monkeypatch, env: dict[str, str] | None = None) -> list[str]:
    """Under the given env, register all modules and return the bare tool name list.

    Go through the public ``list_tools()`` rather than reading the private component table — the private surface is a guaranteed blow-up point of fastmcp 4.0,
    and there is no point in a test double hitting it before the production code does.
    """
    monkeypatch.delenv("AITEAM_TOOLSETS", raising=False)
    monkeypatch.delenv("AITEAM_READONLY", raising=False)
    for key, val in (env or {}).items():
        monkeypatch.setenv(key, val)
    mcp = FastMCP(name="test")
    register_all(mcp)
    return sorted(tool.name for tool in asyncio.run(mcp.list_tools()))


# ---------------------------------------------------------------------------
# resolve_toolsets: pure function parsing
# ---------------------------------------------------------------------------


def test_resolve_none_is_all() -> None:
    assert resolve_toolsets(None) == set(ALL_TOOLSETS)


def test_resolve_empty_string_is_all() -> None:
    assert resolve_toolsets("") == set(ALL_TOOLSETS)


def test_resolve_all_keyword() -> None:
    assert resolve_toolsets("all") == set(ALL_TOOLSETS)
    assert resolve_toolsets("ALL") == set(ALL_TOOLSETS)


def test_resolve_default_keyword() -> None:
    assert resolve_toolsets("default") == set(DEFAULT_TOOLSETS)


def test_resolve_explicit_list() -> None:
    assert resolve_toolsets("task,team") == {"task", "team"}


def test_resolve_list_trims_and_lowercases() -> None:
    assert resolve_toolsets(" Task , TEAM ") == {"task", "team"}


def test_resolve_unknown_name_warns_and_ignored(capsys: pytest.CaptureFixture) -> None:
    result = resolve_toolsets("task,bogus")
    assert result == {"task"}
    err = capsys.readouterr().err
    assert "bogus" in err


def test_resolve_incremental_default_plus_group() -> None:
    # default + incremental groups
    result = resolve_toolsets("default,ecosystem")
    assert result == set(DEFAULT_TOOLSETS) | {"ecosystem"}


def test_resolve_all_unknown_falls_back_to_all(
    capsys: pytest.CaptureFixture,
) -> None:
    result = resolve_toolsets("zzz,qqq")
    assert result == set(ALL_TOOLSETS)
    assert "falling back" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# resolve_readonly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_resolve_readonly_truthy(raw: str) -> None:
    assert resolve_readonly(raw) is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "off", "random"])
def test_resolve_readonly_falsy(raw) -> None:
    assert resolve_readonly(raw) is False


# ---------------------------------------------------------------------------
# Registration-time behavior
# ---------------------------------------------------------------------------


def test_default_env_registers_full_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env at all → full tool registration (TOTAL_TOOLS), backward compatible and unchanged."""
    names = _registered_names(monkeypatch)
    assert len(names) == TOTAL_TOOLS


def test_toolsets_all_registers_full_total(monkeypatch: pytest.MonkeyPatch) -> None:
    names = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "all"})
    assert len(names) == TOTAL_TOOLS


def test_default_group_under_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """After registering the default group, the tool count hard cap is ≤50 (universal guardrail + the official 30-50 inflection point)."""
    names = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "default"})
    assert len(names) <= DEFAULT_HARD_CAP
    # Representative core-group tools must be present
    assert "task_create" in names
    assert "task_memo_add" in names
    assert "memory_search" in names


def test_explicit_group_only_registers_that_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "task"})
    assert all(n.startswith("task") for n in names)
    # The ecosystem group should not appear
    assert not any(n.startswith("ecosystem_") for n in names)


def test_incremental_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "default"})
    inc = _registered_names(
        monkeypatch, {"AITEAM_TOOLSETS": "default,ecosystem"}
    )
    assert len(inc) > len(base)
    assert any(n.startswith("ecosystem_") for n in inc)


# ---------------------------------------------------------------------------
# Read-only tier
# ---------------------------------------------------------------------------


def test_readonly_removes_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full set + read-only → all write tools removed, all read tools kept."""
    full = set(_registered_names(monkeypatch))
    ro = set(_registered_names(monkeypatch, {"AITEAM_READONLY": "1"}))
    # The removed count == the count of WRITE_TOOLS hits in the full set
    assert full - ro == (full & WRITE_TOOLS)
    # Not one write tool remains
    assert not (ro & WRITE_TOOLS)
    # Representative write tools are indeed gone
    for w in ("task_create", "task_update", "report_save", "os_restart_api"):
        assert w not in ro
    # Representative read tools are still there
    for r in ("task_list_project", "task_memo_read", "task_status", "team_list"):
        assert r in ro


def test_readonly_orthogonal_to_toolsets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only is orthogonal to grouping: default + read-only."""
    ro = set(
        _registered_names(
            monkeypatch,
            {"AITEAM_TOOLSETS": "default", "AITEAM_READONLY": "1"},
        )
    )
    assert "task_create" not in ro  # Write, removed
    assert "task_memo_add" not in ro  # Write, removed
    assert "task_memo_read" in ro  # Read, kept
    assert not (ro & WRITE_TOOLS)


def test_write_tools_all_exist_in_full_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WRITE_TOOLS list must not contain ghost names (guards against the list going stale after a tool rename)."""
    full = set(_registered_names(monkeypatch))
    ghosts = WRITE_TOOLS - full
    assert not ghosts, f"WRITE_TOOLS contains tools that no longer exist: {ghosts}"


def test_agent_templates_deny_no_ghost_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disallowedTools of plugin/agents/*.md must not name retired tools.

    The denylist in frontmatter is a second, hand-maintained tool list; when a tool retires it rots
    there — CC does not error on unknown tool names, so ghost names can hang there unnoticed (2026-07-27
    batch 8a measurement: all 5 templates still deny git_auto_commit / git_create_pr).
    """
    import re
    from pathlib import Path

    live = set(_registered_names(monkeypatch))
    repo_root = Path(__file__).resolve().parents[2]
    ghosts: dict[str, list[str]] = {}
    for md in sorted((repo_root / "plugin" / "agents").glob("*.md")):
        named = re.findall(r"^\s*-\s*mcp__ai-team-os__([\w-]+)\s*$", md.read_text(encoding="utf-8"), re.M)
        missing = [n for n in named if n not in live]
        if missing:
            ghosts[md.name] = missing
    assert not ghosts, f"agent template disallowedTools contains tools that no longer exist: {ghosts}"
