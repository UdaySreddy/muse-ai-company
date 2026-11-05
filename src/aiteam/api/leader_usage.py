"""Leader main-session token usage collection — attribution v1 stage 4.

Sub-agent usage is already recorded into the agents table's five columns by SubagentStop, while the main-session side is
a **complete blank**
(measured: the five token columns and transcript_path of all 117 leader rows are empty). The gap itself is easy to fill:
the main-session transcript sits right at ``~/.claude/projects/<slug>/<session_id>.jsonl``, and
:func:`aiteam.services.token_attribution.parse_transcript_usage` can parse it without modification.

What truly turns numbers into lies is **how they are persisted**. Three semantics each correspond to a pitfall; do not
simplify them in implementation:

1. **Snapshot overwrite, never accumulate.** A main-session transcript is a **cumulative file** — every parse yields
   the total "from session start to now", not this round's increment. So persistence can only overwrite; copying the
   sub-
   agent's "write once at stop" accumulation mindset would inflate the numbers by however many rounds a session has.
   All writes in this module go through :meth:`UsageSnapshot.as_agent_updates`, where everything is assignment
   and there is not a single ``+=`` — that is no coincidence; it is the only permitted form.
2. **Throttling.** ``Stop`` fires on every conversation round, while a full parse grows linearly more expensive with the
   session (measured 45.1 MB /
   0.18 s). So ``Stop`` goes through the :meth:`SessionUsageMeter.should_measure` gate;
   ``SessionEnd`` / ``PostCompact`` / ``SessionStart`` force a freeze-frame with ``force=True``.
3. **Synthetic-row filtering.** Compaction leaves ``model="<synthetic>"`` assistant rows
   in the transcript. Measured in practice, these rows' four usage fields are **all 0** (they do not inflate tokens),
   but
   ``parse_transcript_usage`` does not skip them when taking the model, so the model parsed from a main session
   comes out as ``<synthetic>`` (reproduced in practice on this machine's 54d51683 transcript; the real model was
   claude-fable-5).
   This module adds a caller-side safety layer with ``session_probe.SYNTHETIC_MODEL``; see :func:`_resolve_model`.

Measured compact behavior (stage 4 archaeology along the way, answering design §8 open question 2): compact **appends in
place**
one ``system/compact_boundary`` + one ``user/isCompactSummary`` row, and **does not truncate, replace,
or archive** — a single 45.1 MB transcript holds 7 compactions, with rows from 19 days ago still present as-is.
So the ``PostCompact`` forced freeze-frame is not "rescuing data about to disappear", but ① freezing the moment of
compaction;
② covering the handover point where "after compact/resume the session starts a new file" (the new file replays an
identical
compact_boundary, but the replayed assistant rows have all-zero usage, so parsing the two files separately does not
double-count).

**Presentation constraint (only noted here; implementation belongs to stage 5)**: a Leader main session's magnitude far
exceeds the sum of all sub-agents
(measured for a single session: 850M vs the sub-agent total); mixing the two into one leaderboard would completely drown
the sub-agents' attribution results.
Leader rows (``role='leader'``) and sub-agent rows **must therefore be presented in separate columns and are not merged
by default**,
and both use the ``usage_sum`` caliber — which cannot be added to ``workflow_agents.tokens``' ``ctx_last``
caliber (design §0.2 / §3.3). This module deliberately **produces no total field**; the four layers are always presented
separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiteam.api import session_probe
from aiteam.clock import from_timestamp, utc_now
from aiteam.config import settings
from aiteam.services import token_attribution
from aiteam.types import TokenSource

# Cap on the in-process throttle state. One API process can span many sessions; the dict itself is small (two timestamps
# per session),
# but an unbounded dict in a long-lived process is a slow leak. When the cap is exceeded, it is discarded wholesale:
# losing the throttle state
# means at worst the next event parses once more; semantics are lossless (overwriting is idempotent).
_MAX_TRACKED_SESSIONS = 512

# The concrete reason when a capture was not persisted. Originally three outcomes shared one ``None``, making it
# impossible to distinguish afterwards between "blocked by throttling"
# and "file unreadable" — the 2026-08-03 investigation got stuck exactly on this point (see
# tests/unit/api/test_leader_usage_observability.py 's module header). The names themselves are the contract:
# the caller decides from them whether to raise a WARNING, so do not change the literals casually.
SKIP_THROTTLED = "throttled"
SKIP_UNREADABLE = "transcript-unreadable"
SKIP_NO_USAGE = "no-usage-rows"


def _projects_dir() -> Path:
    """Root directory of CC main-session transcripts — by convention, only session_probe keeps a copy of this."""
    return session_probe._claude_projects_dir()  # noqa: SLF001


def locate_main_transcript(
    payload_path: str,
    *,
    cwd: str = "",
    session_id: str = "",
) -> Path | None:
    """Resolve the main-session transcript for a hook payload, or None.

    The path in the payload takes priority, but **you must first confirm it really is on disk**: the hook side appends
    ``...(truncated)`` to over-long paths (send_event._trim_payload), and oversized payloads are stripped wholesale down
    to the must-keep fields,
    so in both cases the path obtained is unusable. The fallback follows the existing ``<slug>/<session_id>.jsonl``
    convention (``session_probe.session_last_active`` uses the same one), so that even ``PostCompact``,
    whose payload is the most easily trimmed, can still locate the file.
    """
    candidate = (payload_path or "").strip()
    if candidate:
        path = Path(candidate)
        if path.is_file():
            return path
    if cwd and session_id:
        path = _projects_dir() / session_probe.project_slug(cwd) / f"{session_id}.jsonl"
        if path.is_file():
            return path
    return None


def _resolve_model(raw: str, transcript: Path) -> str:
    """Drop the compact placeholder model, falling back to the last real one.

    TODO(stage 0): once ``parse_transcript_usage`` reuses session_probe's synthetic-row filtering
    (design §1.3 / §7 stage 0), this caller-side fallback degrades into a redundant safety layer and can be consolidated
    together with this comment.
    Until then, main-session collection must not blindly trust usage['model'] — main sessions have synthetic rows 100%
    of the time.
    """
    if raw and raw != session_probe.SYNTHETIC_MODEL:
        return raw
    return session_probe.read_session_model(str(transcript))


@dataclass(frozen=True)
class UsageSnapshot:
    """The result of one main-session usage measurement — a **snapshot of the current cumulative value**, not an
    increment.

    The four layers are listed separately and deliberately **have no total field**: any "total" is effectively a synonym
    for "cache-read amount"
    (measured: cache_read accounts for 95.6%); presenting it alone would systematically distort cross-model and
    cross-path comparisons
    (design §1.2). For a total, go to stage 2's TokenAttribution and take it together with its caliber and denominator.
    """

    session_id: str
    transcript_path: str
    measured_at: datetime
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    api_calls: int
    model: str
    forced: bool

    def as_agent_updates(self) -> dict[str, Any]:
        """Fields to write onto the leader row — every one is an assignment.

        The single landing point of the overwrite semantics. Any expression of the form ``old + new`` appearing here
        treats the cumulative file as
        an incremental file, making the values inflate linearly with the round count (design §3.3 pitfall 1, R4).
        """
        updates: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "tokens_measured_at": self.measured_at,
            # Leader rows previously had a transcript_path in 0/117 cases; when usage is captured, the source path is
            # persisted along with it,
            # so that coverage and backfill have evidence to check against (design §2.6 backfill item).
            "transcript_path": self.transcript_path,
            "tokens_source": TokenSource.TRANSCRIPT.value,
        }
        if self.model:
            updates["model"] = self.model
        return updates

    def as_summary(self) -> dict[str, Any]:
        """Compact form for hook responses / logs (likewise contains no total)."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "api_calls": self.api_calls,
            "model": self.model,
            "forced": self.forced,
            "metric": "usage_sum",
        }


@dataclass
class _LastMeasure:
    """The time of the last **parse attempt** and the file's mtime at that time."""

    measured_at: datetime
    mtime: datetime | None


class SessionUsageMeter:
    """Throttled, snapshot-overwrite usage probe for main-session transcripts.

    Keeps each session's last measurement fingerprint in-process and decides from it whether this round's ``Stop``
    should really parse the file.
    Losing the state (process restart, cap-exceeded clearing) does not affect correctness: overwriting is idempotent,
    and the worst case is one extra parse.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: int | None = None,
        mtime_advance_seconds: int | None = None,
    ) -> None:
        self._min_interval = timedelta(
            seconds=(
                settings.LEADER_USAGE_MIN_INTERVAL_SECONDS
                if min_interval_seconds is None
                else min_interval_seconds
            )
        )
        self._mtime_advance = timedelta(
            seconds=(
                settings.LEADER_USAGE_MTIME_ADVANCE_SECONDS
                if mtime_advance_seconds is None
                else mtime_advance_seconds
            )
        )
        self._last: dict[str, _LastMeasure] = {}

    def should_measure(
        self,
        session_id: str,
        *,
        mtime: datetime | None,
        now: datetime,
        force: bool = False,
    ) -> bool:
        """Whether this event is allowed to pay for a full transcript parse.

        The order of the criteria is itself semantic; do not reorder:

        1. ``force`` (SessionStart / SessionEnd / PostCompact) always passes straight through — a forced
           freeze-frame is not constrained by any window, otherwise "freeze-frame" would be a misnomer.
        2. This session has not been measured yet -> measure (the first measurement establishes the baseline; only then
           does the Leader row go from empty to valued).
        3. **The file has not moved a single byte -> do not measure.** Re-parsing the same file necessarily yields the
           same snapshot;
           the parser is a pure function, so this trip would be pure waste.
        4. More than ``min_interval`` has passed since the last parse -> measure.
        5. The transcript's on-disk time has advanced by more than ``mtime_advance`` since the last measurement ->
           measure.
           This rule covers long tasks where "one round ran for a very long time": the window has not arrived yet, but
           the amount written in during that period
           may already be considerable.
        """
        if force:
            return True
        last = self._last.get(session_id)
        if last is None:
            return True
        if mtime is not None and last.mtime is not None and mtime <= last.mtime:
            return False
        if now - last.measured_at >= self._min_interval:
            return True
        return bool(
            mtime is not None
            and last.mtime is not None
            and (mtime - last.mtime) >= self._mtime_advance
        )

    def capture(
        self,
        session_id: str,
        transcript: Path,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> UsageSnapshot | None:
        """Parse the transcript and return a snapshot, or None when skipped.

        None has three meanings, and in all of them the caller need only "write nothing": blocked by throttling / file
        unreadable /
        the file has no usage rows yet. **Never write any of these three as 0** — no-data ≠ zero.
        To know which one it is, use :meth:`capture_or_reason`.
        """
        return self.capture_or_reason(session_id, transcript, force=force, now=now)[0]

    def capture_or_reason(
        self,
        session_id: str,
        transcript: Path,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> tuple[UsageSnapshot | None, str | None]:
        """Same as :meth:`capture`, but says **why** when it hands back nothing.

        The persistence semantics are identical to ``capture`` word for word (in those three outcomes, still not a
        single column is written); the only extra is
        a reason string. Distinguishing them is information with a cost: ``throttled`` is the most common normal outcome
        in steady state,
        while ``transcript-unreadable`` appearing on a ``force=True`` forced freeze-frame
        means this moment's watermark is permanently lost — the two used to look exactly alike.
        """
        now = now or utc_now()
        try:
            mtime = from_timestamp(transcript.stat().st_mtime)
        except OSError:
            return None, SKIP_UNREADABLE
        if not self.should_measure(session_id, mtime=mtime, now=now, force=force):
            return None, SKIP_THROTTLED

        usage = token_attribution.parse_transcript_usage(transcript)
        # Recorded **after** parsing and before returning: what is throttled is the "parse" action, so even if this
        # parse
        # gains nothing (a new session has no assistant rows yet), the timestamp must still be recorded; otherwise every
        # round's Stop would
        # rescan the same file all over again.
        self._remember(session_id, measured_at=now, mtime=mtime)
        if not usage:
            return None, SKIP_NO_USAGE
        snapshot = UsageSnapshot(
            session_id=session_id,
            transcript_path=str(transcript),
            measured_at=now,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            api_calls=int(usage.get("api_calls") or 0),
            model=_resolve_model(str(usage.get("model") or ""), transcript),
            forced=force,
        )
        return snapshot, None

    def _remember(self, session_id: str, *, measured_at: datetime, mtime: datetime | None) -> None:
        if len(self._last) >= _MAX_TRACKED_SESSIONS:
            self._last.clear()
        self._last[session_id] = _LastMeasure(measured_at=measured_at, mtime=mtime)
