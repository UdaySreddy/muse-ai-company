"""**Failure observability** of Leader main-session capture — the rule established by the 2026-08-03 accident
investigation.

Accident record: the Leader rows of three live sessions had ``tokens_measured_at`` long stuck at NULL, while one
session had a ``PostCompact`` that very day (the hook returned successfully and the ``session.compact_completed``
event was also stored), so by design that trip was a ``force=True`` forced freeze that had to write to the database. The
investigation took two hours
yet still **could not determine which link broke**, not because the chain is complex, but because it is entirely
unfalsifiable:

``_capture_leader_usage`` collapses five entirely different outcomes into the same ``None`` —
No session_id / no Leader row found / transcript not locatable / blocked by throttling /
``_capture_leader_usage`` collapses five entirely different outcomes into the same ``None`` —
never enables DEBUG. So "collection ran but produced no data" and "collection threw an exception" look, in the database,
the logs, and the receipts,
**exactly the same in all three places**.

So what this test pins is not 'capture can succeed' (that is test_leader_usage_capture.py's job),
but 'when capture fails, it can be seen':

1. A forced freeze that misses -> a WARNING-level log, with the concrete reason (not DEBUG, not silence);
2. It raises -> an event is recorded, retrievable afterwards with a single SQL query (logs are truncated on process
restart; events are not);
3. Every kind of skip has **its own name**, carried out in the receipt, no longer one all-purpose None.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from aiteam.api import leader_usage, session_probe
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator

CAPTURE_FAILED_EVENT = "leader.usage_capture_failed"


def _assistant(req: str, *, inp: int, out: int) -> dict:
    return {
        "type": "assistant",
        "requestId": req,
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": out,
            },
        },
    }


def _transcript(tmp_path: Path, name: str = "sess.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(_assistant("req_a", inp=11, out=22)) + "\n",
        encoding="utf-8",
    )
    return path


async def _make_leader(repo, session_id: str):
    agent = await repo.create_agent(
        team_id="team-leader",
        name="Leader",
        role="leader",
        source="hook",
        session_id=session_id,
    )
    await repo.update_agent(agent.id, status="busy")
    return agent


def _translator(repo) -> HookTranslator:
    return HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


# ============================================================
# 1. A forced freeze that misses must be a WARNING, and state its reason
# ============================================================


class TestForcedMissIsLoud:
    @pytest.mark.asyncio
    async def test_post_compact_that_cannot_locate_the_transcript_warns(
        self, db_repository, tmp_path: Path, caplog
    ):
        """PostCompact cannot locate the transcript = this moment's watermark is permanently lost; it should not
        leave only a DEBUG."""
        repo = db_repository
        await _make_leader(repo, "sess-miss")
        with caplog.at_level(logging.WARNING, logger="aiteam.api.hook_translator"):
            await _translator(repo).handle_event(
                {
                    "hook_event_name": "PostCompact",
                    "session_id": "sess-miss",
                    "transcript_path": str(tmp_path / "gone.jsonl"),
                    "trigger": "manual",
                }
            )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "A forced freeze missed and was stored nowhere, yet the log does not say a single word"
        blob = " ".join(r.getMessage() for r in warnings)
        assert "no-transcript" in blob, f"The reason was not written into the log: {blob}"

    @pytest.mark.asyncio
    async def test_throttled_stop_stays_quiet(self, db_repository, tmp_path: Path, caplog):
        """Throttling is a **normal** outcome — it must not, because of the previous case, shout every round of Stop
        as a WARNING as well."""
        repo = db_repository
        await _make_leader(repo, "sess-quiet")
        t = _transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-quiet",
            "transcript_path": str(t),
        }
        await translator.handle_event(payload)  # The first measurement establishes the baseline
        with caplog.at_level(logging.WARNING, logger="aiteam.api.hook_translator"):
            await translator.handle_event(payload)  # Within the window, throttled
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ============================================================
# 2. Raising an exception must leave a **durable** trace (logs get truncated; events do not)
# ============================================================


class TestFailureLeavesADurableTrace:
    @pytest.mark.asyncio
    async def test_write_failure_emits_a_queryable_event(self, db_repository, tmp_path: Path):
        repo = db_repository
        await _make_leader(repo, "sess-boom")
        t = _transcript(tmp_path)
        translator = _translator(repo)

        async def boom(*_args, **_kwargs):
            msg = "database is locked"
            raise RuntimeError(msg)

        repo.update_agent = boom  # type: ignore[method-assign]
        result = await translator.handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-boom",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        # The main flow must never be dragged down by capture — the receipt is returned as usual
        assert result["status"] == "recorded"

        events = await repo.list_events(limit=200)
        failures = [e for e in events if e.type == CAPTURE_FAILED_EVENT]
        assert failures, "A capture exception left no trace that can be looked up"
        data = failures[0].data
        assert data["session_id"] == "sess-boom"
        assert data["forced"] is True
        assert "RuntimeError" in data["reason"]

    @pytest.mark.asyncio
    async def test_ordinary_throttle_does_not_pollute_the_events_table(
        self, db_repository, tmp_path: Path
    ):
        """This batch just cut events writes by forty percent — failure events are only allowed to appear on real
        failures."""
        repo = db_repository
        await _make_leader(repo, "sess-noevent")
        t = _transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-noevent",
            "transcript_path": str(t),
        }
        for _ in range(5):
            await translator.handle_event(payload)
        events = await repo.list_events(limit=200)
        assert not [e for e in events if e.type == CAPTURE_FAILED_EVENT]


# ============================================================
# 3. Every kind of skip has its own name, and the receipt can carry it out
# ============================================================


class TestSkipReasonsAreNamed:
    @pytest.mark.asyncio
    async def test_success_reports_no_skip_reason(self, db_repository, tmp_path: Path):
        repo = db_repository
        await _make_leader(repo, "sess-ok")
        t = _transcript(tmp_path)
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-ok",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        assert result["leader_usage"] is not None
        assert result["leader_usage_skip"] is None

    @pytest.mark.asyncio
    async def test_no_leader_row_is_named(self, db_repository, tmp_path: Path):
        repo = db_repository
        t = _transcript(tmp_path)
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-nobody",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        assert result["leader_usage"] is None
        assert result["leader_usage_skip"] == "no-leader-row"

    @pytest.mark.asyncio
    async def test_missing_transcript_is_named(self, db_repository, tmp_path: Path):
        repo = db_repository
        await _make_leader(repo, "sess-nofile")
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-nofile",
                "transcript_path": str(tmp_path / "nope.jsonl"),
                "trigger": "manual",
            }
        )
        assert result["leader_usage"] is None
        assert result["leader_usage_skip"] == "no-transcript"

    @pytest.mark.asyncio
    async def test_throttled_stop_is_named(self, db_repository, tmp_path: Path):
        """"None" once simultaneously meant success with no data / throttled / errored — now throttling has its own
        name."""
        repo = db_repository
        await _make_leader(repo, "sess-thrname")
        t = _transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-thrname",
            "transcript_path": str(t),
        }
        first = await translator.handle_event(payload)
        assert first["leader_usage_skip"] is None
        second = await translator.handle_event(payload)
        assert second["leader_usage"] is None
        assert second["leader_usage_skip"] == "throttled"

    @pytest.mark.asyncio
    async def test_transcript_without_usage_rows_is_named(self, db_repository, tmp_path: Path):
        """no-data ≠ zero: write no column at all, but be able to state 'it is no data, not that it did not run'."""
        repo = db_repository
        leader = await _make_leader(repo, "sess-empty")
        empty = tmp_path / "empty.jsonl"
        empty.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8",
        )
        result = await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-empty",
                "transcript_path": str(empty),
                "trigger": "manual",
            }
        )
        assert result["leader_usage_skip"] == "no-usage-rows"
        fetched = await repo.get_agent(leader.id)
        assert fetched.tokens_measured_at is None  # Still writes no column at all

    @pytest.mark.asyncio
    async def test_meter_reports_the_same_names(self, tmp_path: Path):
        """The collector itself must also be able to state the reason; it cannot be left to the caller to guess."""
        meter = leader_usage.SessionUsageMeter()
        t = _transcript(tmp_path)
        snap, reason = meter.capture_or_reason("s1", t, force=True)
        assert snap is not None
        assert reason is None
        snap, reason = meter.capture_or_reason("s1", t, force=False)
        assert snap is None
        assert reason == leader_usage.SKIP_THROTTLED
        snap, reason = meter.capture_or_reason("s2", tmp_path / "absent.jsonl", force=True)
        assert snap is None
        assert reason == leader_usage.SKIP_UNREADABLE


# ============================================================
# 4. The premises of the fallback path must really be able to reach the server side
# ============================================================


class TestSlugFallbackPrerequisite:
    def test_stripped_payload_still_carries_cwd(self):
        """``locate_main_transcript``'s slug fallback requires cwd + session_id to hold.

        Its docstring states it covers exactly the tier of 'an oversized payload stripped down to the must-keep fields',
        yet
        ``send_event.ESSENTIAL_FIELDS`` happens to have no ``cwd`` — once stripping happens, the fallback's
        premises go with it, so this sole retreat **necessarily fails in the very scenario it was born for**.
        This is a self-contradiction, not a trade-off.
        """
        from aiteam.hooks import send_event

        assert "cwd" in send_event.ESSENTIAL_FIELDS

    def test_fallback_locates_transcript_from_cwd_alone(self, tmp_path: Path, monkeypatch):
        """After stripping, with only session_id + cwd remaining, positioning must still hold (the path fields are
        unusable)."""
        cwd = "/Users/x/Desktop/proj"
        projects = tmp_path / "projects"
        (projects / session_probe.project_slug(cwd)).mkdir(parents=True)
        real = _transcript(projects / session_probe.project_slug(cwd), name="sid-77.jsonl")
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: projects)
        truncated = "/x" * 400 + "...(truncated)"
        assert leader_usage.locate_main_transcript(truncated, cwd=cwd, session_id="sid-77") == real
