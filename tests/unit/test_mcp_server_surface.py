"""MCP server surface — what a tool-search client can actually find.

CC no longer loads every tool schema up front; it searches tool text and loads
what matches. Two things decide whether 112 tools are reachable at all:

  * the server ``instructions`` — the only text describing the server as a
    whole, so it has to name capabilities the way a searcher would phrase them,
    while staying small enough to always be in context;
  * per-parameter descriptions — an undescribed parameter cannot be matched and
    gets guessed at.

These tests pin both. The parameter invariant is also enforced outside pytest by
scripts/check_tool_param_descriptions.py (I9) so a commit cannot skip it.

A third thing rides on the same surface: the ``serverInfo`` block of the
``initialize`` response. FastMCP fills ``version`` with **its own** package
version unless the server passes one, so any client-side version gate or
telemetry would read the framework version instead of the OS version. That is
pinned here through a real handshake, not by reading the constructor argument.
"""

from __future__ import annotations

import re

import pytest

import aiteam
from aiteam.mcp.server import mcp

# Budget: instructions ride along in every session, so they must stay cheap.
MAX_INSTRUCTIONS_BYTES = 2048

# Capabilities a user/agent would search for. Each entry is a set of synonyms —
# the instructions must offer at least one phrasing per capability.
REQUIRED_CAPABILITIES = {
    "Cross-session task ledger": ("cross-session", "task wall", "ledger"),
    "Agent observability attribution": ("observability", "attribution", "failure"),
    "Memory governance": ("memory",),
    "Project isolation": ("project isolation", "mutually", "isolation"),
    "Report knowledge base": ("report",),
}


@pytest.fixture(scope="module")
def instructions() -> str:
    text = mcp.instructions
    assert text, "The MCP server must have instructions — this is the only server-level description for tool search"
    return text


@pytest.fixture(scope="module")
def tool_names() -> set[str]:
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


def test_instructions_fit_the_context_budget(instructions):
    size = len(instructions.encode("utf-8"))
    assert size <= MAX_INSTRUCTIONS_BYTES, (
        f"instructions are {size} bytes, exceeding the {MAX_INSTRUCTIONS_BYTES}-byte budget"
    )


def test_instructions_are_capability_oriented(instructions):
    """A one-line label is not searchable; the differentiators must be spelled out."""
    assert len(instructions.encode("utf-8")) > 400, (
        "instructions too short — a one-line label cannot support capability-based retrieval"
    )
    missing = [
        cap
        for cap, synonyms in REQUIRED_CAPABILITIES.items()
        if not any(s in instructions for s in synonyms)
    ]
    assert not missing, f"instructions do not cover a core capability: {missing}"


def test_instructions_only_advertise_real_tools(instructions, tool_names):
    """Naming a tool that does not exist sends the searcher down a dead end."""
    # snake_case identifiers of >=2 segments, plus `family_*` wildcards.
    cited = set(re.findall(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", instructions))
    wildcards = {m for m in re.findall(r"\b([a-z][a-z0-9_]*)_\*", instructions)}
    unknown = sorted(
        name
        for name in cited
        if name not in tool_names
        and not any(name.startswith(f"{w}_") for w in wildcards)
    )
    assert not unknown, f"instructions mention tools that do not exist: {unknown}"


def test_cited_tool_families_are_non_empty(instructions, tool_names):
    """`ecosystem_*` style shorthand must resolve to tools that actually exist."""
    for family in re.findall(r"\b([a-z][a-z0-9_]*)_\*", instructions):
        prefix = f"{family}_"
        assert any(n.startswith(prefix) for n in tool_names), (
            f"the tool family {prefix}* referenced by instructions has no actual tools"
        )


@pytest.fixture(scope="module")
def initialize_result():
    """Real MCP initialize handshake against the live server object."""
    import asyncio

    from fastmcp import Client

    async def _handshake():
        async with Client(mcp) as client:
            return client.initialize_result

    return asyncio.run(_handshake())


def test_server_info_reports_os_version_not_framework_version(initialize_result):
    """serverInfo.version must be the OS version, compared against the live value.

    Both sides are read at runtime - hardcoding the string here would turn this
    file into a sixth place the version has to be kept in step.
    """
    reported = initialize_result.serverInfo.version
    assert reported == aiteam.__version__, (
        f"serverInfo.version={reported!r} should be aiteam.__version__={aiteam.__version__!r} "
        "— when version is not passed, FastMCP fills in its own package version (it once reported fastmcp's 3.4.3)"
    )


def test_server_info_reports_server_name(initialize_result):
    assert initialize_result.serverInfo.name == "ai-team-os"


def test_every_tool_and_parameter_is_described(tool_names):
    """I9 invariant, mirrored in pytest so `pytest tests` alone would catch it."""
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    assert tools, "Enumerated 0 tools — register_all did not take effect"

    undescribed_tools = [t.name for t in tools if not (t.description or "").strip()]
    assert not undescribed_tools, f"Tools missing a description: {undescribed_tools}"

    missing = [
        f"{t.name}.{param}"
        for t in tools
        for param, spec in (t.parameters or {}).get("properties", {}).items()
        if not (spec.get("description") or "").strip()
    ]
    assert not missing, (
        f"Parameters missing a description (tool search cannot find them): {missing}\n"
        "Note: the docstring Args section can only hold one parameter "
            "per line; `limit / offset: ...` gets dropped wholesale"
    )
