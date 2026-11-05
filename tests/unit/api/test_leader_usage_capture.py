"""Leader main-session token usage capture tests (attribution v1, phase 4).

The skeleton of this test is exactly the three traps named in design §3.3 — each is of the 'code looks runnable, yet the
numbers
are fake' kind, so each has its own set of targeted cases rather than being covered incidentally inside end-to-end
cases:

* Trap (1) **snapshot overwrite**: the main session transcript is a cumulative file; accumulating it would inflate
multiplicatively with rounds.
  Pin = force triggering 10 times leaves the values unchanged (design R4's acceptance, verbatim) + after the file grows,
  take the new cumulative value.
* Trap (2) **throttling**: Stop triggers on every round; full parsing grows linearly more expensive with the session.
  Pin = the number of parses within the throttling window is 0 (not 'a little less', it is 0).
* Trap (3) **synthetic rows**: the ``model="<synthetic>"`` rows left by compact pollute model.
  Pin = in any path, the Leader row's model never becomes a placeholder.

see docs/token-attribution-v1-design.md §3.3 / §7 phase 4
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from aiteam.api import leader_usage, session_probe
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.services import token_attribution

SYNTHETIC = session_probe.SYNTHETIC_MODEL


# ============================================================
# Fixtures / helpers
# ============================================================


def _assistant(req: str, *, model: str, inp: int, cache_c: int, cache_r: int, out: int) -> dict:
    return {
        "type": "assistant",
        "requestId": req,
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cache_c,
                "cache_read_input_tokens": cache_r,
                "output_tokens": out,
            },
        },
    }


def _write_lines(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _append_lines(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _main_transcript(tmp_path: Path, name: str = "sess.jsonl") -> Path:
    """The main-session transcript of two API calls: totals in=3 / out=600 / cc=2000 / cr=90000."""
    path = tmp_path / name
    _write_lines(
        path,
        [
            {"type": "user", "message": {"role": "user", "content": "go"}},
            _assistant("req_a", model="claude-opus-5", inp=1, cache_c=1000, cache_r=40000, out=5),
            # For streaming follow-up rows with the same requestId: output is an increasing snapshot, not an increment,
            # so only the last one counts
            _assistant("req_a", model="claude-opus-5", inp=1, cache_c=1000, cache_r=40000, out=100),
            _assistant("req_b", model="claude-opus-5", inp=2, cache_c=1000, cache_r=50000, out=500),
        ],
    )
    return path


EXPECTED = {
    "input_tokens": 3,
    "output_tokens": 600,
    "cache_creation_tokens": 2000,
    "cache_read_tokens": 90000,
}


def _touch(path: Path, *, seconds_ahead: float) -> None:
    """Push the mtime forward — simulating 'some more content was written this round'."""
    ts = path.stat().st_mtime + seconds_ahead
    os.utime(path, (ts, ts))


async def _make_leader(repo, session_id: str, *, name: str = "Leader"):
    agent = await repo.create_agent(
        team_id="team-leader",
        name=name,
        role="leader",
        source="hook",
        session_id=session_id,
    )
    await repo.update_agent(agent.id, status="busy")
    return agent


def _translator(repo) -> HookTranslator:
    return HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


class _ParseCounter:
    """Push the mtime forward — simulating 'some more content was written in this round'."""

    def __init__(self) -> None:
        self.calls = 0
        self._real = token_attribution.parse_transcript_usage

    def __call__(self, path):
        self.calls += 1
        return self._real(path)


def _count_parses(monkeypatch) -> _ParseCounter:
    """Swap the parser for a counting version (monkeypatch handles restoration, so a failed assertion will not
    pollute later cases)."""
    counter = _ParseCounter()
    monkeypatch.setattr(token_attribution, "parse_transcript_usage", counter)
    return counter


# ============================================================
# The criterion for trap (2) itself: the throttling decision is a pure function, pinned branch by branch
# ============================================================


class TestShouldMeasure:
    def _meter(self) -> leader_usage.SessionUsageMeter:
        return leader_usage.SessionUsageMeter(
            min_interval_seconds=300, mtime_advance_seconds=600
        )

    def test_first_time_always_measures(self):
        meter = self._meter()
        now = utc_now()
        assert meter.should_measure("s1", mtime=now, now=now) is True

    def test_force_bypasses_every_gate(self):
        meter = self._meter()
        now = utc_now()
        meter._remember("s1", measured_at=now, mtime=now)
        # The window has not arrived and the file has not moved — the only remaining reason to pass is force
        assert meter.should_measure("s1", mtime=now, now=now) is False
        assert meter.should_measure("s1", mtime=now, now=now, force=True) is True

    def test_unchanged_file_is_never_reparsed(self):
        """The file has not moved by a single byte -> do not measure, even if the window passed long ago.

        Re-parsing the same file necessarily yields the same snapshot (the parser is a pure function); this trip is pure
        waste.
        """
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert meter.should_measure("s1", mtime=t0, now=t0 + timedelta(hours=3)) is False

    def test_inside_window_with_fresh_bytes_is_skipped(self):
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert (
            meter.should_measure(
                "s1", mtime=t0 + timedelta(seconds=30), now=t0 + timedelta(seconds=30)
            )
            is False
        )

    def test_past_min_interval_measures(self):
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert (
            meter.should_measure(
                "s1", mtime=t0 + timedelta(seconds=301), now=t0 + timedelta(seconds=301)
            )
            is True
        )

    def test_long_turn_escapes_the_window_via_mtime_advance(self):
        """The window has not arrived yet, but this round has already been writing for 11 minutes — measuring once
        early is closer to reality than waiting dead for the window."""
        meter = self._meter()
        t0 = utc_now()
        meter._remember("s1", measured_at=t0, mtime=t0)
        assert (
            meter.should_measure(
                "s1", mtime=t0 + timedelta(seconds=660), now=t0 + timedelta(seconds=120)
            )
            is True
        )


# ============================================================
# Positioning: payload first, slug reverse lookup as fallback
# ============================================================


class TestLocateMainTranscript:
    def test_payload_path_wins_when_it_exists(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        assert leader_usage.locate_main_transcript(str(t)) == t

    def test_falls_back_to_slug_lookup_when_payload_path_unusable(
        self, tmp_path: Path, monkeypatch
    ):
        """The hook side truncates over-long paths and, when over the limit, strips them entirely — both cases must
        be covered."""
        cwd = "/Users/x/Desktop/proj"
        slug = session_probe.project_slug(cwd)
        projects = tmp_path / "projects"
        (projects / slug).mkdir(parents=True)
        real = _main_transcript(projects / slug, name="sid-1234.jsonl")
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: projects)

        truncated = "/some/very/long/path...(truncated)"
        assert (
            leader_usage.locate_main_transcript(truncated, cwd=cwd, session_id="sid-1234") == real
        )
        assert leader_usage.locate_main_transcript("", cwd=cwd, session_id="sid-1234") == real

    def test_returns_none_when_nothing_resolves(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: tmp_path)
        assert leader_usage.locate_main_transcript("", cwd="/nope", session_id="sid") is None


# ============================================================
# Trap (3) synthetic-row filtering
# ============================================================


class TestSyntheticFilter:
    def test_real_model_passes_through(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        assert leader_usage._resolve_model("claude-opus-5", t) == "claude-opus-5"

    def test_synthetic_placeholder_is_replaced_by_the_last_real_model(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        _append_lines(
            t,
            [_assistant("req_c", model=SYNTHETIC, inp=0, cache_c=0, cache_r=0, out=0)],
        )
        assert leader_usage._resolve_model(SYNTHETIC, t) == "claude-opus-5"

    def test_capture_never_reports_the_placeholder_as_model(self, tmp_path: Path):
        """Synthetic compact rows are the norm in a main session (measured 100% reproduction), so the capture side must
        withstand it itself.

        Deliberately do not assert the four-layer values: the measured usage of synthetic rows is all 0, and the totals
        before and after phase 0 fixed the filtering
        are the same, but asserting values would bind this test to one side's implementation. Here we only pin model.
        """
        t = _main_transcript(tmp_path)
        _append_lines(
            t,
            [_assistant("req_syn", model=SYNTHETIC, inp=7, cache_c=7, cache_r=7, out=7)],
        )
        meter = leader_usage.SessionUsageMeter()
        snap = meter.capture("s1", t, force=True)
        assert snap is not None
        assert snap.model != SYNTHETIC
        assert snap.model == "claude-opus-5"

    @pytest.mark.asyncio
    async def test_leader_row_never_gets_the_placeholder_model(
        self, db_repository, tmp_path: Path
    ):
        repo = db_repository
        leader = await _make_leader(repo, "sess-syn")
        t = _main_transcript(tmp_path)
        _append_lines(
            t,
            [_assistant("req_syn", model=SYNTHETIC, inp=0, cache_c=0, cache_r=0, out=0)],
        )
        await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sess-syn",
                "transcript_path": str(t),
                "trigger": "manual",
            }
        )
        fetched = await repo.get_agent(leader.id)
        assert fetched.model == "claude-opus-5"
        for field, value in EXPECTED.items():
            assert getattr(fetched, field) == value


# ============================================================
# Trap (1) snapshot overwrite (never accumulate)
# ============================================================


class TestSnapshotOverwrite:
    def test_agent_updates_are_absolute_values(self, tmp_path: Path):
        t = _main_transcript(tmp_path)
        snap = leader_usage.SessionUsageMeter().capture("s1", t, force=True)
        assert snap is not None
        updates = snap.as_agent_updates()
        for field, value in EXPECTED.items():
            assert updates[field] == value
        assert updates["transcript_path"] == str(t)
        assert updates["tokens_measured_at"] is not None
        # The four layers in separate columns, with no total — 'the total' is actually equal to 'the cache read volume',
        # and presenting it separately would distort (§1.2)
        assert "total_tokens" not in updates

    @pytest.mark.asyncio
    async def test_ten_forced_captures_do_not_accumulate(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        """Design R4's acceptance, verbatim: triggering 10 times repeatedly leaves the values unchanged.

        Go through PostCompact (the forced path) to ensure every trigger really parses once — if the last
        nine were blocked by throttling, the test would degenerate into 'measuring nothing'.
        """
        repo = db_repository
        leader = await _make_leader(repo, "sess-ten")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        counter = _count_parses(monkeypatch)
        for _ in range(10):
            await translator.handle_event(
                {
                    "hook_event_name": "PostCompact",
                    "session_id": "sess-ten",
                    "transcript_path": str(t),
                    "trigger": "manual",
                }
            )
            fetched = await repo.get_agent(leader.id)
            for field, value in EXPECTED.items():
                assert getattr(fetched, field) == value, f"{field} drifted — was it accumulated?"
        assert counter.calls == 10  # All 10 really parsed; they were not fobbed off by throttling

    @pytest.mark.asyncio
    async def test_growing_transcript_yields_the_new_cumulative_total(
        self, db_repository, tmp_path: Path
    ):
        """After the file grows, what is stored is the **new cumulative value**, not the sum of the old and new."""
        repo = db_repository
        leader = await _make_leader(repo, "sess-grow")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "PostCompact",
            "session_id": "sess-grow",
            "transcript_path": str(t),
        }
        await translator.handle_event(payload)
        _append_lines(
            t,
            [_assistant("req_c", model="claude-opus-5", inp=4, cache_c=10, cache_r=20, out=30)],
        )
        await translator.handle_event(payload)
        fetched = await repo.get_agent(leader.id)
        assert fetched.input_tokens == EXPECTED["input_tokens"] + 4
        assert fetched.output_tokens == EXPECTED["output_tokens"] + 30
        assert fetched.cache_creation_tokens == EXPECTED["cache_creation_tokens"] + 10
        assert fetched.cache_read_tokens == EXPECTED["cache_read_tokens"] + 20


# ============================================================
# Trap (2) throttling (end-to-end: the Stop path)
# ============================================================


class TestStopThrottle:
    @pytest.mark.asyncio
    async def test_inside_the_window_stop_parses_zero_times(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        repo = db_repository
        leader = await _make_leader(repo, "sess-thr")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-thr",
            "transcript_path": str(t),
        }

        first = await translator.handle_event(payload)
        assert first["leader_usage"] is not None  # The first measurement establishes the baseline
        after_first = await repo.get_agent(leader.id)

        counter = _count_parses(monkeypatch)
        for _ in range(5):
            # Each round, let the file 'write a little more'; otherwise, without changing the mtime, it would be blocked
            # anyway and the window itself could not be measured
            _touch(t, seconds_ahead=20)
            result = await translator.handle_event(payload)
            assert result["leader_usage"] is None

        assert counter.calls == 0  # Not parsed even once within the throttling window
        again = await repo.get_agent(leader.id)
        assert again.tokens_measured_at == after_first.tokens_measured_at

    @pytest.mark.asyncio
    async def test_forced_events_bypass_the_window(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        repo = db_repository
        await _make_leader(repo, "sess-force")
        t = _main_transcript(tmp_path)
        translator = _translator(repo)
        base = {"session_id": "sess-force", "transcript_path": str(t)}

        await translator.handle_event({**base, "hook_event_name": "Stop"})
        counter = _count_parses(monkeypatch)
        _touch(t, seconds_ahead=20)
        assert (
            await translator.handle_event({**base, "hook_event_name": "Stop"})
        )["leader_usage"] is None
        compacted = await translator.handle_event({**base, "hook_event_name": "PostCompact"})
        ended = await translator.handle_event({**base, "hook_event_name": "SessionEnd"})
        assert compacted["leader_usage"] is not None
        assert ended["leader_usage"] is not None
        assert counter.calls == 2  # The throttled attempt did not parse; the two forced freezes parsed once each


# ============================================================
# Mounting points and boundaries
# ============================================================


class TestMountPoints:
    @pytest.mark.asyncio
    async def test_session_start_backfills_five_columns_and_path(
        self, db_repository, tmp_path: Path
    ):
        """The Leader row previously had a transcript_path in 0/117 cases and all five token columns empty — the
        first measurement fills in both."""
        repo = db_repository
        leader = await _make_leader(repo, "sess-start")
        t = _main_transcript(tmp_path)
        await _translator(repo).handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "sess-start",
                "transcript_path": str(t),
                "cwd": str(tmp_path),
            }
        )
        fetched = await repo.get_agent(leader.id)
        for field, value in EXPECTED.items():
            assert getattr(fetched, field) == value
        assert fetched.tokens_measured_at is not None
        assert fetched.transcript_path == str(t)

    @pytest.mark.asyncio
    async def test_post_compact_locates_transcript_without_a_payload_path(
        self, db_repository, tmp_path: Path, monkeypatch
    ):
        """The PostCompact payload is the most easily trimmed — even with the path gone, it must be locatable by a
        slug reverse lookup."""
        repo = db_repository
        leader = await _make_leader(repo, "sid-9999")
        cwd = "/Users/x/Desktop/proj"
        projects = tmp_path / "projects"
        (projects / session_probe.project_slug(cwd)).mkdir(parents=True)
        _main_transcript(projects / session_probe.project_slug(cwd), name="sid-9999.jsonl")
        monkeypatch.setattr(leader_usage, "_projects_dir", lambda: projects)

        await _translator(repo).handle_event(
            {
                "hook_event_name": "PostCompact",
                "session_id": "sid-9999",
                "cwd": cwd,
                "compact_summary_chars": 12000,
            }
        )
        fetched = await repo.get_agent(leader.id)
        assert fetched.output_tokens == EXPECTED["output_tokens"]

    @pytest.mark.asyncio
    async def test_missing_transcript_writes_nothing(self, db_repository, tmp_path: Path):
        """no-data ≠ zero: if the file is not there, write no column at all; never record 0."""
        repo = db_repository
        leader = await _make_leader(repo, "sess-gone")
        await _translator(repo).handle_event(
            {
                "hook_event_name": "SessionEnd",
                "session_id": "sess-gone",
                "transcript_path": str(tmp_path / "not-here.jsonl"),
            }
        )
        fetched = await repo.get_agent(leader.id)
        assert fetched.input_tokens is None
        assert fetched.output_tokens is None
        assert fetched.cache_read_tokens is None
        assert fetched.tokens_measured_at is None

    @pytest.mark.asyncio
    async def test_sub_agent_rows_are_never_touched_by_the_leader_path(
        self, db_repository, tmp_path: Path
    ):
        """When there is no Leader row in the session, usage must never land on a sub-agent in the same session.

        The presentation layer must list the Leader and sub-agents in separate columns (they differ by two orders of
        magnitude); the capture layer first ensures these two kinds of
        rows never cross-contaminate.
        """
        repo = db_repository
        worker = await repo.create_agent(
            team_id="team-x",
            name="researcher",
            role="researcher",
            source="hook",
            session_id="sess-noleader",
        )
        t = _main_transcript(tmp_path)
        await _translator(repo).handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "sess-noleader",
                "transcript_path": str(t),
            }
        )
        fetched = await repo.get_agent(worker.id)
        assert fetched.input_tokens is None
        assert fetched.tokens_measured_at is None
