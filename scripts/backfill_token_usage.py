#!/usr/bin/env python3
"""Historical backfill: the agents four token layers + tokens_measured_at + tokens_source. Dry-run by default.

Spec in docs/token-attribution-v1-design.md §6 (whole section) / §7 stage 3. The problem to solve: billing-metric
usage collection (``usage_sum``) only went live on 2026-07-28; before that, the tokens of nearly two thousand dispatches
never landed in the database, while
**their transcripts are all still on disk**. At forensics time, all 1,957 files of the 1,957 candidate rows were alive —
but
CC cleans up older local session history, and the ``transcript_gone`` class **only ever grows**. This is the one thing
in all of v1
that "done late means doable never" (§6.2-2); everything else done late is just done late.

The backfill has exactly one target: five columns of the ``agents`` table + ``tokens_source`` (plus the observational
backfill of ``model``, see below). Four disciplines determine the shape of this script:

1. **Never touch ``workflow_agents.tokens``** (§6.4-1, risk R3). That column is in the ``ctx_last``
   metric — the last-turn context watermark, measured 5-25x apart from the ``usage_sum`` this script produces. Writing
   backfilled values
   into it would **permanently solidify the mixed metrics into the historical data**, indistinguishable afterwards — far
   worse than not backfilling. So this is not
   a matter of "being careful not to write": the writable-column allowlist :data:`WRITABLE_COLUMNS` is a closed set,
   ``(workflow_agents, tokens)`` is not in it; the column's **per-row sha256 checksum** is computed once before and once
   after ``--apply``,
   and any mismatch rolls back the whole transaction. The static side has the I14 machine check as well (see
   ``scripts/check_backfill_safety.py``）。
2. **Coverage windowed by ``tokens_measured_at``** (§6.4-2, countermeasure 6.3-2). The backfill makes overall coverage
jump
   from 0.5% to 78%, while "are new dispatches being collected" is **a different question** — the latter is the
   indicator of whether the collection chain is healthy.
   One number masks another, so these two numbers are **never presented merged** in the report. The windowing relies on
   this: the ``tokens_measured_at`` written by the live
   chain is a **per-row, microsecond-unique** stop time, while one backfill stamps thousands of rows with
   **the same** batch time, so "the same timestamp shared by ≥ :data:`BATCH_COHORT_MIN` rows" is
   the natural signature of a backfill batch (the batch time is also recorded in the journal, for precise auditing).
3. **Only write empty columns, idempotent, dry-run first** (§6.4-3). Idempotency is structural rather than marker-based:
the first branch of
   the verdict is "``tokens_measured_at`` non-empty → ``already_measured``"; after one run the target column is
   non-empty,
   so re-running necessarily lands everything in that class with zero changes. The SQL layer also carries an ``IS NULL``
   guard, defending against the race where
   the live system has already written the value between dry-run and apply.
4. **Rows that cannot be written must be classified, never silently skipped** (Council discipline 1, no-data ≠ zero).
The reason codes are in
   :data:`REASONS`, sharing their source with the unattributed classification in design §3.4. ``transcript_gone`` rows
   are **listed one by one**
   — that is not noise; the report itself is the evidence that "the backfill window has closed on these rows".

**The observational backfill of the model column** (§6.2-4): a transcript's ``message.model`` is always a full model
name, never
an alias. So rows in ``workflow_agents.model`` still carrying the alias ``opus`` (170 measured, of which
138 can be joined via ``os_agent_id`` to a surviving transcript) can directly read out their real model name — this is
**observational backfill**,
exactly the shape the deliberate decision "model defaults stay empty, backfilled by observation" wants, and it does not
conflict with "never hardcode model names". Two
boundaries: **the resolution result of the alias ledger (``MODEL_ALIAS_LEDGER``) is never written into any row** (that
is a read-side fallback, not
an observation); **rows without a transcript are not guessed at and not touched**. The same holds for ``agents.model``
(1,913 of the candidate rows have that column
empty), consistent with the behavior of the live ``SubagentStop`` path; if unwanted it can be turned off with
``--no-model``.

**Leader main sessions are not collected by default** (only with ``--include-leader``), for two independent reasons,
either of which suffices:

* **One file shared by multiple rows.** Measured: 47 Leader rows with a path point to only **13** main-session
  transcripts, the most-shared one being shared by 11 rows (for the origin of ghost rows see
  ``backfill_agent_session_ids.py``).
  Taking everything as-is would count the same 850 million tokens of usage **repeatedly, 11 times**. So even when
  explicitly enabled, this script
  only writes values for the **first row** of each file (earliest created_at, sharing its source with ``_find_leader``'s
  resolution rule); the
  remaining rows are recorded as ``duplicate_main_transcript``.
* **The semantics are the opposite of stage 4.** A main-session transcript is a **live, still-growing** file, and stage
4's semantics for it are
  ``snapshot overwrite``; while this script is ``only write empty columns, never update after writing``. Touching the
  former with the latter would permanently freeze a mid-session
  partial value as "measured". Main-session collection belongs to stage 4.

Usage::

    python3 scripts/backfill_token_usage.py                      # dry-run, full report
    python3 scripts/backfill_token_usage.py --db /tmp/copy.db    # rehearse against a copy
    python3 scripts/backfill_token_usage.py --sample 30          # sample 30 rows for human checking
    python3 scripts/backfill_token_usage.py --apply --journal ~/token-backfill.json

Back up before ``--apply`` (the journal is the only recovery credential; if the target file already exists, overwriting
is hard-refused)::

    cp ~/.claude/data/ai-team-os/aiteam.db \\
       ~/aiteam.db.bak-tokenusage-$(date +%Y%m%d%H%M%S)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.clock import naive_utc_now, utc_now  # noqa: E402
from aiteam.services.token_attribution import parse_transcript_usage  # noqa: E402

DEFAULT_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"

# The closed set of columns this script **may** write. Adding a column requires changing this and the I14 machine
# check's expected set at the same time,
# so "writing one more column while at it" is visible in review. (workflow_agents, tokens) is never in it.
WRITABLE_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("agents", "input_tokens"),
        ("agents", "output_tokens"),
        ("agents", "cache_creation_tokens"),
        ("agents", "cache_read_tokens"),
        ("agents", "tokens_measured_at"),
        ("agents", "tokens_source"),
        ("agents", "model"),
        ("workflow_agents", "model"),
    }
)

# This column is a historical asset in the ctx_last metric; this script's output (usage_sum) differs from it by 5-25x.
# Making it a standalone constant lets the machine check reference it directly, rather than relying on reading the
# comments.
FORBIDDEN_COLUMN: tuple[str, str] = ("workflow_agents", "tokens")

# The column names of the four token layers, in fixed order (presentation surfaces always list them separately and never
# give a total).
TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)

# One backfill stamps thousands of rows with the same instant; the live chain writes each row's own stop time
# (microsecond-unique).
# So "the same timestamp shared by this many rows" = a backfill batch signature. The threshold is 50: the live chain
# cannot possibly stop
# 50 agents in the same microsecond, while any real backfill is far more than 50 rows.
BATCH_COHORT_MIN = 50

# Full model names always start with this (measured in transcripts: claude-opus-5 / claude-opus-4-8[1m] …);
# aliases are things like opus / fable / ''. Judging by "is it a full model name" is more robust than enumerating
# aliases — a future new
# alias needs no change here.
CONCRETE_MODEL_PREFIX = "claude-"

REASONS: dict[str, str] = {
    "written": "writable (a candidate in a dry-run / actually written by --apply)",
    "already_measured": "tokens_measured_at already has a value — an idempotent re-run necessarily lands entirely "
        "here; no re-measuring, no overwriting",
    "already_set": "the target column already has a value and it agrees with the observation",
    "no_transcript_path": "this row never registered a transcript path, so there is nothing to backfill from "
        "(historical rows; new rows are covered)",
    "transcript_gone": "the path exists but the file is no longer on disk — the backfill window has closed on these "
        "rows; this only ever grows",
    "unreadable_transcript": "the file is there but no usage snapshot can be parsed out of it (empty file / all lines "
        "corrupt)",
    "duplicate_main_transcript": "the same main-session transcript is already represented by an earlier row — writing "
        "again would double-count",
    "no_observed_model": "no full model name was read from the transcript (only synthetic rows like <synthetic>)",
    "already_concrete": "model is already a full model name, not an alias — an observation does not overwrite an "
        "observation",
    "transcript_grew": "the recomputed values are all ≥ the database values — the transcript was still written after "
        "measurement (the agent is still alive); do not change",
    "recompute_mismatch": "the recomputed values do not match the database values and it is not growth — a real "
        "anomaly; a human must look",
}

# The report prints in this order (actionable first, then non-actionable).
REASON_ORDER = list(REASONS)


@dataclass
class Row:
    """One row's verdict. ``values`` is only meaningful when the verdict is written."""

    table: str
    row_id: str
    name: str
    reason: str
    values: dict[str, Any] = field(default_factory=dict)
    guard: dict[str, Any] = field(default_factory=dict)
    warn: str = ""
    note: str = ""

    def value_repr(self) -> str:
        if not self.values:
            return ""
        parts = []
        for k, v in self.values.items():
            parts.append(f"{k.replace('_tokens', '')}={v}")
        return " ".join(parts)


@dataclass
class Job:
    title: str
    table: str
    rows: list[Row] = field(default_factory=list)
    enabled: bool = True

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out

    def written(self) -> list[Row]:
        return [r for r in self.rows if r.reason == "written"]

    def by_reason(self, reason: str) -> list[Row]:
        return [r for r in self.rows if r.reason == reason]


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------
def open_db(path: Path, *, writable: bool) -> sqlite3.Connection:
    """Only write mode uses a normal connection; reads always go through a ``mode=ro`` URI, physically incapable of
    writing."""
    if writable:
        con = sqlite3.connect(str(path))
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_agents(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        select id, name, role, model, created_at, transcript_path,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
               tokens_measured_at, tokens_source
        from agents
        order by created_at
        """
    ).fetchall()


def load_workflow_agents(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """``workflow_agents`` has **no** transcript_path column — a file can only be reached via os_agent_id, borrowing the
    path through
    the agents row. Measured: os_agent_id has no duplicates in the whole table, so this join does not fan out."""
    return con.execute(
        """
        select wa.id, wa.label, wa.model, wa.os_agent_id, wa.cc_agent_id,
               a.transcript_path, a.model as agent_model
        from workflow_agents wa
        left join agents a on a.id = wa.os_agent_id
        order by wa.created_at
        """
    ).fetchall()


def workflow_tokens_fingerprint(con: sqlite3.Connection) -> dict[str, Any]:
    """The per-row fingerprint of ``workflow_agents.tokens`` — the acceptance credential of hard constraint 1.

    Comparing only the total is not enough: one row up and another down can leave the sum unchanged. So take the sha256
    of ``(id, tokens)`` sorted by id;
    any row being touched changes it.
    """
    h = hashlib.sha256()
    n = 0
    total = 0
    for rid, tokens in con.execute(
        "select id, tokens from workflow_agents order by id"
    ):
        h.update(f"{rid}\x1f{tokens!r}\x1e".encode())
        n += 1
        total += tokens or 0
    return {"rows": n, "sum": total, "sha256": h.hexdigest()}


# ---------------------------------------------------------------------------
# Parsing (the only disk IO surface)
# ---------------------------------------------------------------------------
class Parser:
    """A transcript parser with cache and progress.

    The cache is a necessity, not an optimization: 47 Leader rows point to only 13 files, and parsing the same 35 MB
    file
    11 times would be pure waste. The cache key is the path; the value is the parse result (which may be None).
    """

    def __init__(self, *, verbose: bool = True) -> None:
        self._cache: dict[str, dict[str, Any] | None] = {}
        self._verbose = verbose
        self.parsed = 0
        self.bytes_read = 0

    def usage(self, path: str) -> dict[str, Any] | None:
        if path in self._cache:
            return self._cache[path]
        try:
            self.bytes_read += os.path.getsize(path)
        except OSError:
            pass
        result = parse_transcript_usage(path)
        self._cache[path] = result
        self.parsed += 1
        if self._verbose and self.parsed % 200 == 0:
            print(
                f"  …parsed {self.parsed} transcripts so far ({self.bytes_read / 1e6:.0f} MB)",
                file=sys.stderr,
            )
        return result


def transcript_exists(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Job A — sub-agents' four token layers + tokens_measured_at + tokens_source
# ---------------------------------------------------------------------------
def job_subagent_usage(
    agents: list[sqlite3.Row], parser: Parser, batch_ts: str, *, with_model: bool = True
) -> Job:
    """The main backfill: rows with ``role != 'leader'`` and an empty ``tokens_measured_at``.

    The branch order is the implementation of idempotency: ``already_measured`` comes first, so on a second run every
    written
    row is out **before any file is read** — a re-run changes nothing and does not re-parse 700 MB either.
    """
    job = Job("A. agents four token layers + tokens_measured_at + tokens_source (sub-agents)", "agents")
    for a in agents:
        if a["role"] == "leader":
            continue
        if a["tokens_measured_at"]:
            job.rows.append(Row("agents", a["id"], a["name"], "already_measured"))
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row("agents", a["id"], a["name"], "no_transcript_path"))
            continue
        if not transcript_exists(path):
            job.rows.append(
                Row("agents", a["id"], a["name"], "transcript_gone", note=path)
            )
            continue
        usage = parser.usage(path)
        if not usage:
            job.rows.append(
                Row("agents", a["id"], a["name"], "unreadable_transcript", note=path)
            )
            continue
        values: dict[str, Any] = {c: usage[c] for c in TOKEN_COLUMNS}
        values["tokens_measured_at"] = batch_ts
        values["tokens_source"] = "transcript"
        warn = ""
        observed = usage.get("model") or ""
        if with_model and observed and not (a["model"] or ""):
            # Observational backfill: that column in the database is empty, and the transcript holds a full model name.
            # The same behavior as
            # the live SubagentStop path (hook_translator also writes this column).
            values["model"] = observed
        elif observed and (a["model"] or "") and a["model"] != observed:
            # An existing value disagrees with the observation: **do not touch it**, only report. Overwriting observed
            # values is not this script's business.
            warn = (f"the database model={a['model']} is inconsistent with the transcript observation {observed} "
                f"(unchanged)")
        job.rows.append(
            Row(
                "agents",
                a["id"],
                a["name"],
                "written",
                values=values,
                guard={"tokens_measured_at": None},
                warn=warn,
                note=f"api_calls={usage.get('api_calls')}",
            )
        )
    return job


# ---------------------------------------------------------------------------
# Job B — back-labeling tokens_source on already-measured rows (with an independent recomputation reconciliation on the
# side)
# ---------------------------------------------------------------------------
def job_source_label(agents: list[sqlite3.Row], parser: Parser) -> Job:
    """Rows whose ``tokens_measured_at`` is non-empty but ``tokens_source`` is empty (13 measured).

    These are rows the D1 live chain collected; the ``tokens_source`` column landed later than they did. Before
    back-labeling, do one
    more valuable thing: **recompute with this script's parser and compare field by field against the four-layer values
    the live chain wrote at the time**.
    Only label when they are equal — so the value in this column is not "I assume it came from the transcript" but "I
    recomputed it, and it really is".
    This is also the zero-tolerance same-metric reconciliation design §4.4 gate 2-1 asks for (the parser is a pure
    function; the tolerance is zero).

    Recomputed values all ≥ the database values mean the transcript grew after measurement (the agent is still alive,
    e.g. the one
    running this script) — that is not an anomaly, and gets its own class; the rest of the mismatches are the real
    anomalies.
    """
    job = Job("B. agents.tokens_source back-labeling (already-measured rows; written only after recomputation "
        "matches)", "agents")
    for a in agents:
        if not a["tokens_measured_at"]:
            continue
        if a["tokens_source"]:
            job.rows.append(Row("agents", a["id"], a["name"], "already_set"))
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row("agents", a["id"], a["name"], "no_transcript_path"))
            continue
        if not transcript_exists(path):
            job.rows.append(Row("agents", a["id"], a["name"], "transcript_gone", note=path))
            continue
        usage = parser.usage(path)
        if not usage:
            job.rows.append(Row("agents", a["id"], a["name"], "unreadable_transcript", note=path))
            continue
        stored = {c: (a[c] or 0) for c in TOKEN_COLUMNS}
        fresh = {c: usage[c] for c in TOKEN_COLUMNS}
        if stored == fresh:
            job.rows.append(
                Row(
                    "agents",
                    a["id"],
                    a["name"],
                    "written",
                    values={"tokens_source": "transcript"},
                    guard={"tokens_source": None},
                    note="recomputation equal field by field",
                )
            )
        elif all(fresh[c] >= stored[c] for c in TOKEN_COLUMNS):
            delta = {c: fresh[c] - stored[c] for c in TOKEN_COLUMNS if fresh[c] != stored[c]}
            job.rows.append(
                Row(
                    "agents",
                    a["id"],
                    a["name"],
                    "transcript_grew",
                    note=f"increment {delta}",
                )
            )
        else:
            job.rows.append(
                Row(
                    "agents",
                    a["id"],
                    a["name"],
                    "recompute_mismatch",
                    warn=f"in database {stored} vs recomputed {fresh}",
                )
            )
    return job


# ---------------------------------------------------------------------------
# Job C — observational backfill of workflow_agents.model
# ---------------------------------------------------------------------------
def job_workflow_model(rows: list[sqlite3.Row], parser: Parser) -> Job:
    """Replace the **aliases** in ``workflow_agents.model`` with the full model names observed in transcripts.

    This is the only place this script writes ``workflow_agents``, and it writes the ``model`` column. The ``tokens``
    column is not in
    the allowlist, not in this function, and would not pass the post-apply fingerprint comparison either — three
    independent gates.

    The correct form of "only write empty columns" here is not a literal copy: the alias ``opus`` is a **request
    specification** (which model is wanted),
    not an observation result. Replacing a request specification with an observation is completing the truth, the
    opposite direction from "overwriting an observation with an inference". So the criterion is
    **only touch it when it is not a full model name**, and the SQL guard pins "still equal to the alias I read" — if in
    the meantime someone wrote a full
    model name there, it yields automatically.
    """
    job = Job("C. workflow_agents.model ← transcript observation (aliases replaced by real names)", "workflow_agents")
    for r in rows:
        current = r["model"] or ""
        label = r["label"] or r["cc_agent_id"] or ""
        if current.startswith(CONCRETE_MODEL_PREFIX):
            job.rows.append(Row("workflow_agents", r["id"], label, "already_concrete"))
            continue
        path = r["transcript_path"] or ""
        if not path:
            job.rows.append(
                Row("workflow_agents", r["id"], label, "no_transcript_path",
                    note=f"model={current!r} os_agent_id={r['os_agent_id']}")
            )
            continue
        if not transcript_exists(path):
            job.rows.append(Row("workflow_agents", r["id"], label, "transcript_gone", note=path))
            continue
        usage = parser.usage(path)
        observed = (usage or {}).get("model") or ""
        if not observed:
            job.rows.append(
                Row("workflow_agents", r["id"], label, "no_observed_model", note=path)
            )
            continue
        job.rows.append(
            Row(
                "workflow_agents",
                r["id"],
                label,
                "written",
                values={"model": observed},
                guard={"model": current},
                note=f"alias {current!r} → observed {observed}",
            )
        )
    return job


# ---------------------------------------------------------------------------
# Job D — Leader main sessions (off by default)
# ---------------------------------------------------------------------------
def job_leader_usage(
    agents: list[sqlite3.Row], parser: Parser, batch_ts: str, *, enabled: bool
) -> Job:
    """Leader rows' four token layers. **Not executed by default**; see the module docstring for the reasons.

    Even when enabled, it **dedupes by file**: the same main-session transcript is represented only by the row with the
    earliest created_at
    (sharing its source with ``_find_leader``'s resolution rule); the rest are recorded as
    ``duplicate_main_transcript``. Without dedup,
    the measured transcript shared by 11 rows would count the same usage 11 times over.
    """
    job = Job(
        "D. agents four token layers (Leader main sessions, requires --include-leader)", "agents", enabled=enabled
    )
    seen: dict[str, str] = {}  # transcript_path -> the agent_id already representing it
    # Two-pass scan: already-measured rows claim file representation rights first. Representation rights must follow
    # "who already carries this file's account",
    # not whoever happens to be scanned first in this round — otherwise, after one --apply round, the representative row
    # lands in already_measured,
    # continues early and no longer claims its place, and the ghost rows of the same file dodge the duplicate gate and
    # are promoted to candidates; re-running
    # --apply would then count the same main session's usage again (caught by a production re-run on 2026-08-03:
    # after the first round wrote 10 rows, the re-run reported 5 rows "pending write", all ghost rows of
    # already-measured files).
    for a in agents:
        if a["role"] == "leader" and a["tokens_measured_at"] and (a["transcript_path"] or ""):
            seen.setdefault(a["transcript_path"], a["id"])
    for a in agents:  # load_agents is already sorted by created_at → the first to arrive is the earliest
        if a["role"] != "leader":
            continue
        if a["tokens_measured_at"]:
            job.rows.append(Row("agents", a["id"], a["name"], "already_measured"))
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row("agents", a["id"], a["name"], "no_transcript_path"))
            continue
        if path in seen:
            job.rows.append(
                Row("agents", a["id"], a["name"], "duplicate_main_transcript",
                    note=f"already represented by {seen[path][:8]}: {path}")
            )
            continue
        if not transcript_exists(path):
            job.rows.append(Row("agents", a["id"], a["name"], "transcript_gone", note=path))
            continue
        usage = parser.usage(path)
        if not usage:
            job.rows.append(Row("agents", a["id"], a["name"], "unreadable_transcript", note=path))
            continue
        seen[path] = a["id"]
        values: dict[str, Any] = {c: usage[c] for c in TOKEN_COLUMNS}
        values["tokens_measured_at"] = batch_ts
        values["tokens_source"] = "transcript"
        job.rows.append(
            Row(
                "agents", a["id"], a["name"], "written", values=values,
                guard={"tokens_measured_at": None},
                warn="the main session is a live file; this value is a snapshot at backfill time; continuous "
                    "collection belongs to stage 4",
                note=f"api_calls={usage.get('api_calls')}",
            )
        )
    return job


# ---------------------------------------------------------------------------
# Coverage windowing (hard constraint 2)
# ---------------------------------------------------------------------------
def detect_backfill_cohorts(con: sqlite3.Connection) -> set[str]:
    """The backfill batch times already present in the database — "the same timestamp shared by ≥ BATCH_COHORT_MIN
    rows"."""
    return {
        str(ts)
        for ts, n in con.execute(
            "select tokens_measured_at, count(*) from agents "
            "where tokens_measured_at is not null group by tokens_measured_at"
        )
        if n >= BATCH_COHORT_MIN
    }


@dataclass
class Coverage:
    """The coverage three-way split under one denominator: incremental collection / historical backfill / unattributed.

    The three numbers are **never merged into one**. §6.3-2 is direct about it: after backfill, overall coverage jumps
    to 78%, but "the collection
    rate of new dispatches" is a different matter — the latter is the indicator for judging whether the collection chain
    is healthy — one number would mask another.
    """

    label: str
    total: int
    incremental: int  # collected by the live chain (per-row unique timestamps)
    backfilled: int  # collected by a backfill batch (shared batch timestamp)

    @property
    def measured(self) -> int:
        return self.incremental + self.backfilled

    def line(self) -> str:
        def pct(n: int) -> str:
            return f"{n / self.total * 100:5.1f}%" if self.total else "  n/a"

        return (
            f"  {self.label:<26} Denominator {self.total:5d} │ "
            f"incremental collection {self.incremental:5d} {pct(self.incremental)} │ "
            f"historical backfill {self.backfilled:5d} {pct(self.backfilled)} │ "
            f"unattributed {self.total - self.measured:5d} {pct(self.total - self.measured)}"
        )


def measure_coverage(
    agents: list[sqlite3.Row], cohorts: set[str], *, leader: bool
) -> Coverage:
    """§4.1: the denominator is **all dispatch rows of that scope**, including rows without a transcript.

    Rows may not be moved out of the denominator on the grounds of "no path, so it does not count" — that is exactly
    letting a part impersonate the whole (risk R2).
    The denominator is counted by whether a row exists, **not by tokens_measured_at**; otherwise unmeasured rows would
    disappear from the denominator and
    coverage would be a constant 100%.
    """
    rows = [a for a in agents if (a["role"] == "leader") == leader]
    incremental = backfilled = 0
    for a in rows:
        ts = a["tokens_measured_at"]
        if not ts:
            continue
        if str(ts) in cohorts:
            backfilled += 1
        else:
            incremental += 1
    return Coverage(
        "Leader main session" if leader else "Sub-agents (dispatches)", len(rows), incremental, backfilled
    )


def project_coverage(before: Coverage, newly_written: int) -> Coverage:
    """The backfill **only adds to the historical-backfill cell**; the incremental-collection cell is untouched word
    for word — that is exactly what people need to see."""
    return Coverage(before.label, before.total, before.incremental, before.backfilled + newly_written)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def print_job(job: Job, sample_n: int, rng: random.Random) -> None:
    print()
    print("=" * 88)
    print(job.title + ("" if job.enabled else "   [not executed this time; the following is a verdict preview only]"))
    print("-" * 88)
    counts = job.counts()
    for reason in REASON_ORDER:
        if reason in counts:
            print(f"  {counts[reason]:6d}  {reason:<26} {REASONS[reason]}")
    print(f"  {sum(counts.values()):6d}  Total")

    written = job.written()
    if written:
        picks = written if len(written) <= sample_n else rng.sample(written, sample_n)
        picks.sort(key=lambda r: r.row_id)
        print()
        print(f"  ── Sampled {len(picks)}/{len(written)} rows (for human checking) ──")
        for r in picks:
            note = f"  ({r.note})" if r.note else ""
            warn = f"  ⚠ {r.warn}" if r.warn else ""
            print(f"    {r.row_id[:8]}  {r.name[:26]:<26} {r.value_repr()}{note}{warn}")

    # Rows with a warn are listed once more separately: sampling may miss them, and they are exactly what a human needs
    # to see.
    warned = [r for r in job.rows if r.warn and r.reason != "written"]
    warned += [r for r in written if r.warn]
    if warned:
        print()
        print(f"  ── {len(warned)} rows needing human review ──")
        for r in warned[:20]:
            print(f"    ⚠ {r.row_id[:8]}  {r.name[:26]:<26} [{r.reason}] {r.warn or r.note}")
        if len(warned) > 20:
            print(f"      …… {len(warned) - 20} more rows")


def print_transcript_gone(jobs: list[Job]) -> None:
    """List ``transcript_gone`` rows one by one — §3.4 requires it, and this class only ever grows.

    This is not noise: the report itself is the evidence that "the backfill window has permanently closed on these
    rows". Every row listed today
    is a row nobody will ever be able to backfill again.
    """
    print()
    print("=" * 88)
    print("transcript_gone row-by-row list (§3.4: only ever grows; the report is the evidence that the window has "
        "closed)")
    print("-" * 88)
    gone = [(j, r) for j in jobs for r in j.by_reason("transcript_gone")]
    if not gone:
        print("  ✓ 0 rows — this backfill window is fully open: every registered transcript is still on disk, not one "
            "missing.")
        return
    print(f"  The transcripts of {len(gone)} rows are no longer on disk; this usage can never be backfilled:")
    for job, r in gone:
        print(f"    [{job.table}] {r.row_id[:8]}  {r.name[:26]:<26} {r.note}")


def print_coverage(
    agents: list[sqlite3.Row], cohorts: set[str], job_a: Job, job_d: Job
) -> None:
    print()
    print("=" * 88)
    print("Coverage windowing (hard constraint 2: historical backfill and incremental collection are always two "
        "numbers, never merged)")
    print("-" * 88)
    sub_before = measure_coverage(agents, cohorts, leader=False)
    lead_before = measure_coverage(agents, cohorts, leader=True)
    sub_after = project_coverage(sub_before, len(job_a.written()))
    lead_after = project_coverage(lead_before, len(job_d.written()) if job_d.enabled else 0)

    print("  Before backfill:")
    print(sub_before.line())
    print(lead_before.line())
    print("  After backfill (projected):")
    print(sub_after.line())
    print(lead_after.line())
    print()
    print("  How to read it: the 'incremental collection' cell is **word-for-word unchanged before and after "
        "backfill** — it is the indicator of whether the collection chain is healthy;")
    print("        the backfill only raises the 'historical backfill' cell. Adding the two cells together is "
        "meaningless, and this report does not give that sum.")
    print("  Denominator convention: all agents-table rows split by role, **including rows without a transcript** "
        "(§4.1).")


def print_fingerprint(fp: dict[str, Any], *, title: str) -> None:
    print(f"  {title}: {fp['rows']} rows / sum={fp['sum']:,} / sha256={fp['sha256'][:16]}…")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def apply_job(con: sqlite3.Connection, job: Job) -> int:
    """Write row by row, with SQL-level guards.

    The guards live in the SQL and not only in the verdict layer because the database may be rewritten by the live
    system at any moment between dry-run and apply:
    ``tokens_measured_at IS NULL`` ensures that even concurrently it will not overwrite an observation someone just
    wrote;
    ``model = <the alias I read>`` makes a row that in the meantime was written as a full model name yield
    automatically.
    """
    if not job.enabled:
        return 0
    n = 0
    for r in job.written():
        for col in r.values:
            if (job.table, col) not in WRITABLE_COLUMNS:
                raise RuntimeError(
                    f"write-column whitelist interception: ({job.table}, {col}) is not in WRITABLE_COLUMNS"
                )
        assign = ", ".join(f"{c} = ?" for c in r.values)
        params: list[Any] = list(r.values.values())
        where = ["id = ?"]
        params.append(r.row_id)
        for col, expected in r.guard.items():
            if expected is None:
                where.append(f"({col} is null or {col} = '')")
            else:
                where.append(f"{col} = ?")
                params.append(expected)
        cur = con.execute(
            f"update {job.table} set {assign} where {' and '.join(where)}",  # noqa: S608 — table/column names all come
            # from constants and the allowlist in this file, not external input
            params,
        )
        n += cur.rowcount
    return n


def build_journal(
    args: argparse.Namespace,
    batch_ts: str,
    jobs: list[Job],
    fp_before: dict[str, Any],
    fp_after: dict[str, Any],
    written: int,
) -> dict[str, Any]:
    """journal = the only recovery credential: the batch time + the per-row written content + the before/after
    fingerprints of the forbidden column."""
    return {
        "script": "backfill_token_usage.py",
        "db": str(args.db),
        "batch_ts": batch_ts,
        "metric": "usage_sum",
        "generated_at": utc_now().isoformat(),
        "options": {
            "include_leader": args.include_leader,
            "no_model": args.no_model,
        },
        "forbidden_column": {
            "column": f"{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]}",
            "before": fp_before,
            "after": fp_after,
            "unchanged": fp_before["sha256"] == fp_after["sha256"],
        },
        "rows_written": written,
        "jobs": [
            {
                "title": j.title,
                "table": j.table,
                "enabled": j.enabled,
                "counts": j.counts(),
                "written": [
                    {"id": r.row_id, "name": r.name, "values": r.values, "guard": r.guard}
                    for r in j.written()
                ],
                "transcript_gone": [
                    {"id": r.row_id, "name": r.name, "path": r.note}
                    for r in j.by_reason("transcript_gone")
                ],
            }
            for j in jobs
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="target database (default: the production database)")
    ap.add_argument("--apply", action="store_true", help="actually write (default: report only)")
    ap.add_argument("--journal", type=Path, help="required with --apply: where the recovery credential lands "
        "(hard-refused if it already exists)")
    ap.add_argument("--sample", type=int, default=30, help="number of rows sampled and printed per Job")
    ap.add_argument("--seed", type=int, default=0, help="sampling random seed (default 0, reproducible)")
    ap.add_argument(
        "--include-leader", action="store_true",
        help="additionally run Job D (Leader main-session backfill) — its semantics are the opposite of stage 4's "
            "snapshot overwrite, so it is not done by default",
    )
    ap.add_argument(
        "--no-model", action="store_true",
        help="do no model observational backfill (Job C fully disabled + Job A does not write the model column)",
    )
    ap.add_argument("--quiet", action="store_true", help="do not print parsing progress")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"Database does not exist: {args.db}", file=sys.stderr)
        return 2
    if args.apply and args.journal is None:
        print("❌ --apply requires --journal — that is the only recovery credential", file=sys.stderr)
        return 2
    if args.journal is not None and args.journal.exists():
        print(
            f"❌ The journal target file already exists: {args.journal}\n"
            "   A second apply would overwrite it with the post-backfill state, and the recovery credential would be "
            "gone. Choose another filename.",
            file=sys.stderr,
        )
        return 2

    rng = random.Random(args.seed)
    # Batch time: all rows of one backfill share the same value — that is both the fact (they were measured at this very
    # moment) and
    # the windowing signature of hard constraint 2. It goes through aiteam.clock's single clock (I11 red line: no second
    # clock may
    # exist in the database); the persisted form of naive_utc_now agrees with UtcDateTime, so the two sides align
    # naturally.
    batch_ts = naive_utc_now().isoformat(sep=" ")

    con = open_db(args.db, writable=args.apply)
    try:
        agents = load_agents(con)
        wa_rows = load_workflow_agents(con)
        cohorts = detect_backfill_cohorts(con)
        fp_before = workflow_tokens_fingerprint(con)

        print(f"Database: {args.db}")
        print(f"Mode: {'APPLY (writing)' if args.apply else 'DRY-RUN (read-only, mode=ro)'}")
        print("Metric: usage_sum (grouped by requestId, take the last snapshot, then accumulate across groups) — it "
            "differs from"
              "the ctx_last metric of workflow_agents.tokens by a measured 5-25x, and the two are never added together")
        print(f"Batch time: {batch_ts} (all rows written this time share this value; that is the windowing signature)")
        print(f"agents {len(agents)} rows; workflow_agents {len(wa_rows)} rows;"
              f"{len(cohorts)} historical backfill batches identified")
        print()
        print("Starting to parse transcripts…", file=sys.stderr)

        parser = Parser(verbose=not args.quiet)
        job_a = job_subagent_usage(agents, parser, batch_ts, with_model=not args.no_model)
        job_b = job_source_label(agents, parser)
        job_c = job_workflow_model(wa_rows, parser)
        job_c.enabled = not args.no_model
        job_d = job_leader_usage(agents, parser, batch_ts, enabled=args.include_leader)
        jobs = [job_a, job_b, job_c, job_d]

        print(f"Parsing complete: {parser.parsed} files / {parser.bytes_read / 1e6:.1f} MB",
              file=sys.stderr)

        for job in jobs:
            print_job(job, args.sample, rng)
        if not args.include_leader:
            print()
            print("  Job D is not executed by default (it requires --include-leader). The above is only a preview of "
                "its verdicts.")
            print("  Two independent reasons: 1) 47 Leader rows point to only 13 transcripts, and taking everything "
                "as-is would")
            print("  repeatedly count the same usage up to 11 times; 2) a main session is a live file, and continuous "
                "collection belongs to stage 4 (snapshot")
            print("  overwrite semantics), while this script's 'only write empty columns, never update after writing' "
                "would permanently freeze a mid-way value as 'measured'.")

        print_transcript_gone(jobs)
        print_coverage(agents, cohorts, job_a, job_d)

        print()
        print("=" * 88)
        print("Hard constraint 1: workflow_agents.tokens unchanged row by row (the ctx_last metric must not be "
            "polluted by usage_sum)")
        print("-" * 88)
        print(f"  Writable-column allowlist: {sorted(f'{t}.{c}' for t, c in WRITABLE_COLUMNS)}")
        print(f"  Forbidden column: {FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} — not in the allowlist;"
              f"the apply layer's assert and the before/after fingerprint comparison are the second and third gates")
        print_fingerprint(fp_before, title="Pre-backfill fingerprint")

        planned = sum(len(j.written()) for j in jobs if j.enabled)
        print()
        print("=" * 88)
        print(f"Total to write: {planned} rows"
              f"（agents {len(job_a.written()) + (len(job_d.written()) if job_d.enabled else 0)}"
              f" + tokens_source back-labels {len(job_b.written())}"
              f" + workflow_agents.model {len(job_c.written()) if job_c.enabled else 0}）")

        if not args.apply:
            print("Dry-run finished; not one byte was written. Once confirmed correct, the Founder executes"
                  " --apply --journal <path> (back up first).")
            print("Idempotency acceptance: after --apply, re-run this script and pending writes should be 0 rows"
                  "(everything lands in already_measured / already_set / already_concrete).")
            return 0

        written = 0
        try:
            for job in jobs:
                written += apply_job(con, job)
            fp_after = workflow_tokens_fingerprint(con)
            if fp_after["sha256"] != fp_before["sha256"]:
                con.rollback()
                print()
                print("❌ Hard constraint 1 breached: the workflow_agents.tokens fingerprint changed; the whole "
                    "transaction has been rolled back.",
                      file=sys.stderr)
                print_fingerprint(fp_before, title="Before backfill")
                print_fingerprint(fp_after, title="After backfill")
                return 4
            con.commit()
        except Exception:
            con.rollback()
            raise

        fp_final = workflow_tokens_fingerprint(con)
        print()
        print(f"Written: {written} rows (verdict candidates {planned} rows;"
              f"the difference = rows the live system has written in the meantime; guards yielding is the expected "
              f"behavior)")
        print_fingerprint(fp_before, title="Pre-backfill fingerprint")
        print_fingerprint(fp_final, title="Post-backfill fingerprint")
        print(f"  ✓ Hard constraint 1 passed: {FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} unchanged row by row")

        args.journal.parent.mkdir(parents=True, exist_ok=True)
        args.journal.write_text(
            json.dumps(
                build_journal(args, batch_ts, jobs, fp_before, fp_final, written),
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Journal written to disk: {args.journal}")
        print("Re-running this script should report 0 pending writes — that is the idempotency acceptance test.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
