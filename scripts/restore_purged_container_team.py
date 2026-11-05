#!/usr/bin/env python3
"""One-off restore: a whole team wrongly deleted by the container-team cleanup + the four-layer token accounts on its
member rows. Dry-run by default.

Forensics in report ``b168eb34-9c19-40e5-8ecb-624a74ccbc5a`` §3 class 1 and the §4 recovery plan. The problem to solve
is: when ``purge_stale_session_containers`` shipped with v1.11.0 (2026-07-27), the five token columns had
not yet landed on the ``agents`` rows (they landed in v1.11.1, 2026-07-29); the retention gate asked "who still has
records
that someone will come and look at attached to this team" (the six associated tables), while the token account is **a
column of the row itself**, present in no associated table, so the criterion system was naturally
blind to it. On 2026-08-03 01:49 (UTC), team ``835c6d0a`` was cleaned out as an empty shell, together with the
attribution accounts of
**325,075,503 tokens** on 6 worker rows — 4.74% of all measured volume in the database at the time.

**Three sources, each with its own job** (all read-only; if one is missing this script should not exist):

* **Identity primary source** ``aiteam.db.bak-tokenbackfill-20260729115732`` (07-29 11:57 +0800).
  The team row, member rows and activity records are taken column by column from here. There are two **measured**
  reasons for choosing it over the 07-28 backup named by the forensics report,
  see "Why not use the 07-28 one as the primary source" below.
* **The account credential** ``token-backfill-journal-20260729.json``.
  The recovery credential of ``backfill_token_usage.py --apply``, recording row by row the four-layer values written
  into each row at the time, the batch
  time and the observed ``model``. It answers "at deletion time, did these rows carry accounts or not".
* **Third-party corroboration source** ``aiteam.db.bak-ledgerfix-20260728164849`` (07-28 16:48 +0800).
  After conversion to the target database's clock convention, it is compared **column by column** with the identity
  primary source: only continue if they agree. Its reading is special — it carries a WAL header
  and has no ``-shm``, so ``mode=ro`` fails to open it with error 14; it must use ``immutable=1`` (read-only, does not
  look for a WAL,
  creates no sidecar files; for "one of only two credentials", producing no sidecar files is itself a protection).

Plus the **transcripts themselves**: all 6 are still on disk with mtimes earlier than the journal (a sub-agent
transcript is frozen the moment it completes),
and are re-parsed through the **same production** :func:`parse_transcript_usage`. The journal and the re-parse are
independent
comparisons of each other; **if any single field of either disagrees, the whole transaction aborts**, with no "take one
of them" degradation. This check's precondition
determines that it necessarily passes, so the moment it does not pass, it means reality disagrees with the forensics
conclusion, and that is exactly the moment to stop.

**Why not use the 07-28 one as the primary source** — the forensics report §4.3's remark that "the backup and the live
database differ only in the ``tokens_source``
column" compared schemas, not content, and in content it differs from the target database in two things:

1. **A different clock convention.** The 07-28 backup has ``PRAGMA user_version = 0`` while the target database is at
   :data:`MIGRATION_MARKER` — it was taken **before** the UTC shift (v1.11.1 / ``migrate_timestamps_utc.py``),
   on one database's local wall clock (+0800). Comparing rows of the same id measures exactly 8 hours, with identical
   microsecond digits. INSERTing them verbatim
   would stuff hundreds of rows of +8h timestamps into a UTC database — precisely the "a second clock appeared in the
   database" of the I11 red line. Corroboration: this
   team's ``updated_at`` read as UTC is 07-27 01:49:19, and the purge happened on 08-03 01:49:14, landing **exactly**
   on the 7-day threshold; read as local it is only 6.67 days and would never have been deleted at all — the very fact
   of deletion says the target database is
   on the UTC convention.
2. **Columns backfilled in the meantime would be lost.** In the 07-28 backup, the 6 worker rows' ``session_id`` are all
NULL, while at deletion
   time they were already ``abff40af-…`` (backfilled on 07-29 by ``backfill_agent_session_ids.py``). Restoring from
   07-28
   would be throwing away that backfill's result a second time.

So the 07-28 one was not discarded but **demoted to a corroboration source**: the script uses
``migrate_timestamps_utc.py``'s own
:data:`LOCAL_COLUMNS` and ``shift_for`` (looking the offset up in the system timezone database by the date in question,
never hardcoding −8) to convert it to the target
convention, then compares column by column with the primary source. Measured: 866 timestamp cells with zero deviation;
among non-timestamp columns only 6 ``session_id`` places
went NULL → a value, judged "backfilled in the meantime; take the later one". **Any contradiction between the two
sources aborts**: both non-empty and unequal, or the later one having lost
a value to NULL instead, are both "two snapshots disagreeing about the identity of the same row", not something the
script should decide on its own authority.

Six disciplines determine the shape of this script (the same paradigm as ``backfill_token_usage.py``, reused rather than
rebuilt):

1. **Sources are always read-only; the target is not written by default.** All three sources are read-only throughout;
the journal and transcripts are neither changed nor moved. By default
   it is a dry-run; only ``--apply`` writes, and with ``--apply`` ``--journal`` is required and an existing target file
   hard-refuses (a second
   apply would overwrite the credential with the after-the-fact state). Before ``--apply``, a pre-backup of the target
   database is made, going through SQLite's own
   backup API rather than ``cp``: the target database is in WAL mode, and ``cp`` copying only the main file would yield
   an **old snapshot without the WAL contents**,
   the kind of "backup" that is only discovered to be useless at the moment it is really needed.
2. **The write surface is a closed set.** It only INSERTs into the three tables in :data:`RESTORE_TABLES`, and only for
this run's id set;
   everything is an INSERT, with **no UPDATE/DELETE path at all**. ``workflow_agents.tokens`` (the ctx_last
   metric) is never in the write surface, and the backfill script's **per-row sha256 fingerprint** is reused for
   before/after comparison, rolling back the whole transaction on any mismatch
   — directly reusing ``backfill_token_usage.workflow_tokens_fingerprint``; there is only one copy of the fingerprint
   algorithm,
   so the two sides cannot each drift a little.
3. **Rows and accounts land in the same transaction.** Any reap tick between inserting rows and writing accounts would
delete this batch of "account-less husks"
   once more (the new gate ``_carries_token_ledger`` judges per team with ``any(...)``, looking at exactly these
   columns).
   So ``BEGIN IMMEDIATE`` takes the write lock in one go, and rows, accounts and activity records commit together; full
   acceptance is done inside the transaction, and
   any item failing rolls back on the spot with ``ROLLBACK``. Once the restore completes, this team hits the new gate
   because its 6 members carry accounts, and its self-protection holds.
4. **``tokens_measured_at`` gets the original batch time :data:`BATCH_TS`, never now().** That field serves
   two purposes at once: one backfill stamping thousands of rows with the same value = **the natural signature of a
   backfill batch** (``BATCH_COHORT_MIN``
   windows by it), while the live chain writing each row's own microsecond stop time = **collection health**. Writing
   now() would fabricate
   6 "new measurements" and pollute the live-coverage cell; writing the original time returns these 6 rows to the
   existing batch of 1903 rows —
   which is also the fact: they really were measured in that batch. The value comes from the 07-29 journal, and the
   backfill that wrote it ran
   after the UTC shift, so it is already UTC and takes part in no clock conversion.
5. **The clock convention is an explicit criterion, not an assumption.** The target database and every source get their
``PRAGMA user_version`` read: if equal,
   values are taken as-is; if the source is 0 and the target is :data:`MIGRATION_MARKER`, conversion follows the shift
   script's rules; **any other
   combination aborts**. The conversion reuses the shift script's column list and shift function, and replicates word
   for word its way of preserving sub-second digits
   (only the prefix "up to the minute" is moved; the seconds and sub-second digits are spliced back as-is) — so restored
   rows and their never-deleted sibling rows
   are the same kind of thing at the byte level.
6. **Idempotency is structural.** The first branch of the verdict is "target id already exists → ``already_present``",
and the SQL layer
   also carries a ``WHERE NOT EXISTS`` guard (against someone inserting between dry-run and apply), so a re-run
   necessarily
   changes nothing, rather than relying on marker bits or "I remember running it".

**Schema adaptation**: the INSERT lists column names explicitly **per the target database**; a column missing from a
source must have an explicit rule in
:data:`MISSING_COLUMN_DEFAULTS`, or it is treated as schema drift and aborts directly — silently filling NULL
is exactly the cause of "a field quietly disappeared and nobody knew". The rule for ``agents.tokens_source`` is: **rows
verified by both sources to carry an account**
get ``'transcript'`` (that is this batch of accounts' real source), while **rows that never carried an account keep
NULL** — in the live database, all 697 unmeasured
rows have a NULL ``tokens_source``; stamping a source onto a row that was never measured would be fabricating a
provenance out of nothing.

**Scope**: only :data:`RESTORE_TEAM_ID` is restored (1 team + 7 agents rows + 835 activities).
The other 10 Leader husks named by forensics report §4.1 are **out of scope**: they carry no accounts, and the moment
they are restored they would again satisfy
every purge criterion and be deleted again at the next reap tick, adding only 10 more purge events. Wanting them back is
a policy decision
(the criteria must be changed at the same time), not a call this script can make for anyone.

Usage::

    python3 scripts/restore_purged_container_team.py                         # dry-run (default; the live database is
    read-only)
    python3 scripts/restore_purged_container_team.py --db /tmp/rehearsal.db  # rehearse against a copy
    python3 scripts/restore_purged_container_team.py --db /tmp/rehearsal.db \\
        --apply --journal /tmp/restore-journal.json

Running ``--apply`` against the live database **requires user approval** (the interruptible-release-pipeline
discipline), and it is advisable to stop the API first or confirm there is no concurrent
reap tick — although ``BEGIN IMMEDIATE`` already makes this stretch atomic, one less concurrency surface is always less
to worry about.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiteam.clock import utc_now  # noqa: E402
from aiteam.services.token_attribution import parse_transcript_usage  # noqa: E402

BACKFILL_SCRIPT = ROOT / "scripts" / "backfill_token_usage.py"
MIGRATE_SCRIPT = ROOT / "scripts" / "migrate_timestamps_utc.py"


def _load_script_module(name: str, path: Path) -> Any:
    """Load a sibling script from the same directory, reusing the criteria it has already fixed in stone.

    Deliberately not copied: the forbidden column's fingerprint algorithm, and the UTC shift's column list and shift
    function — if each kept its own copy, changing one place in the future
    would grow two sets of criteria, and both classes of error are **indistinguishable after the fact**. Both scripts
    have only imports and constants at top level,
    with ``main()`` behind a ``__main__`` guard, so loading has no side effects.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file is in the repo; if it is missing, the
    # environment is broken
        msg = f"Sibling script cannot be loaded: {path}"
        raise RuntimeError(msg)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_bf = _load_script_module("backfill_token_usage", BACKFILL_SCRIPT)
_mig = _load_script_module("migrate_timestamps_utc", MIGRATE_SCRIPT)

TOKEN_COLUMNS: tuple[str, ...] = _bf.TOKEN_COLUMNS
FORBIDDEN_COLUMN: tuple[str, str] = _bf.FORBIDDEN_COLUMN
workflow_tokens_fingerprint = _bf.workflow_tokens_fingerprint

# The UTC shift script's three authoritative criteria: which columns are local wall clock, how far to shift, and the
# idempotency marker on the database.
LOCAL_COLUMNS: dict[str, tuple[str, ...]] = _mig.LOCAL_COLUMNS
MIGRATION_MARKER: int = _mig.MIGRATION_MARKER
shift_for = _mig.shift_for
parse_db_ts = _mig.parse_db_ts

DATA_DIR = Path.home() / ".claude" / "data" / "ai-team-os"
DEFAULT_DB = DATA_DIR / "aiteam.db"
# Identity primary source: 07-29 11:57, a snapshot after the UTC shift, after the session_id backfill, and before the
# deletion.
DEFAULT_SOURCE = Path.home() / "aiteam.db.bak-tokenbackfill-20260729115732"
# Third-party corroboration source: 07-28 16:48, before the UTC shift (local wall clock); must be converted before
# comparing.
DEFAULT_CORROBORANT = DATA_DIR / "aiteam.db.bak-ledgerfix-20260728164849"
DEFAULT_VERIFY_JOURNAL = Path.home() / "token-backfill-journal-20260729.json"

# The wrongly deleted team. The whole script revolves around this one id; --team exists only so tests can run the same
# path against another database.
RESTORE_TEAM_ID = "835c6d0a-6652-4058-b263-ef891ea84579"

# The batch time of the 2026-07-29 backfill (journal.batch_ts verbatim, already UTC). See discipline 4 for the reason.
BATCH_TS = "2026-07-29 03:57:32.707912"

# Write surface: only INSERT into these three tables, and only for this run's id set. There is no UPDATE / DELETE path.
RESTORE_TABLES: tuple[str, ...] = ("teams", "agents", "agent_activities")
WRITABLE_TABLES: frozenset[str] = frozenset(RESTORE_TABLES)

# A column the target database has but the source does not must have an explicit rule here; otherwise it counts as
# schema drift and aborts.
# The value is "what to fill when there is no overlay". The 'transcript' for ``agents.tokens_source`` is supplied by the
# overlay
# (only rows verified by both sources to really carry an account deserve a source label); the None here is the fallback
# for account-less rows.
MISSING_COLUMN_DEFAULTS: dict[tuple[str, str], Any] = {
    ("agents", "tokens_source"): None,
}

MISSING_COLUMN_NOTES: dict[tuple[str, str], str] = {
    ("agents", "tokens_source"): (
        "This column only went live in v1.11.1 (07-29). Rows with accounts get 'transcript' from the overlay; rows "
        "without accounts keep NULL — "
        "consistent with the convention of the 697 unmeasured rows in the live database"
    ),
}


@dataclass(frozen=True)
class TeamExpectation:
    """The expected fingerprint of "did this team restore correctly" — the numbers from forensics report §3 class 1,
    compared item by item, hard.

    Only in effect for the known team. Other teams (synthetic databases used by tests) have no expected values, and the
    counting gate will explicitly declare itself skipped,
    rather than pretending an empty dict passed the comparison.
    """

    agents: int
    measured_agents: int
    activities: int
    layers: dict[str, int]

    @property
    def token_total(self) -> int:
        return sum(self.layers.values())


EXPECTATIONS: dict[str, TeamExpectation] = {
    RESTORE_TEAM_ID: TeamExpectation(
        agents=7,  # 6 worker + 1 Leader
        measured_agents=6,  # the Leader row carried no account to begin with
        activities=835,
        layers={
            "input_tokens": 3_912,
            "output_tokens": 1_043_862,
            "cache_creation_tokens": 17_684_918,
            "cache_read_tokens": 306_342_811,
        },
    ),
}


class VerificationError(RuntimeError):
    """Dual-source verification / third-party corroboration failed — reality disagrees with the forensics conclusion;
    the whole restore aborts."""


class SchemaDriftError(RuntimeError):
    """The column sets of source and target databases show an unregistered difference — silently filling NULL would
    quietly lose fields."""


class ClockConventionError(RuntimeError):
    """The clock conventions of source and target databases do not match and no known conversion exists — mixed
    conventions entering the database are indistinguishable after the fact."""


# ---------------------------------------------------------------------------
# Connections (sources being read-only is physical, not a matter of self-discipline)
# ---------------------------------------------------------------------------
def open_immutable(path: Path) -> sqlite3.Connection:
    """Source-only: ``immutable=1``.

    Some of these snapshots carry a WAL header and have no ``-shm``; ``mode=ro`` fails to open them with error 14;
    ``immutable=1`` tells SQLite this file will not change, so it neither looks for a WAL nor creates any sidecar files.
    """
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def open_target(path: Path, *, writable: bool) -> sqlite3.Connection:
    """Target database: a dry-run always uses ``mode=ro`` (physically incapable of writing); only ``--apply`` gets a
    normal connection.

    ``isolation_level=None`` turns off sqlite3's implicit transaction management — this script issues
    ``BEGIN IMMEDIATE`` itself; discipline 3 wants "rows and accounts in the same transaction", not "however the driver
    sees fit".
    """
    if writable:
        con = sqlite3.connect(str(path), isolation_level=None)
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(r["name"]) for r in con.execute(f"pragma table_info({table})")]


def clock_marker(con: sqlite3.Connection) -> int:
    """The UTC-shift idempotency marker on a database — to know which clock convention a database is on, ask the
    database itself."""
    return int(con.execute("pragma user_version").fetchone()[0])


def needs_clock_shift(source_marker: int, target_marker: int) -> bool:
    """Whether a source needs conversion before it can enter the target database. Other than the two known combinations,
    refuse to execute.

    * Both markers equal → same convention, values taken as-is;
    * Source 0 / target :data:`MIGRATION_MARKER` → the source is pre-shift local wall clock; convert per the shift
    script;
    * Everything else (including "the source has been shifted but the target has not") → there is no known correct
    approach; aborting is far safer than guessing a direction.
    """
    if source_marker == target_marker:
        return False
    if source_marker == 0 and target_marker == MIGRATION_MARKER:
        return True
    msg = (
        f"There is no known clock conversion between source database user_version={source_marker} and target database "
        f"user_version={target_marker}"
        f"(known: equal, or 0→{MIGRATION_MARKER}) — aborting"
    )
    raise ClockConventionError(msg)


def shift_value(raw: Any) -> Any:
    """Local wall clock → UTC, replicating ``migrate_timestamps_utc.shift_expr``'s minute-prefix semantics word for
    word.

    The shift script only moves the prefix "up to the minute" and splices the seconds and sub-second digits back as-is
    (``datetime(col,'-8 hours')`` would
    truncate the result to whole seconds, casually erasing the microsecond digits, while the event ledger's
    within-the-same-second ordering, when sorted by timestamp, depends on
    those very digits). It must be exactly the same here, so restored rows are byte-level isomorphic with their
    never-deleted sibling rows.

    The shift comes from ``shift_for``: the system timezone database is consulted **for the date in question**, rather
    than hardcoding −8.
    """
    if raw is None:
        return None
    text = str(raw)
    head = parse_db_ts(text[:16] + ":00")
    if head is None:
        msg = f"Timestamp cannot be parsed; dare not convert: {text!r}"
        raise ClockConventionError(msg)
    moved = head + shift_for(head)
    return moved.strftime("%Y-%m-%d %H:%M") + text[16:]


def normalize_clock(table: str, values: dict[str, Any], *, shift: bool) -> dict[str, Any]:
    """Convert the local-wall-clock columns of one row to UTC. The column list is taken from the shift script; no
    second copy is established."""
    if not shift:
        return values
    out = dict(values)
    for col in LOCAL_COLUMNS.get(table, ()):
        if col in out and out[col] is not None:
            out[col] = shift_value(out[col])
    return out


# ---------------------------------------------------------------------------
# Identity primary source
# ---------------------------------------------------------------------------
def load_source_team(con: sqlite3.Connection, team_id: str) -> sqlite3.Row | None:
    return con.execute("select * from teams where id = ?", (team_id,)).fetchone()


def load_source_agents(con: sqlite3.Connection, team_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "select * from agents where team_id = ? order by created_at", (team_id,)
    ).fetchall()


def load_source_activities(con: sqlite3.Connection, agent_ids: list[str]) -> list[sqlite3.Row]:
    if not agent_ids:
        return []
    marks = ",".join("?" * len(agent_ids))
    return con.execute(
        f"select * from agent_activities where agent_id in ({marks}) order by timestamp",  # noqa: S608 - the number of
        # placeholders is determined by the number of ids; all values are parameterized
        agent_ids,
    ).fetchall()


# ---------------------------------------------------------------------------
# The account credential: the per-row written values recorded in the backfill journal
# ---------------------------------------------------------------------------
def load_journal_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """Take the per-row written values for the ``agents`` table out of the backfill journal, returning ``id -> values``.

    Only jobs with ``table == 'agents'`` that really carry four-layer values count: the same journal also holds
    ``workflow_agents.model`` and ``tokens_source`` back-label records; mixing those in would blur the judgment of "does
    this row carry an account".
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    ledger: dict[str, dict[str, Any]] = {}
    for job in data.get("jobs", []):
        if job.get("table") != "agents" or not job.get("enabled"):
            continue
        for row in job.get("written", []):
            values = row.get("values") or {}
            if not any(c in values for c in TOKEN_COLUMNS):
                continue
            ledger[str(row["id"])] = values
    return ledger


# ---------------------------------------------------------------------------
# Dual-source strong verification (discipline: any disagreement between the two aborts the whole transaction)
# ---------------------------------------------------------------------------
@dataclass
class LedgerCheck:
    """The dual-source verification result for one row — only on success is there an ``overlay``; failure raises
    inside :func:`verify_ledger`."""

    agent_id: str
    name: str
    transcript_path: str
    layers: dict[str, int]
    model: str
    api_calls: int
    overlay: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.layers.values())


def verify_ledger(
    agent_rows: list[sqlite3.Row],
    journal_ledger: dict[str, dict[str, Any]],
    *,
    batch_ts: str = BATCH_TS,
) -> list[LedgerCheck]:
    """Re-parse the transcripts and reconcile them against the journal field by field, returning the overlay for rows
    that carry accounts.

    Four classes of abort conditions, each corresponding to one kind of "reality disagrees with the forensics
    conclusion":

    * The row has a ``transcript_path`` but the file is no longer on disk → one source short; dual-source verification
    cannot be done;
    * The file is there but no snapshot can be parsed → same as above (no-data ≠ zero; never let it land as "used 0
    tokens");
    * A row with an account is not found in the journal / the four layers do not match / the observed model name does
    not match → the two sources diverge;
    * The journal has an account but the source row has no ``transcript_path`` → an account with no explanation.

    There is no degradation switch like ``--force``: this check's preconditions (a sub-agent transcript is frozen on
    completion, and all
    mtimes are earlier than the journal) determine that it necessarily passes, so when it does not pass, the only
    correct action is to stop.
    """
    checks: list[LedgerCheck] = []
    for row in agent_rows:
        aid = str(row["id"])
        name = str(row["name"])
        path = str(row["transcript_path"] or "")
        journal_values = journal_ledger.get(aid)

        if not path:
            if journal_values:
                msg = (
                    f"{name}({aid[:8]}) has an account in the journal, but the source row has no transcript_path — "
                    f"an account with no explanation; not restoring"
                )
                raise VerificationError(msg)
            continue  # No path and no account (a Leader husk): restore the identity as-is; the account never existed

        if journal_values is None:
            msg = (f"{name}({aid[:8]}) has a transcript_path but its account is not in the journal — the two sources "
                f"diverge")
            raise VerificationError(msg)

        usage = parse_transcript_usage(path)
        if usage is None:
            msg = (
                f"{name}({aid[:8]})'s transcript yields no snapshot (file missing or entirely corrupt): {path} — "
                f"dual-source verification is one source short; aborting"
            )
            raise VerificationError(msg)

        fresh = {c: int(usage[c]) for c in TOKEN_COLUMNS}
        recorded = {c: int(journal_values.get(c, 0)) for c in TOKEN_COLUMNS}
        if fresh != recorded:
            diff = {c: (recorded[c], fresh[c]) for c in TOKEN_COLUMNS if recorded[c] != fresh[c]}
            msg = (
                f"{name}({aid[:8]}) dual-source mismatch (journal vs re-parse): {diff} — "
                f"the whole restore aborts; no take-one-of-them degradation"
            )
            raise VerificationError(msg)

        observed = str(usage.get("model") or "")
        journal_model = str(journal_values.get("model") or "")
        if journal_model and journal_model != observed:
            msg = (
                f"{name}({aid[:8]}) observed model name mismatch: journal={journal_model!r} vs "
                f"re-parse={observed!r} — the whole restore aborts"
            )
            raise VerificationError(msg)

        overlay: dict[str, Any] = dict(fresh)
        overlay["tokens_measured_at"] = batch_ts
        overlay["tokens_source"] = "transcript"
        if observed and not str(row["model"] or ""):
            # Observational backfill: that column in the source row is empty, and the transcript holds a full model
            # name. The same source and value
            # as what the backfill wrote at the time (just compared above), so what is restored is "what the row really
            # looked like at the moment of deletion".
            overlay["model"] = observed

        checks.append(
            LedgerCheck(
                agent_id=aid,
                name=name,
                transcript_path=path,
                layers=fresh,
                model=observed,
                api_calls=int(usage.get("api_calls") or 0),
                overlay=overlay,
            )
        )
    return checks


# ---------------------------------------------------------------------------
# Third-party corroboration (what another snapshot says about the same batch of rows)
# ---------------------------------------------------------------------------
@dataclass
class Corroboration:
    """One discrepancy between the corroboration source and the primary source. ``agree`` entries are not recorded
    individually, only counted."""

    table: str
    row_id: str
    column: str
    primary: Any
    secondary: Any
    verdict: str  # filled_later / contradiction


@dataclass
class CorroborationReport:
    source: str
    shifted: bool
    cells_compared: int = 0
    rows_missing: int = 0
    findings: list[Corroboration] = field(default_factory=list)

    @property
    def contradictions(self) -> list[Corroboration]:
        return [f for f in self.findings if f.verdict == "contradiction"]

    @property
    def filled_later(self) -> list[Corroboration]:
        return [f for f in self.findings if f.verdict == "filled_later"]


def corroborate_rows(
    report: CorroborationReport,
    con: sqlite3.Connection,
    table: str,
    primary_rows: list[sqlite3.Row],
    *,
    id_col: str = "id",
) -> None:
    """Convert the corroboration source to the target convention and compare it with the primary source column by
    column, recording the result in ``report``.

    Three criteria, covering every shape of "two snapshots saying different things about the same row":

    * Both sides equal → agreement (counted only);
    * The corroboration source is NULL and the primary source has a value → **backfilled in the meantime**; take the
    primary source (it is the later snapshot). Record it,
      because it simultaneously shows "restoring from the corroboration source would lose this column" — which is
      precisely the evidence that the primary source must be the later one;
    * Everything else (both non-empty and unequal / the primary source having lost the value instead) →
    **contradiction**; abort. Which side is right is not for the script to guess.

    Rows missing from the corroboration source are not a problem: it may simply be a snapshot from before that row
    existed; they are counted and reported truthfully.
    """
    secondary_cols = set(table_columns(con, table))
    for row in primary_rows:
        rid = str(row[id_col])
        other = con.execute(
            f"select * from {table} where {id_col} = ?",  # noqa: S608 - the table name comes from an allowlist
            # constant, the column names from constants in this file
            (rid,),
        ).fetchone()
        if other is None:
            report.rows_missing += 1
            continue
        shared = [c for c in row.keys() if c in secondary_cols]
        normalized = normalize_clock(
            table, {c: other[c] for c in shared}, shift=report.shifted
        )
        for col in shared:
            report.cells_compared += 1
            mine, theirs = row[col], normalized[col]
            if mine == theirs:
                continue
            verdict = "filled_later" if theirs is None and mine is not None else "contradiction"
            report.findings.append(Corroboration(table, rid, col, mine, theirs, verdict))


# ---------------------------------------------------------------------------
# Plan (the first branch of idempotency: target id already exists → already_present)
# ---------------------------------------------------------------------------
@dataclass
class RowPlan:
    table: str
    row_id: str
    label: str
    reason: str  # restorable / already_present
    values: dict[str, Any] = field(default_factory=dict)


def build_values(
    table: str,
    source_row: sqlite3.Row,
    target_cols: list[str],
    source_cols: set[str],
    overlay: dict[str, Any],
    *,
    shift_clock: bool,
) -> dict[str, Any]:
    """Take values column by column **per the target database's** column names: source → clock conversion → overlay on
    top.

    A "column missing from the source" may not degenerate into "just fill NULL": it must be registered in
    :data:`MISSING_COLUMN_DEFAULTS`,
    or it means schema drift nobody has ever looked at has appeared between source and target; aborting is far safer
    than guessing a value.

    The overlay lands last, and **takes part in no clock conversion** — its ``tokens_measured_at`` comes from the 07-29
    journal, and that backfill ran after the UTC shift, so it is UTC to begin with.
    """
    values: dict[str, Any] = {}
    for col in target_cols:
        if col in source_cols:
            values[col] = source_row[col]
        elif (table, col) in MISSING_COLUMN_DEFAULTS:
            values[col] = MISSING_COLUMN_DEFAULTS[(table, col)]
        else:
            msg = (
                f"{table}.{col} is in the target database but not in the source, and has no explicit missing-column "
                f"rule — "
                f"schema drift; aborting (filling NULL would quietly lose a field)"
            )
            raise SchemaDriftError(msg)
    values = normalize_clock(table, values, shift=shift_clock)
    for col, val in overlay.items():
        if col not in target_cols:
            msg = f"The overlay wants to write {table}.{col}, but the target database has no such column — aborting"
            raise SchemaDriftError(msg)
        values[col] = val
    return values


def existing_ids(con: sqlite3.Connection, table: str, ids: list[str]) -> set[str]:
    """The ids already in place in the target database — the sole basis for the idempotency verdict (queried in
    batches, avoiding the SQLite variable limit)."""
    found: set[str] = set()
    chunk = 400
    for i in range(0, len(ids), chunk):
        part = ids[i : i + chunk]
        marks = ",".join("?" * len(part))
        rows = con.execute(
            f"select id from {table} where id in ({marks})",  # noqa: S608 - the table name comes from a constant in
            # this file; all values are parameterized
            part,
        )
        found.update(str(r["id"]) for r in rows)
    return found


def plan_table(
    target_con: sqlite3.Connection,
    table: str,
    source_rows: list[sqlite3.Row],
    source_cols: set[str],
    *,
    overlays: dict[str, dict[str, Any]] | None = None,
    label_col: str = "name",
    shift_clock: bool = False,
) -> list[RowPlan]:
    if table not in WRITABLE_TABLES:
        msg = f"write-table whitelist interception: {table} is not in RESTORE_TABLES"
        raise RuntimeError(msg)
    target_cols = table_columns(target_con, table)
    dropped = source_cols - set(target_cols)
    if dropped:
        msg = (
            f"Columns {table} has in the source but the target database does not: {sorted(dropped)} — "
            f"the target database has dropped columns, restored content would be lost; a human must rule before "
            f"re-running"
        )
        raise SchemaDriftError(msg)

    ids = [str(r["id"]) for r in source_rows]
    present = existing_ids(target_con, table, ids)
    plans: list[RowPlan] = []
    for row in source_rows:
        rid = str(row["id"])
        label = str(row[label_col]) if label_col in source_cols else rid[:8]
        if rid in present:
            plans.append(RowPlan(table, rid, label, "already_present"))
            continue
        values = build_values(
            table, row, target_cols, source_cols, (overlays or {}).get(rid, {}),
            shift_clock=shift_clock,
        )
        plans.append(RowPlan(table, rid, label, "restorable", values))
    return plans


# ---------------------------------------------------------------------------
# Writing (single transaction + in-transaction acceptance + rollback on failure)
# ---------------------------------------------------------------------------
def insert_row(con: sqlite3.Connection, plan: RowPlan) -> int:
    """An insert carrying a ``WHERE NOT EXISTS`` guard — the guard lives in the SQL, not only in the verdict layer.

    Between verdict and write, the target database may be written by someone else at any moment: the guard makes a row
    that "has been inserted in the meantime" yield automatically,
    so a re-run necessarily changes nothing, rather than relying on "I remember running it".
    """
    if plan.table not in WRITABLE_TABLES:
        msg = f"write-table whitelist interception: {plan.table} is not in RESTORE_TABLES"
        raise RuntimeError(msg)
    cols = list(plan.values)
    marks = ",".join("?" * len(cols))
    sql = (
        f"insert into {plan.table} ({', '.join(cols)}) "  # noqa: S608 - the table name comes from an allowlist
        # constant, the column names from the target database's pragma
        f"select {marks} where not exists (select 1 from {plan.table} where id = ?)"
    )
    cur = con.execute(sql, [*plan.values.values(), plan.row_id])
    return int(cur.rowcount or 0)


def verify_after(
    con: sqlite3.Connection,
    team_id: str,
    agent_ids: list[str],
    activity_ids: list[str],
    expectation: TeamExpectation | None,
    *,
    batch_ts: str = BATCH_TS,
) -> list[str]:
    """In-transaction acceptance, returning a list of problems (empty = pass). Any single item failing rolls back."""
    problems: list[str] = []

    if not con.execute("select 1 from teams where id = ?", (team_id,)).fetchone():
        problems.append("The team row is not in place")

    marks = ",".join("?" * len(agent_ids)) if agent_ids else "''"
    present_agents = int(
        con.execute(
            f"select count(*) from agents where id in ({marks})",  # noqa: S608 - the number of placeholders is
            # determined by the number of ids
            agent_ids,
        ).fetchone()[0]
    )
    if present_agents != len(agent_ids):
        problems.append(f"Member rows in place: {present_agents}/{len(agent_ids)}")

    layer_sql = ", ".join(f"coalesce(sum({c}), 0)" for c in TOKEN_COLUMNS)
    layer_row = con.execute(
        f"select {layer_sql}, count(tokens_measured_at) from agents where id in ({marks})",  # noqa: S608 - the column
        # names come from constants in this file
        agent_ids,
    ).fetchone()
    layers = {c: int(layer_row[i]) for i, c in enumerate(TOKEN_COLUMNS)}
    measured = int(layer_row[len(TOKEN_COLUMNS)])

    activity_present = 0
    chunk = 400
    for i in range(0, len(activity_ids), chunk):
        part = activity_ids[i : i + chunk]
        amarks = ",".join("?" * len(part))
        activity_present += int(
            con.execute(
                f"select count(*) from agent_activities where id in ({amarks})",  # noqa: S608 - the number of
                # placeholders is determined by the number of ids
                part,
            ).fetchone()[0]
        )
    if activity_present != len(activity_ids):
        problems.append(f"Activity records in place: {activity_present}/{len(activity_ids)}")

    stray_ts = int(
        con.execute(
            f"select count(*) from agents where id in ({marks}) "  # noqa: S608 - the number of placeholders is
            # determined by the number of ids
            f"and tokens_measured_at is not null and tokens_measured_at != ?",
            [*agent_ids, batch_ts],
        ).fetchone()[0]
    )
    if stray_ts:
        problems.append(f"{stray_ts} rows' tokens_measured_at is not the original batch time {batch_ts}")

    # The clock convention's backstop gate: restored rows are all historical rows, so any "future" timestamp means the
    # conversion went the wrong way.
    now_text = utc_now().strftime("%Y-%m-%d %H:%M:%S.%f")
    future = int(
        con.execute(
            f"select count(*) from agents where id in ({marks}) and created_at > ?",  # noqa: S608 - the number of
            # placeholders is determined by the number of ids
            [*agent_ids, now_text],
        ).fetchone()[0]
    )
    if future:
        problems.append(f"{future} rows' created_at falls in the future — the clock conversion went the wrong way")

    if expectation is None:
        return problems

    if len(agent_ids) != expectation.agents:
        problems.append(f"Member row count {len(agent_ids)} ≠ expected {expectation.agents}")
    if len(activity_ids) != expectation.activities:
        problems.append(f"Activity record count {len(activity_ids)} ≠ expected {expectation.activities}")
    if measured != expectation.measured_agents:
        problems.append(f"Account-carrying rows {measured} ≠ expected {expectation.measured_agents}")
    for col, want in expectation.layers.items():
        if layers[col] != want:
            problems.append(f"{col} total {layers[col]:,} ≠ expected {want:,}")
    if sum(layers.values()) != expectation.token_total:
        problems.append(f"Four-layer total {sum(layers.values()):,} ≠ expected {expectation.token_total:,}")
    return problems


def make_pre_backup(db: Path, dest_dir: Path) -> Path:
    """The pre-backup before ``--apply`` — through the SQLite backup API rather than ``cp``.

    The target database is in WAL mode: ``cp`` taking only the main file would produce an **old snapshot without the WAL
    contents**, and that kind of "backup"
    is only discovered to be empty at the moment it is really needed. The backup API reads a consistent snapshot, with
    the WAL contents included.
    """
    # Go through aiteam.clock's single clock (I11 red line: no second clock may exist in the database).
    stamp = utc_now().strftime("%Y%m%d%H%M%S")
    dest = dest_dir / f"{db.name}.bak-restore-{stamp}"
    # A re-run within the same second must start a new filename rather than overwrite — the whole value of a pre-backup
    # lies in "never having been written".
    serial = 1
    while dest.exists():
        serial += 1
        dest = dest_dir / f"{db.name}.bak-restore-{stamp}-{serial}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def print_checks(checks: list[LedgerCheck]) -> None:
    print()
    print("=" * 92)
    print("Dual-source strong verification: re-parsing the transcripts (production parse_transcript_usage) vs the "
        "07-29 backfill journal")
    print("-" * 92)
    if not checks:
        print("  This team has no account-carrying rows at all — only identity is restored; no accounts are involved.")
        return
    for c in checks:
        layers = " ".join(f"{k.replace('_tokens', '')}={v:,}" for k, v in c.layers.items())
        print(f"  ✓ {c.agent_id[:8]}  {c.name[:24]:<24} {layers}")
        print(f"      Total {c.total:,} / api_calls={c.api_calls} / model={c.model or '(no output)'}")
    print(f"  ── {len(checks)} rows agree between the two sources field by field, {sum(c.total for c in checks):,} "
        f"tokens in total")


def print_corroboration(report: CorroborationReport | None) -> None:
    print()
    print("=" * 92)
    print("Third-party corroboration: what another snapshot says about the same batch of rows")
    print("-" * 92)
    if report is None:
        print("  ⚠ Review source unavailable — this time the identity columns and clock conversion are guaranteed by "
            "computation only, not corroborated by an independent snapshot.")
        return
    print(f"  Corroboration source: {report.source}")
    print(
        "  Clock conversion: "
        + (
            "converted to UTC per the shift script before comparing"
            if report.shifted
            else "same convention, compared as-is"
        )
    )
    print(f"  Compared cells: {report.cells_compared}; Rows missing from the review source: {report.rows_missing}")
    if report.filled_later:
        print(f"  ── {len(report.filled_later)} places backfilled in the meantime (corroboration source NULL → primary "
            f"source has a value; take the primary source)")
        for f in report.filled_later[:10]:
            print(f"     · [{f.table}] {f.row_id[:8]} {f.column} = {f.primary!r}")
        if len(report.filled_later) > 10:
            print(f"       …… {len(report.filled_later) - 10} more places")
    if not report.contradictions:
        print("  ✓ zero contradictions — the two snapshots agree on the identity of this batch of rows")


def print_plan(plans: list[RowPlan]) -> None:
    print()
    print("=" * 92)
    print("Restore plan (the first branch of idempotency: target id already exists → already_present)")
    print("-" * 92)
    for table in RESTORE_TABLES:
        rows = [p for p in plans if p.table == table]
        if not rows:
            continue
        restorable = [p for p in rows if p.reason == "restorable"]
        present = [p for p in rows if p.reason == "already_present"]
        print(
            f"  {table:<18} to restore {len(restorable):5d} │ already_present {len(present):5d} │ "
            f"total {len(rows):5d}"
        )
    print()
    for p in plans:
        if p.table == "agent_activities":
            continue  # Printing 835 activity records one by one carries no information; the count is above
        mark = "+" if p.reason == "restorable" else "="
        print(f"    {mark} [{p.table}] {p.row_id[:8]}  {p.label[:32]}  ({p.reason})")


def build_journal(
    args: argparse.Namespace,
    plans: list[RowPlan],
    checks: list[LedgerCheck],
    corroboration: CorroborationReport | None,
    clock: dict[str, Any],
    fp_before: dict[str, Any],
    fp_after: dict[str, Any],
    inserted: int,
    pre_backup: Path | None,
) -> dict[str, Any]:
    """journal = this restore's only credential: which ids were inserted, what the values are, each source's
    evidence, and the forbidden column's before/after fingerprints."""
    return {
        "script": "restore_purged_container_team.py",
        "db": str(args.db),
        "team_id": args.team,
        "metric": "usage_sum",
        "batch_ts": args.batch_ts,
        "generated_at": utc_now().isoformat(),
        "sources": {
            "identity": str(args.from_backup),
            "verify_journal": str(args.verify_journal),
            "corroborant": str(args.corroborate_with) if corroboration else "",
            "transcripts": [c.transcript_path for c in checks],
        },
        "clock": clock,
        "pre_backup": str(pre_backup) if pre_backup else "",
        "forbidden_column": {
            "column": f"{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]}",
            "before": fp_before,
            "after": fp_after,
            "unchanged": fp_before["sha256"] == fp_after["sha256"],
        },
        "dual_source_verification": [
            {
                "id": c.agent_id,
                "name": c.name,
                "transcript": c.transcript_path,
                "layers": c.layers,
                "total": c.total,
                "api_calls": c.api_calls,
                "model": c.model,
            }
            for c in checks
        ],
        "corroboration": (
            {
                "source": corroboration.source,
                "clock_shifted": corroboration.shifted,
                "cells_compared": corroboration.cells_compared,
                "rows_missing": corroboration.rows_missing,
                "filled_later": [
                    {"table": f.table, "id": f.row_id, "column": f.column, "value": f.primary}
                    for f in corroboration.filled_later
                ],
                "contradictions": [
                    {
                        "table": f.table,
                        "id": f.row_id,
                        "column": f.column,
                        "primary": f.primary,
                        "secondary": f.secondary,
                    }
                    for f in corroboration.contradictions
                ],
            }
            if corroboration
            else None
        ),
        "rows_inserted": inserted,
        "restored": {
            table: [
                {"id": p.row_id, "name": p.label, "values": p.values}
                if table != "agent_activities"
                else {"id": p.row_id}
                for p in plans
                if p.table == table and p.reason == "restorable"
            ]
            for table in RESTORE_TABLES
        },
        "already_present": {
            table: [p.row_id for p in plans if p.table == table and p.reason == "already_present"]
            for table in RESTORE_TABLES
        },
    }


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="target database (default: the production database)")
    ap.add_argument(
        "--from-backup", type=Path, default=DEFAULT_SOURCE,
        help="identity primary-source snapshot, read-only with immutable=1 throughout",
    )
    ap.add_argument(
        "--corroborate-with", type=Path, default=DEFAULT_CORROBORANT,
        help="third-party corroboration-source snapshot (read-only); if it does not exist, corroboration is skipped "
            "with a loud warning",
    )
    ap.add_argument(
        "--verify-journal", type=Path, default=DEFAULT_VERIFY_JOURNAL,
        help="the 07-29 backfill journal, as the independent comparison source for token values (read-only)",
    )
    ap.add_argument("--team", default=RESTORE_TEAM_ID, help="the id of the team to restore")
    ap.add_argument("--batch-ts", default=BATCH_TS, help="the batch time to write into tokens_measured_at")
    ap.add_argument("--apply", action="store_true", help="actually write (default: report only)")
    ap.add_argument("--journal", type=Path, help="required with --apply: where the recovery credential lands "
        "(hard-refused if it already exists)")
    ap.add_argument(
        "--pre-backup-dir", type=Path, help="directory where the pre-backup lands (default: the same directory as the "
            "target database)",
    )
    return ap


def _check_args(args: argparse.Namespace) -> int:
    """The hard-refusal list at the entry point. A non-zero return is the exit code."""
    if not args.db.exists():
        print(f"Target database does not exist: {args.db}", file=sys.stderr)
        return 2
    if not args.from_backup.exists():
        print(f"Identity primary source does not exist: {args.from_backup}", file=sys.stderr)
        return 2
    if not args.verify_journal.exists():
        print(f"Backfill journal does not exist: {args.verify_journal}", file=sys.stderr)
        return 2
    if args.db.resolve() == args.from_backup.resolve():
        print("❌ The target database and the identity primary source are the same file — a source must be read-only "
            "and may never be written", file=sys.stderr)
        return 2
    if args.apply and args.journal is None:
        print("❌ --apply requires --journal — that is this restore's only credential", file=sys.stderr)
        return 2
    if args.journal is not None and args.journal.exists():
        print(
            f"❌ The journal target file already exists: {args.journal}\n"
            "   A second apply would overwrite it with the post-restore state, and the credential would be gone. "
            "Choose another filename.",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bad = _check_args(args)
    if bad:
        return bad

    expectation = EXPECTATIONS.get(args.team)

    print(f"Target database: {args.db}")
    print(f"Mode: {'APPLY (writing)' if args.apply else 'DRY-RUN (read-only, mode=ro)'}")
    print(f"Identity primary source (immutable=1 read-only): {args.from_backup}")
    print(f"Account credential (read-only): {args.verify_journal}")
    print(f"Restore scope: team {args.team} (the whole team; the other 10 husks named by report §4.1 are out of scope)")
    print(f"Batch time: {args.batch_ts} (the original batch, **not** now() — this column doubles as the backfill "
        f"signature and collection health)")
    if expectation is None:
        print("⚠ No expected fingerprint registered for this team; the count/total gates are skipped this time (only "
            "forensically-established teams have hard expectations)")

    src = open_immutable(args.from_backup)
    tgt = open_target(args.db, writable=args.apply)
    corro_con: sqlite3.Connection | None = None
    try:
        target_marker = clock_marker(tgt)
        source_marker = clock_marker(src)
        shift_source = needs_clock_shift(source_marker, target_marker)
        print()
        print("=" * 92)
        print("Clock convention (PRAGMA user_version; mixed conventions are indistinguishable after the fact, so this "
            "is an explicit criterion, not an assumption)")
        print("-" * 92)
        print(
            f"  Target database = {target_marker} │ Identity primary source = {source_marker} │ "
            + "Conversion: "
            + (
                "local wall clock → UTC (per the column list and shift "
                "function of the shift script)"
                if shift_source
                else "not needed; same convention"
            )
        )

        team_row = load_source_team(src, args.team)
        if team_row is None:
            print(f"❌ There is no team {args.team} in the identity primary source", file=sys.stderr)
            return 2
        agent_rows = load_source_agents(src, args.team)
        agent_ids = [str(r["id"]) for r in agent_rows]
        activity_rows = load_source_activities(src, agent_ids)
        activity_ids = [str(r["id"]) for r in activity_rows]

        team_cols = set(table_columns(src, "teams"))
        agent_cols = set(table_columns(src, "agents"))
        activity_cols = set(table_columns(src, "agent_activities"))

        print()
        print(f"Read from the primary source: 1 team / {len(agent_rows)} agents rows / {len(activity_rows)} activities")
        for table, cols in (
            ("teams", team_cols), ("agents", agent_cols), ("agent_activities", activity_cols)
        ):
            for col in sorted(set(table_columns(tgt, table)) - cols):
                note = MISSING_COLUMN_NOTES.get(
                    (table, col), "(no note — will be treated as schema drift and aborted below)"
                )
                print(f"  Schema difference: {table}.{col} is in the target database but not the primary source — "
                    f"{note}")

        # Third-party corroboration — what another snapshot besides the primary source says
        corroboration: CorroborationReport | None = None
        if args.corroborate_with and args.corroborate_with.exists():
            corro_con = open_immutable(args.corroborate_with)
            corro_marker = clock_marker(corro_con)
            corroboration = CorroborationReport(
                source=str(args.corroborate_with),
                shifted=needs_clock_shift(corro_marker, target_marker),
            )
            corroborate_rows(corroboration, corro_con, "teams", [team_row])
            corroborate_rows(corroboration, corro_con, "agents", agent_rows)
            corroborate_rows(corroboration, corro_con, "agent_activities", activity_rows)
        print_corroboration(corroboration)
        if corroboration and corroboration.contradictions:
            lines = "\n".join(
                f"   - [{f.table}] {f.row_id[:8]} {f.column}: primary source={f.primary!r} vs "
                f"corroboration source={f.secondary!r}"
                for f in corroboration.contradictions[:20]
            )
            msg = (f"The two snapshots disagree on the identity columns in {len(corroboration.contradictions)} "
                f"places:\n{lines}")
            raise VerificationError(msg)

        journal_ledger = load_journal_ledger(args.verify_journal)
        checks = verify_ledger(agent_rows, journal_ledger, batch_ts=args.batch_ts)
        print_checks(checks)

        overlays = {c.agent_id: c.overlay for c in checks}
        plans = plan_table(tgt, "teams", [team_row], team_cols, shift_clock=shift_source)
        plans += plan_table(
            tgt, "agents", agent_rows, agent_cols, overlays=overlays, shift_clock=shift_source
        )
        plans += plan_table(
            tgt, "agent_activities", activity_rows, activity_cols,
            label_col="tool_name", shift_clock=shift_source,
        )
        print_plan(plans)

        fp_before = workflow_tokens_fingerprint(tgt)
        print()
        print("=" * 92)
        print("Hard constraint: the write surface is a closed set; INSERT only, and only for this run's ids")
        print("-" * 92)
        print(f"  Write-table allowlist: {sorted(WRITABLE_TABLES)} (no UPDATE / DELETE path)")
        print(
            f"  Forbidden column: {FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} — not in the write surface,"
            f"and compared before/after with the same per-row fingerprint as the backfill script"
        )
        print(
            f"  Pre-restore fingerprint: {fp_before['rows']} rows / sum={fp_before['sum']:,} / "
            f"sha256={fp_before['sha256'][:16]}…"
        )

        restorable = [p for p in plans if p.reason == "restorable"]
        print()
        print("=" * 92)
        print(f"Total to restore: {len(restorable)} rows")

        clock_note = {
            "target_user_version": target_marker,
            "source_user_version": source_marker,
            "source_shifted_to_utc": shift_source,
            "shifted_columns": {t: list(LOCAL_COLUMNS.get(t, ())) for t in RESTORE_TABLES}
            if shift_source
            else {},
        }

        if not args.apply:
            print("Dry-run finished; not one byte was written. Once confirmed correct, the Founder approves --apply "
                "--journal <path>.")
            print("Idempotency acceptance: after --apply, re-run this script and the rows to restore should be 0 (all "
                "already_present).")
            return 0

        pre_backup = make_pre_backup(args.db, args.pre_backup_dir or args.db.parent)
        print(f"Pre-backup generated (SQLite backup API, including WAL contents): {pre_backup}")

        inserted = 0
        tgt.execute("BEGIN IMMEDIATE")
        try:
            for plan in restorable:
                inserted += insert_row(tgt, plan)
            problems = verify_after(
                tgt, args.team, agent_ids, activity_ids, expectation, batch_ts=args.batch_ts
            )
            fp_after = workflow_tokens_fingerprint(tgt)
            if fp_after["sha256"] != fp_before["sha256"]:
                problems.append(
                    f"The {FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} fingerprint changed — the ctx_last metric was "
                    f"touched"
                )
            if problems:
                tgt.execute("ROLLBACK")
                print()
                print("❌ In-transaction acceptance failed; the whole transaction has been rolled back and the target "
                    "database is unchanged, word for word:", file=sys.stderr)
                for p in problems:
                    print(f"   - {p}", file=sys.stderr)
                return 4
            tgt.execute("COMMIT")
        except Exception:
            tgt.execute("ROLLBACK")
            raise

        fp_final = workflow_tokens_fingerprint(tgt)
        print()
        print(
            f"Restored: {inserted} rows (planned {len(restorable)} rows;"
            f"the difference = rows someone else inserted in the meantime; guards yielding is the expected behavior)"
        )
        print("  ✓ Rows and accounts landed in the same transaction — there is no 'account-less husk' window in "
            "between, and the reap tick has no opening")
        print(f"  ✓ Forbidden column unchanged row by row: sum={fp_final['sum']:,} / sha256={fp_final['sha256'][:16]}…")
        if expectation is not None:
            print(
                f"  ✓ Expected fingerprint fully hit: {expectation.agents} agents rows / "
                f"{expectation.activities} activities / four-layer total {expectation.token_total:,}"
            )

        args.journal.parent.mkdir(parents=True, exist_ok=True)
        args.journal.write_text(
            json.dumps(
                build_journal(
                    args, plans, checks, corroboration, clock_note,
                    fp_before, fp_final, inserted, pre_backup,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Journal written to disk: {args.journal}")
        print("Re-running this script should report 0 rows to restore — that is the idempotency acceptance test.")
        return 0
    except VerificationError as exc:
        print()
        print(f"❌ Verification failed; restore aborted (target database unchanged, word for word): "
            f"{exc}", file=sys.stderr)
        return 3
    except SchemaDriftError as exc:
        print()
        print(f"❌ Schema drift; restore aborted (target database unchanged, word for word): {exc}", file=sys.stderr)
        return 5
    except ClockConventionError as exc:
        print()
        print(f"❌ Clock convention not convertible; restore aborted (target database unchanged, word for word): "
            f"{exc}", file=sys.stderr)
        return 6
    finally:
        src.close()
        tgt.close()
        if corro_con is not None:
            corro_con.close()


if __name__ == "__main__":
    raise SystemExit(main())
