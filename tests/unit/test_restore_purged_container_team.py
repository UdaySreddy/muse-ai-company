"""The behavioral contract of the restore script — see report b168eb34-9c19-40e5-8ecb-624a74ccbc5a §4.

This script **inserts back** 843 rows of historical data (1 team + 7 agent rows + 835 activities) together with
an attribution ledger of 325 million tokens into the production database, executed by the creator personally. The cost
of a wrong insert is symmetric to the re-collection script: mixed-convention data written in
is indistinguishable afterwards. So every one of its disciplines must be machine-checkable:

* **Dual-source strong validation** — token values take the re-parsed transcript as authoritative, with the 07-29
journal as an independent comparison;
  any single field mismatch aborts the whole transaction, **with no downgrade switch**.
* **The clock convention is an explicit criterion** — source and target each read ``PRAGMA user_version``; only known
combinations are converted, everything else
  aborts. The conversion reuses ``migrate_timestamps_utc.py``'s column list and shift function, and preserves sub-second
  digits.
  This is a real problem the drill caught: the 07-28 backup was taken before the UTC shift, and restoring it as-is would
  stuff 843 rows
  of +8h timestamps into a UTC database (I11 red line: "no second clock in the database").
* **Third-party review** — what another snapshot says about the same batch of rows: NULL→valued is judged a backfill in
the meantime and takes the latter; both non-empty
  and unequal (or the latter losing a value) is judged a contradiction and aborts.
* **Rows and ledger in one transaction** — if acceptance fails the whole transaction rolls back and the target database
keeps not one row; there is no "insert rows first, write the ledger later"
  intermediate window (any reap tick in that window would delete the ledger-less husks again).
* **Closed write surface + structural idempotency** — only INSERTs into three tables; ``workflow_agents.tokens`` per-row
fingerprints are unchanged
  before and after; ``already_present`` is the first branch of judgment, and the SQL layer has a ``WHERE NOT EXISTS``
  guard too.
* **``tokens_measured_at`` writes the original batch moment, not now()** — that column doubles as the re-collection
batch signature and live-collection
  health; writing now() would pollute the coverage windowing.

All cases run only against temporary databases and temporary transcripts in tmp_path; not one byte touches the
production database or those backups.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "restore_purged_container_team.py"
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("restore_purged_container_team", SCRIPT)
assert _spec and _spec.loader
rp = importlib.util.module_from_spec(_spec)
# Must be registered in sys.modules before exec: the @dataclass in the script looks up its own module namespace.
sys.modules["restore_purged_container_team"] = rp
_spec.loader.exec_module(rp)

TEAM = rp.RESTORE_TEAM_ID
# Regular cases run a **synthetic team**: the real one carries the §3 class-① expected fingerprint (7 rows / 835 entries
# / 325 million),
# using it as a general fixture would make every case hit the count gate. The expectation gate itself is verified
# specifically by TestSingleTransaction.
SYNTH_TEAM = "synthetic-team-for-unit-tests"
LEADER = "e4495c71-1721-4c08-83af-45f299242bd7"
W1 = "3603792a-d574-4f8c-b9ee-1d879e0fafff"
W2 = "321ec68a-df2f-48f0-b88a-d77076e1e7e2"

# Isomorphic to the real shape of §3 class ①: two worker rows with ledgers, one Leader row without.
USAGE_A = {
    "input_tokens": 70,
    "output_tokens": 32255,
    "cache_creation_tokens": 117189,
    "cache_read_tokens": 2753368,
}
USAGE_B = {
    "input_tokens": 323,
    "output_tokens": 252416,
    "cache_creation_tokens": 2159530,
    "cache_read_tokens": 46165122,
}

SCHEMA = """
create table teams (
    id text primary key, name text, mode text, project_id text, leader_agent_id text,
    status text, summary text, config text, created_at text, updated_at text, completed_at text
);
create table agents (
    id text primary key, team_id text, name text, role text, model text, status text,
    source text, session_id text, transcript_path text, created_at text, last_active_at text,
    ctx_measured_at text,
    input_tokens integer, output_tokens integer,
    cache_creation_tokens integer, cache_read_tokens integer,
    tokens_measured_at text{tokens_source}
);
create table agent_activities (
    id text primary key, agent_id text, session_id text, tool_name text,
    input_summary text, output_summary text, timestamp text, duration_ms integer,
    status text, error text
);
create table workflow_agents (
    id text primary key, label text, model text, os_agent_id text, cc_agent_id text,
    created_at text, tokens integer
);
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def write_transcript(path: Path, usage: dict[str, int], *, model: str = "claude-opus-5") -> Path:
    """Write a real jsonl — do not mock the parser.

    The same requestId is written as two rows, the second with a larger output: this replicates streaming's real form
    (output is an increasing snapshot, not
    an increment). The parser must take the last entry per requestId. This fixture exists so that "validation passed"
    means
    "the real parse result agrees with the journal", not "the parse result I assumed agrees".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for out in (max(usage["output_tokens"] // 2, 1), usage["output_tokens"]):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "requestId": "r1",
                    "message": {
                        "model": model,
                        "usage": {
                            "input_tokens": usage["input_tokens"],
                            "output_tokens": out,
                            "cache_creation_input_tokens": usage["cache_creation_tokens"],
                            "cache_read_input_tokens": usage["cache_read_tokens"],
                        },
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_db(path: Path, *, marker: int, with_tokens_source: bool = True) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        SCHEMA.format(tokens_source=", tokens_source text" if with_tokens_source else "")
    )
    con.execute(f"pragma user_version = {marker}")
    con.commit()
    con.close()
    return path


def add_team(db: Path, *, team_id: str = SYNTH_TEAM, created: str = "2026-07-24 19:01:03.228316",
             updated: str = "2026-07-27 01:49:19.704237") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into teams values (?,?,?,?,?,?,?,?,?,?,?)",
        (team_id, "session-87500462", "coordinate", "proj-1", LEADER, "completed", "",
         '{"kind": "session"}', created, updated, None),
    )
    con.commit()
    con.close()


def add_agent(
    db: Path,
    aid: str,
    *,
    name: str,
    team_id: str = SYNTH_TEAM,
    role: str = "worker",
    transcript_path: str | None = None,
    session_id: str | None = "sess-1",
    created: str = "2026-07-24 21:17:16.577439",
    last_active: str | None = "2026-07-24 21:28:05.741571",
    ctx_measured: str | None = "2026-07-24 21:26:44.152175",
    model: str = "",
    with_tokens_source: bool = True,
) -> None:
    con = sqlite3.connect(db)
    cols = (
        "id,team_id,name,role,model,status,source,session_id,transcript_path,created_at,"
        "last_active_at,ctx_measured_at,input_tokens,output_tokens,cache_creation_tokens,"
        "cache_read_tokens,tokens_measured_at"
    )
    vals: list[Any] = [
        aid, team_id, name, role, model, "offline", "hook", session_id, transcript_path,
        created, last_active, ctx_measured, None, None, None, None, None,
    ]
    if with_tokens_source:
        cols += ",tokens_source"
        vals.append(None)
    marks = ",".join("?" * len(vals))
    con.execute(f"insert into agents ({cols}) values ({marks})", vals)  # noqa: S608
    con.commit()
    con.close()


def add_activity(db: Path, aid: str, agent_id: str, ts: str = "2026-07-24 21:17:19.712719") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into agent_activities values (?,?,?,?,?,?,?,?,?,?)",
        (aid, agent_id, "sess-1", "Bash", "in", "out", ts, 12, "ok", None),
    )
    con.commit()
    con.close()


def add_wa(db: Path, wid: str, *, tokens: int) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into workflow_agents values (?,?,?,?,?,?,?)",
        (wid, "wf", "opus", None, "cc1", "2026-07-24 19:00:00", tokens),
    )
    con.commit()
    con.close()


def write_journal(path: Path, entries: dict[str, dict[str, Any]]) -> Path:
    """Write a re-collection credential in the journal shape of backfill_token_usage."""
    path.write_text(
        json.dumps(
            {
                "script": "backfill_token_usage.py",
                "batch_ts": rp.BATCH_TS,
                "metric": "usage_sum",
                "jobs": [
                    {
                        "title": "A",
                        "table": "agents",
                        "enabled": True,
                        "written": [
                            {"id": k, "name": k[:8], "values": v, "guard": {}}
                            for k, v in entries.items()
                        ],
                    },
                    {
                        "title": "C",
                        "table": "workflow_agents",
                        "enabled": True,
                        "written": [{"id": "wa1", "name": "wf", "values": {"model": "x"}}],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def ledger_values(usage: dict[str, int], *, model: str | None = "claude-opus-5") -> dict[str, Any]:
    v: dict[str, Any] = dict(usage)
    v["tokens_measured_at"] = rp.BATCH_TS
    v["tokens_source"] = "transcript"
    if model:
        v["model"] = model
    return v


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    """A team of two workers + one Leader: present in the source database, empty in the target, isomorphic to the
    real one."""
    src = make_db(tmp_path / "source.db", marker=rp.MIGRATION_MARKER)
    tgt = make_db(tmp_path / "target.db", marker=rp.MIGRATION_MARKER)
    add_wa(tgt, "wa1", tokens=424242)

    t1 = write_transcript(tmp_path / "t1.jsonl", USAGE_A)
    t2 = write_transcript(tmp_path / "t2.jsonl", USAGE_B)

    add_team(src)
    add_agent(src, LEADER, name="Leader", role="leader", transcript_path=None, session_id=None,
              ctx_measured=None)
    add_agent(src, W1, name="wb-html-final", transcript_path=str(t1))
    add_agent(src, W2, name="wb-w1a-sandbox", transcript_path=str(t2))
    add_activity(src, "act-1", W1)
    add_activity(src, "act-2", W2)

    journal = write_journal(
        tmp_path / "backfill.json",
        {W1: ledger_values(USAGE_A), W2: ledger_values(USAGE_B)},
    )
    return {
        "src": src, "tgt": tgt, "journal": journal, "t1": t1, "t2": t2, "tmp": tmp_path,
    }


def run(world: dict[str, Any], *extra: str, corroborant: Path | None = None) -> int:
    argv = [
        "--db", str(world["tgt"]),
        "--from-backup", str(world["src"]),
        "--verify-journal", str(world["journal"]),
        "--corroborate-with", str(corroborant or world["tmp"] / "no-such.db"),
        "--team", SYNTH_TEAM,
        *extra,
    ]
    return rp.main(argv)


def fetch(db: Path, sql: str, *params: Any) -> Any:
    con = sqlite3.connect(db)
    row = con.execute(sql, params).fetchone()
    con.close()
    return row


def count(db: Path, table: str) -> int:
    return int(fetch(db, f"select count(*) from {table}")[0])  # noqa: S608


# ===========================================================================
# Dual-source strong validation — any disagreement aborts; there is no downgrade switch
# ===========================================================================
class TestDualSourceVerification:
    def test_matching_sources_produce_the_ledger_overlay(self, world):
        """happy path: re-parse agrees with the journal → the overlay carries the four layers + the original batch
        moment + source annotation."""
        con = rp.open_immutable(world["src"])
        rows = rp.load_source_agents(con, SYNTH_TEAM)
        con.close()
        checks = rp.verify_ledger(rows, rp.load_journal_ledger(world["journal"]))
        assert {c.agent_id for c in checks} == {W1, W2}
        overlay = next(c for c in checks if c.agent_id == W1).overlay
        assert {k: overlay[k] for k in rp.TOKEN_COLUMNS} == USAGE_A
        assert overlay["tokens_measured_at"] == rp.BATCH_TS
        assert overlay["tokens_source"] == "transcript"

    def test_a_single_layer_disagreement_aborts_everything(self, world):
        """One mismatched field is enough — not "mostly the same means pass"."""
        bad = dict(USAGE_A, cache_read_tokens=USAGE_A["cache_read_tokens"] + 1)
        write_journal(world["journal"], {W1: ledger_values(bad), W2: ledger_values(USAGE_B)})
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 3
        assert count(world["tgt"], "agents") == 0

    def test_a_missing_transcript_aborts_instead_of_trusting_the_journal(self, world):
        """One source missing = dual-source validation cannot be done. A journal alone being trustworthy is not
        acceptable either; that is "take-one downgrade"."""
        world["t1"].unlink()
        assert run(world) == 3

    def test_an_empty_transcript_is_never_read_as_zero_tokens(self, world):
        """no-data ≠ zero: if no snapshot can be parsed, abort; never persist it as "used 0 tokens"."""
        world["t1"].write_text("")
        assert run(world) == 3

    def test_a_ledger_without_a_transcript_path_is_unexplainable(self, world, tmp_path):
        """The journal says this row has a ledger, but the source row has no path — inexplicable, do not restore."""
        write_journal(
            world["journal"],
            {W1: ledger_values(USAGE_A), W2: ledger_values(USAGE_B),
             LEADER: ledger_values(USAGE_A)},
        )
        assert run(world) == 3

    def test_a_transcript_without_a_ledger_entry_is_a_source_disagreement(self, world):
        """Has a path but no such row in the journal — the two sources diverge; abort the same way."""
        write_journal(world["journal"], {W2: ledger_values(USAGE_B)})
        assert run(world) == 3

    def test_an_observed_model_mismatch_aborts(self, world):
        """The model is also part of the dual source: what the journal recorded and what the re-parse produced must
        be the same."""
        write_journal(
            world["journal"],
            {W1: ledger_values(USAGE_A, model="claude-opus-4-8"),
             W2: ledger_values(USAGE_B)},
        )
        assert run(world) == 3

    def test_a_row_that_never_had_a_ledger_is_restored_without_one(self, world):
        """Leader husk: restore the identity as-is; the ledger never existed, so do not fabricate one."""
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 0
        row = fetch(
            world["tgt"],
            "select tokens_measured_at, tokens_source, input_tokens from agents where id=?",
            LEADER,
        )
        assert row == (None, None, None)


# ===========================================================================
# Clock convention — an explicit criterion, not an assumption
# ===========================================================================
class TestClockConvention:
    @pytest.fixture(autouse=True)
    def _pin_host_timezone(self):
        """shift_for's contract is "shift the source database's wall clock by the host timezone" — inherited verbatim
        from the migration script,
        because the wall clock in the backup is this machine's wall clock. The test therefore pins the host timezone to
        the machine that wrote the data
        (+0800); unpinned, the offset is zero on a UTC runner and the assertions go falsely red (2026-08-03 public
        CI record). Running the real script on the wrong machine does not corrupt data: the third-party review gate (866
        timestamp cells
        with zero deviation per column) aborts before writing.
        """
        if not hasattr(time, "tzset"):
            pytest.skip("Needs POSIX tzset to pin the host timezone")
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        yield
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    def test_same_marker_means_no_shift(self):
        assert rp.needs_clock_shift(rp.MIGRATION_MARKER, rp.MIGRATION_MARKER) is False
        assert rp.needs_clock_shift(0, 0) is False

    def test_pre_migration_source_into_migrated_target_is_shifted(self):
        assert rp.needs_clock_shift(0, rp.MIGRATION_MARKER) is True

    def test_any_unknown_pairing_is_refused_rather_than_guessed(self):
        """Stop when there is no known conversion — guessing a direction costs hundreds of rows permanently off by
        one timezone."""
        with pytest.raises(rp.ClockConventionError):
            rp.needs_clock_shift(rp.MIGRATION_MARKER, 0)
        with pytest.raises(rp.ClockConventionError):
            rp.needs_clock_shift(0, 99999999)

    def test_the_shift_preserves_sub_minute_bytes(self):
        """The migration script only moves the "to the minute" prefix; seconds and sub-second digits are spliced back
        as-is — the event ledger's ordering relies on those digits."""
        shifted = rp.shift_value("2026-07-25 03:01:03.230396")
        assert shifted.endswith(":03.230396")
        assert shifted == "2026-07-24 19:01:03.230396"

    def test_a_null_timestamp_stays_null(self):
        assert rp.shift_value(None) is None

    def test_restoring_from_a_pre_migration_source_lands_utc_values(self, tmp_path):
        """End-to-end: the source is a pre-shift database; **every** local wall-clock column inserted into the target
        database must already be UTC.

        This is a real problem the drill caught — the forensics report compared only the schema, not the clock
        convention; restoring directly from it would
        write hundreds of rows of +8h timestamps into a UTC database, looking exactly like the real values afterwards.
        """
        src = make_db(tmp_path / "old.db", marker=0)
        tgt = make_db(tmp_path / "new.db", marker=rp.MIGRATION_MARKER)
        add_team(src, created="2026-07-25 03:01:03.228316", updated="2026-07-27 09:49:19.704237")
        add_agent(src, LEADER, name="Leader", role="leader", transcript_path=None,
                  session_id=None, ctx_measured=None, created="2026-07-25 03:01:03.230396",
                  last_active="2026-07-25 03:01:03.232585")
        add_activity(src, "act-1", LEADER, ts="2026-07-25 05:17:19.712719")
        journal = write_journal(tmp_path / "j0.json", {})

        assert rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", SYNTH_TEAM,
            "--apply", "--journal", str(tmp_path / "out.json"),
        ]) == 0

        assert fetch(tgt, "select created_at, updated_at from teams where id=?", SYNTH_TEAM) == (
            "2026-07-24 19:01:03.228316", "2026-07-27 01:49:19.704237"
        )
        assert fetch(
            tgt, "select created_at, last_active_at from agents where id=?", LEADER
        ) == ("2026-07-24 19:01:03.230396", "2026-07-24 19:01:03.232585")
        assert fetch(tgt, "select timestamp from agent_activities where id='act-1'")[0] == (
            "2026-07-24 21:17:19.712719"
        )
        # The shift marker itself does not move along — the target database's convention is declared by the target
        # database itself
        assert fetch(tgt, "pragma user_version")[0] == rp.MIGRATION_MARKER

    def test_a_future_timestamp_trips_the_direction_guard(self, world):
        """A reversed conversion direction moves historical rows into the future — the in-transaction backstop gate
        must catch it."""
        con = sqlite3.connect(world["tgt"])
        con.row_factory = sqlite3.Row
        add_team(world["tgt"])
        add_agent(world["tgt"], W1, name="wb-html-final", created="2099-01-01 00:00:00.000000")
        problems = rp.verify_after(con, SYNTH_TEAM, [W1], [], None)
        con.close()
        assert any("falls in the future" in p for p in problems)


# ===========================================================================
# Third-party review — what another snapshot says about the same batch of rows
# ===========================================================================
class TestCorroboration:
    def _corroborant(self, tmp_path: Path, *, session_id: str | None) -> Path:
        db = make_db(tmp_path / "corro.db", marker=rp.MIGRATION_MARKER)
        add_team(db)
        add_agent(db, LEADER, name="Leader", role="leader", transcript_path=None,
                  session_id=None, ctx_measured=None)
        add_agent(db, W1, name="wb-html-final",
                  transcript_path=str(tmp_path / "t1.jsonl"), session_id=session_id)
        add_agent(db, W2, name="wb-w1a-sandbox",
                  transcript_path=str(tmp_path / "t2.jsonl"), session_id="sess-1")
        add_activity(db, "act-1", W1)
        add_activity(db, "act-2", W2)
        return db

    def test_a_null_that_later_became_a_value_is_accepted_and_reported(self, world, capsys):
        """Backfilled in the meantime: review source NULL, main source valued → take the main source, but it must be
        visible in the report.

        This is exactly the proof that "the main source must be the later snapshot" — restoring from the earlier one
        would drop this column.
        """
        corro = self._corroborant(world["tmp"], session_id=None)
        assert run(world, corroborant=corro) == 0
        out = capsys.readouterr().out
        assert "backfilled in the meantime" in out and "session_id" in out
        assert "zero contradictions" in out

    def test_two_snapshots_disagreeing_on_a_value_aborts(self, world):
        """Both non-empty and unequal = the two snapshots disagree about the same row's identity; which is right is
        not the script's to guess."""
        corro = self._corroborant(world["tmp"], session_id="sess-OTHER")
        assert run(world, corroborant=corro, ) == 3

    def test_a_missing_corroborant_is_loudly_flagged_not_silently_skipped(self, world, capsys):
        """Skipping the review is allowed, but not silently — otherwise "no error" would be read as "the review
        passed"."""
        assert run(world) == 0
        assert "Review source unavailable" in capsys.readouterr().out

    def test_rows_absent_from_the_corroborant_are_counted_not_fatal(self, world, capsys):
        """The review source may simply be an earlier snapshot than some row — count it truthfully; do not treat it
        as a problem."""
        corro = make_db(world["tmp"] / "thin.db", marker=rp.MIGRATION_MARKER)
        add_team(corro)  # Only the team row; the 3 member rows + 2 activities are all later than it
        assert run(world, corroborant=corro) == 0
        assert "Rows missing from the review source: 5" in capsys.readouterr().out


# ===========================================================================
# Closed write surface + forbidden columns
# ===========================================================================
class TestWriteSurface:
    def test_the_write_surface_is_three_tables_and_nothing_else(self):
        assert rp.WRITABLE_TABLES == frozenset({"teams", "agents", "agent_activities"})
        assert rp.FORBIDDEN_COLUMN[0] not in rp.WRITABLE_TABLES

    def test_the_fingerprint_is_the_backfill_scripts_own_function(self):
        """The fingerprint algorithm exists in exactly one copy — copy one out and changing one place later grows two
        criteria of "unchanged"."""
        import backfill_token_usage as bf

        assert rp.workflow_tokens_fingerprint is bf.workflow_tokens_fingerprint
        assert rp.FORBIDDEN_COLUMN == bf.FORBIDDEN_COLUMN

    def test_the_clock_columns_come_from_the_migration_script(self):
        """Which columns are local wall clock is defined by the migration script — do not keep a separate list here."""
        import migrate_timestamps_utc as mig

        assert rp.LOCAL_COLUMNS is mig.LOCAL_COLUMNS
        assert rp.MIGRATION_MARKER == mig.MIGRATION_MARKER

    def test_insert_refuses_a_table_outside_the_whitelist(self, world):
        """Even if someone constructs such a plan in the future, the write layer blows up on the spot instead of
        writing it."""
        plan = rp.RowPlan("workflow_agents", "wa1", "wf", "restorable", {"id": "wa1", "tokens": 1})
        con = sqlite3.connect(world["tgt"])
        with pytest.raises(RuntimeError, match="write-table whitelist interception"):
            rp.insert_row(con, plan)
        con.close()
        assert fetch(world["tgt"], "select tokens from workflow_agents where id='wa1'")[0] == 424242

    def test_a_real_apply_leaves_the_forbidden_column_byte_identical(self, world):
        con = sqlite3.connect(world["tgt"])
        before = rp.workflow_tokens_fingerprint(con)
        con.close()
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 0
        con = sqlite3.connect(world["tgt"])
        after = rp.workflow_tokens_fingerprint(con)
        con.close()
        assert after == before
        assert count(world["tgt"], "agents") == 3  # But this apply did do work

    def test_the_journal_carries_the_fingerprint_and_the_clock_verdict(self, world):
        """The journal is the only credential: the forbidden-column fingerprints before/after and the clock judgment
        must all be in it."""
        journal = world["tmp"] / "j.json"
        assert run(world, "--apply", "--journal", str(journal)) == 0
        data = json.loads(journal.read_text())
        assert data["forbidden_column"]["unchanged"] is True
        assert data["metric"] == "usage_sum"
        assert data["clock"]["source_shifted_to_utc"] is False
        assert data["batch_ts"] == rp.BATCH_TS
        assert len(data["restored"]["agent_activities"]) == 2


# ===========================================================================
# Rows and ledger in one transaction — failed acceptance rolls the whole transaction back
# ===========================================================================
class TestSingleTransaction:
    def test_a_failed_check_leaves_not_one_row_behind(self, tmp_path):
        """Expected fingerprint mismatch → rollback. If the rows landed but the ledger did not, the next reap tick
        would delete them again."""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER)
        add_team(src, team_id=TEAM)
        add_agent(src, LEADER, name="Leader", role="leader", team_id=TEAM,
                  transcript_path=None, session_id=None, ctx_measured=None)
        add_activity(src, "act-1", LEADER)
        journal = write_journal(tmp_path / "j0.json", {})

        rc = rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"),
            "--apply", "--journal", str(tmp_path / "out.json"),
        ])
        assert rc == 4  # The real team is 7 rows / 835 entries; this one has only 1 row / 1 entry
        assert count(tgt, "teams") == 0
        assert count(tgt, "agents") == 0
        assert count(tgt, "agent_activities") == 0
        assert not (tmp_path / "out.json").exists()  # A failed write should not leave a "success credential" behind

    def test_the_expectation_gate_is_skipped_loudly_for_unknown_teams(self, tmp_path, capsys):
        """The synthetic team has no expected fingerprint — say the skip plainly instead of pretending an empty dict
        passed."""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER)
        add_team(src, team_id="other-team")
        journal = write_journal(tmp_path / "j0.json", {})
        rc = rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", "other-team",
        ])
        assert rc == 0
        assert "No expected fingerprint registered" in capsys.readouterr().out


# ===========================================================================
# Idempotency / dry-run / credentials
# ===========================================================================
class TestIdempotenceAndDryRun:
    def test_dry_run_is_the_default_and_writes_nothing(self, world):
        assert run(world) == 0
        assert count(world["tgt"], "agents") == 0
        assert count(world["tgt"], "teams") == 0

    def test_rerun_after_apply_plans_zero_rows(self, world, capsys):
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j1.json")) == 0
        capsys.readouterr()
        assert run(world) == 0
        out = capsys.readouterr().out
        assert "Total to restore: 0 rows" in out
        assert "already_present" in out

    def test_a_second_apply_inserts_nothing(self, world):
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j1.json")) == 0
        before = fetch(world["tgt"], "select count(*) from agents")[0]
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j2.json")) == 0
        assert fetch(world["tgt"], "select count(*) from agents")[0] == before

    def test_the_sql_guard_lets_a_concurrent_insert_win(self, world):
        """Someone else inserted between judgment and write — the guard is in the SQL, so no duplicate insert even
        under concurrency."""
        con = rp.open_immutable(world["src"])
        row = con.execute("select * from agents where id=?", (W1,)).fetchone()
        cols = set(rp.table_columns(con, "agents"))
        con.close()
        target = sqlite3.connect(world["tgt"])
        target.row_factory = sqlite3.Row
        values = rp.build_values(
            "agents", row, rp.table_columns(target, "agents"), cols, {}, shift_clock=False
        )
        plan = rp.RowPlan("agents", W1, "wb-html-final", "restorable", values)
        assert rp.insert_row(target, plan) == 1
        assert rp.insert_row(target, plan) == 0  # The second time moves not a single row
        target.close()

    def test_apply_without_a_journal_is_refused(self, world):
        assert run(world, "--apply") == 2

    def test_an_existing_journal_is_never_overwritten(self, world):
        journal = world["tmp"] / "taken.json"
        journal.write_text("{}")
        assert run(world, "--apply", "--journal", str(journal)) == 2
        assert journal.read_text() == "{}"

    def test_the_source_may_never_be_the_target(self, world):
        assert rp.main([
            "--db", str(world["src"]), "--from-backup", str(world["src"]),
            "--verify-journal", str(world["journal"]),
        ]) == 2


# ===========================================================================
# Batch moment — write the original moment, not now()
# ===========================================================================
class TestBatchTimestamp:
    def test_the_original_batch_timestamp_is_written_not_now(self, world):
        """This column doubles as the re-collection batch signature and live-collection health: writing now() would
        fabricate "new measurements" and pollute the windowing."""
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 0
        stamps = {
            r[0]
            for r in sqlite3.connect(world["tgt"]).execute(
                "select tokens_measured_at from agents where tokens_measured_at is not null"
            )
        }
        assert stamps == {rp.BATCH_TS}

    def test_a_declared_batch_timestamp_is_the_one_that_lands(self, world):
        """The batch moment is parameterized (unit tests must be able to swap it), but what is written must be
        exactly the declared one."""
        assert run(
            world, "--apply", "--journal", str(world["tmp"] / "j.json"),
            "--batch-ts", "2026-08-03 00:00:00.000000",
        ) == 0
        stamps = {
            r[0]
            for r in sqlite3.connect(world["tgt"]).execute(
                "select tokens_measured_at from agents where tokens_measured_at is not null"
            )
        }
        assert stamps == {"2026-08-03 00:00:00.000000"}

    def test_a_stray_batch_timestamp_is_caught_inside_the_transaction(self, world):
        """In-transaction gate: a moment in the database that does not belong to this batch → report a problem (the
        caller rolls back accordingly).

        This guards against "half the rows stamped with the batch stamp, the other half written with another stamp by
        another path" — then this batch of rows is no longer
        a windowable cohort, and coverage windowing identifies batches precisely by "the same moment shared by enough
        rows".
        """
        con = sqlite3.connect(world["tgt"])
        con.row_factory = sqlite3.Row
        add_team(world["tgt"])
        add_agent(world["tgt"], W1, name="wb-html-final")
        con.execute(
            "update agents set tokens_measured_at = ? where id = ?",
            ("2026-08-01 12:00:00.000000", W1),
        )
        con.commit()
        problems = rp.verify_after(con, SYNTH_TEAM, [W1], [], None)
        con.close()
        assert any("not the original batch time" in p for p in problems)


# ===========================================================================
# Schema adaptation — a missing column must have an explicit rule
# ===========================================================================
class TestSchemaAdaptation:
    def test_a_known_missing_column_falls_back_to_its_declared_rule(self, tmp_path):
        """The source has no tokens_source (it only launched in v1.11.1) → ledger-less rows stay NULL, not backfilled
        with 'transcript'."""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER, with_tokens_source=False)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER)
        add_team(src, team_id="other-team")
        add_agent(src, LEADER, name="Leader", role="leader", team_id="other-team",
                  transcript_path=None, session_id=None, ctx_measured=None,
                  with_tokens_source=False)
        journal = write_journal(tmp_path / "j0.json", {})
        rc = rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", "other-team",
            "--apply", "--journal", str(tmp_path / "out.json"),
        ])
        assert rc == 0
        assert fetch(tgt, "select tokens_source from agents where id=?", LEADER)[0] is None

    def test_an_unknown_missing_column_is_schema_drift_not_a_silent_null(self, world):
        """An unregistered missing column = drift nobody has seen. Backfilling NULL is exactly how "fields quietly
        disappeared" happens."""
        con = sqlite3.connect(world["tgt"])
        con.execute("alter table agents add column brand_new text")
        con.commit()
        con.close()
        assert run(world) == 5

    def test_a_column_dropped_by_the_target_aborts_rather_than_losing_data(self, tmp_path):
        """The target database has one column fewer than the source → restored content would be lost; requires human
        ruling, no head-down inserting."""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER, with_tokens_source=False)
        add_team(src, team_id="other-team")
        journal = write_journal(tmp_path / "j0.json", {})
        assert rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", "other-team",
        ]) == 5


# ===========================================================================
# Pre-backup — via the SQLite backup API, and never overwriting
# ===========================================================================
class TestPreBackup:
    def test_apply_takes_a_consistent_backup_first(self, world):
        backups = world["tmp"] / "baks"
        assert run(
            world, "--apply", "--journal", str(world["tmp"] / "j.json"),
            "--pre-backup-dir", str(backups),
        ) == 0
        made = list(backups.glob("*.bak-restore-*"))
        assert len(made) == 1
        # The pre-backup records the **pre-restore** state: it should not contain the restored rows
        assert count(made[0], "agents") == 0
        assert count(world["tgt"], "agents") == 3

    def test_a_second_backup_in_the_same_second_never_overwrites_the_first(self, world):
        """The whole value of the pre-backup is "never written" — on a name collision, start a new one; never
        overwrite."""
        backups = world["tmp"] / "baks"
        run(world, "--apply", "--journal", str(world["tmp"] / "j1.json"),
            "--pre-backup-dir", str(backups))
        run(world, "--apply", "--journal", str(world["tmp"] / "j2.json"),
            "--pre-backup-dir", str(backups))
        made = sorted(backups.glob("*.bak-restore-*"))
        assert len(made) == 2
        assert count(made[0], "agents") == 0  # The first one is still the pre-restore state
