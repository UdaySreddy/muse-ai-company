#!/usr/bin/env python3
"""Shift existing timestamps: local wall clock → UTC. Dry-run by default.

Spec in docs/utc-unification-design.md. In brief: this database used to run two wall clocks — the core domain wrote
host-local time, the ecosystem domain wrote UTC — and SQLite storage silently strips the offset, so rows of the two
conventions look
exactly alike. The code side has been unified to UTC (``aiteam.clock``); this script converts the local wall-clock
values
**already written** into UTC.

Four things determine the shape of this script:

1. **Shift per column + exclude row by row.** A column's writer kept one convention for most of the database's life (the
core domain
   always wrote local), but after the new code merged, **every newly started process writes UTC**, so a column will
   contain a minority of rows that are already
   UTC. While shifting the whole column, those rows must be picked out and excluded one by one — they are already in the
   target state, and subtracting
   another timezone would put them permanently 16 hours wrong. Detection is computed fresh against the whole database at
   execution time (see :func:`detect_contamination`)
   and consumes no fixed row-number list: the contamination grows with time, so yesterday's list is wrong today.
2. **Rows that cannot be judged may not be silently shifted.** Rows the evidence can judge (convention certain) are
handled automatically; rows it cannot
   go one by one into the journal's undecidable list, and only proceed after a human review plus ``--ack-undecidable``.
3. **Shift only once.** After a successful ``--apply``, an idempotency marker is stamped on the database (``PRAGMA
user_version``);
   a second ``--apply`` hard-refuses. A ``--journal`` target file that already exists likewise hard-refuses — it is the
   only recovery credential, and
   overwriting it is destroying it.
4. **Reversible and lossless.** The shift is a pure value transformation; sub-second digits are preserved as-is (see
:func:`shift_expr`);
   ``--rollback`` moves values back by the shift amount and exclusion list recorded in the journal; excluded rows are
   untouched both times.

Usage::

    python3 scripts/migrate_timestamps_utc.py                    # dry-run, whole-database report
    python3 scripts/migrate_timestamps_utc.py --db /tmp/copy.db  # rehearse against a copy
    python3 scripts/migrate_timestamps_utc.py --apply --journal ~/utc-journal.json
    python3 scripts/migrate_timestamps_utc.py --rollback --journal ~/utc-journal.json

``--apply`` / ``--rollback`` are executed personally by the Founder; back up first::

    cp ~/.claude/data/ai-team-os/aiteam.db ~/aiteam.db.bak-utc-$(date +%Y%m%d%H%M%S)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"

# ---------------------------------------------------------------------------
# The list of columns to shift — corresponding item by item to docs/utc-unification-design.md §3.1.
#
# The basis for judging is the writer's code (the core domain uniformly uses host-local datetime.now()), verified
# against the production database:
# each table's max(value) equals the **local** wall clock at forensics time, while the ecosystem domain's tables' max
# values fall on the UTC side,
# and the two domains' distributions do not overlap. ecosystem_* and pipeline_stage_history already write UTC and are
# **not in this list**.
# ---------------------------------------------------------------------------
LOCAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": ("created_at", "updated_at"),
    "phases": ("created_at", "updated_at"),
    "teams": ("created_at", "updated_at", "completed_at"),
    "agents": ("created_at", "last_active_at", "ctx_measured_at", "tokens_measured_at"),
    "tasks": ("created_at", "started_at", "completed_at"),
    "task_memos": ("created_at", "invalid_at"),
    "memories": ("created_at", "accessed_at", "invalid_at"),
    "events": ("timestamp",),
    "meetings": ("created_at", "concluded_at"),
    "meeting_messages": ("timestamp",),
    "agent_activities": ("timestamp",),
    "scheduled_tasks": ("created_at", "last_run_at", "next_run_at"),
    "cross_messages": ("created_at", "read_at"),
    "wake_sessions": ("started_at", "finished_at"),
    "leader_briefings": ("created_at", "resolved_at"),
    "channel_messages": ("created_at",),
    "reports": ("created_at",),
    "workflow_runs": (
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "last_activity_at",
    ),
    "workflow_agents": (
        "created_at",
        "updated_at",
        "started_at",
        "queued_at",
        "last_activity_at",
    ),
    "knowledge_links": ("created_at",),
}

# Each table's "birth column": written once at insert time and never updated afterwards. It is the **only** column that
# can use rowid (physical write
# order) for monotonicity detection — an increasing rowid means an increasing write order, and a value that drops back
# by about one timezone relative to the prefix maximum is
# the fingerprint of a UTC write. Every other column is rewritten in place by UPDATE, decoupling rowid from the value's
# timing; those can only be judged by the cross-column oracle.
# Which class a column belongs to **is not a heuristic; it is a fact of the writer's code**; when the ORM changes, this
# must be synced.
ROW_BIRTH_COLUMN: dict[str, str] = {
    "projects": "created_at",
    "phases": "created_at",
    "teams": "created_at",
    "agents": "created_at",
    "tasks": "created_at",
    "task_memos": "created_at",
    "memories": "created_at",
    "events": "timestamp",
    "meetings": "created_at",
    "meeting_messages": "timestamp",
    "agent_activities": "timestamp",
    "scheduled_tasks": "created_at",
    "cross_messages": "created_at",
    "wake_sessions": "started_at",
    "leader_briefings": "created_at",
    "channel_messages": "created_at",
    "reports": "created_at",
    "workflow_runs": "created_at",
    "workflow_agents": "created_at",
    "knowledge_links": "created_at",
}

# Columns guaranteed "not earlier than the birth column" — they record "what happened to this row later", so ``birth
# column <= this column``
# for the same row is a **structural invariant**, usable bidirectionally as an oracle (it constrains this column, and
# also gives the birth column
# an upper bound). The list is an allowlist, not a blacklist, because counterexamples really exist:
#   · scheduled_tasks.next_run_at records a future instant;
#   · workflow_runs/agents' started_at / queued_at / completed_at come from CC state files'
#     epochs, measured as early as 2026-06-23, weeks earlier than the created_at (2026-07-06) of the row landing in the
# database.
# Stuffing these columns in would make the oracle judge against a fake invariant.
AFTER_BIRTH_COLUMNS: set[tuple[str, str]] = {
    ("projects", "updated_at"),
    ("phases", "updated_at"),
    ("teams", "updated_at"),
    ("teams", "completed_at"),
    ("agents", "last_active_at"),
    ("agents", "ctx_measured_at"),
    ("agents", "tokens_measured_at"),
    ("tasks", "started_at"),
    ("tasks", "completed_at"),
    ("task_memos", "invalid_at"),
    ("memories", "accessed_at"),
    ("memories", "invalid_at"),
    ("meetings", "concluded_at"),
    ("scheduled_tasks", "last_run_at"),
    ("cross_messages", "read_at"),
    ("wake_sessions", "finished_at"),
    ("leader_briefings", "resolved_at"),
    ("workflow_runs", "updated_at"),
    ("workflow_runs", "last_activity_at"),
    ("workflow_agents", "updated_at"),
    ("workflow_agents", "last_activity_at"),
}

# Frozen/sensitive objects — shifted anyway (the column itself is not a frozen object), but archived separately and
# named in the report.
# The old memo JSON in tasks.config is the frozen archive; this script does not touch one byte of it.
FROZEN_NOTE = {
    "tasks": "tasks.config.memo is a frozen archive (CLAUDE.md); this script only touches DATETIME columns, not the "
        "JSON",
    "task_memos": "live data after the memory v2 table migration; the old JSON archive stays in tasks.config, "
        "untouched",
    "events": "append-only ledger (no modification surface); this is a value conversion, adding and removing no rows",
    "memories": "direction-layer memories; invalid_at is the invalidation axis and is shifted along, lest invalidation "
        "judging go out of alignment",
}

# Objects explicitly not touched, with reasons — printed verbatim in the report, so the next person does not have to
# re-investigate.
EXCLUDED = [
    ("ecosystem_* (15 tables / 36 columns)", "already written in UTC; already in the target state"),
    ("pipeline_stage_history.transitioned_at", "same as above, ecosystem-side UTC"),
    ("ecosystem_repo_profiles.pushed_at / last_commit_at", "values come from the GitHub API and are true UTC to begin "
        "with"),
    ("governance_lease.expires_at / updated_at", "VARCHAR, storing isoformat with +00:00"),
    ("loop_states.updated_at", "a dead table left by the retired cron engine; zero writers in the whole repo"),
    ("timestamps embedded in events.data", "an event payload is a verbatim record of what the hook reported at the "
        "time; changing it is tampering with testimony"),
    ("timestamps embedded in tasks.config.memo", "frozen archive (CLAUDE.md explicitly forbids cleaning it or writing "
        "to it)"),
    ("times in the body text of reports.content / task_memos.content", "words written by a human, not data"),
    ("rows detected as already UTC", "rows written by the new code are already in the target state and are excluded "
        "row by row (see the report's per-row convention verdicts section)"),
]

# Tolerance for whole-database judging: in a living system, "the most recent write" is never further in the past than
# this. It is set looser than one
# session interval but far smaller than a timezone offset, so that "hugging local now" and "hugging UTC now" can be told
# apart.
GUARD_TOLERANCE = timedelta(hours=3)

# Timezone fingerprint tolerance: for a value to be recognized as "another convention's expression of the same instant",
# its deviation from the expected position must
# be smaller than this. Minute-level rather than hour-level has a measured basis — in the production database,
# task_memos.created_at contains
# There exist 7.1-8.7 hours of **legitimate** disorder (the batch backfill during the memory v2 table migration); an
# hour-level tolerance would mistake them
# for timezone fingerprints.
SIGNATURE_TOLERANCE = timedelta(minutes=5)

# Convention witness sampling: only look at this many most recent rows. A median rather than max — max by nature only
# reflects the very few newest rows,
# and a minority mixed in can never enter max, which is exactly why the old guardrail was structurally blind to "mixed
# conventions".
WITNESS_TAIL = 200

# Idempotency marker: written into PRAGMA user_version after a successful shift. It is chosen because it is the least
# intrusive — no table, no column,
# no business-visible object, and SQLAlchemy / this repo's storage layer never reads or writes it (the production
# database measures 0).
MIGRATION_MARKER = 20260728


# ---------------------------------------------------------------------------
def shift_for(naive_local: datetime) -> timedelta:
    """The shift needed to convert a naive local wall-clock value to UTC (−8h on UTC+8).

    The offset is obtained from the system timezone database **for the date in question**, rather than hardcoding −8:
    hardcoding would move
    half a year of data wrongly on a machine with daylight saving time. Asia/Shanghai has had no DST since 1991, so on
    this machine it is effectively a constant −8 — but
    that is something :func:`assert_shift_is_constant` must **prove**, not assume.
    """
    return naive_local.astimezone(UTC).replace(tzinfo=None) - naive_local


def parse_db_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class Contamination:
    """The per-row convention verdict result for one column.

    ``excluded`` are the rows that are **already UTC** (written by the new code); shifting and rollback must both go
    around them;
    ``undecidable`` are rows whose evidence is insufficient to judge the convention; they go to a human for review.
    """

    def __init__(
        self,
        key: str,
        suspects: int = 0,
        excluded: list[int] | None = None,
        undecidable: list[dict] | None = None,
        method: str = "Not detected",
    ) -> None:
        self.key = key
        self.suspects = suspects
        self.excluded: list[int] = excluded or []
        self.undecidable: list[dict] = undecidable or []
        self.method = method

    @property
    def excluded_set(self) -> set[int]:
        return set(self.excluded)

    def as_journal(self) -> dict:
        return {
            "suspects": self.suspects,
            "method": self.method,
            "excluded_rowids": self.excluded,
            "undecidable": self.undecidable,
        }


class ColumnReport:
    def __init__(self, table: str, column: str) -> None:
        self.table = table
        self.column = column
        self.rows = 0
        self.non_null = 0
        self.min_before: str | None = None
        self.max_before: str | None = None
        self.samples: list[str] = []
        self.guard: str = "n/a"
        self.guard_ok = True
        # Only append-type columns (birth columns) have rowid monotonicity available, and only they qualify as
        # convention witnesses.
        self.is_birth = ROW_BIRTH_COLUMN.get(table) == column
        self.tail_median: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"

    def as_journal(self) -> dict:
        return {
            "table": self.table,
            "column": self.column,
            "non_null": self.non_null,
            "min_before": self.min_before,
            "max_before": self.max_before,
            "samples_before": self.samples,
            "guard": self.guard,
        }


def shift_expr(column: str, hours: float) -> str:
    """Shift expression — **preserves sub-second precision**.

    A direct ``datetime(col, '-8 hours')`` would truncate the result to whole seconds, casually erasing the microsecond
    digits of 320,000 rows.
    And the event ledger sorts by timestamp; within the same second, ordering depends on those very digits.

    Because the shift amount is necessarily a whole number of minutes (every existing timezone offset is a whole number
    of minutes, including +05:30 / +05:45),
    only the prefix "up to the minute" is moved, and the seconds and sub-second digits are spliced back as-is:

        "2026-07-28 17:01:25.068573"
         └─ first 16 chars ─┘└─ preserved as-is from char 17 on ─┘
    """
    minutes = round(hours * 60)
    sign = "+" if minutes >= 0 else "-"
    shifted_prefix = (
        f"datetime(substr({column}, 1, 16) || ':00', '{sign}{abs(minutes)} minutes')"
    )
    return f"substr({shifted_prefix}, 1, 16) || substr({column}, 17)"


def assert_uniform_width(
    conn: sqlite3.Connection, reports: list[ColumnReport], hours: float = -8.0
) -> None:
    """Confirm that every value to be shifted can be safely split by :func:`shift_expr`.

    Two checks, neither dispensable:

    * Width: a value shorter than ``YYYY-MM-DD HH:MM`` will certainly be destroyed by the prefix split;
    * **The direct criterion**: run the very expression that will be executed; any row whose "original value is
    non-empty but whose result is NULL"
      would be silently emptied. Sufficient width does not imply parseability (epoch float strings, illegal month/day,
      slash formats are all longer than 16
      characters yet still collapse to NULL), so the width check is necessary but not sufficient; this is the one that
      really works.
    """
    bad: list[str] = []
    for rep in reports:
        if rep.non_null == 0:
            continue
        n = conn.execute(
            f"select count(*) from {rep.table} "
            f"where {rep.column} is not null and length({rep.column}) < 16"
        ).fetchone()[0]
        if n:
            bad.append(f"{rep.key}: {n} rows have values shorter than 'YYYY-MM-DD HH:MM'")
        n = conn.execute(
            f"select count(*) from {rep.table} where {rep.column} is not null "
            f"and ({shift_expr(rep.column, hours)}) is null"
        ).fetchone()[0]
        if n:
            bad.append(f"{rep.key}: {n} rows become NULL after the shift expression (illegal value shape)")
    if bad:
        raise SystemExit(
            "❌ Cannot safely split: time values exist that cannot be safely split; refusing to execute (continuing "
            "would empty them into NULL):\n  "
            + "\n  ".join(bad)
        )


# ---------------------------------------------------------------------------
# Mixed-convention detection
#
# Background (measured, not deduced): the UTC new code has merged into master and aiteam is an editable install, so any
# process started after
# the merge (a new session's MCP server, a hook subprocess, the CLI, an API auto-revived) writes
# UTC. So the same column can hold rows of both conventions at once, and the contamination grows monotonically with time
# — therefore detection
# must be computed fresh at execution time.
#
# The skeleton of the criterion is one hard constraint: for a value v written in UTC, its real instant is v + offset,
# and that real
# instant must fall within [T0, now] (T0 = the earliest moment the new code could have run). Conversely,
#
#     only values falling in the **risk band** [T0 − offset, now − offset] could possibly have been written in UTC.
#
# Values outside the band are all **proven** local and are shifted as usual. Values inside the band are then judged row
# by row:
#
# * Append-type columns (birth columns): rowid is the write order. Take the maximum of all out-of-band rows before this
# row as the lower bound, and the value of the first
#   out-of-band row after it as the upper bound, and fit both readings, v and v+offset, into them — when only one
# reading is self-consistent, the verdict
#   stands; when both are or neither is, it goes to undecidable.
# * In-place update columns: take the same row's birth column as an anchor and judge by the invariant "an update cannot
# be earlier than the creation".
# ---------------------------------------------------------------------------
def risk_band(t0: datetime, now_local: datetime, forward_hours: float) -> tuple[datetime, datetime]:
    """Risk band: only **raw values** falling here could have been written in UTC; everything outside the band is proven
    local.

    The upper edge hugs the **execution moment**, not the newest write in the database — a UTC write could perfectly
    well have happened after the last
    local row, and closing the band at the in-database max would miss it. The cost is that running a dry-run against a
    long-stale snapshot lets the bandwidth
    grow with the wall clock and the candidate rows grow with it; that is not a false positive, it is the truthful
    reflection of "the longer you drag it out, the harder it is to judge".
    """
    offset = timedelta(hours=-forward_hours)
    edges = (t0 - offset, now_local - offset)
    return min(edges), max(edges)


def _scan(conn: sqlite3.Connection, table: str, column: str) -> list[tuple[int, datetime]]:
    rows = conn.execute(
        f"select rowid, {column} from {table} where {column} is not null order by rowid"
    ).fetchall()
    out: list[tuple[int, datetime]] = []
    for rid, raw in rows:
        parsed = parse_db_ts(raw)
        if parsed is not None:
            out.append((int(rid), parsed))
    return out


def detect_utc_writer_since(
    conn: sqlite3.Connection, reports: list[ColumnReport], offset: timedelta
) -> tuple[datetime | None, list[dict]]:
    """Forensics: when did the earliest UTC write in the database happen (expressed in local wall clock).

    Only look for **timezone-fingerprint disorder** on append-type columns — a value dropping back by exactly one
    offset relative to the prefix maximum of the write order (eastern hemisphere; in the western hemisphere it instead
    runs ahead of its successor rows). The tolerance is given in minutes; see :data:`SIGNATURE_TOLERANCE`.

    Returns (T0, evidence rows). T0 being None means there is no trace of any UTC write anywhere in the database.
    """
    evidence: list[dict] = []
    for rep in reports:
        if not rep.is_birth or rep.non_null == 0:
            continue
        rows = _scan(conn, rep.table, rep.column)
        if offset > timedelta(0):
            # Eastern hemisphere: a UTC value is earlier than a local value of the same instant, appearing as a
            # drop-back in rowid order.
            running: datetime | None = None
            for rid, v in rows:
                if running is not None and abs((running - v) - offset) <= SIGNATURE_TOLERANCE:
                    evidence.append(
                        {
                            "table": rep.table,
                            "column": rep.column,
                            "rowid": rid,
                            "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                            "real_time": (v + offset).strftime("%Y-%m-%d %H:%M:%S.%f"),
                        }
                    )
                if running is None or v > running:
                    running = v
        else:
            # Western hemisphere (UTC itself never gets here): a UTC value is later instead, appearing as running ahead
            # of successor rows.
            following: datetime | None = None
            for rid, v in reversed(rows):
                if following is not None and abs((v - following) + offset) <= SIGNATURE_TOLERANCE:
                    evidence.append(
                        {
                            "table": rep.table,
                            "column": rep.column,
                            "rowid": rid,
                            "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                            "real_time": (v + offset).strftime("%Y-%m-%d %H:%M:%S.%f"),
                        }
                    )
                if following is None or v < following:
                    following = v
    if not evidence:
        return None, []
    t0 = min(datetime.strptime(e["real_time"], "%Y-%m-%d %H:%M:%S.%f") for e in evidence)
    return t0, evidence


def _classify_birth_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    band: tuple[datetime, datetime],
    offset: timedelta,
) -> tuple[list[int], list[dict], int]:
    """Per-row verdict for an append-type column: pin the two readings down to one with rowid neighbours + same-row
    successor columns.

    Two evidence sources:

    * **rowid neighbours** (the lower bound takes the maximum of all out-of-band rows before it; the upper bound takes
    the first out-of-band row after it) — rowid
      is the physical write order, and a row's real instant necessarily lies between the two.
    * **Same-row successor columns** (:data:`AFTER_BIRTH_COLUMNS`) — a birth instant cannot be later than the instant
    this row was later
      updated. Only successor-column values falling **outside** the risk band are trusted (those have been proven
      local),
      so there is no circular dependency.
    """
    lo, hi = band
    siblings = [
        c
        for c in LOCAL_COLUMNS.get(table, ())
        if (table, c) in AFTER_BIRTH_COLUMNS
        and c in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    ]
    picked = ", ".join([column, *siblings])
    raw_rows = conn.execute(
        f"select rowid, {picked} from {table} where {column} is not null order by rowid"
    ).fetchall()
    rows: list[tuple[int, datetime]] = []
    ceiling: dict[int, datetime] = {}
    for record in raw_rows:
        parsed = parse_db_ts(record[1])
        if parsed is None:
            continue
        rid = int(record[0])
        rows.append((rid, parsed))
        for value in record[2:]:
            sib = parse_db_ts(value)
            # A successor column inside the band has an unsettled convention itself and cannot serve as evidence.
            if sib is None or lo <= sib <= hi:
                continue
            if rid not in ceiling or sib < ceiling[rid]:
                ceiling[rid] = sib

    suspect = [lo <= v <= hi for _, v in rows]
    n = len(rows)

    prev_clean: list[datetime | None] = [None] * n
    running: datetime | None = None
    for i, (_, v) in enumerate(rows):
        prev_clean[i] = running
        if not suspect[i] and (running is None or v > running):
            running = v

    next_clean: list[datetime | None] = [None] * n
    following: datetime | None = None
    for i in range(n - 1, -1, -1):
        next_clean[i] = following
        if not suspect[i]:
            following = rows[i][1]

    excluded: list[int] = []
    undecidable: list[dict] = []
    suspects = 0
    for i in range(n):
        if not suspect[i]:
            continue
        suspects += 1
        rid, v = rows[i]
        low = prev_clean[i]
        high = next_clean[i]
        roof = ceiling.get(rid)
        if roof is not None and (high is None or roof < high):
            high = roof

        def fits(moment: datetime, low: datetime | None = low, high: datetime | None = high) -> bool:
            if low is not None and moment < low - SIGNATURE_TOLERANCE:
                return False
            return not (high is not None and moment > high + SIGNATURE_TOLERANCE)

        local_ok, utc_ok = fits(v), fits(v + offset)
        if utc_ok and not local_ok:
            excluded.append(rid)
        elif local_ok and not utc_ok:
            continue
        else:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "reason": "both readings are self-consistent" if local_ok else "both readings conflict with the "
                        "neighbours",
                }
            )
    return excluded, undecidable, suspects


def _classify_updated_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    birth_column: str,
    birth_excluded: set[int],
    birth_pending: set[int],
    band: tuple[datetime, datetime],
    offset: timedelta,
) -> tuple[list[int], list[dict], int]:
    """Per-row verdict for an in-place update column: the cross-column consistency oracle.

    rowid is meaningless for this kind of column (values are rewritten in place by UPDATE, decoupling write order from
    value timing); the only usable thing
    is the invariant that **the update instant of the same row cannot be earlier than its creation instant**: take the
    same row's birth column as the anchor and fit both
    readings into it — if an update value is earlier than the creation instant by nearly one offset, it can only be in
    the UTC convention.

    This invariant only holds for :data:`AFTER_BIRTH_COLUMNS`; the other columns (future instants, external epoch
    sources) have no usable oracle, and their in-band rows all go to a human for review.
    """
    lo, hi = band
    excluded: list[int] = []
    undecidable: list[dict] = []
    suspects = 0
    usable = (table, column) in AFTER_BIRTH_COLUMNS
    rows = conn.execute(
        f"select rowid, {column}, {birth_column} from {table} where {column} is not null"
    ).fetchall()
    for raw_rid, raw, birth_raw in rows:
        v = parse_db_ts(raw)
        if v is None or not (lo <= v <= hi):
            continue
        suspects += 1
        rid = int(raw_rid)
        if not usable:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "reason": "this column may be earlier than the creation instant (a future instant or an external "
                        "epoch source); no usable invariant",
                }
            )
            continue
        anchor = parse_db_ts(birth_raw)
        if anchor is None:
            undecidable.append({"rowid": rid, "value": str(raw), "reason": "the anchor column is empty; nothing to "
                "compare against"})
            continue
        if rid in birth_pending:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "anchor": f"{birth_column}={anchor:%Y-%m-%d %H:%M:%S}",
                    "reason": "the anchor column's own convention awaits human review and cannot be used to settle the "
                        "case",
                }
            )
            continue
        # If the anchor column itself was judged UTC, its real creation instant is anchor + offset.
        born = anchor + offset if rid in birth_excluded else anchor
        local_ok = v >= born - SIGNATURE_TOLERANCE
        utc_ok = (v + offset) >= born - SIGNATURE_TOLERANCE
        if utc_ok and not local_ok:
            excluded.append(rid)
        elif local_ok and not utc_ok:
            continue
        else:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "anchor": f"{birth_column}={anchor:%Y-%m-%d %H:%M:%S}",
                    "reason": "neither reading violates 'an update is not earlier than the "
                        "creation'" if local_ok else "both readings conflict with the anchor",
                }
            )
    return excluded, undecidable, suspects


def detect_contamination(
    conn: sqlite3.Connection,
    reports: list[ColumnReport],
    forward_hours: float,
    now_local: datetime,
    since: datetime | None = None,
) -> tuple[dict[str, Contamination], datetime | None, list[dict]]:
    """Whole-database per-row convention detection. Computed fresh at execution time; consumes no fixed row-number list.

    Returns (per-column results, T0, evidence rows). When ``since`` is given it overrides the automatically
    forensically-derived T0 (the operations manual
    should fill in the moment the new code merged — that is the earliest time a UTC writer could have appeared).
    """
    offset = timedelta(hours=-forward_hours)
    found: dict[str, Contamination] = {r.key: Contamination(key=r.key) for r in reports}

    t0, evidence = detect_utc_writer_since(conn, reports, offset)
    if since is not None:
        t0 = since if t0 is None else min(t0, since)
    if t0 is None:
        for c in found.values():
            c.method = "No trace of UTC writes (risk band empty)"
        return found, None, evidence

    band = risk_band(t0, now_local, forward_hours)

    for rep in reports:
        if rep.non_null == 0:
            found[rep.key].method = "Empty column"
            continue
        birth_column = ROW_BIRTH_COLUMN.get(rep.table)
        if rep.is_birth:
            excluded, undecidable, suspects = _classify_birth_column(
                conn, rep.table, rep.column, band, offset
            )
            method = "rowid monotonicity + same-row successor columns (append-type column)"
        elif birth_column is None:
            found[rep.key].method = "No birth column to anchor on; all candidate rows go to human review"
            continue
        else:
            birth = found[f"{rep.table}.{birth_column}"]
            excluded, undecidable, suspects = _classify_updated_column(
                conn,
                rep.table,
                rep.column,
                birth_column,
                birth.excluded_set,
                {u["rowid"] for u in birth.undecidable},
                band,
                offset,
            )
            method = f"Cross-column consistency (anchor {birth_column})"
        entry = found[rep.key]
        entry.suspects = suspects
        entry.excluded = sorted(excluded)
        entry.undecidable = undecidable
        entry.method = method

    # An evidence row is certainly a UTC row; even if the neighbour verdict could not settle it due to sparse data, it
    # must be merged into the exclusion set.
    for e in evidence:
        entry = found.get(f"{e['table']}.{e['column']}")
        if entry is None:
            continue
        if e["rowid"] not in entry.excluded:
            entry.excluded = sorted([*entry.excluded, e["rowid"]])
        entry.undecidable = [u for u in entry.undecidable if u["rowid"] != e["rowid"]]
    return found, t0, evidence


def all_undecidable(contamination: dict[str, Contamination]) -> list[dict]:
    out: list[dict] = []
    for key, entry in contamination.items():
        for row in entry.undecidable:
            out.append({"column": key, **row})
    return out


# ---------------------------------------------------------------------------
def collect(conn: sqlite3.Connection, now_local: datetime) -> list[ColumnReport]:
    reports: list[ColumnReport] = []
    existing = {
        r[0] for r in conn.execute("select name from sqlite_master where type='table'")
    }
    now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)

    for table, columns in LOCAL_COLUMNS.items():
        if table not in existing:
            print(f"⚠️  Table {table} is not in the database, skipping", file=sys.stderr)
            continue
        table_rows = conn.execute(f"select count(*) from {table}").fetchone()[0]
        cols_present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            rep = ColumnReport(table, column)
            rep.rows = table_rows
            if column not in cols_present:
                print(f"⚠️  Column {table}.{column} is not in the database, skipping", file=sys.stderr)
                continue
            cnt, lo, hi = conn.execute(
                f"select count({column}), min({column}), max({column}) from {table}"
            ).fetchone()
            rep.non_null = cnt
            rep.min_before, rep.max_before = lo, hi
            rep.samples = [
                str(r[0])
                for r in conn.execute(
                    f"select {column} from {table} where {column} is not null "
                    f"order by {column} desc limit 3"
                )
            ]
            if rep.is_birth and cnt:
                tail = [
                    parsed
                    for r in conn.execute(
                        f"select {column} from {table} where {column} is not null "
                        f"order by rowid desc limit {WITNESS_TAIL}"
                    )
                    if (parsed := parse_db_ts(str(r[0]))) is not None
                ]
                if tail:
                    rep.tail_median = sorted(tail)[len(tail) // 2]

            # ---- Per-column self-proof (one-sided criterion; see the verdict() notes) ----
            newest = parse_db_ts(hi)
            if newest is None:
                rep.guard = "Empty column (no rows to judge)"
            elif newest > now_utc_naive:
                # A UTC clock cannot write a future value, so this column **necessarily** contains rows written by the
                # local wall clock.
                # This is positive confirmation, not a heuristic.
                rep.guard = f"Proven local (latest row {str(hi)[:19]} is later than UTC now {now_utc_naive:%H:%M:%S})"
            else:
                age = now_local - newest
                rep.guard = (f"Cold data (latest row is {age.days} days {age.seconds // 3600} hours ago, cannot "
                    f"self-prove)")
            reports.append(rep)
    return reports


def verdict(
    reports: list[ColumnReport],
    now_local: datetime,
    contamination: dict[str, Contamination] | None = None,
    ack_undecidable: bool = False,
) -> tuple[bool, list[str]]:
    """Whole-database verdict: is this database still being written by the local wall clock, and have the UTC rows that
    mixed in been recognized.

    The verdict sits at the **database level** rather than the column level: the real risk is a reversed deployment
    order, and deployment is global. A column that has been cold for
    three days has no fresh rows to judge in the first place; guessing column by column would only manufacture false
    alarms against cold data.

    The convention witness uses the **median of the tail of the freshest append-type column**, with two points of care:

    * Only append-type columns (birth columns) qualify as witnesses — their rowid is the write order, and the concept of
    "the most recent batch of writes"
      only holds for them; an in-place update column's max could come from any update of any row.
    * A median rather than max. Max by nature only reflects the very few newest rows: when the database has been shifted
    wholesale to UTC and old code
      writes one more local row, that single row suffices to make max hug local now — that is exactly how the old
      guardrail judged "an already-shifted
      database" as "shiftable". The median requires **the majority** of recent writes to be of one convention; a
      minority cannot overturn it.

    Positive confirmation is still one-sided and unambiguous: a UTC clock cannot write a future value, so a witness
    median later than UTC
    now is **necessarily** written by the local wall clock.

    The second layer of the verdict is mixed conventions: detected UTC rows must already have been recognized row by row
    and excluded; rows whose convention cannot be judged must
    pass human review (``--ack-undecidable``) before being let through — **never silently shifted**.
    """
    now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)
    notes: list[str] = []

    proven_local = [r for r in reports if r.guard.startswith("Proven local")]
    freshest = max(
        (parse_db_ts(r.max_before) for r in reports if r.max_before),
        default=None,
    )

    if freshest is None:
        notes.append("There are no non-empty time values in the database at all — no shift needed, and no verdict "
            "possible either.")
        return True, notes

    witness = None
    for rep in reports:
        if rep.tail_median is None:
            continue
        if witness is None or rep.tail_median > witness.tail_median:
            witness = rep

    notes.append(
        f"Newest write in the whole database {freshest:%Y-%m-%d %H:%M:%S};"
        f"{abs((now_local - freshest).total_seconds()) / 3600:.1f}h from local now,"
        f"{abs((now_utc_naive - freshest).total_seconds()) / 3600:.1f}h from UTC now"
    )
    notes.append(
        f"Columns positively proven to be on the local wall clock: {len(proven_local)} / {len(reports)}"
        "(max later than UTC now — a UTC clock cannot write a future value)"
    )

    passed = True
    if witness is None:
        notes.append("⚠️  No append-type column can serve as a convention witness (no rowid-trustworthy column in the "
            "database). Please confirm manually and add --force.")
        passed = False
    else:
        median = witness.tail_median
        assert median is not None
        d_local = abs((now_local - median).total_seconds())
        d_utc = abs((now_utc_naive - median).total_seconds())
        notes.append(
            f"Convention witness {witness.key}: median of the most recent {WITNESS_TAIL} rows "
            f"{median:%Y-%m-%d %H:%M:%S}"
            f"({d_local / 3600:.1f}h from local now, {d_utc / 3600:.1f}h from UTC now)"
        )
        if median > now_utc_naive + SIGNATURE_TOLERANCE:
            notes.append("✅ The writer is still the old code (local wall clock); shifting is OK.")
        elif d_utc <= GUARD_TOLERANCE.total_seconds() and d_utc < d_local:
            notes.append(
                "❌ The majority of recent writes already hug UTC now — the new code is very likely already writing to "
                "the database,"
                "or this database has already been shifted. Shifting again now would push these UTC rows one timezone "
                "early."
            )
            passed = False
        else:
            notes.append(
                "⚠️  The witness column is earlier than UTC now and hugs neither side (the whole database is cold "
                "data); it cannot self-prove."
                "Please confirm manually that this database really has not been written by the new code, then add "
                "--force to execute."
            )
            passed = False

    if contamination is not None:
        total_excluded = sum(len(c.excluded) for c in contamination.values())
        dirty = {k: c for k, c in contamination.items() if c.excluded}
        if dirty:
            notes.append(
                f"Mixed-convention detection: {total_excluded} rows across {len(dirty)} columns have been judged to be "
                f"in the UTC convention,"
                "and will be excluded row by row, taking no part in the shift."
            )
            for key, entry in sorted(dirty.items()):
                notes.append(f"    · {key}: {len(entry.excluded)} rows ({entry.method})")
        else:
            notes.append("Mixed-convention detection: no rows already in the UTC convention were found.")
        pending = all_undecidable(contamination)
        if pending:
            if ack_undecidable:
                notes.append(
                    f"⚠️  The convention of {len(pending)} rows cannot be judged; they have been confirmed via "
                    f"--ack-undecidable to be shifted in the local convention"
                    "(the row-by-row list is in the journal and in the per-row convention verdicts section above)."
                )
            else:
                notes.append(
                    f"❌ The convention of {len(pending)} rows cannot be judged — never silently shift. Please review "
                    f"the list above row by row,"
                    "confirm they really were written by the local wall clock, then add --ack-undecidable to execute."
                )
                passed = False
        else:
            notes.append("Rows whose convention cannot be judged: 0 (no --ack-undecidable needed).")

    return passed, notes


def assert_shift_is_constant(reports: list[ColumnReport]) -> float:
    """Prove the shift is constant over the whole data range; if it is not constant, refuse to shift with constant SQL.

    Check the endpoints of every column in full (not only the global min/max). Note this is endpoint sampling: if a
    column's head and tail are in the same
    daylight-saving state but its **middle** crosses a switchover point, this cannot detect it. It has no practical
    effect on Asia/Shanghai (no DST
    since 1991); if the script is moved to a host with DST, it must be changed to per-row conversion.
    """
    stamps = [
        parsed
        for rep in reports
        for raw in (rep.min_before, rep.max_before)
        if raw and (parsed := parse_db_ts(raw))
    ]
    if not stamps:
        return 0.0
    shifts = {shift_for(s).total_seconds() / 3600.0 for s in stamps}
    if len(shifts) != 1:
        raise SystemExit(
            f"❌ The local offset is not constant over the data range ({sorted(shifts)} hours observed) —"
            "this machine's timezone has daylight saving time, so shifting by a constant number of hours is not "
            "possible. Per-row conversion is needed."
        )
    return shifts.pop()


# ---------------------------------------------------------------------------
def read_marker(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def write_marker(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(value)}")


def _rowids(contamination: dict[str, Contamination] | None, key: str) -> set[int]:
    if not contamination or key not in contamination:
        return set()
    return contamination[key].excluded_set


# ---------------------------------------------------------------------------
def render(
    reports: list[ColumnReport],
    hours: float,
    mode: str,
    contamination: dict[str, Contamination] | None = None,
    since: datetime | None = None,
    band: tuple[datetime, datetime] | None = None,
) -> None:
    print(f"\n{'=' * 108}")
    print(f"Existing-timestamp shift · {mode} · shift {hours:+g} hours (local wall clock → UTC)")
    print(f"{'=' * 108}\n")
    print(f"{'Table':<20} {'Column':<20} {'Non-empty':>8} {'Excluded':>6}  {'Before min→max':<42} After min→max")
    print("-" * 108)

    total = 0
    total_excluded = 0
    for rep in reports:
        skipped = len(_rowids(contamination, rep.key))
        total_excluded += skipped
        total += max(rep.non_null - skipped, 0)
        if rep.non_null == 0:
            print(f"{rep.table:<20} {rep.column:<20} {0:>8} {0:>6}  (empty column, no rows to shift)")
            continue
        lo, hi = parse_db_ts(rep.min_before), parse_db_ts(rep.max_before)
        delta = timedelta(hours=hours)
        lo_a = (lo + delta).strftime("%Y-%m-%d %H:%M:%S") if lo else "?"
        hi_a = (hi + delta).strftime("%Y-%m-%d %H:%M:%S") if hi else "?"
        before = f"{str(rep.min_before)[:19]} → {str(rep.max_before)[:19]}"
        print(
            f"{rep.table:<20} {rep.column:<20} {rep.non_null:>8} {skipped:>6}  "
            f"{before:<42} {lo_a} → {hi_a}"
        )

    print("-" * 108)
    print(f"{'Total':<43} {total:>8} non-empty cells will be shifted, {total_excluded} cells excluded\n")

    print("Before/after samples (the newest 3 rows of each column)")
    print("-" * 108)
    for rep in reports:
        if not rep.samples:
            continue
        pairs = []
        for s in rep.samples:
            d = parse_db_ts(s)
            pairs.append(
                f"{str(s)[:19]} → {(d + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S') if d else '?'}"
            )
        print(f"  {rep.key:<46} {' | '.join(pairs)}")

    if contamination is not None:
        print("\nPer-row convention verdicts (mixed-convention detection: which rows are already UTC and take no part "
            "in the shift)")
        print("-" * 108)
        print(
            "  Forensic baseline T0 (the earliest UTC write instant, local wall clock):"
            + (f"{since:%Y-%m-%d %H:%M:%S}" if since else "no trace (risk band empty; the whole database is of a "
                "single convention)")
        )
        if band is not None:
            print(
                f"  Risk band (only raw values falling in this interval could be UTC-written): "
                f"{band[0]:%Y-%m-%d %H:%M:%S}"
                f" … {band[1]:%Y-%m-%d %H:%M:%S}; everything outside the band is proven local"
            )
        dirty = False
        for rep in reports:
            entry = contamination.get(rep.key)
            if entry is None or (not entry.excluded and not entry.undecidable):
                continue
            dirty = True
            shown = ", ".join(str(r) for r in entry.excluded[:20])
            more = f" …{len(entry.excluded)} rows in total" if len(entry.excluded) > 20 else ""
            print(
                f"  {rep.key:<40} candidates {entry.suspects:>6} rows · excluded {len(entry.excluded):>4} rows"
                f" · awaiting human review {len(entry.undecidable):>4} rows  [{entry.method}]"
            )
            if entry.excluded:
                print(f"      Excluded rowids: {shown}{more}")
            for row in entry.undecidable[:20]:
                print(f"      ⚠️ Awaiting human review rowid={row['rowid']} value={row['value']} — {row['reason']}")
            if len(entry.undecidable) > 20:
                print(f"      ⚠️ …and {len(entry.undecidable) - 20} more rows awaiting human review (full list in the "
                    f"journal)")
        if not dirty:
            print("  All columns are of a single convention; no rows need excluding.")

    print("\nPer-column self-proof (can it be proven that this column is still written by the local wall clock)")
    print("-" * 108)
    for rep in reports:
        print(f"   {rep.key:<46} {rep.guard}")

    print("\nFrozen/sensitive object notes")
    print("-" * 108)
    for table, note in FROZEN_NOTE.items():
        if any(r.table == table for r in reports):
            print(f"  {table:<22} {note}")

    print("\nObjects explicitly not shifted")
    print("-" * 108)
    for what, why in EXCLUDED:
        print(f"  {what:<48} {why}")


# ---------------------------------------------------------------------------
def execute(
    conn: sqlite3.Connection,
    reports: list[ColumnReport],
    hours: float,
    contamination: dict[str, Contamination] | None = None,
) -> int:
    """Shift per column, going around the rows that are already UTC, row by row.

    Immediately re-verify the non-empty row count after shifting: the shift expression returns NULL for malformed values
    (silently emptying them),
    while ``cur.rowcount`` only counts matched rows and cannot see this. If it does not add up, raise — at that point
    the transaction has not
    been committed, and closing the connection rolls the whole batch back; there is no half-shifted state.
    """
    assert_uniform_width(conn, reports, hours)
    touched = 0
    for rep in reports:
        if rep.non_null == 0:
            continue
        skip = _rowids(contamination, rep.key)
        where = f"{rep.column} is not null"
        if skip:
            conn.execute("drop table if exists temp._utc_skip")
            conn.execute("create temp table _utc_skip (rid integer primary key)")
            conn.executemany(
                "insert into _utc_skip (rid) values (?)", [(int(r),) for r in sorted(skip)]
            )
            where += " and rowid not in (select rid from _utc_skip)"
        cur = conn.execute(
            f"update {rep.table} set {rep.column} = {shift_expr(rep.column, hours)} where {where}"
        )
        after = conn.execute(
            f"select count({rep.column}) from {rep.table}"
        ).fetchone()[0]
        if after != rep.non_null:
            raise SystemExit(
                f"❌ {rep.key}: non-empty row count after shifting {after} ≠ before shifting {rep.non_null} — "
                "the shift emptied values into NULL. The transaction was not committed; the database is in its "
                "original state."
            )
        touched += cur.rowcount
        note = f"(plus {len(skip)} rows already UTC, excluded)" if skip else ""
        print(f"  {rep.key:<46} {cur.rowcount:>8} rows{note}")
    conn.execute("drop table if exists temp._utc_skip")
    conn.commit()
    return touched


def verify_rollback(conn: sqlite3.Connection, journal: dict) -> int:
    """After rollback, re-verify column by column: row counts and min/max must return to the pre-shift state recorded
    in the journal."""
    problems = 0
    for entry in journal["columns"]:
        table, column = entry["table"], entry["column"]
        cnt, lo, hi = conn.execute(
            f"select count({column}), min({column}), max({column}) from {table}"
        ).fetchone()
        if cnt != entry["non_null"]:
            print(f"  ❌ {table}.{column} row count {cnt} ≠ pre-shift {entry['non_null']}")
            problems += 1
        elif str(lo)[:19] != str(entry["min_before"])[:19] or str(hi)[:19] != str(
            entry["max_before"]
        )[:19]:
            print(
                f"  ❌ {table}.{column} range {str(lo)[:19]}→{str(hi)[:19]} "
                f"≠ pre-shift {str(entry['min_before'])[:19]}→{str(entry['max_before'])[:19]}"
            )
            problems += 1
        else:
            print(f"  ✅ {table}.{column} has returned to its pre-shift state")
    return problems


def contamination_from_journal(journal: dict) -> dict[str, Contamination]:
    """At rollback time the exclusion list **only recognizes the journal** — after the shift, the database itself no
    longer shows who is who."""
    out: dict[str, Contamination] = {}
    for key, entry in (journal.get("detection", {}).get("columns", {}) or {}).items():
        out[key] = Contamination(
            key=key,
            suspects=entry.get("suspects", 0),
            excluded=list(entry.get("excluded_rowids", [])),
            undecidable=list(entry.get("undecidable", [])),
            method=entry.get("method", ""),
        )
    return out


def build_journal(
    db: Path,
    reports: list[ColumnReport],
    forward: float,
    contamination: dict[str, Contamination],
    since: datetime | None,
    evidence: list[dict],
    mode: str,
    band: tuple[datetime, datetime] | None = None,
) -> dict:
    return {
        "db": str(db),
        "mode": mode,
        "executed_at": datetime.now(tz=UTC).isoformat(),
        "shift_hours": forward,
        "marker": MIGRATION_MARKER,
        "detection": {
            "utc_writer_since": since.strftime("%Y-%m-%d %H:%M:%S.%f") if since else None,
            "risk_band": [b.strftime("%Y-%m-%d %H:%M:%S.%f") for b in band] if band else None,
            "evidence": evidence,
            "undecidable_total": len(all_undecidable(contamination)),
            "columns": {k: v.as_journal() for k, v in contamination.items()},
        },
        "columns": [r.as_journal() for r in reports],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file path")
    ap.add_argument("--apply", action="store_true", help="really write to the database (default dry-run)")
    ap.add_argument("--rollback", action="store_true", help="reverse shift (+offset); requires --journal")
    ap.add_argument(
        "--journal",
        default="",
        help="archive JSON path (required for --apply / --rollback; with a dry-run, giving it writes the full list to "
            "disk for human review)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="execute anyway when a guardrail alarms — only usable after a human has confirmed the column's "
            "convention; it cannot override rows awaiting human review or the idempotency marker",
    )
    ap.add_argument(
        "--ack-undecidable",
        action="store_true",
        help="confirm the undecidable list has been human-reviewed row by row; shift them in the local convention",
    )
    ap.add_argument(
        "--utc-writer-since",
        default="",
        help="the earliest local instant at which the new code could have written to the database (YYYY-MM-DD "
            "HH:MM:SS); if not given, inferred automatically from in-database forensics",
    )
    args = ap.parse_args()

    db = Path(args.db).expanduser()
    if not db.is_file():
        print(f"❌ Database not found: {db}", file=sys.stderr)
        return 2
    if args.apply and args.rollback:
        print("❌ --apply and --rollback cannot be given at the same time", file=sys.stderr)
        return 2
    if (args.apply or args.rollback) and not args.journal:
        print(
            "❌ --apply / --rollback require --journal — that is the only recovery credential (including the per-row "
            "exclusion list)",
            file=sys.stderr,
        )
        return 2

    journal_path = Path(args.journal).expanduser() if args.journal else None
    # ---- Journal overwrite protection: a second apply would overwrite the "pre-shift baseline" with the "post-shift
    # state",
    #      and a subsequent --rollback would then report "all re-verification passed" against the polluted baseline. The
    # credential may only be written once.
    if journal_path is not None and not args.rollback and journal_path.exists():
        print(
            f"❌ The journal target file already exists: {journal_path}\n"
            "   Overwriting it destroys the only recovery credential. Please choose another filename.",
            file=sys.stderr,
        )
        return 2

    since = None
    if args.utc_writer_since:
        since = parse_db_ts(args.utc_writer_since)
        if since is None:
            print(f"❌ --utc-writer-since cannot be parsed: {args.utc_writer_since}", file=sys.stderr)
            return 2

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA busy_timeout = 30000")
    marker = read_marker(conn)

    # ---- Idempotency marker: a shift may only succeed once ----
    if args.apply and marker == MIGRATION_MARKER:
        print(
            f"❌ This database has already been shifted (PRAGMA user_version = {MIGRATION_MARKER}).\n"
            "   Shifting once more would subtract another timezone from the whole database. To start over, --rollback "
            "first.",
            file=sys.stderr,
        )
        return 3
    if args.apply and marker != 0:
        print(
            f"❌ PRAGMA user_version = {marker} is occupied by another use — the idempotency marker has nowhere to "
            f"live; refusing to execute.",
            file=sys.stderr,
        )
        return 3
    if args.rollback and marker != MIGRATION_MARKER:
        print(
            f"❌ This database has no shift marker (user_version = {marker}, expected {MIGRATION_MARKER}) — "
            "it was never shifted by this script, and rolling it back would only push the data one timezone late.",
            file=sys.stderr,
        )
        return 3

    now_local = datetime.now().astimezone()
    reports = collect(conn, now_local.replace(tzinfo=None))
    if not reports:
        print("❌ No columns to process", file=sys.stderr)
        return 2

    forward = assert_shift_is_constant(reports)
    if forward == 0.0:
        print("ℹ️  The local timezone of this machine is UTC; no shift is needed.")
        return 0

    # ---------------- rollback ----------------
    if args.rollback:
        assert journal_path is not None
        if not journal_path.is_file():
            print(f"❌ Journal not found: {journal_path}", file=sys.stderr)
            return 2
        journal = json.loads(journal_path.read_text())
        if journal.get("db") != str(db) and not args.force:
            print(
                f"❌ The journal records {journal.get('db')}, while the current database is {db} — a mismatched "
                f"rollback would move the wrong data.",
                file=sys.stderr,
            )
            return 2
        # The shift amount takes the value recorded in the journal rather than being recomputed from the current host
        # timezone, so a rollback on another machine/timezone does not move things wrongly.
        hours = -float(journal.get("shift_hours", forward))
        skip = contamination_from_journal(journal)
        skipped_total = sum(len(c.excluded) for c in skip.values())
        print(f"Rollback: reverse-shifting {hours:+g} hours per journal {journal_path}")
        if skipped_total:
            print(f"      The {skipped_total} rows excluded at shift time are likewise untouched (they were UTC to "
                f"begin with)")
        touched = execute(conn, reports, hours, skip)
        print(f"\nRolled back {touched} cells in total. Per-column re-verification:")
        problems = verify_rollback(conn, journal)
        if not problems:
            write_marker(conn, 0)
            conn.commit()
            print("\nShift marker cleared (PRAGMA user_version = 0)")
        print("\n" + ("❌ The rollback re-verification has discrepancies; human intervention is "
            "needed" if problems else "✅ Rollback re-verification all passed"))
        return 1 if problems else 0

    contamination, detected_since, evidence = detect_contamination(
        conn, reports, forward, now_local.replace(tzinfo=None), since
    )
    hours = forward

    mode = "APPLY (really writing)" if args.apply else "DRY-RUN (not writing)"
    band = (
        risk_band(detected_since, now_local.replace(tzinfo=None), forward)
        if detected_since is not None
        else None
    )
    render(reports, hours, mode, contamination, detected_since, band)

    passed, notes = verdict(
        reports, now_local.replace(tzinfo=None), contamination, args.ack_undecidable
    )
    pending = all_undecidable(contamination)
    hard_block = bool(pending) and not args.ack_undecidable

    print("\nWhole-database guardrail verdict")
    print("-" * 108)
    if marker:
        print(f"  ⚠️  PRAGMA user_version = {marker} ({MIGRATION_MARKER} means already shifted)")
    for note in notes:
        print(f"  {note}")
    if not passed:
        print(f"\n{'!' * 108}")
        print("Guardrail block — the correct order is: 1) stop the writers as far as possible 2) back up 3) shift "
            "(rows written by the new code will be excluded row by row) 4) restart the API.")
        if hard_block:
            print("Of these, the 'rows awaiting human review' item cannot be overridden by --force: please check row "
                "by row and then add --ack-undecidable.")
        else:
            print("If a human has confirmed the convention is correct, add --force to force execution.")
        print(f"{'!' * 108}")

    if journal_path is not None and not args.apply:
        journal_path.write_text(
            json.dumps(
                build_journal(db, reports, forward, contamination, detected_since, evidence, "dry-run", band),
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"\nThe full per-row list has been written to {journal_path} (a dry-run archive, for human review)")

    if not args.apply:
        print("\n(Dry-run; nothing was written. Once confirmed correct, the Founder executes --apply; back up before "
            "executing.)")
        return 0

    if hard_block or (not passed and not args.force):
        return 1

    assert journal_path is not None
    journal_path.write_text(
        json.dumps(
            build_journal(db, reports, forward, contamination, detected_since, evidence, "apply", band),
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\nArchive written to {journal_path}")

    print("\nExecuting:")
    touched = execute(conn, reports, hours, contamination)
    write_marker(conn, MIGRATION_MARKER)
    conn.commit()
    print(f"\n✅ Shifted {touched} cells; the idempotency marker has been written (user_version = {MIGRATION_MARKER}).")
    print("Please re-verify the max(time column) of a few tables: they should now equal UTC now, not local now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
