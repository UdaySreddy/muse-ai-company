"""Completing the CC task observation mounting points — closing the observability broken chain, batch (2).

Forensic conclusions (2026-07-28, cross-checked against the official CC hook documentation + the local registration
surface + the production ledger):

* Among CC hooks related to tasks/sub-agents, there are **only 4 events**: SubagentStart / SubagentStop /
  TaskCreated / TaskCompleted. **There is no hook at all on the abort side** — there are no events such as TaskStop,
  TaskAborted or SubagentAbort; Esc interrupts do not trigger a hook either. ``TaskStop`` is a
  **tool** rather than an event, and can only be observed via PreToolUse/PostToolUse.
* The bridge (cc_task_bridge.py) mounts only on TaskCompleted, and that event **does not also mount send_event.py**
  — so 'how many times the bridge was triggered, how many entries it filtered out' has zero telemetry in the event
  stream, and there is no way to judge whether the bridge is even alive.
* TaskCreated was never registered at all.
* The measured claim 'TaskStop 40 > TaskCreate 14' refers to observations of **tool calls** in cc.tool_use, not
  hook events; and TaskCreate/TaskUpdate dropped to zero after 7/7-7/8 while TaskStop remained in use — in newer
  CC versions a background subagent is a task, and stopping an agent goes through TaskStop, so the abort path really is
  the main path.

In this batch, three observation points are added accordingly, **without touching Q1's 'accounting at completion time'
ruling**: the wall-posting logic is still solely the bridge's responsibility;
all the new additions are events that only observe and do not account.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


class _RecordingBus:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, event_type, source, data, **_kwargs):
        EventType(event_type)
        self.events.append((event_type, source, data))

    def of(self, event_type: str) -> list[dict]:
        return [d for t, _s, d in self.events if t == event_type]


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    bus = _RecordingBus()
    yield HookTranslator(repo=repo, event_bus=bus), bus


class TestBridgeTelemetry:
    """Zero bridge telemetry: whether TaskCompleted was ever triggered had no factual basis before this."""

    @pytest.mark.asyncio()
    async def test_task_completed_lands_observation_event(self, translator):
        tr, bus = translator
        result = await tr.handle_event(
            {
                "hook_event_name": "TaskCompleted",
                "session_id": SESSION,
                "task_id": "cc-task-77",
                "task_subject": "Fix the bridge",
                "teammate_name": "d1-worker",
                "team_name": "workflow-abc",
            }
        )
        (event,) = bus.of("cc.task_completed")
        assert event["cc_task_id"] == "cc-task-77"
        assert event["subject"] == "Fix the bridge"
        assert event["owner"] == "d1-worker"
        assert result["status"] != "ignored", "TaskCompleted is still being discarded as an unknown event"

    @pytest.mark.asyncio()
    async def test_task_created_is_observed_only(self, translator):
        """TaskCreated only observes and does not go on the wall — Q1's accounting-at-completion-time ruling is
        untouched."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "TaskCreated",
                "session_id": SESSION,
                "task_id": "cc-task-78",
                "task_subject": "New task",
            }
        )
        (event,) = bus.of("cc.task_created")
        assert event["cc_task_id"] == "cc-task-78"
        assert event["observed_only"] is True
        # An OS task must not be created on the side
        assert bus.of("task.created") == []


class TestAbortObservation:
    """There is no hook to mount on the abort side; TaskStop can only be lifted into a first-class event from the
    tool-call surface.

    Mounted on **PreToolUse** rather than Post: (1) measurements show those 40 abort signals originally land in
    cc.tool_use
    (Pre); (2) the act of stopping an agent may take the other side away together with the receipt, so Pre is the only
    point guaranteed to get it
    sent out.
    """

    @pytest.mark.asyncio()
    async def test_task_stop_tool_becomes_first_class_event(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": SESSION,
                "tool_name": "TaskStop",
                "tool_input": {"taskId": "cc-task-79", "reason": "Interrupted by the Creator"},
            }
        )
        (event,) = bus.of("cc.task_stopped")
        assert event["cc_task_id"] == "cc-task-79"
        assert event["reason"] == "Interrupted by the Creator"

    @pytest.mark.asyncio()
    async def test_not_double_counted_on_post(self, translator):
        """The Post side must not send it again — an abort is one fact, not two."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "TaskStop",
                "tool_input": {"taskId": "cc-task-79", "reason": "Interrupted by the Creator"},
            }
        )
        assert bus.of("cc.task_stopped") == []

    @pytest.mark.asyncio()
    async def test_other_tools_raise_no_stop_event(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": SESSION,
                "tool_name": "TaskGet",
                "tool_input": {"taskId": "cc-task-80"},
            }
        )
        assert bus.of("cc.task_stopped") == []


class TestRegistrationSurface:
    """The four-piece installation tax: both installation chains must recognize the new mounting point at the same
    time."""

    def test_task_events_registered_on_both_chains(self):
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        manifest = json.loads((root / "plugin" / "hooks" / "hooks.json").read_text())
        hooks = manifest["hooks"]

        # TaskCompleted must also mount send_event (the bridge + telemetry, two legs)
        completed = [
            h["command"] for grp in hooks["TaskCompleted"] for h in grp["hooks"]
        ]
        assert any("cc_task_bridge.py" in c for c in completed)
        assert any("send_event.py" in c for c in completed), "The bridge still has zero telemetry"

        # TaskCreated must exist and mount only observation
        created = [h["command"] for grp in hooks["TaskCreated"] for h in grp["hooks"]]
        assert any("send_event.py" in c for c in created)
        assert not any("cc_task_bridge.py" in c for c in created), "TaskCreated should not go on the wall"

    def test_source_install_surface_matches(self):
        """install.py's HOOK_SURFACE is the single source of truth for the source-installation chain (I8
        machine-checks that the two chains are 1:1)."""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location("_install", root / "install.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        surface = {evt: scripts for evt, _m, scripts in mod.HOOK_SURFACE}
        assert any(s[0] == "send_event.py" for s in surface["TaskCompleted"])
        assert any(s[0] == "cc_task_bridge.py" for s in surface["TaskCompleted"])
        assert any(s[0] == "send_event.py" for s in surface["TaskCreated"])
