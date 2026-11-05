"""The behavioral contract of the legacy-data shifting script — see docs/utc-unification-design.md §6.

This script rewrites 320,000 cells in one pass and is executed by the creator personally, so its promises must be machine-checked:
**lossless** (sub-second digits not dropped), **reversible** (rollback restores byte for byte), **able to stop** (aborts when the order is reversed),
**able to recognize mixed conventions** (UTC rows already written by new code are excluded row by row; neither of the two shifts may touch them),
**runs only once** (idempotency marker + journal overwrite protection).

All cases run only against temporary databases in tmp_path; not one byte of the production database is touched.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_timestamps_utc.py"

_spec = importlib.util.spec_from_file_location("migrate_timestamps_utc", SCRIPT)
assert _spec and _spec.loader
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


LOCAL_SHIFT_HOURS = mig.shift_for(datetime(2026, 7, 28, 17, 0)).total_seconds() / 3600.0
IS_UTC_HOST = LOCAL_SHIFT_HOURS == 0


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """A minimal database containing only the events table, with row timestamps written by the local wall clock."""
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("create table events (id integer primary key, timestamp text)")
    conn.commit()
    return conn


def _seed(conn: sqlite3.Connection, values: list[str]) -> None:
    conn.executemany("insert into events (timestamp) values (?)", [(v,) for v in values])
    conn.commit()


def _reports(conn: sqlite3.Connection, now_local: datetime):
    original = mig.LOCAL_COLUMNS
    mig.LOCAL_COLUMNS = {"events": ("timestamp",)}
    try:
        return mig.collect(conn, now_local)
    finally:
        mig.LOCAL_COLUMNS = original


class TestLosslessShift:
    def test_subsecond_digits_survive(self, db: sqlite3.Connection) -> None:
        """Second truncation would erase the microsecond digits of 320,000 rows, and the event ledger orders same-second precedence by timestamp."""
        _seed(db, ["2026-07-28 17:01:25.068573"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.commit()
        got = db.execute("select timestamp from events").fetchone()[0]
        assert got == "2026-07-28 09:01:25.068573"

    def test_values_without_subseconds_are_fine_too(self, db: sqlite3.Connection) -> None:
        _seed(db, ["2026-07-28 17:01:25"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.commit()
        assert db.execute("select timestamp from events").fetchone()[0] == "2026-07-28 09:01:25"

    def test_shift_crosses_the_date_boundary(self, db: sqlite3.Connection) -> None:
        _seed(db, ["2026-07-28 03:30:00.500000"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.commit()
        assert db.execute("select timestamp from events").fetchone()[0] == "2026-07-27 19:30:00.500000"

    def test_half_hour_zones_are_supported(self, db: sqlite3.Connection) -> None:
        """Time zones like +05:30 are not theoretical corners — India is one."""
        _seed(db, ["2026-07-28 17:01:25.068573"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -5.5)}")
        db.commit()
        assert db.execute("select timestamp from events").fetchone()[0] == "2026-07-28 11:31:25.068573"

    def test_round_trip_is_byte_exact(self, db: sqlite3.Connection) -> None:
        values = [
            "2026-07-28 17:01:25.068573",
            "2026-03-13 11:59:54.000001",
            "2026-06-23 20:50:09",
        ]
        _seed(db, values)
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', 8)}")
        db.commit()
        assert [r[0] for r in db.execute("select timestamp from events order by id")] == values


class TestWidthGuard:
    def test_date_only_values_abort_instead_of_being_nulled(
        self, db: sqlite3.Connection
    ) -> None:
        """Prefix splitting on a date-only value builds an illegal string and SQLite silently gives NULL — that is data loss."""
        _seed(db, ["2026-07-28"])
        reports = _reports(db, datetime(2026, 7, 28, 17, 0))
        with pytest.raises(SystemExit, match="Cannot safely split"):
            mig.assert_uniform_width(db, reports)


@pytest.mark.skipif(IS_UTC_HOST, reason="The host timezone is UTC; there is no local/UTC distinction to judge")
class TestOrderGuard:
    """What the guardrail blocks is the reversed order of "deploy new code first, run the shift afterwards"."""

    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    def test_local_written_rows_are_positively_proven(self, db: sqlite3.Connection) -> None:
        """A UTC clock cannot write a future value — so a max later than the current UTC moment is positive proof, not a heuristic."""
        now_local = self._now()
        _seed(db, [now_local.strftime("%Y-%m-%d %H:%M:%S.%f")])
        reports = _reports(db, now_local)
        assert reports[0].guard.startswith("Confirmed local")
        passed, notes = mig.verdict(reports, now_local)
        assert passed, notes

    def test_utc_written_rows_block_the_migration(self, db: sqlite3.Connection) -> None:
        """Shifting a database that is already UTC pushes every row earlier by one timezone again."""
        now_local = self._now()
        now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)
        _seed(db, [now_utc_naive.strftime("%Y-%m-%d %H:%M:%S.%f")])
        reports = _reports(db, now_local)
        passed, notes = mig.verdict(reports, now_local)
        assert not passed, notes
        assert any("New code is likely already writing to the database" in n for n in notes)

    def test_all_cold_data_is_refused_rather_than_guessed(
        self, db: sqlite3.Connection
    ) -> None:
        """When everything is cold data there is no way to judge — stop and ask a human to confirm rather than guessing for them."""
        now_local = self._now()
        _seed(db, [(now_local - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S.%f")])
        reports = _reports(db, now_local)
        passed, notes = mig.verdict(reports, now_local)
        assert not passed
        assert any("Please confirm manually" in n for n in notes)


class TestManifest:
    def test_ecosystem_tables_are_never_in_the_shift_list(self) -> None:
        """The ecosystem domain already writes UTC; shifting it pushes those rows earlier by one timezone."""
        offenders = [t for t in mig.LOCAL_COLUMNS if t.startswith("ecosystem")]
        assert not offenders, offenders
        assert "pipeline_stage_history" not in mig.LOCAL_COLUMNS

    def test_manifest_matches_the_design_document(self) -> None:
        """The checklist and design document §3.1 are the same ledger; a count mismatch means one side was changed unilaterally."""
        columns = sum(len(v) for v in mig.LOCAL_COLUMNS.values())
        assert len(mig.LOCAL_COLUMNS) == 20, f"Table count changed: {len(mig.LOCAL_COLUMNS)}"
        assert columns == 46, f"Column count changed: {columns}"

    def test_every_table_declares_a_birth_column(self) -> None:
        """The birth column is the foundation of per-row convention judgment; a missing table means that table's rows are judged by no one."""
        assert set(mig.ROW_BIRTH_COLUMN) == set(mig.LOCAL_COLUMNS)
        for table, birth in mig.ROW_BIRTH_COLUMN.items():
            assert birth in mig.LOCAL_COLUMNS[table], f"{table}.{birth} is not in the migration list"

    def test_birth_column_is_classified_before_the_columns_that_anchor_on_it(self) -> None:
        """In-place updated columns anchor on the birth column, so the birth column must be first in its table and judged first."""
        for table, columns in mig.LOCAL_COLUMNS.items():
            assert columns[0] == mig.ROW_BIRTH_COLUMN[table], f"{table}'s birth column is not first"

    def test_after_birth_whitelist_excludes_columns_that_predate_creation(self) -> None:
        """Once the whitelist mixes in a column that "may predate creation", the oracle judges against a fake invariant.

        Measured counterexample: workflow_*'s started_at/queued_at/completed_at come from CC status files'
        epoch, earliest 2026-06-23, several weeks earlier than the row's created_at (2026-07-06);
        scheduled_tasks.next_run_at simply records the future.
        """
        forbidden = {
            ("workflow_runs", "started_at"),
            ("workflow_runs", "completed_at"),
            ("workflow_agents", "started_at"),
            ("workflow_agents", "queued_at"),
            ("scheduled_tasks", "next_run_at"),
        }
        assert not (mig.AFTER_BIRTH_COLUMNS & forbidden)
        for table, column in mig.AFTER_BIRTH_COLUMNS:
            assert column in mig.LOCAL_COLUMNS[table]
            assert column != mig.ROW_BIRTH_COLUMN[table]


# ---------------------------------------------------------------------------
# Mixed conventions — the production reality of 2026-07-28: UTC new code was merged into master and is an editable install,
# so every newly started process writes UTC rows into a not-yet-shifted database. All cases below are fault injections
# made against this reality.
# ---------------------------------------------------------------------------
OFFSET = timedelta(hours=-LOCAL_SHIFT_HOURS)  # local − UTC; +8h on UTC+8


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["migrate_timestamps_utc.py", *argv])
    return mig.main()


def _dump(conn: sqlite3.Connection, table: str, columns: str) -> list[tuple]:
    return list(conn.execute(f"select rowid, {columns} from {table} order by rowid"))


def _read_journal(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture()
def mixed(tmp_path: Path) -> tuple[Path, sqlite3.Connection, datetime]:
    """A database the old code is still writing and new code has just mixed a few rows into — i.e. the one --apply actually faces.

    events (append-only): a run of local rows → 3 UTC rows written by new code → a few more local rows.
    teams (with in-place update columns): one row whose update value judges as UTC, one row where both readings are self-consistent (goes to human review),
    one row fully clean.
    """
    now = datetime.now().replace(microsecond=246810)
    path = tmp_path / "mixed.db"
    conn = sqlite3.connect(path)
    conn.execute("create table events (id integer primary key, timestamp text)")
    conn.execute(
        "create table teams (id integer primary key, created_at text, "
        "updated_at text, completed_at text)"
    )
    early = [now - timedelta(minutes=20) + timedelta(seconds=30 * i) for i in range(35)]
    utc_rows = [now - timedelta(minutes=2), now - timedelta(seconds=110), now - timedelta(seconds=100)]
    late = [now - timedelta(seconds=60), now - timedelta(seconds=30), now]
    conn.executemany(
        "insert into events (timestamp) values (?)",
        [(_stamp(m),) for m in early]
        + [(_stamp(m - OFFSET),) for m in utc_rows]  # Written by new code: the UTC expression of the same instant
        + [(_stamp(m),) for m in late],
    )
    conn.executemany(
        "insert into teams (created_at, updated_at, completed_at) values (?, ?, ?)",
        [
            # ① The update value is a full timezone earlier than creation — can only be the UTC convention
            (_stamp(now - timedelta(minutes=5)), _stamp(now - timedelta(seconds=90) - OFFSET), None),
            # ② A row created three days ago whose update value falls in the risk band: neither reading violates "updates do not predate creation"
            (_stamp(now - timedelta(days=3)), _stamp(now - timedelta(seconds=100) - OFFSET), None),
            # ③ Fully clean
            (
                _stamp(now - timedelta(minutes=4)),
                _stamp(now - timedelta(minutes=2)),
                _stamp(now - timedelta(minutes=1)),
            ),
        ],
    )
    conn.commit()
    return path, conn, now


def _contamination(conn: sqlite3.Connection, now_local: datetime) -> dict:
    reports = mig.collect(conn, now_local)
    found, _, _ = mig.detect_contamination(conn, reports, LOCAL_SHIFT_HOURS, now_local)
    return found


@pytest.mark.skipif(IS_UTC_HOST, reason="The host timezone is UTC; there is no local/UTC distinction to judge")
class TestMixedCaliberDetection:
    """Mixed conventions are a **per-row** problem, not a binary "pass/block" problem."""

    def test_minority_utc_rows_are_pinned_down_row_by_row(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """Minority UTC rows affect neither the column max nor the whole-database max — only the rowid write order catches them."""
        _, conn, now = mixed
        found = _contamination(conn, now)
        assert found["events.timestamp"].excluded == [36, 37, 38]
        assert found["events.timestamp"].undecidable == []

    def test_clean_rows_are_never_excluded(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """Exclusion has a cost: missing one pushes it 8 hours earlier; wrongly excluding one leaves a row forever 8 hours later."""
        _, conn, now = mixed
        found = _contamination(conn, now)
        assert found["teams.created_at"].excluded == []
        assert found["teams.completed_at"].excluded == []

    def test_in_place_columns_are_judged_against_their_birth_column(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """rowid is meaningless for in-place update columns; what it relies on is the invariant "an update cannot predate creation"."""
        _, conn, now = mixed
        found = _contamination(conn, now)
        assert found["teams.updated_at"].excluded == [1]

    def test_undecidable_rows_are_listed_rather_than_silently_shifted(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """What cannot be judged is named and handed to human review — silently following the majority is exactly how such incidents happen."""
        _, conn, now = mixed
        found = _contamination(conn, now)
        pending = mig.all_undecidable(found)
        assert [(p["column"], p["rowid"]) for p in pending] == [("teams.updated_at", 2)]

    def test_legitimate_out_of_order_backfill_is_not_mistaken_for_a_timezone(
        self, db: sqlite3.Connection
    ) -> None:
        """The production task_memos.created_at has 7.1–8.7 hours of legitimate disorder (memory v2 batch backfill).

        An hour-level tolerance would treat them as a timezone fingerprint and exclude them wholesale — so the tolerance is given in minutes.
        """
        now = datetime.now()
        _seed(
            db,
            [
                _stamp(now - timedelta(minutes=5)),
                _stamp(now - timedelta(hours=8, minutes=31)),  # Backfill row, 8.5h backwards
                _stamp(now - timedelta(minutes=1)),
            ],
        )
        reports = _reports(db, now)
        found, since, evidence = mig.detect_contamination(db, reports, LOCAL_SHIFT_HOURS, now)
        assert evidence == []
        assert since is None
        assert found["events.timestamp"].excluded == []


@pytest.mark.skipif(IS_UTC_HOST, reason="The host timezone is UTC; there is no local/UTC distinction to judge")
class TestGuardSeesMinorities:
    def test_already_shifted_db_with_one_stray_local_row_is_not_waved_through(
        self, db: sqlite3.Connection
    ) -> None:
        """The old guardrail waves this through: one local value backfilled by old code tops the whole-database max at the local now.

        The real criterion must be the convention of the **majority** of recent writes; a minority cannot overturn the median.
        """
        now_local = datetime.now()
        now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)
        _seed(
            db,
            [_stamp(now_utc_naive - timedelta(seconds=5 * i)) for i in range(40, 0, -1)]
            + [_stamp(now_local)],  # A row just backfilled by a not-quite-dead old-code process
        )
        reports = _reports(db, now_local)
        assert any(r.guard.startswith("Confirmed local") for r in reports)  # The old criterion still "confirms"
        passed, notes = mig.verdict(reports, now_local)
        assert not passed, notes
        assert any("New code is likely already writing to the database" in n for n in notes)

    def test_guard_reports_how_many_rows_will_be_excluded(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """Mixed conventions are no longer a binary pass/block — the guardrail's duty is to confirm polluted rows are identified and excluded."""
        _, conn, now = mixed
        found = _contamination(conn, now)
        _, notes = mig.verdict(mig.collect(conn, now), now, found, ack_undecidable=True)
        assert any("events.timestamp: 3 rows" in n for n in notes)

    def test_undecidable_rows_block_until_acknowledged(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        _, conn, now = mixed
        found = _contamination(conn, now)
        reports = mig.collect(conn, now)
        passed, notes = mig.verdict(reports, now, found, ack_undecidable=False)
        assert not passed
        assert any("--ack-undecidable" in n for n in notes)
        passed, _ = mig.verdict(reports, now, found, ack_undecidable=True)
        assert passed


@pytest.mark.skipif(IS_UTC_HOST, reason="The host timezone is UTC; there is no local/UTC distinction to judge")
class TestApplyIsIdempotentAndReversible:
    def test_apply_excludes_utc_rows_and_rollback_leaves_them_alone_too(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Excluded rows must not move in either pass: shifting gives −8h (earlier), rollback gives +8h (later)."""
        path, conn, _ = mixed
        before_events = _dump(conn, "events", "timestamp")
        before_teams = _dump(conn, "teams", "created_at, updated_at, completed_at")
        journal = path.parent / "j.json"

        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal", str(journal),
                    "--ack-undecidable") == 0
        after = dict(_dump(conn, "events", "timestamp"))
        for rid, value in before_events:
            expected = value if rid in (36, 37, 38) else _stamp(
                datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f") + timedelta(hours=LOCAL_SHIFT_HOURS)
            )
            assert after[rid] == expected, f"rowid {rid}"
        assert mig.read_marker(conn) == mig.MIGRATION_MARKER

        recorded = _read_journal(journal)["detection"]["columns"]["events.timestamp"]
        assert recorded["excluded_rowids"] == [36, 37, 38]

        assert _run(monkeypatch, "--db", str(path), "--rollback", "--journal", str(journal)) == 0
        assert _dump(conn, "events", "timestamp") == before_events
        assert _dump(conn, "teams", "created_at, updated_at, completed_at") == before_teams
        assert mig.read_marker(conn) == 0

    def test_second_apply_is_refused_by_the_marker(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second --apply subtracts another 8 hours, and the old guardrail may still wave through "majority UTC + minority local"."""
        path, conn, _ = mixed
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal",
                    str(path.parent / "j1.json"), "--ack-undecidable") == 0
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal",
                    str(path.parent / "j2.json"), "--ack-undecidable", "--force") == 3
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_rollback_without_the_marker_is_refused(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rolling back a never-shifted database pushes everything 8 hours later wholesale."""
        path, conn, _ = mixed
        journal = path.parent / "fake.json"
        journal.write_text('{"db": "x", "shift_hours": -8, "columns": []}')
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--rollback", "--journal", str(journal)) == 3
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_existing_journal_is_never_overwritten(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The journal is the only recovery credential (including the per-row exclusion list); overwriting it destroys it."""
        path, conn, _ = mixed
        journal = path.parent / "j.json"
        journal.write_text('{"This is the credential of the previous migration": true}')
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal", str(journal),
                    "--ack-undecidable", "--force") == 2
        assert journal.read_text() == '{"This is the credential of the previous migration": true}'
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_apply_demands_a_journal(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path, conn, _ = mixed
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--ack-undecidable") == 2
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_undecidable_rows_stop_apply_even_with_force(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--force is for "cold data cannot prove itself", not for "some rows' conventions cannot be judged"."""
        path, conn, _ = mixed
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal",
                    str(path.parent / "j.json"), "--force") == 1
        assert _dump(conn, "events", "timestamp") == snapshot
        assert mig.read_marker(conn) == 0
