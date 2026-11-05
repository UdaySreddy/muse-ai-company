"""The convention net for unifying the whole database on UTC — see docs/utc-unification-design.md.

This is not an ordinary functional test; it is a **machine check of the conventions**. The old dual wall clocks survived
for months precisely because they were wrong
silently: SQLite storage strips the offset, and cross-domain comparisons return an answer off by 8 hours without
raising. Here,
every assertion corresponds to one convention in the design; any one quietly reverted will go red here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import DateTime, select

from aiteam.clock import ensure_utc, from_timestamp, parse_utc, to_naive_utc, utc_now
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import Base, EventModel
from aiteam.storage.repository import StorageRepository
from aiteam.storage.utc_type import UtcDateTime

SRC = Path(__file__).resolve().parents[3] / "src" / "aiteam"


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


# --------------------------------------------------------------------------
# (1) schema: every time column must go through the single conversion point
# --------------------------------------------------------------------------
class TestEveryColumnGoesThroughTheOneConverter:
    def test_no_datetime_column_bypasses_utc_datetime(self) -> None:
        """Miss one column and that column falls back to the old world — silently naive, and read out without tzinfo."""
        offenders = [
            f"{table.name}.{col.name}"
            for table in Base.metadata.sorted_tables
            for col in table.columns
            if isinstance(col.type, DateTime) and not isinstance(col.type, UtcDateTime)
        ]
        assert not offenders, f"These columns bypassed UtcDateTime: {offenders}"

    def test_the_inventory_is_not_silently_shrinking(self) -> None:
        """The column count is the anchor of the design document §3 inventory. A change is legal in itself, but it
        must be conscious."""
        count = sum(
            1
            for table in Base.metadata.sorted_tables
            for col in table.columns
            if isinstance(col.type, UtcDateTime)
        )
        assert count >= 82, (
            f"Only {count} UtcDateTime columns remain (the design document records 82) —"
            "if a table/column really was deleted, please also update docs/utc-unification-design.md §3"
        )


# --------------------------------------------------------------------------
# (2) Round-trip: what is written reads back as the same moment, and always carries a UTC label
# --------------------------------------------------------------------------
class TestRoundTrip:
    @staticmethod
    async def _write_and_read(repo: StorageRepository, moment: datetime | None):
        kwargs = {"timestamp": moment} if moment is not None else {}
        async with get_session(repo._db_url) as session:
            session.add(
                EventModel(id="e1", type="task.created", source="t", data={}, **kwargs)
            )
        async with get_session(repo._db_url) as session:
            return (await session.execute(select(EventModel))).scalar_one().timestamp

    @pytest.mark.asyncio()
    async def test_aware_non_utc_input_is_converted_not_truncated(
        self, repo: StorageRepository
    ) -> None:
        """Input with +08:00 must be **converted** to UTC, not dealt with by throwing the offset away.

        Throwing it away is exactly what the SQLite dialect used to do, and the cause of the dual wall clocks.
        """
        shanghai = timezone(timedelta(hours=8))
        moment = datetime(2026, 7, 28, 17, 0, 0, tzinfo=shanghai)
        stored = await self._write_and_read(repo, moment)
        assert stored == moment, "What reads back must be the same moment"
        assert stored.tzinfo is UTC, "The read side is uniformly aware UTC"
        assert stored.hour == 9, "17:00+08:00 stored in is 09:00 UTC"

    @pytest.mark.asyncio()
    async def test_naive_input_is_taken_as_utc(self, repo: StorageRepository) -> None:
        """Naive input is treated as 'already UTC' — this is the storage convention, not a guess."""
        moment = datetime(2026, 7, 28, 9, 0, 0)
        stored = await self._write_and_read(repo, moment)
        assert stored == moment.replace(tzinfo=UTC)

    @pytest.mark.asyncio()
    async def test_default_stamped_rows_come_back_aware(
        self, repo: StorageRepository
    ) -> None:
        """Even column defaults must be aware — the default is the easiest place to miss."""
        stored = await self._write_and_read(repo, None)
        assert stored.tzinfo is UTC
        # Only seconds away from real UTC; if the local wall clock were written by mistake, this would be off by a
        # timezone offset
        assert abs((utc_now() - stored).total_seconds()) < 60


# --------------------------------------------------------------------------
# (3) API contract: the string carries its own convention with it
# --------------------------------------------------------------------------
class TestWireFormat:
    @pytest.mark.asyncio()
    async def test_isoformat_carries_the_offset(self, repo: StorageRepository) -> None:
        """A time string without an offset makes the consumer guess — guesswork is exactly what this change
        eliminates."""
        await repo.create_event("task.created", "t", {})
        events = await repo.list_events(limit=1)
        wire = events[0].timestamp.isoformat()
        assert wire.endswith("+00:00"), f"The time string in the response carries no offset: {wire}"
        # And it must be readable back, as is, to the same moment
        assert parse_utc(wire) == events[0].timestamp


# --------------------------------------------------------------------------
# (4) The clock module's own semantics
# --------------------------------------------------------------------------
class TestClockHelpers:
    def test_utc_now_is_aware(self) -> None:
        assert utc_now().tzinfo is UTC

    def test_from_timestamp_labels_utc_instead_of_local(self) -> None:
        """A bare fromtimestamp attaches the local offset, which amounts to sending the value into another clock."""
        assert from_timestamp(0) == datetime(1970, 1, 1, tzinfo=UTC)

    def test_parse_utc_reads_bare_strings_as_utc(self) -> None:
        assert parse_utc("2026-07-28T09:00:00") == datetime(2026, 7, 28, 9, tzinfo=UTC)

    def test_parse_utc_normalises_every_spelling_of_one_instant(self) -> None:
        one = datetime(2026, 7, 28, 9, tzinfo=UTC)
        assert parse_utc("2026-07-28T09:00:00Z") == one
        assert parse_utc("2026-07-28T17:00:00+08:00") == one
        assert parse_utc("2026-07-28 09:00:00") == one

    def test_parse_utc_returns_none_rather_than_raising(self) -> None:
        assert parse_utc(None) is None
        assert parse_utc("") is None
        assert parse_utc("garbage") is None

    def test_to_naive_utc_shifts_before_dropping_the_offset(self) -> None:
        shanghai = timezone(timedelta(hours=8))
        assert to_naive_utc(datetime(2026, 7, 28, 17, tzinfo=shanghai)) == datetime(
            2026, 7, 28, 9
        )

    def test_ensure_utc_is_idempotent(self) -> None:
        moment = utc_now()
        assert ensure_utc(ensure_utc(moment)) == moment
        assert ensure_utc(None) is None


# --------------------------------------------------------------------------
# (5) Source guard: no second clock may exist
# --------------------------------------------------------------------------
class TestNoSecondClockInSource:
    """Machine check — the value of this guard is higher than all the assertions above.

    The tests above prove 'it is correct now'; this one proves 'it will not quietly go wrong again'. The dual wall
    clocks back then were not
    decided by anyone; they accumulated module by module as host-local wall clocks were casually written down — without
    a guard, the same
    thing will happen again, and likewise nobody will see it.
    """

    # hooks are standalone processes running outside the aiteam package (and must be byte-identical to plugin/hooks,
    # I1),
    # so they cannot import aiteam.clock; they only use UTC anyway and need no guard.
    EXEMPT = {"clock.py", "utc_type.py"}

    def _offenders(self, pattern: str) -> list[str]:
        hits: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name in self.EXEMPT or "hooks" in path.relative_to(SRC).parts:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(pattern, line):
                    hits.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
        return hits

    def test_no_bare_datetime_now(self) -> None:
        offenders = self._offenders(r"\bdatetime\.now\(")
        assert not offenders, (
            "Found a wall-clock call bypassing aiteam.clock; always use utc_now():\n"
            + "\n".join(offenders)
        )

    def test_no_bare_fromtimestamp(self) -> None:
        offenders = self._offenders(r"\bdatetime\.fromtimestamp\(")
        assert not offenders, (
            "A bare fromtimestamp attaches a local offset to the moment; use from_timestamp():\n"
            + "\n".join(offenders)
        )

    def test_no_hand_rolled_offset_stripping(self) -> None:
        """``astimezone().replace(tzinfo=None)`` is the old action of moving a value back to the local clock."""
        offenders = self._offenders(r"astimezone\(\)\s*\.replace\(tzinfo=None\)")
        assert not offenders, "Manual offset removal has been replaced by to_naive_utc():\n" + "\n".join(offenders)
