"""The historical re-collection script's behavioral contract — see docs/token-attribution-v1-design.md §6 / §7 phase 3.

This script will rewrite more than two thousand rows of production data in one pass, executed by the Creator personally, so the three 'hard constraints' in the design
(§6.4) cannot only be written in comments; they must be machine-checkable one by one:

* **Hard constraint (1) never touch ``workflow_agents.tokens``** — that is the ctx_last metric (the last round's context
  watermark), differing from the usage_sum this script produces by a measured 5-25x. Writing it in = mixing metrics, permanently solidified into history and
  indistinguishable afterwards (risk R3). Three blocks: the write-column whitelist, an assert at the apply layer, and per-row sha256 fingerprints before and after.
* **Hard constraint (2) coverage split by window by tokens_measured_at** — re-collection raises the total coverage from 0.5% to 78%,
  but 'whether new dispatches were collected' is another matter. One number masking another number (countermeasure 6.3-2), so the two numbers
  are never merged.
* **Hard constraint (3) dry-run first, write only empty columns, a rerun changes nothing** — idempotency is structural (``already_measured``
  is the first branch of the determination), not reliant on a marker bit.

Plus two disciplines of this script itself: **rows that cannot be written must carry a reason code** (no-data ≠ zero), **model is only
backfilled by observation** (the alias ledger's resolution results never land in the database; rows without a transcript are not guessed at or touched).

All cases run only against temporary databases and temporary transcripts in tmp_path; not a single byte touches the production database.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_token_usage.py"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_spec = importlib.util.spec_from_file_location("backfill_token_usage", SCRIPT)
assert _spec and _spec.loader
bf = importlib.util.module_from_spec(_spec)
# It must first be registered into sys.modules and then exec'd: the @dataclass in the script looks back into its own module namespace.
sys.modules["backfill_token_usage"] = bf
_spec.loader.exec_module(bf)

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
SLUG = "-Users-dev-Desktop-AI-team-OS"


# ---------------------------------------------------------------------------
# Fixture: a real transcript file + a minimal two-table database
# ---------------------------------------------------------------------------
def write_transcript(
    path: Path, calls: list[tuple[str, dict[str, int]]], *, model: str = "claude-opus-5"
) -> Path:
    """Write a real jsonl — do not mock the parser.

    Each (requestId, usage) pair is written as **two rows**, the second row with a larger output_tokens: this replicates the real form of streaming
    (within the same requestId, output is an increasing snapshot rather than an increment). The parser must take the last entry per requestId;
    bare per-row addition would seriously inflate. This fixture exists so that 'the re-collection value = the real parse result' rather than '= what I assumed the'
    parse result was'.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for req, usage in calls:
        half = dict(usage, output_tokens=max(usage["output_tokens"] // 2, 1))
        for u in (half, usage):
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "requestId": req,
                        "message": {"model": model, "usage": {
                            "input_tokens": u["input_tokens"],
                            "output_tokens": u["output_tokens"],
                            "cache_creation_input_tokens": u["cache_creation_tokens"],
                            "cache_read_input_tokens": u["cache_read_tokens"],
                        }},
                    }
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


USAGE_ONE = {
    "input_tokens": 10,
    "output_tokens": 500,
    "cache_creation_tokens": 2000,
    "cache_read_tokens": 90000,
}
# The expected accumulated value of a transcript with two calls (accumulated across requestIds).
EXPECTED_TWO_CALLS = {k: v * 2 for k, v in USAGE_ONE.items()}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def dbfile(tmp_path: Path) -> Path:
    """A minimal two-table database, with column names matching production."""
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(
        """
        create table agents (
            id text primary key, name text, role text, model text, created_at text,
            transcript_path text,
            input_tokens integer, output_tokens integer,
            cache_creation_tokens integer, cache_read_tokens integer,
            tokens_measured_at text, tokens_source text
        );
        create table workflow_agents (
            id text primary key, label text, model text, os_agent_id text,
            cc_agent_id text, created_at text, tokens integer
        );
        """
    )
    con.commit()
    con.close()
    return p


def add_agent(
    dbfile: Path,
    aid: str,
    *,
    name: str = "worker",
    role: str = "worker",
    model: str | None = None,
    transcript_path: str | None = None,
    created_at: str = "2026-07-20 10:00:00",
    measured_at: str | None = None,
    source: str | None = None,
    tokens: dict[str, int] | None = None,
) -> None:
    t = tokens or {}
    con = sqlite3.connect(dbfile)
    con.execute(
        "insert into agents values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            aid, name, role, model, created_at, transcript_path,
            t.get("input_tokens"), t.get("output_tokens"),
            t.get("cache_creation_tokens"), t.get("cache_read_tokens"),
            measured_at, source,
        ),
    )
    con.commit()
    con.close()


def add_wa(
    dbfile: Path, wid: str, *, model: str, os_agent_id: str | None = None,
    tokens: int | None = 12345, label: str = "wf-x",
) -> None:
    con = sqlite3.connect(dbfile)
    con.execute(
        "insert into workflow_agents values (?,?,?,?,?,?,?)",
        (wid, label, model, os_agent_id, "cc1", "2026-07-20 10:00:00", tokens),
    )
    con.commit()
    con.close()


def load(dbfile: Path):
    con = bf.open_db(dbfile, writable=False)
    rows = bf.load_agents(con)
    con.close()
    return rows


def run(monkeypatch, dbfile: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["backfill", "--db", str(dbfile), "--quiet", *extra])
    return bf.main()


def fetch(dbfile: Path, sql: str, *params):
    con = sqlite3.connect(dbfile)
    row = con.execute(sql, params).fetchone()
    con.close()
    return row


BATCH = "2026-07-29 03:00:00.000000"


# ===========================================================================
# Hard constraint (1) — workflow_agents.tokens unchanged row by row
# ===========================================================================
class TestHardConstraint1ForbiddenColumn:
    def test_the_forbidden_column_is_not_in_the_write_whitelist(self):
        """The whitelist is a closed set; forbidden columns are not in it — this is the first block, and the cheapest."""
        assert bf.FORBIDDEN_COLUMN == ("workflow_agents", "tokens")
        assert bf.FORBIDDEN_COLUMN not in bf.WRITABLE_COLUMNS

    def test_only_model_is_writable_on_workflow_agents(self):
        """When this script touches workflow_agents it only swaps an alias for an observed model; no other column is ever written."""
        wa_cols = {c for t, c in bf.WRITABLE_COLUMNS if t == "workflow_agents"}
        assert wa_cols == {"model"}

    def test_apply_refuses_a_job_that_targets_the_forbidden_column(self, dbfile):
        """The second block: even if someone constructs such a Job in the future, the write layer blows up on the spot rather than writing as told."""
        add_wa(dbfile, "wa1", model="opus", tokens=999)
        job = bf.Job("malicious", "workflow_agents")
        job.rows.append(bf.Row("workflow_agents", "wa1", "x", "written", values={"tokens": 1}))
        con = sqlite3.connect(dbfile)
        with pytest.raises(RuntimeError, match="write-column whitelist interception"):
            bf.apply_job(con, job)
        con.close()
        assert fetch(dbfile, "select tokens from workflow_agents where id='wa1'")[0] == 999

    def test_fingerprint_catches_compensating_edits_that_preserve_the_sum(self, dbfile):
        """The third block must be stronger than 'comparing totals': one row up and one row down can leave the sum utterly unmoved."""
        add_wa(dbfile, "wa1", model="opus", tokens=100)
        add_wa(dbfile, "wa2", model="opus", tokens=200)
        con = sqlite3.connect(dbfile)
        before = bf.workflow_tokens_fingerprint(con)
        con.execute("update workflow_agents set tokens=200 where id='wa1'")
        con.execute("update workflow_agents set tokens=100 where id='wa2'")
        con.commit()
        after = bf.workflow_tokens_fingerprint(con)
        con.close()
        assert before["sum"] == after["sum"]  # The total can be fooled
        assert before["sha256"] != after["sha256"]  # The per-row fingerprint cannot be fooled

    def test_a_real_apply_leaves_the_column_byte_identical(self, dbfile, tmp_path, monkeypatch):
        """End-to-end: really run one re-collection; the forbidden columns are unchanged row by row (§7 phase 3 acceptance item)."""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE), ("r2", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1", tokens=777)
        con = sqlite3.connect(dbfile)
        before = bf.workflow_tokens_fingerprint(con)
        con.close()

        assert run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json")) == 0

        con = sqlite3.connect(dbfile)
        after = bf.workflow_tokens_fingerprint(con)
        con.close()
        assert after == before
        # But the model column really was backfilled by observation — proving this apply did not do nothing
        assert fetch(dbfile, "select model from workflow_agents where id='wa1'")[0] == "claude-opus-5"

    def test_journal_records_the_fingerprint_both_sides(self, dbfile, tmp_path, monkeypatch):
        """The journal is the only recovery credential; the forbidden columns' before/after fingerprints must be in it."""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1", tokens=777)
        journal = tmp_path / "j.json"
        run(monkeypatch, dbfile, "--apply", "--journal", str(journal))
        data = json.loads(journal.read_text())
        assert data["forbidden_column"]["column"] == "workflow_agents.tokens"
        assert data["forbidden_column"]["unchanged"] is True
        assert data["metric"] == "usage_sum"  # The metric travels with the credential, not by later recollection


# ===========================================================================
# Hard constraint (2) — coverage split by window
# ===========================================================================
class TestHardConstraint2CoverageWindowing:
    def test_denominator_keeps_rows_that_have_no_transcript(self, dbfile):
        """§4.1: rows must not be moved out of the denominator on the grounds of 'no path, so they do not count' — that is exactly passing a part off as the whole (R2)."""
        add_agent(dbfile, "a1", transcript_path=None)
        add_agent(dbfile, "a2", transcript_path="/nope.jsonl")
        cov = bf.measure_coverage(load(dbfile), set(), leader=False)
        assert cov.total == 2
        assert cov.measured == 0

    def test_leader_rows_are_counted_under_their_own_denominator(self, dbfile):
        """A main session's magnitude is two to three orders larger than a sub-agent's; sharing one denominator would drown the sub-agents (§3.3)."""
        add_agent(dbfile, "a1")
        add_agent(dbfile, "L1", role="leader")
        assert bf.measure_coverage(load(dbfile), set(), leader=False).total == 1
        assert bf.measure_coverage(load(dbfile), set(), leader=True).total == 1

    def test_a_shared_timestamp_is_what_marks_a_backfill_cohort(self, dbfile):
        """The live chain writes each row's own stop moment (unique to the microsecond); re-collection stamps the same value on thousands of rows."""
        for i in range(bf.BATCH_COHORT_MIN):
            add_agent(dbfile, f"b{i}", measured_at=BATCH)
        add_agent(dbfile, "live", measured_at="2026-07-28 11:20:20.521255")
        con = bf.open_db(dbfile, writable=False)
        cohorts = bf.detect_backfill_cohorts(con)
        con.close()
        assert cohorts == {BATCH}

    def test_a_handful_of_rows_sharing_a_timestamp_is_not_a_cohort(self, dbfile):
        """The point of the threshold: two or three rows accidentally at the same moment should not be misjudged as one re-collection."""
        for i in range(3):
            add_agent(dbfile, f"b{i}", measured_at=BATCH)
        con = bf.open_db(dbfile, writable=False)
        assert bf.detect_backfill_cohorts(con) == set()
        con.close()

    def test_backfill_never_moves_the_incremental_number(self, dbfile):
        """The core of countermeasure 6.3-2: re-collection raises only the 'historical backfill' cell; 'incremental collection' is untouched, verbatim."""
        add_agent(dbfile, "live", measured_at="2026-07-28 11:20:20.521255")
        for i in range(5):
            add_agent(dbfile, f"todo{i}")
        before = bf.measure_coverage(load(dbfile), set(), leader=False)
        after = bf.project_coverage(before, 5)
        assert (before.incremental, before.backfilled) == (1, 0)
        assert (after.incremental, after.backfilled) == (1, 5)
        assert after.total == before.total  # The denominator does not change because of re-collection

    def test_the_rendered_line_shows_three_numbers_and_no_sum(self, dbfile):
        """Adding the two cells together is meaningless, and the report does not give that sum either — the next step after mixing metrics is mixing denominators."""
        add_agent(dbfile, "live", measured_at="2026-07-28 11:20:20.521255")
        add_agent(dbfile, "todo")
        line = bf.measure_coverage(load(dbfile), set(), leader=False).line()
        assert "incremental collection" in line and "historical backfill" in line and "unattributed" in line
        assert "combined total" not in line and "grand total" not in line


# ===========================================================================
# Hard constraint (3) — dry-run / write only empty columns / idempotency
# ===========================================================================
class TestHardConstraint3DryRunAndIdempotence:
    def test_dry_run_is_the_default_and_writes_nothing(self, dbfile, tmp_path, monkeypatch):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        assert run(monkeypatch, dbfile) == 0
        assert fetch(dbfile, "select tokens_measured_at from agents where id='a1'")[0] is None

    def test_apply_writes_the_parsed_four_layers_and_the_source_tag(
        self, dbfile, tmp_path, monkeypatch
    ):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE), ("r2", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"))
        row = fetch(
            dbfile,
            "select input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
            " tokens_source from agents where id='a1'",
        )
        assert tuple(row[:4]) == tuple(EXPECTED_TWO_CALLS[c] for c in bf.TOKEN_COLUMNS)
        assert row[4] == "transcript"

    def test_rerun_after_apply_plans_zero_writes(self, dbfile, tmp_path, monkeypatch, capsys):
        """§7 phase 3 acceptance: after ``--apply``, a rerun changes nothing."""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"))
        capsys.readouterr()
        assert run(monkeypatch, dbfile) == 0
        assert "Total to write: 0 rows" in capsys.readouterr().out

    def test_a_measured_row_is_never_re_measured(self, dbfile, tmp_path):
        """Where idempotency is implemented: ``already_measured`` is the first branch of the determination, earlier than reading files."""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp), measured_at=BATCH, source="transcript")
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert [r.reason for r in job.rows] == ["already_measured"]
        assert job.written() == []

    def test_sql_guard_lets_a_concurrent_writer_win(self, dbfile, tmp_path):
        """Between dry-run and apply, the live system may have written the value — the guard is in the SQL and does not overwrite even under concurrency."""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert len(job.written()) == 1
        # After the determination and before the write, the live chain wrote it first
        con = sqlite3.connect(dbfile)
        con.execute("update agents set tokens_measured_at='2026-07-29 09:00:00' where id='a1'")
        con.commit()
        assert bf.apply_job(con, job) == 0  # Not a single row was changed
        con.close()
        assert fetch(dbfile, "select tokens_measured_at from agents where id='a1'")[0] == (
            "2026-07-29 09:00:00"
        )

    def test_apply_without_a_journal_is_refused(self, dbfile, monkeypatch):
        """The journal is the only recovery credential; without it, writing is not allowed."""
        assert run(monkeypatch, dbfile, "--apply") == 2

    def test_an_existing_journal_is_never_overwritten(self, dbfile, tmp_path, monkeypatch):
        """A second apply would overwrite the 'pre-re-collection baseline' with the 'post-re-collection state', and the credential would be gone (following the UTC script's precedent)."""
        journal = tmp_path / "j.json"
        journal.write_text("{}")
        assert run(monkeypatch, dbfile, "--apply", "--journal", str(journal)) == 2
        assert journal.read_text() == "{}"

    def test_all_rows_of_one_run_share_one_batch_timestamp(self, dbfile, tmp_path):
        """The same batch shares one moment — both a fact and the signature of hard constraint (2)'s windowing."""
        for i in range(3):
            tp = write_transcript(tmp_path / f"a{i}.jsonl", [("r1", USAGE_ONE)])
            add_agent(dbfile, f"a{i}", transcript_path=str(tp))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert {r.values["tokens_measured_at"] for r in job.written()} == {BATCH}

    def test_leader_rerun_never_promotes_a_ghost_of_a_measured_file(self, dbfile, tmp_path):
        """Job D's idempotency holds for ghost rows too: a file's representation follows 'who has already posted accounts', not the scan order.

        A main-session transcript file is shared by multiple rows (ghost rows). After the first apply measures the earliest row,
        a rerun puts that row into already_measured and exits early — if it no longer occupies its place, the ghost rows of the same file would slip past the
        duplicate interception and be promoted to candidates; a second apply would count the same usage twice (production re-run measured 2026-08-03:
        after the first round wrote 10 rows, a rerun reported 5 rows to write, all ghosts of already-measured files).
        """
        tp = write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)])
        add_agent(
            dbfile, "L1", role="leader", transcript_path=str(tp),
            created_at="2026-07-20 10:00:00", measured_at=BATCH, source="transcript",
            tokens=USAGE_ONE,
        )
        add_agent(
            dbfile, "L2-ghost", role="leader", transcript_path=str(tp),
            created_at="2026-07-21 10:00:00",
        )
        job = bf.job_leader_usage(load(dbfile), bf.Parser(verbose=False), BATCH, enabled=True)
        assert job.written() == []
        reasons = {r.row_id: r.reason for r in job.rows}
        assert reasons["L1"] == "already_measured"
        assert reasons["L2-ghost"] == "duplicate_main_transcript"


# ===========================================================================
# Reason codes — rows that cannot be written must be classified (no-data ≠ zero)
# ===========================================================================
class TestReasonCodes:
    def test_missing_path_and_gone_file_are_different_reasons(self, dbfile):
        """§3.4: a single 'not measured' total cannot say whether they can be saved; the two classes are handled completely differently."""
        add_agent(dbfile, "a1", transcript_path=None)
        add_agent(dbfile, "a2", transcript_path="/definitely/not/here.jsonl")
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert job.counts() == {"no_transcript_path": 1, "transcript_gone": 1}

    def test_an_empty_transcript_is_unreadable_not_zero_tokens(self, dbfile, tmp_path):
        """no-data ≠ zero: failing to parse a snapshot must never land as 'used 0 tokens'."""
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        add_agent(dbfile, "a1", transcript_path=str(empty))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert [r.reason for r in job.rows] == ["unreadable_transcript"]
        assert job.written() == []

    def test_gone_rows_are_listed_row_by_row_not_just_counted(self, dbfile, capsys):
        """This class only grows and never shrinks; the report itself is the evidence that 'the window has closed on these rows' (§3.4)."""
        add_agent(dbfile, "a1", name="lost-worker", transcript_path="/gone/x.jsonl")
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        bf.print_transcript_gone([job])
        out = capsys.readouterr().out
        assert "lost-worker" in out and "/gone/x.jsonl" in out

    def test_the_all_clear_case_says_so_explicitly(self, dbfile, capsys):
        """0 rows cannot be left blank — a blank would be read as a bug, whereas here 0 is good news."""
        add_agent(dbfile, "a1", transcript_path=None)
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        bf.print_transcript_gone([job])
        assert "0 rows" in capsys.readouterr().out

    def test_every_reason_used_by_the_jobs_has_a_human_description(self):
        """Reason codes are for humans to read; miss one description and the report will contain a code nobody recognizes."""
        assert set(bf.REASON_ORDER) == set(bf.REASONS)
        assert all(bf.REASONS.values())


# ===========================================================================
# Model backfill by observation — observe only, do not infer
# ===========================================================================
class TestModelObservationBackfill:
    def test_alias_is_replaced_by_the_model_observed_in_the_transcript(self, dbfile, tmp_path):
        """§6.2-4: a transcript's message.model is always the full model number, never an alias."""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)], model="claude-opus-4-8")
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1")
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert job.written()[0].values == {"model": "claude-opus-4-8"}
        assert job.written()[0].guard == {"model": "opus"}  # The guard pins 'still the alias I read'

    def test_a_row_without_a_transcript_is_left_alone_not_guessed(self, dbfile):
        """"Rows without a transcript are not guessed at and not touched" — the alias ledger only covers on the read side and never lands in the database."""
        add_wa(dbfile, "wa1", model="opus", os_agent_id=None)
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert [r.reason for r in job.rows] == ["no_transcript_path"]
        assert job.written() == []

    def test_the_alias_ledger_is_never_consulted_for_writing(self):
        """MODEL_ALIAS_LEDGER is a read-side fallback; the re-collection script should not even reference it.

        The criterion goes through the AST rather than a text search: in docstrings it **must** be possible to talk about this boundary ('the alias resolution result never
        gets written into any row' is exactly the discipline to write down); it can be talked about but not called. A text search would judge talking about the discipline itself as
        a violation, and that kind of machine check would only force people to delete the discipline from the comments.
        """
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        referenced = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {
            alias.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
        }
        assert "MODEL_ALIAS_LEDGER" not in referenced
        assert "resolve_model_alias" not in referenced

    def test_a_concrete_model_is_never_overwritten_by_another_observation(self, dbfile, tmp_path):
        """An observed value does not overwrite an observed value — this script only fills in truth; it does not re-judge."""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)], model="claude-opus-5")
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="claude-opus-4-8", os_agent_id="a1")
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert [r.reason for r in job.rows] == ["already_concrete"]

    def test_synthetic_only_transcript_yields_no_observed_model(self, dbfile, tmp_path):
        """A compact synthetic row's model is <synthetic>, not any real model number (§1.3)."""
        tp = tmp_path / "syn.jsonl"
        tp.write_text(
            json.dumps({
                "type": "assistant", "requestId": "r1",
                "message": {"model": "<synthetic>", "usage": {
                    "input_tokens": 1, "output_tokens": 2,
                    "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}},
            }) + "\n",
            encoding="utf-8",
        )
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1")
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert [r.reason for r in job.rows] == ["no_observed_model"]

    def test_agents_model_is_filled_only_when_the_column_is_empty(self, dbfile, tmp_path):
        add_agent(dbfile, "a1", model="claude-fable-5",
                  transcript_path=str(write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        row = job.written()[0]
        assert "model" not in row.values          # An observed value already exists; do not touch it
        assert "inconsistent" in row.warn                # But a human can see it

    def test_no_model_flag_turns_the_observation_backfill_off(self, dbfile, tmp_path, monkeypatch):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1")
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"), "--no-model")
        assert fetch(dbfile, "select model from workflow_agents where id='wa1'")[0] == "opus"
        assert fetch(dbfile, "select model from agents where id='a1'")[0] is None
        # But the tokens that should be collected were collected
        assert fetch(dbfile, "select tokens_source from agents where id='a1'")[0] == "transcript"


# ===========================================================================
# Job B — supplementing tokens_source for already-measured rows (including zero-tolerance recomputation reconciliation)
# ===========================================================================
class TestSourceLabelRecompute:
    def _measured(self, dbfile, tmp_path, stored: dict[str, int]):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp), measured_at=BATCH, tokens=stored)
        return bf.job_source_label(load(dbfile), bf.Parser(verbose=False))

    def test_label_is_written_only_when_recompute_matches_field_by_field(self, dbfile, tmp_path):
        """The shape of gate 2 (1): the parser is a pure function, with zero tolerance rather than 10%. Only equality dares to mark provenance."""
        job = self._measured(dbfile, tmp_path, USAGE_ONE)
        assert job.written()[0].values == {"tokens_source": "transcript"}

    def test_a_grown_transcript_is_its_own_class_not_an_anomaly(self, dbfile, tmp_path):
        """The agent is still alive (e.g. the one running this script); after measurement the transcript grew again — normal."""
        smaller = {k: v - 1 for k, v in USAGE_ONE.items()}
        job = self._measured(dbfile, tmp_path, smaller)
        assert [r.reason for r in job.rows] == ["transcript_grew"]
        assert job.written() == []

    def test_a_genuine_divergence_is_reported_and_never_written(self, dbfile, tmp_path):
        """A mismatch that is not growth = a real anomaly; a human must look, and no value may be changed because of it."""
        bigger = {k: v + 1000 for k, v in USAGE_ONE.items()}
        job = self._measured(dbfile, tmp_path, bigger)
        assert [r.reason for r in job.rows] == ["recompute_mismatch"]
        assert job.written() == []

    def test_an_already_labelled_row_is_left_alone(self, dbfile, tmp_path):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp), measured_at=BATCH, source="transcript")
        job = bf.job_source_label(load(dbfile), bf.Parser(verbose=False))
        assert [r.reason for r in job.rows] == ["already_set"]

    def test_unmeasured_rows_are_not_this_jobs_business(self, dbfile, tmp_path):
        add_agent(dbfile, "a1", transcript_path=str(
            write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])))
        assert bf.job_source_label(load(dbfile), bf.Parser(verbose=False)).rows == []


# ===========================================================================
# Leader main session — off by default + dedup by file
# ===========================================================================
class TestLeaderIsOptIn:
    def test_leader_rows_are_untouched_by_default(self, dbfile, tmp_path, monkeypatch, capsys):
        tp = write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"))
        assert fetch(dbfile, "select tokens_measured_at from agents where id='L1'")[0] is None
        assert "Job D is not executed by default" in capsys.readouterr().out

    def test_include_leader_actually_writes(self, dbfile, tmp_path, monkeypatch):
        tp = write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"),
            "--include-leader")
        assert fetch(dbfile, "select tokens_source from agents where id='L1'")[0] == "transcript"

    def test_one_transcript_is_counted_once_no_matter_how_many_rows_point_at_it(
        self, dbfile, tmp_path
    ):
        """A measured 47 Leader rows point to only 13 files, with at most one file shared by 11 rows. Accepting them all as listed =
        counting the same 850 million tokens of usage 11 times over."""
        tp = str(write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)]))
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=tp,
                  created_at="2026-07-20 08:00:00")
        add_agent(dbfile, "L2", name="Leader", role="leader", transcript_path=tp,
                  created_at="2026-07-20 09:00:00")
        add_agent(dbfile, "L3", name="Leader", role="leader", transcript_path=tp,
                  created_at="2026-07-20 10:00:00")
        job = bf.job_leader_usage(load(dbfile), bf.Parser(verbose=False), BATCH, enabled=True)
        assert len(job.written()) == 1
        assert job.written()[0].row_id == "L1"  # The earliest row represents; same source as _find_leader
        assert job.counts()["duplicate_main_transcript"] == 2

    def test_a_disabled_job_is_previewed_but_never_applied(self, dbfile, tmp_path):
        """The preview must be computed as usual — otherwise a human has no way to judge whether to flip this switch."""
        tp = write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=str(tp))
        job = bf.job_leader_usage(load(dbfile), bf.Parser(verbose=False), BATCH, enabled=False)
        assert len(job.written()) == 1  # The determination is made as usual
        con = sqlite3.connect(dbfile)
        assert bf.apply_job(con, job) == 0  # But the write layer skips it directly
        con.close()


# ===========================================================================
# Parser reuse — the same file is not read repeatedly
# ===========================================================================
class TestParserCache:
    def test_a_shared_transcript_is_parsed_once(self, tmp_path):
        """13 main-session files are shared by 47 rows; parsing row by row would read 2 GB several times over."""
        tp = str(write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)]))
        parser = bf.Parser(verbose=False)
        first = parser.usage(tp)
        second = parser.usage(tp)
        assert first == second
        assert parser.parsed == 1

    def test_a_missing_file_is_cached_as_none_not_retried(self, tmp_path):
        parser = bf.Parser(verbose=False)
        assert parser.usage(str(tmp_path / "nope.jsonl")) is None
        assert parser.usage(str(tmp_path / "nope.jsonl")) is None
        assert parser.parsed == 1
