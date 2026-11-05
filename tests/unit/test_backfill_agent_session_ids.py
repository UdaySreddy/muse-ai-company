"""The backfill script's behavioral contract — see docs/token-attribution-v1-design.md §7 phase 1.

This script will rewrite nearly two thousand rows of production data in one pass, executed by the Creator personally, so
its promises must be machine-checked:
**write only empty columns** (never overwrite observed values), **idempotent** (a rerun changes nothing), **classify
rather than silently skip**
(rows that cannot be written must carry a reason code), **do not cross boundaries** (touch only the two columns of
agents; ``workflow_agents.tokens``
unchanged row by row — that is the ctx_last metric, and mixing it into usage_sum would permanently pollute history),
**risks visible**
(when it would create (session,name) collisions, refuse to write, hard).

All cases run only against temporary databases in tmp_path; not a single byte touches the production database.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_agent_session_ids.py"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_spec = importlib.util.spec_from_file_location("backfill_agent_session_ids", SCRIPT)
assert _spec and _spec.loader
bf = importlib.util.module_from_spec(_spec)
# It must first be registered into sys.modules and then exec'd: the @dataclass in the script looks back into its own
# module namespace.
sys.modules["backfill_agent_session_ids"] = bf
_spec.loader.exec_module(bf)

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
OTHER = "abff40af-58a1-4a7e-84ee-a68f07f72ed3"
ROOT = "/Users/dev/Desktop/AI team OS"
SLUG = "-Users-dev-Desktop-AI-team-OS"


def _path(session_id: str, cc_id: str, slug: str = SLUG) -> str:
    return f"/Users/dev/.claude/projects/{slug}/{session_id}/subagents/agent-{cc_id}.jsonl"


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """A minimal three-table database: agents / teams / projects, with column names matching production."""
    con = sqlite3.connect(tmp_path / "t.db")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        create table agents (
            id text primary key, team_id text, name text, role text, status text,
            created_at text, session_id text, transcript_path text, project_id text
        );
        create table teams (id text primary key, name text, config text, project_id text);
        create table projects (id text primary key, root_path text);
        create table workflow_agents (id text primary key, tokens integer);
        """
    )
    con.execute("insert into projects values ('p1', ?)", (ROOT,))
    con.execute(
        "insert into teams values ('t1', 'session-80d0cc5e', ?, 'p1')",
        (json.dumps({"kind": "session", "owner_session_id": SESSION}),),
    )
    con.execute("insert into workflow_agents values ('wa1', 12345)")
    con.commit()
    return con


def _add_agent(
    con: sqlite3.Connection,
    aid: str,
    *,
    name: str = "worker",
    role: str = "worker",
    session_id: str | None = None,
    transcript_path: str | None = None,
    created_at: str = "2026-07-20 10:00:00",
) -> None:
    con.execute(
        "insert into agents values (?, 't1', ?, ?, 'offline', ?, ?, ?, 'p1')",
        (aid, name, role, created_at, session_id, transcript_path),
    )
    con.commit()


class TestJobASubagentSessionId:
    def test_derives_session_from_path(self, db):
        _add_agent(db, "a1", transcript_path=_path(SESSION, "cc1"))
        job = bf.job_subagent_session_id(bf.load_agents(db))
        assert [(r.reason, r.value) for r in job.rows] == [("written", SESSION)]

    def test_never_overwrites_an_existing_value(self, db):
        """An existing value differing from the derived value = conflict; not a single row moves. Overwriting would
        erase observation with inference."""
        _add_agent(db, "a1", session_id=OTHER, transcript_path=_path(SESSION, "cc1"))
        job = bf.job_subagent_session_id(bf.load_agents(db))
        assert job.rows[0].reason == "conflict"
        assert job.written() == []

    def test_identical_existing_value_is_already_set_not_a_rewrite(self, db):
        _add_agent(db, "a1", session_id=SESSION, transcript_path=_path(SESSION, "cc1"))
        job = bf.job_subagent_session_id(bf.load_agents(db))
        assert job.rows[0].reason == "already_set"
        assert job.written() == []

    def test_missing_and_unparseable_paths_get_distinct_reason_codes(self, db):
        """Rows that cannot be written must be classified: no-data ≠ zero; a single total cannot say whether they can
        be saved."""
        _add_agent(db, "a1", transcript_path=None)
        _add_agent(db, "a2", transcript_path="/tmp/whatever.jsonl")
        job = bf.job_subagent_session_id(bf.load_agents(db))
        assert job.counts() == {"no_transcript_path": 1, "unparseable_path": 1}

    def test_slug_mismatch_is_flagged_but_never_changes_attribution(self, db):
        """The slug is a lossy mapping; a mismatch is only a reminder and must never be used to change the ownership
        column."""
        _add_agent(db, "a1", transcript_path=_path(SESSION, "cc1", slug="-Volumes-other"))
        job = bf.job_subagent_session_id(bf.load_agents(db))
        row = job.rows[0]
        assert row.reason == "written"  # The session_id is backfilled anyway
        assert "does not match" in row.warn  # But a human can see it
        assert row.column == "session_id"  # Only this column is written; project_id is untouched, not a single
        # character

    def test_leader_rows_are_not_this_jobs_business(self, db):
        _add_agent(db, "L1", name="Leader", role="leader")
        assert bf.job_subagent_session_id(bf.load_agents(db)).rows == []


class TestJobBLeaderTranscriptPath:
    @pytest.fixture()
    def disk(self, tmp_path, monkeypatch):
        projects = tmp_path / "projects"
        (projects / SLUG).mkdir(parents=True)
        (projects / SLUG / f"{SESSION}.jsonl").write_text("{}\n")
        monkeypatch.setattr(bf, "PROJECTS_DIR", projects)
        return bf.index_main_transcripts()

    def test_reverse_lookup_via_team_owner_session(self, db, disk):
        _add_agent(db, "L1", name="Leader", role="leader")
        job = bf.job_leader_transcript_path(bf.load_agents(db), disk)
        assert job.rows[0].reason == "written"
        assert job.rows[0].value.endswith(f"{SLUG}/{SESSION}.jsonl")

    def test_file_gone_is_its_own_reason_not_silence(self, db, disk):
        """The re-collection window only closes and never opens — this class must be countable separately."""
        db.execute(
            "update teams set config=? where id='t1'",
            (json.dumps({"owner_session_id": "deadbeef-0000-0000-0000-000000000000"}),),
        )
        db.commit()
        _add_agent(db, "L1", name="Leader", role="leader")
        job = bf.job_leader_transcript_path(bf.load_agents(db), disk)
        assert job.rows[0].reason == "transcript_gone"

    def test_no_session_identity_at_all(self, db, disk):
        db.execute("update teams set config='{}' where id='t1'")
        db.commit()
        _add_agent(db, "L1", name="Leader", role="leader")
        job = bf.job_leader_transcript_path(bf.load_agents(db), disk)
        assert job.rows[0].reason == "no_session_id"

    def test_team_name_prefix_is_not_accepted_as_identity(self, db, disk):
        """A team name kept only the first 8 characters of the session — a prefix is not an identity and must not be
        used as a fallback."""
        db.execute("update teams set config='{}' where id='t1'")
        db.commit()
        _add_agent(db, "L1", name="Leader", role="leader")
        job = bf.job_leader_transcript_path(bf.load_agents(db), disk)
        assert job.written() == []


def _run(monkeypatch, dbfile: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["backfill", "--db", str(dbfile), *extra])
    return bf.main()


class TestJobCIsOptIn:
    def test_leader_session_id_defaults_to_not_written(self, db, tmp_path, capsys, monkeypatch):
        _add_agent(db, "L1", name="Leader", role="leader")
        db.close()
        assert _run(monkeypatch, tmp_path / "t.db", "--sample", "1") == 0
        out = capsys.readouterr().out
        assert "Job C is not executed by default" in out
        con = sqlite3.connect(tmp_path / "t.db")
        assert con.execute("select session_id from agents where id='L1'").fetchone()[0] is None
        con.close()

    def test_resolution_shift_is_spelled_out_before_it_happens(self, db):
        """Job C changes live resolution results, so it must first make clear 'which row it will hit'."""
        _add_agent(db, "L1", name="Leader", role="leader", created_at="2026-07-20 09:00:00")
        _add_agent(db, "L2", name="Leader", role="leader", created_at="2026-07-28 09:00:00")
        agents = bf.load_agents(db)
        job_c = bf.job_leader_session_id(agents)
        lines = bf.describe_leader_resolution_shift(agents, job_c)
        assert len(lines) == 1
        assert "2 Leader rows" in lines[0]
        assert "L1"[:8] in lines[0]  # It hits the earliest row, not the newest one


class TestApplyIsSafeAndIdempotent:
    def test_apply_then_rerun_writes_nothing(self, db, tmp_path, capsys, monkeypatch):
        _add_agent(db, "a1", transcript_path=_path(SESSION, "cc1"))
        db.close()
        dbfile = tmp_path / "t.db"

        assert _run(monkeypatch, dbfile, "--apply") == 0
        assert "Written: 1 row" in capsys.readouterr().out

        assert _run(monkeypatch, dbfile) == 0
        assert "Total to write: 0 rows" in capsys.readouterr().out

    def test_dry_run_writes_nothing_at_all(self, db, tmp_path, monkeypatch):
        _add_agent(db, "a1", transcript_path=_path(SESSION, "cc1"))
        db.close()
        dbfile = tmp_path / "t.db"

        assert _run(monkeypatch, dbfile) == 0
        con = sqlite3.connect(dbfile)
        assert con.execute("select session_id from agents where id='a1'").fetchone()[0] is None
        con.close()

    def test_workflow_agents_tokens_is_never_touched(self, db, tmp_path, monkeypatch):
        """R3: writing usage_sum into the ctx_last column would permanently pollute history and be indistinguishable
        afterwards."""
        _add_agent(db, "a1", transcript_path=_path(SESSION, "cc1"))
        db.close()
        dbfile = tmp_path / "t.db"
        _run(monkeypatch, dbfile, "--apply")
        con = sqlite3.connect(dbfile)
        assert con.execute("select tokens from workflow_agents where id='wa1'").fetchone()[0] == 12345
        con.close()

    def test_apply_refuses_when_it_would_create_a_name_collision(self, db, tmp_path, monkeypatch):
        """Multiple rows with the same (session,name) would make the registration dedup bypass reuse an old row and
        overwrite its token columns."""
        _add_agent(db, "a1", name="Explore", transcript_path=_path(SESSION, "cc1"))
        _add_agent(db, "a2", name="Explore", transcript_path=_path(SESSION, "cc2"))
        db.close()
        dbfile = tmp_path / "t.db"

        assert _run(monkeypatch, dbfile, "--apply") == 3
        con = sqlite3.connect(dbfile)
        assert con.execute("select count(*) from agents where session_id is not null").fetchone()[0] == 0
        con.close()

    def test_acknowledged_collisions_may_proceed(self, db, tmp_path, monkeypatch):
        _add_agent(db, "a1", name="Explore", transcript_path=_path(SESSION, "cc1"))
        _add_agent(db, "a2", name="Explore", transcript_path=_path(SESSION, "cc2"))
        db.close()
        dbfile = tmp_path / "t.db"

        assert _run(monkeypatch, dbfile, "--apply", "--ack-collisions") == 0
        con = sqlite3.connect(dbfile)
        assert con.execute("select count(*) from agents where session_id is not null").fetchone()[0] == 2
        con.close()

    def test_workflow_subagents_are_exempt_from_the_collision_check(self, db, tmp_path, monkeypatch):
        """Workflow fan-out dedupes by cc_agent_id and never by name — their name collisions constitute no risk."""
        _add_agent(db, "a1", name="wf-x", role="workflow-subagent",
                   transcript_path=_path(SESSION, "cc1"))
        _add_agent(db, "a2", name="wf-x", role="workflow-subagent",
                   transcript_path=_path(SESSION, "cc2"))
        db.close()
        assert _run(monkeypatch, tmp_path / "t.db", "--apply") == 0
