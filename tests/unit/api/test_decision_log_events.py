"""Batch 3 (7): the two dead branches for decision_log in hook_translator.

The tool_name in a PreToolUse payload is the **client's full name** (mcp__ai-team-os__meeting_create),
while the branches compare against bare-name literals:

1. ``tool_name == "meeting_start"`` — the tool is not called that at all (its real name is meeting_create),
   and even if the name were right the prefix would still be off — a double misalignment;
2. ``tool_name == "task_run"`` — missing the mcp__ai-team-os__ prefix by one.

Both branches never fired once since launch; the decision.* decision event stream idled for a long time. Prefix
stripping uses
always_load.normalize_tool_name (an existing one; the deleted pipeline/tool_classifier must not be referenced).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

SESSION = "sess-decision-1"


@pytest_asyncio.fixture()
async def translator():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    ht = HookTranslator(repo=repo, event_bus=EventBus(repo=repo))
    yield ht
    await close_db()


async def _run(ht: HookTranslator, tool_name: str, tool_input: dict) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []

    async def fake_emit(event_type, source, data, *args, **kwargs):
        captured.append((event_type, data))

    ht.event_bus.emit = fake_emit  # type: ignore[method-assign]
    await ht._on_pre_tool_use(
        {
            "tool_name": tool_name,
            "session_id": SESSION,
            "tool_input": tool_input,
            "agent_type": "team-lead",
        }
    )
    return captured


@pytest.mark.asyncio
async def test_meeting_create_emits_decision_event(translator):
    captured = await _run(
        translator,
        "mcp__ai-team-os__meeting_create",
        {"topic": "Batch 3 closing review", "participants": ["a", "b"], "purpose": "Settle the plan"},
    )
    decisions = [d for t, d in captured if t == "decision.meeting_started"]
    assert len(decisions) == 1
    assert decisions[0]["topic"] == "Batch 3 closing review"
    assert decisions[0]["participants"] == ["a", "b"]


@pytest.mark.asyncio
async def test_task_run_emits_decision_event(translator):
    captured = await _run(
        translator,
        "mcp__ai-team-os__task_run",
        {"title": "Fix decision_log", "agent_name": "fixer", "description": "Rationale"},
    )
    decisions = [d for t, d in captured if t == "decision.task_assigned"]
    assert len(decisions) == 1
    assert decisions[0]["task_title"] == "Fix decision_log"
    assert decisions[0]["assigned_to"] == "fixer"


@pytest.mark.asyncio
async def test_bare_tool_name_still_works(translator):
    """Calling with a bare name (no prefix) is also recognized — normalization is idempotent."""
    captured = await _run(translator, "meeting_create", {"topic": "Bare name"})
    assert len([d for t, d in captured if t == "decision.meeting_started"]) == 1


@pytest.mark.asyncio
async def test_unrelated_tool_emits_no_decision(translator):
    captured = await _run(translator, "Read", {"file_path": "/tmp/x"})
    assert not [t for t, _ in captured if t.startswith("decision.")]


@pytest.mark.asyncio
async def test_retired_meeting_start_name_is_gone(translator):
    """The old literal meeting_start is not a real tool name; no branch should recognize it any more."""
    import inspect

    src = inspect.getsource(HookTranslator._on_pre_tool_use)
    assert '"meeting_start"' not in src
    assert '"meeting_create"' in src
