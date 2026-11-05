"""Batch 9 item (5): connecting worktree / background tasks / Plan mode / Skill — four blind spots — to observability.

All four kinds are new forms that 'CC has and the OS cannot see at all'. This batch makes only the minimal set — events
can come in,
numbers can be seen — no new tables, no new pages.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.api import background_jobs
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


class _RecordingBus:
    """As strict as production: an unrecognized event type blows up on the spot; the stub must not be more lenient
    than production."""

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


class TestInputSummaries:
    """Among 48K tool events, these tools previously left only a 200-character JSON wall."""

    @pytest.mark.parametrize(
        ("tool", "tool_input", "expected"),
        [
            ("Skill", {"skill": "os-workflow", "args": "x"}, "os-workflow"),
            ("SendMessage", {"to": "main", "summary": "Batch 9 complete", "message": "Long body text…"},
                "Batch 9 complete"),
            ("TaskCreate", {"subject": "Fix the bridge", "description": "d"}, "Fix the bridge"),
            ("TaskUpdate", {"subject": "Fix the bridge", "status": "completed"}, "Fix the bridge"),
            ("TaskStop", {"taskId": "wf_1", "reason": "Shutdown"}, "wf_1"),
            ("AskUserQuestion", {"question": "Should we change the criterion?"}, "Should we change the criterion?"),
            ("ExitPlanMode", {"plan": "Step one…"}, "Step one…"),
            ("EnterWorktree", {"name": "wt-a"}, "wt-a"),
            ("WebSearch", {"query": "claude hooks"}, "claude hooks"),
        ],
    )
    def test_the_identifying_field_wins(self, tool, tool_input, expected):
        t = HookTranslator(repo=None, event_bus=None)  # type: ignore[arg-type]
        assert t._extract_input_summary(tool, tool_input) == expected

    def test_unknown_tools_keep_the_old_fallback(self):
        t = HookTranslator(repo=None, event_bus=None)  # type: ignore[arg-type]
        assert t._extract_input_summary("Bash", {"command": "ls"}) == "ls"
        assert t._extract_input_summary("Edit", {"file_path": "/a/b.py"}) == "/a/b.py"

    def test_summaries_are_capped(self):
        t = HookTranslator(repo=None, event_bus=None)  # type: ignore[arg-type]
        assert len(t._extract_input_summary("ExitPlanMode", {"plan": "x" * 9999})) == 200


class TestDecisionMoments:
    @pytest.mark.asyncio
    async def test_plan_text_is_kept_whole_not_truncated(self, translator):
        tr, bus = translator
        plan = "Step one…" * 300
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "ExitPlanMode",
                "tool_input": {"plan": plan},
            }
        )
        (event,) = bus.of("decision.plan_presented")
        assert event["plan"] == plan  # The body is complete, not a 200-character summary
        assert event["plan_chars"] == len(plan)

    @pytest.mark.asyncio
    async def test_user_question_keeps_options_and_answer(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "AskUserQuestion",
                "tool_input": {"question": "Stop heartbeat writes?", "options": ["Stop", "Do not stop"]},
                "tool_response": "Stop",
            }
        )
        (event,) = bus.of("decision.user_asked")
        assert event["question"] == "Stop heartbeat writes?"
        assert event["options"] == ["Stop", "Do not stop"]
        assert event["answer"] == "Stop"

    @pytest.mark.asyncio
    async def test_empty_plan_records_nothing(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "ExitPlanMode",
                "tool_input": {},
            }
        )
        assert bus.of("decision.plan_presented") == []

    @pytest.mark.asyncio
    async def test_ordinary_tools_raise_no_decision_event(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )
        assert bus.of("decision.plan_presented") == []
        assert bus.of("decision.user_asked") == []


class TestWorktreeEvents:
    @pytest.mark.asyncio
    async def test_create_and_remove_record_their_own_fields(self, translator):
        tr, bus = translator
        # CC's two payload fields are asymmetric: create gives only name, remove gives only worktree_path.
        await tr.handle_event(
            {"hook_event_name": "WorktreeCreate", "session_id": SESSION, "name": "wt-batch9"}
        )
        await tr.handle_event(
            {
                "hook_event_name": "WorktreeRemove",
                "session_id": SESSION,
                "worktree_path": "/tmp/wt-batch9",
            }
        )
        assert bus.of("cc.worktree_created")[0]["name"] == "wt-batch9"
        assert bus.of("cc.worktree_removed")[0]["worktree_path"] == "/tmp/wt-batch9"


class TestBackgroundJobs:
    def _write(self, tmp_path, monkeypatch, records: list[dict]):
        jobs = tmp_path / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        for r in records:
            d = jobs / r["daemonShort"]
            d.mkdir()
            (d / "state.json").write_text(json.dumps(r), encoding="utf-8")
        monkeypatch.setattr(background_jobs, "_jobs_dir", lambda: jobs)

    def _record(self, short: str, **extra) -> dict:
        base = {
            "daemonShort": short,
            "sessionId": f"{short}-uuid",
            "state": "done",
            "tempo": "idle",
            "backend": "daemon",
            "cwd": "/Users/dev/AI team OS",
            "intent": "",
            "respawnFlags": ["--permission-mode", "auto", "--model", "fable"],
            "createdAt": "2026-07-15T04:09:27.012Z",
            "updatedAt": "2026-07-15T11:41:17.040Z",
            "firstTerminalAt": "2026-07-15T11:41:17.040Z",
        }
        base.update(extra)
        return base

    def test_reads_the_shape_cc_writes(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [self._record("44fbf725")])
        (job,) = background_jobs.read_jobs()
        assert job.job_id == "44fbf725"
        assert job.state == "done"
        assert job.model == "fable"  # Picked out of the flat respawnFlags
        assert job.created_at == datetime(2026, 7, 15, 4, 9, 27, 12000, tzinfo=UTC)

    def test_in_flight_is_absence_of_a_terminal_stamp(self, tmp_path, monkeypatch):
        """Do not guess state strings: local measurements show two terminal states, done / stopped,
        and nowhere in the CC binary is there an enumeration listing them together — a whitelist would inevitably
        miss some."""
        self._write(
            tmp_path,
            monkeypatch,
            [
                self._record("aaaa"),  # Has firstTerminalAt = terminal
                self._record("bbbb", state="running", firstTerminalAt=None),
                self._record("cccc", state="some never-seen state", firstTerminalAt=None),
            ],
        )
        assert {j.job_id for j in background_jobs.in_flight_jobs()} == {"bbbb", "cccc"}

    def test_cwd_filter_respects_path_boundaries(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            monkeypatch,
            [
                self._record("aaaa", firstTerminalAt=None),
                self._record("bbbb", firstTerminalAt=None, cwd="/Users/dev/AI team OS-backup"),
            ],
        )
        found = background_jobs.in_flight_jobs("/Users/dev/AI team OS")
        assert {j.job_id for j in found} == {"aaaa"}

    def test_corrupt_file_is_skipped(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, [self._record("aaaa")])
        bad = tmp_path / "jobs" / "bad"
        bad.mkdir()
        (bad / "state.json").write_text("{oops", encoding="utf-8")
        assert [j.job_id for j in background_jobs.read_jobs()] == ["aaaa"]

    def test_missing_directory_reads_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(background_jobs, "_jobs_dir", lambda: tmp_path / "nope")
        assert background_jobs.read_jobs() == []

    def test_real_machine_parses(self):
        for job in background_jobs.read_jobs():
            assert job.job_id
            assert isinstance(job.in_flight, bool)


class TestSendMessageMirror:
    """CC teammate direct messages are handled by CC's own mailbox; the OS's positioning is a read-only mirror.

    Deliberately **not written into channel_messages**: that table is the territory of the OS's own broadcast channel
    (planned but not yet enabled), whose semantics differ from teammate direct messages; merging them would be
    irreversible.
    """

    @pytest.mark.asyncio
    async def test_recipient_and_body_are_recorded(self, translator):
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "agent_type": "batch9",
                "tool_name": "SendMessage",
                "tool_input": {"to": "main", "summary": "Batch 9 complete", "message": "Detailed report body"},
            }
        )
        (event,) = bus.of("cc.message_sent")
        # The recipient is only recorded here — cc.tool_use stores only the tool name and a one-line summary
        assert event["recipient"] == "main"
        assert event["sender"] == "batch9"
        assert event["summary"] == "Batch 9 complete"
        assert event["message"] == "Detailed report body"

    @pytest.mark.asyncio
    async def test_long_body_is_capped_but_true_length_kept(self, translator):
        tr, bus = translator
        body = "x" * 9000
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "SendMessage",
                "tool_input": {"to": "worker", "message": body},
            }
        )
        (event,) = bus.of("cc.message_sent")
        assert len(event["message"]) == 4000
        assert event["message_chars"] == 9000

    @pytest.mark.asyncio
    async def test_structured_protocol_message_is_serialised_not_dropped(self, translator):
        """SendMessage's message can also be a protocol object (shutdown_response etc.)."""
        tr, bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "SendMessage",
                "tool_input": {"to": "lead", "message": {"type": "shutdown_response", "approve": True}},
            }
        )
        (event,) = bus.of("cc.message_sent")
        assert "shutdown_response" in event["message"]

    @pytest.mark.asyncio
    async def test_nothing_is_written_to_the_channel_lane(self, translator, repo):
        """Regression guardrail: the mirror must never quietly become a write entry point for a fifth message table."""
        tr, _bus = translator
        await tr.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "SendMessage",
                "tool_input": {"to": "main", "message": "hi"},
            }
        )
        assert await repo.list_channel_messages(channel="global") == []
