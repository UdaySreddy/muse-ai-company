#!/usr/bin/env python3
"""Historical backfill: agents.session_id + Leader-row transcript_path. Dry-run by default.

Spec in docs/token-attribution-v1-design.md §2.6 / §7 stage 1. The problem to solve: on the attribution chain,
the "session → agent" hop effectively does not exist in the database (forensics: of 2,568 rows only 2 had a non-empty
session_id),
and this hop **needs no new collection at all** — it is encoded in the transcript path:

    ~/.claude/projects/<slug>/<session_id>/subagents/[workflows/<wf>/]agent-<cc>.jsonl

Three things determine the shape of this script:

1. **Only write empty columns, never overwrite a non-empty value.** Idempotency is therefore structural rather than
marker-based: after one run the target column
   is non-empty, so re-running naturally changes nothing. Rows where an existing value conflicts with the derived value
   are **not touched at all**; they are only named
   in the report (``conflict``) — overwriting would mean erasing an observation with an inference.
2. **Rows that cannot be written must be classified, never silently skipped.** Every row is either written, or enters
the report with a reason code
   (no-data ≠ zero, Council discipline 1). The reason codes share their source with the unattributed classification in
   design §3.4.
3. **The parts that change live behavior are behind a separate switch.** Backfilling a Leader's session_id changes
   the resolution result of ``_find_leader`` / ``_on_session_start`` (they take the row with the
   **earliest created_at** among the leaders of the same session), which is a live behavior change rather than pure data
   repair, so it is **not done** by default;
   doing it requires an explicit ``--leader-session-id``, and the dry-run prints, session by session, "which row it will
   change to".

Precondition (be sure to read the "Risks" column of the report first): the ``agents`` registration dedup has a bypass
keyed on
``session_id + name`` (``repository.find_agent_by_session``, ``limit(1)`` with
no ORDER BY, in practice taking the earliest row). Backfilling brings historical rows back into this bypass's visible
range, so in the future
a new dispatch with the **same name in the same session** may be judged a "reuse of the old row" and overwrite the old
row's token columns in place. At forensics time,
among the 1,923 rows awaiting backfill, all ``(session, name)`` pairs were unique with zero existing collisions, but
generic names (``Explore``
and the like) may collide in the future. The script measures this: once it finds that new collisions would be created,
``--apply`` hard-refuses,
unless ``--ack-collisions`` is given explicitly.

Usage::

    python3 scripts/backfill_agent_session_ids.py                     # dry-run, full report
    python3 scripts/backfill_agent_session_ids.py --db /tmp/copy.db   # rehearse against a copy
    python3 scripts/backfill_agent_session_ids.py --sample 30         # sample 30 rows for human checking
    python3 scripts/backfill_agent_session_ids.py --apply             # executed personally by the Founder

Back up before ``--apply``::

    cp ~/.claude/data/ai-team-os/aiteam.db \
       ~/aiteam.db.bak-sessionid-$(date +%Y%m%d%H%M%S)
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.api.session_probe import project_slug  # noqa: E402
from aiteam.services.transcript_path import (  # noqa: E402
    parse_transcript_path,
    slug_matches_root,
)

DEFAULT_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Reason codes — sharing their source with the unattributed classification in design §3.4. "Cannot write" is not one
# state but several,
# and each is handled differently, so the report must list them separately rather than give one total.
REASONS = {
    "written": "writable (a candidate in this dry-run / actually written by --apply)",
    "already_set": "the target column already has a value and it agrees with the derived value — an idempotent re-run "
        "necessarily lands entirely here",
    "conflict": "the target column already has a value but it differs from the derived value — do not touch, leave for "
        "a human to judge",
    "no_transcript_path": "this row never registered a transcript path, so there is nothing to derive from",
    "unparseable_path": "a path was registered but does not match CC's directory shape; the parser refuses to guess",
    "no_session_id": "the Leader row has neither session_id nor team.config.owner_session_id",
    "no_root_path": "the project root_path cannot be resolved, so no slug can be assembled",
    "transcript_gone": "the path can be assembled but the file is no longer on disk — the backfill window has closed; "
        "this only ever grows",
    "slug_mismatch": "the file is on disk but sits under another slug — ownership in doubt; a human should look",
}


@dataclass
class Row:
    """One row's verdict. ``value`` is only meaningful when the verdict is written.

    ``warn`` and ``note`` are separate: the former means "something is up with this row" (carries ⚠ in the report, a
    human must look), the latter is only
    supplementary context. Merging them into one field would drown real warnings in noise.
    """

    agent_id: str
    name: str
    reason: str
    column: str
    value: str = ""
    warn: str = ""
    note: str = ""


@dataclass
class Job:
    title: str
    rows: list[Row] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out

    def written(self) -> list[Row]:
        return [r for r in self.rows if r.reason == "written"]


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
        select a.id, a.name, a.role, a.status, a.created_at,
               a.session_id, a.transcript_path,
               t.name as team_name, t.config as team_config,
               coalesce(p1.root_path, p2.root_path) as root_path
        from agents a
        left join teams t on t.id = a.team_id
        left join projects p1 on p1.id = a.project_id
        left join projects p2 on p2.id = t.project_id
        order by a.created_at
        """
    ).fetchall()


def index_main_transcripts() -> dict[str, list[str]]:
    """All main-session transcripts on disk: session_id -> [slug, ...] (possibly several)."""
    index: dict[str, list[str]] = {}
    if not PROJECTS_DIR.is_dir():
        return index
    for slug_dir in PROJECTS_DIR.iterdir():
        if not slug_dir.is_dir():
            continue
        try:
            entries = list(slug_dir.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.suffix == ".jsonl" and f.is_file():
                index.setdefault(f.stem, []).append(slug_dir.name)
    return index


def owner_session_of(row: sqlite3.Row) -> str:
    """A Leader row's session identity: its own column first, then the owner_session_id stamped in the team config at
    birth.

    A container team's ``config.owner_session_id`` is stamped at the moment the team is created (see
    ``hook_translator._find_or_create_session_team``), which is more reliable than the team-name prefix — the team name
    only keeps
    the first 8 characters of the session and could theoretically collide. The team-name prefix is **not** used as a
    fallback: an 8-character prefix is not an identity.
    """
    if row["session_id"]:
        return str(row["session_id"])
    try:
        cfg = json.loads(row["team_config"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(cfg.get("owner_session_id") or "")


# ---------------------------------------------------------------------------
# The three Jobs
# ---------------------------------------------------------------------------
def job_subagent_session_id(agents: list[sqlite3.Row]) -> Job:
    """Job A: a sub-agent's session_id ← derived from its transcript path."""
    job = Job("A. agents.session_id ← derived from transcript_path (sub-agents)")
    for a in agents:
        if a["role"] == "leader":
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row(a["id"], a["name"], "no_transcript_path", "session_id"))
            continue
        ref = parse_transcript_path(path)
        if ref is None:
            job.rows.append(
                Row(a["id"], a["name"], "unparseable_path", "session_id", note=path)
            )
            continue
        warn = ""
        if a["root_path"] and not slug_matches_root(ref.project_slug, a["root_path"]):
            # Ownership is **not** rewritten because of this: the slug is a lossy mapping, and a mismatch only means it
            # is worth a human look.
            warn = f"slug={ref.project_slug} does not match the registered project root_path"
        current = a["session_id"] or ""
        if current == ref.session_id:
            job.rows.append(
                Row(a["id"], a["name"], "already_set", "session_id", ref.session_id, warn)
            )
        elif current:
            job.rows.append(
                Row(a["id"], a["name"], "conflict", "session_id", ref.session_id,
                    warn=f"the database already has {current}, the derivation gives {ref.session_id}")
            )
        else:
            job.rows.append(
                Row(a["id"], a["name"], "written", "session_id", ref.session_id, warn)
            )
    return job


def job_leader_transcript_path(
    agents: list[sqlite3.Row], disk: dict[str, list[str]]
) -> Job:
    """Job B: a Leader row's transcript_path ← reverse-looking-up the main-session file by slug + session_id."""
    job = Job("B. agents.transcript_path ← session_probe reverse lookup (Leader rows)")
    for a in agents:
        if a["role"] != "leader":
            continue
        sid = owner_session_of(a)
        if not sid:
            job.rows.append(Row(a["id"], a["name"], "no_session_id", "transcript_path"))
            continue
        if not a["root_path"]:
            job.rows.append(Row(a["id"], a["name"], "no_root_path", "transcript_path", note=sid))
            continue
        expected_slug = project_slug(a["root_path"])
        on_disk = disk.get(sid, [])
        if not on_disk:
            job.rows.append(
                Row(a["id"], a["name"], "transcript_gone", "transcript_path", note=sid)
            )
            continue
        if expected_slug in on_disk:
            slug, reason, warn = expected_slug, "written", ""
        else:
            # The file is there, but sits under another slug. Write the path taking the **file as truth** (backfill
            # wants the file),
            # but mark a separate reason code for it, and never rewrite project_id because of it — ownership only
            # recognizes the registered column.
            slug, reason = on_disk[0], "slug_mismatch"
            warn = f"expected {expected_slug}, actually sits in {'/'.join(on_disk)}"
        path = str(PROJECTS_DIR / slug / f"{sid}.jsonl")
        current = a["transcript_path"] or ""
        if current == path:
            job.rows.append(
                Row(a["id"], a["name"], "already_set", "transcript_path", path, warn)
            )
        elif current:
            job.rows.append(
                Row(a["id"], a["name"], "conflict", "transcript_path", path,
                    warn=f"the database already has {current}")
            )
        else:
            job.rows.append(Row(a["id"], a["name"], reason, "transcript_path", path, warn))
    return job


def job_leader_session_id(agents: list[sqlite3.Row]) -> Job:
    """Job C: a Leader row's session_id ← team.config.owner_session_id. **Not executed by default.**

    This item differs from A/B: it changes the **live resolution result**. ``_on_session_start`` reuses
    a Leader by the criterion "the leader row with the earliest created_at among those with the same session_id", so
    once
    several ghost rows of the same session all get a session_id, the next session restore will reuse the **earliest**
    row,
    rather than the current one. By design this is exactly the convergence direction "one session, one Leader row" wants
    (ghost rows arise
    precisely because, with identity erased, every restore creates a new one), but it is a behavior change and must be
    decided by a human.
    """
    job = Job("C. agents.session_id ← team.config.owner_session_id (Leader rows, requires --leader-session-id)")
    for a in agents:
        if a["role"] != "leader":
            continue
        sid = owner_session_of(a)
        if not sid:
            job.rows.append(Row(a["id"], a["name"], "no_session_id", "session_id"))
            continue
        current = a["session_id"] or ""
        if current == sid:
            job.rows.append(Row(a["id"], a["name"], "already_set", "session_id", sid))
        elif current:
            job.rows.append(
                Row(a["id"], a["name"], "conflict", "session_id", sid,
                    warn=f"the database already has {current}")
            )
        else:
            job.rows.append(
                Row(a["id"], a["name"], "written", "session_id", sid,
                    note=f"team={a['team_name']}")
            )
    return job


# ---------------------------------------------------------------------------
# Risk detection
# ---------------------------------------------------------------------------
def detect_name_collisions(
    agents: list[sqlite3.Row], job_a: Job, job_c: Job, include_c: bool
) -> list[str]:
    """Whether ``(session_id, name)`` duplicates appear after the backfill — the landmine of the registration-dedup
    bypass.

    ``repository.find_agent_by_session(session_id, name)`` is ``limit(1)`` with no
    ORDER BY, and takes the earliest row. When several rows exist under the same (session, name), a future new dispatch
    of the same name
    will be judged a "reuse" and overwrite the old row's token columns in place — exactly destroying the accounts this
    project sets out to build.
    """
    planned: dict[str, str] = {}
    for job, on in ((job_a, True), (job_c, include_c)):
        if not on:
            continue
        for r in job.written():
            if r.column == "session_id":
                planned[r.agent_id] = r.value

    buckets: dict[tuple[str, str], list[str]] = {}
    for a in agents:
        sid = planned.get(a["id"]) or (a["session_id"] or "")
        if not sid:
            continue
        # Workflow fan-out takes an independent registration path (dedup by cc_agent_id, not by name), so this bypass
        # does not affect it
        if a["role"] == "workflow-subagent":
            continue
        buckets.setdefault((sid, a["name"]), []).append(a["id"])

    return [
        f"(session={sid[:8]}, name={name!r}) → {len(ids)} rows"
        for (sid, name), ids in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        if len(ids) > 1
    ]


def describe_leader_resolution_shift(agents: list[sqlite3.Row], job_c: Job) -> list[str]:
    """Which row Job C will change each session's Leader resolution to — printed session by session, for a human to
    decide."""
    by_session: dict[str, list[sqlite3.Row]] = {}
    planned = {r.agent_id: r.value for r in job_c.written()}
    for a in agents:
        if a["role"] != "leader":
            continue
        sid = planned.get(a["id"]) or (a["session_id"] or "")
        if sid:
            by_session.setdefault(sid, []).append(a)
    lines = []
    for sid, rows in sorted(by_session.items(), key=lambda kv: -len(kv[1])):
        rows.sort(key=lambda r: r["created_at"] or "")
        winner = rows[0]
        lines.append(
            f"session {sid[:8]}: {len(rows)} Leader rows → resolution will hit the earliest, "
            f"{winner['id'][:8]}（{winner['created_at']}, team={winner['team_name']}, "
            f"status={winner['status']}）"
        )
    return lines


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def print_job(job: Job, sample_n: int, rng: random.Random) -> None:
    print()
    print("=" * 78)
    print(job.title)
    print("-" * 78)
    counts = job.counts()
    total = sum(counts.values())
    for reason, desc in REASONS.items():
        if reason in counts:
            print(f"  {counts[reason]:6d}  {reason:<20} {desc}")
    print(f"  {total:6d}  Total")

    writable = [r for r in job.rows if r.reason in ("written", "slug_mismatch")]
    if not writable:
        return
    picks = writable if len(writable) <= sample_n else rng.sample(writable, sample_n)
    picks.sort(key=lambda r: r.agent_id)
    print()
    print(f"  ── Sampled {len(picks)}/{len(writable)} rows (for human checking) ──")
    for r in picks:
        flag = "" if r.reason == "written" else f"  [{r.reason}]"
        extra = f"  ({r.note})" if r.note else ""
        warn = f"  ⚠ {r.warn}" if r.warn else ""
        print(f"    {r.agent_id[:8]}  {r.name[:28]:<28} {r.column}={r.value}{flag}{extra}{warn}")


def apply_job(con: sqlite3.Connection, job: Job) -> int:
    """Only write empty columns: the SQL itself carries an ``(col IS NULL OR col='')`` guard, as double insurance with
    the verdict layer.

    The verdict has already filtered once; guarding again here is because the database may have changed between dry-run
    and apply
    (the live system is writing at all times). A guard in the SQL means that even concurrently it will not overwrite a
    value someone else just wrote.
    """
    n = 0
    for r in job.rows:
        if r.reason not in ("written", "slug_mismatch"):
            continue
        cur = con.execute(
            f"update agents set {r.column} = ? "  # noqa: S608 — the column name comes from a constant in this file, not
            # external input
            f"where id = ? and ({r.column} is null or {r.column} = '')",
            (r.value, r.agent_id),
        )
        n += cur.rowcount
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="target database (default: the production database)")
    ap.add_argument("--apply", action="store_true", help="actually write (default: report only)")
    ap.add_argument("--sample", type=int, default=30, help="number of rows sampled and printed per Job")
    ap.add_argument("--seed", type=int, default=0, help="sampling random seed (default 0, reproducible)")
    ap.add_argument(
        "--leader-session-id", action="store_true",
        help="additionally run Job C (backfill Leaders' session_id) — this changes live Leader resolution results",
    )
    ap.add_argument(
        "--ack-collisions", action="store_true",
        help="the (session,name) collision list has been human-reviewed; allow writing with collisions",
    )
    args = ap.parse_args()

    if not args.db.exists():
        print(f"Database does not exist: {args.db}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    con = open_db(args.db, writable=args.apply)
    try:
        agents = load_agents(con)
        disk = index_main_transcripts()

        job_a = job_subagent_session_id(agents)
        job_b = job_leader_transcript_path(agents, disk)
        job_c = job_leader_session_id(agents)

        print(f"Database: {args.db}")
        print(f"Mode: {'APPLY (writing)' if args.apply else 'DRY-RUN (read-only, mode=ro)'}")
        print(f"Total agents rows: {len(agents)}; main-session transcripts on disk: "
            f"{sum(len(v) for v in disk.values())}")
        print("Rule: only write empty columns, never overwrite a non-empty value; a re-run necessarily changes nothing "
            "(everything lands in already_set)")

        print_job(job_a, args.sample, rng)
        print_job(job_b, args.sample, rng)
        print_job(job_c, args.sample, rng)
        if not args.leader_session_id:
            print()
            print("  Job C is not executed by default (it requires --leader-session-id). The above is only a preview "
                "of its verdicts.")

        print()
        print("=" * 78)
        print("Risks")
        print("-" * 78)
        collisions = detect_name_collisions(agents, job_a, job_c, args.leader_session_id)
        if collisions:
            print("  ⚠ After the backfill, (session_id, name) duplicates appear — a future same-name dispatch may be "
                "judged"
                  "a \"reuse of the old row\" and overwrite its token columns:")
            for line in collisions[:20]:
                print(f"      {line}")
            if len(collisions) > 20:
                print(f"      …… {len(collisions) - 20} more groups")
        else:
            print("  ✓ After the backfill, (session_id, name) pairs are all unique; the registration-dedup bypass will "
                "not step on historical rows")

        shift = describe_leader_resolution_shift(agents, job_c if args.leader_session_id else Job(""))
        if args.leader_session_id:
            print()
            print("  Job C will change the Leader resolution result of the following sessions:")
            for line in shift:
                print(f"      {line}")

        jobs = [job_a, job_b] + ([job_c] if args.leader_session_id else [])
        planned = sum(len([r for r in j.rows if r.reason in ("written", "slug_mismatch")]) for j in jobs)
        print()
        print("=" * 78)
        print(f"Total to write: {planned} rows")

        if not args.apply:
            print("Dry-run finished; not one byte was written. Once confirmed correct, the Founder runs --apply (back "
                "up first).")
            return 0

        if collisions and not args.ack_collisions:
            print("Write refused: (session,name) collisions exist and --ack-collisions was not given.", file=sys.stderr)
            return 3

        written = sum(apply_job(con, j) for j in jobs)
        con.commit()
        print(f"Written: {written} rows (verdict candidates {planned} rows; the difference = rows the live system has "
            f"written in the meantime)")
        print("Re-running this script should report 0 pending writes — that is the idempotency acceptance test.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
