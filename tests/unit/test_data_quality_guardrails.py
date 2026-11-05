"""Ledger data quality stopgap — observability broken-chain closure batch ④.

Three confirmed findings (2026-07-28 production ledger forensics):

1. **Test stubs leaking into the real ledger**: ``tests/unit/test_context_scripts.py`` runs the real hook
   ``pre_compact_save.py`` as a subprocess with ``session_id="test-session"``. When that hook
   cannot get the port file it **falls back to localhost:8000**, i.e. the production instance — so every test run
   writes a ``session.compact_checkpoint`` into the real ledger. Of the 18 checkpoints in the production database, 14 came
   this way (all ``trigger=manual`` + ``cwd=""``, matching the test inputs field by field); real data
   is only 4. The guardrail sits at the **single write entry point**: session identities with reserved prefixes (test-/demo-) are never
   persisted — no matter how the tests hit it, the ledger stays clean.

2. **compact_summary stripped by the size gate**: when the overall payload is >32KB, ``send_event.py`` keeps only
   ``ESSENTIAL_FIELDS``, and neither ``compact_summary`` nor ``trigger`` is among them. Compression summaries
   routinely run to tens of thousands of characters, so in large-summary scenarios both vanish from the payload the server receives and are persisted
   as ``summary_chars=0`` — exactly the shape of that production record ``{session_id, trigger:"",
   summary_chars:0}``. The body is **deliberately not stored** anyway, so stop shipping the body over and computing:
   compute the length on the hook side on the spot, drop the body, and let the length pass the gate as a must-keep field.

3. **decision.user_asked field misalignment**: the extraction logic read the singular ``tool_input["question"]``,
   while AskUserQuestion's real payload is the **plural ``questions`` array** (each item containing
   question/header/options). As a result, in 6 production ruling records question/options/agent_name
   were all empty and the real content was all stuffed into the answer JSON — the human-review scene was effectively unrecorded.
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
    """As strict as production: an unrecognized event type blows up on the spot."""

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


# ---------------------------------------------------------------------------
# 1. Reserved-id guardrail at the single write entry point
# ---------------------------------------------------------------------------


class TestReservedIdGuardrail:
    """Reserved identities must not enter the ledger — the guardrail sits in create_event and cannot be bypassed."""

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "reserved",
        ["test-session", "test-session-001", "demo-session", "TEST-Session", "demo-1"],
    )
    async def test_reserved_session_id_is_refused(self, repo, reserved):
        before = len(await repo.list_events(limit=200))
        await repo.create_event(
            event_type="session.compact_checkpoint",
            source=f"session:{reserved}",
            data={"session_id": reserved, "trigger": "manual"},
        )
        after = await repo.list_events(limit=200)
        assert len(after) == before, f"Reserved identity {reserved} was still written into the ledger"

    @pytest.mark.asyncio()
    async def test_real_session_id_still_writes(self, repo):
        """The guardrail must not harm real data."""
        await repo.create_event(
            event_type="session.compact_checkpoint",
            source=f"session:{SESSION}",
            data={"session_id": SESSION, "trigger": "auto"},
        )
        hits = await repo.list_events(event_type="session.compact_checkpoint", limit=50)
        assert len(hits) == 1

    @pytest.mark.asyncio()
    async def test_guardrail_matches_on_entity_id_too(self, repo):
        """The identity may also land on entity_id; block that too."""
        before = len(await repo.list_events(limit=200))
        await repo.create_event(
            event_type="task.created",
            source="repository",
            data={"task_id": "demo-task-1"},
            entity_id="demo-task-1",
            entity_type="task",
        )
        assert len(await repo.list_events(limit=200)) == before

    @pytest.mark.asyncio()
    async def test_substring_test_is_not_matched(self, repo):
        """Only prefixes count; do not harm normal ids whose body contains test."""
        await repo.create_event(
            event_type="task.created",
            source="repository",
            data={"task_id": "latest-run-7"},
            entity_id="latest-run-7",
            entity_type="task",
        )
        assert len(await repo.list_events(event_type="task.created", limit=50)) == 1


# ---------------------------------------------------------------------------
# 2. The compact_summary length must survive the size gate
# ---------------------------------------------------------------------------


class TestCompactSummaryChars:
    @pytest.mark.asyncio()
    async def test_precomputed_chars_wins(self, translator):
        """The hook-side computed length wins — the body was already dropped on the hook side, so the server has no len() to compute."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": SESSION,
                "trigger": "auto",
                "compact_summary_chars": 48213,
            }
        )
        (event,) = bus.of("session.compact_completed")
        assert event["summary_chars"] == 48213, "The hook-side computed length was discarded"
        assert event["trigger"] == "auto"

    @pytest.mark.asyncio()
    async def test_falls_back_to_body_length(self, translator):
        """The old shape (body still present) must still compute correctly."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": SESSION,
                "trigger": "manual",
                "compact_summary": "S" * 4321,
            }
        )
        (event,) = bus.of("session.compact_completed")
        assert event["summary_chars"] == 4321

    def test_hook_precomputes_and_drops_body(self):
        """Hook side: the body is replaced by the length, and the length is a must-keep field (otherwise the size gate zeroes it)."""
        from aiteam.hooks import send_event

        payload = send_event._trim_payload(
            {
                "hook_event_name": "PostCompact",
                "session_id": SESSION,
                "trigger": "auto",
                "compact_summary": "S" * 50_000,
            }
        )
        assert payload.get("compact_summary_chars") == 50_000
        assert "compact_summary" not in payload, "The compact summary body should not be shipped over the network"
        assert "compact_summary_chars" in send_event.ESSENTIAL_FIELDS
        assert "trigger" in send_event.ESSENTIAL_FIELDS


# ---------------------------------------------------------------------------
# 3. decision.user_asked extraction put back in place
# ---------------------------------------------------------------------------


class TestUserAskedExtraction:
    @pytest.mark.asyncio()
    async def test_plural_questions_are_extracted(self, translator):
        """The real payload is a questions array — this is the root cause of the 6 empty records."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "question": "Which two ledger edits are authorized?",
                            "header": "Ledger corrections",
                            "options": [
                                {"label": "Do both", "description": "Archive then delete"},
                                {"label": "Touch neither", "description": "Only add guardrails"},
                            ],
                        }
                    ]
                },
                "tool_response": {"answers": {"Which two ledger edits are authorized?": "Do both"}},
            }
        )
        (event,) = bus.of("decision.user_asked")
        assert event["question"] == "Which two ledger edits are authorized?", f"Question body is still empty: {event}"
        assert [o["label"] for o in event["options"]] == ["Do both", "Touch neither"]
        assert event["headers"] == ["Ledger corrections"]

    @pytest.mark.asyncio()
    async def test_multi_question_batch_all_recorded(self, translator):
        """When asking multiple questions at once, do not keep only the first."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {"question": "A?", "header": "A", "options": [{"label": "1"}]},
                        {"question": "B?", "header": "B", "options": [{"label": "2"}]},
                    ]
                },
                "tool_response": {},
            }
        )
        (event,) = bus.of("decision.user_asked")
        assert event["question"] == "A?\nB?"
        assert event["question_count"] == 2
        assert event["headers"] == ["A", "B"]

    @pytest.mark.asyncio()
    async def test_singular_shape_still_supported(self, translator):
        """The old singular shape stays compatible."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "AskUserQuestion",
                "tool_input": {"question": "Stop heartbeat writes?", "options": ["Stop", "Don't stop"]},
                "tool_response": "Stop",
            }
        )
        (event,) = bus.of("decision.user_asked")
        assert event["question"] == "Stop heartbeat writes?"
        assert event["options"] == ["Stop", "Don't stop"]
