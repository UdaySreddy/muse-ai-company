#!/usr/bin/env python3
"""I14 red-line machine check: the three hard constraints of the historical backfill script (design §6.4) must always
hold.

``scripts/backfill_token_usage.py`` rewrites more than two thousand rows of production data in one pass. Of its three
hard constraints,
**if the first one goes wrong it is irrecoverable**: ``workflow_agents.tokens`` is in the ctx_last metric (last-turn
context watermark),
while what this script produces is usage_sum (accumulated usage), measured 5-25x apart. Writing the latter into the
former would
**permanently solidify the mixed metrics into the historical data, indistinguishable afterwards** — far worse than not
backfilling at all (risk R3).

So this check is **behavioral** rather than textual: it really builds a temporary database, really runs ``--apply``
once,
and really compares per-row fingerprints of the forbidden column. A text scan cannot stop dynamically assembled SQL,
while the criterion here is "after the run, did
that column change" — any future change, whatever style it is written in, is caught as long as it touches that column.

It uses tempfile throughout and does not touch one byte of the production database.

Usage::

    python3 scripts/check_backfill_safety.py
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backfill_token_usage.py"
sys.path.insert(0, str(ROOT / "src"))


def load_backfill():
    spec = importlib.util.spec_from_file_location("backfill_token_usage", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_token_usage"] = mod
    spec.loader.exec_module(mod)
    return mod


USAGE = {
    "input_tokens": 10,
    "output_tokens": 500,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 90000,
}
SENTINEL_TOKENS = 424242  # Put a conspicuous sentinel value in the forbidden column


def seed(tmp: Path) -> Path:
    """Build a minimal database that still covers every branch: with/without transcript, already measured, alias/full
    model names."""
    transcript = tmp / "agent-cc1.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps({
                "type": "assistant", "requestId": req,
                "message": {"model": "claude-opus-4-8", "usage": USAGE},
            })
            for req in ("r1", "r2")
        ) + "\n",
        encoding="utf-8",
    )

    db = tmp / "probe.db"
    con = sqlite3.connect(db)
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
    con.execute(
        "insert into agents values ('a1','w1','worker',null,'2026-07-20 10:00:00',?,"
        "null,null,null,null,null,null)",
        (str(transcript),),
    )
    con.execute(
        "insert into agents values ('a2','w2','worker',null,'2026-07-20 10:00:00',null,"
        "null,null,null,null,null,null)"
    )
    con.execute(
        "insert into agents values ('a3','w3','worker',null,'2026-07-20 10:00:00',?,"
        "1,2,3,4,'2026-07-25 00:00:00','transcript')",
        (str(transcript),),
    )
    con.execute(
        "insert into workflow_agents values ('wa1','l1','opus','a1','cc1',"
        "'2026-07-20 10:00:00', ?)",
        (SENTINEL_TOKENS,),
    )
    con.execute(
        "insert into workflow_agents values ('wa2','l2','claude-opus-5','a1','cc2',"
        "'2026-07-20 10:00:00', 999)"
    )
    con.commit()
    con.close()
    return db


def run_script(bf, db: Path, *argv: str) -> int:
    saved = sys.argv
    sys.argv = ["backfill", "--db", str(db), "--quiet", *argv]
    try:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bf.main()
    finally:
        sys.argv = saved


def fingerprint(db: Path, bf) -> dict:
    con = sqlite3.connect(db)
    try:
        return bf.workflow_tokens_fingerprint(con)
    finally:
        con.close()


def check() -> list[str]:
    """Return the list of violations; empty = pass."""
    bad: list[str] = []
    bf = load_backfill()

    # ── Hard constraint 1, static side: the forbidden column is not in the writable-column allowlist ──
    if bf.FORBIDDEN_COLUMN != ("workflow_agents", "tokens"):
        bad.append(f"FORBIDDEN_COLUMN was changed to {bf.FORBIDDEN_COLUMN} — the definition of the forbidden column "
            f"should not move")
    if bf.FORBIDDEN_COLUMN in bf.WRITABLE_COLUMNS:
        bad.append("workflow_agents.tokens appears in the writable-column allowlist — usage_sum would pollute the "
            "ctx_last column")
    wa_writable = {c for t, c in bf.WRITABLE_COLUMNS if t == "workflow_agents"}
    if wa_writable - {"model"}:
        bad.append(f"workflow_agents gained writable columns {sorted(wa_writable - {'model'})} — only model should be "
            f"writable")
    agents_writable = {c for t, c in bf.WRITABLE_COLUMNS if t == "agents"}
    expected_agents = {
        "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
        "tokens_measured_at", "tokens_source", "model",
    }
    if agents_writable != expected_agents:
        bad.append(
            f"The writable column set of agents changed: added {sorted(agents_writable - expected_agents)} / "
            f"removed {sorted(expected_agents - agents_writable)}"
        )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ── Hard constraint 3: dry-run writes nothing ───────────────────────
        db = seed(tmp)
        before = fingerprint(db, bf)
        if run_script(bf, db) != 0:
            bad.append("dry-run returned non-zero")
        con = sqlite3.connect(db)
        if con.execute("select count(*) from agents where tokens_measured_at is not null")\
                .fetchone()[0] != 1:
            bad.append("dry-run wrote data — by default it must only report")
        con.close()

        # ── Hard constraint 3: --apply requires a journal, and must not overwrite an existing journal ──
        if run_script(bf, db, "--apply") != 2:
            bad.append("--apply without --journal was not rejected — the journal is the only recovery credential")
        occupied = tmp / "taken.json"
        occupied.write_text("{}")
        if run_script(bf, db, "--apply", "--journal", str(occupied)) != 2:
            bad.append("An existing journal was not rejected — a second apply would overwrite the pre-backfill "
                "baseline")

        # ── Hard constraint 1, behavioral side: really run it once, the forbidden column unchanged row by row ──
        journal = tmp / "j.json"
        if run_script(bf, db, "--apply", "--journal", str(journal)) != 0:
            bad.append("--apply returned non-zero")
        after = fingerprint(db, bf)
        if after["sha256"] != before["sha256"]:
            bad.append(
                f"❗ workflow_agents.tokens was changed by the backfill:"
                f"sum {before['sum']} → {after['sum']}（sha {before['sha256'][:12]} → "
                f"{after['sha256'][:12]}) — the ctx_last metric has been polluted by usage_sum"
            )
        con = sqlite3.connect(db)
        if con.execute("select tokens from workflow_agents where id='wa1'").fetchone()[0] \
                != SENTINEL_TOKENS:
            bad.append("The sentinel row's tokens were touched")
        # apply must actually have done work (otherwise "unchanged" is because nothing was done, and this check becomes
        # an idle spin)
        measured = con.execute(
            "select count(*) from agents where tokens_measured_at is not null"
        ).fetchone()[0]
        if measured != 2:
            bad.append(f"Backfill did not take effect ({measured} rows measured, expected 2) — the machine check may "
                f"be spinning idly")
        if con.execute("select model from workflow_agents where id='wa1'").fetchone()[0] \
                != "claude-opus-4-8":
            bad.append("Model observation backfill did not take effect — the machine check may be spinning idly")
        con.close()
        if not journal.is_file():
            bad.append("The journal was not written to disk")
        else:
            data = json.loads(journal.read_text())
            if not data.get("forbidden_column", {}).get("unchanged"):
                bad.append("The journal did not record the credential that the forbidden column is unchanged")
            if data.get("metric") != "usage_sum":
                bad.append("The journal did not label the metric — a token value detached from its metric is "
                    "meaningless")

        # ── Hard constraint 3: idempotency, re-running changes nothing ───────
        saved_out = sys.stdout
        import contextlib
        import io

        buf = io.StringIO()
        sys.argv = ["backfill", "--db", str(db), "--quiet"]
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            bf.main()
        sys.stdout = saved_out
        if "Total to write: 0 rows" not in buf.getvalue():
            bad.append("Re-running after --apply still has rows pending write — idempotency breached")

        # ── Hard constraint 1, second gate: the write layer blows up on the spot for an out-of-bounds column ──
        job = bf.Job("probe", "workflow_agents")
        job.rows.append(bf.Row("workflow_agents", "wa1", "l1", "written", values={"tokens": 1}))
        con = sqlite3.connect(db)
        try:
            bf.apply_job(con, job)
            bad.append("apply_job accepted an out-of-bounds column write — the allowlist assert has failed")
        except RuntimeError:
            pass
        finally:
            con.close()

        # ── Hard constraint 2: coverage window split ─────────────────────────
        con = bf.open_db(db, writable=False)
        agents = bf.load_agents(con)
        cohorts = bf.detect_backfill_cohorts(con)
        con.close()
        cov = bf.measure_coverage(agents, cohorts, leader=False)
        if cov.total != 3:
            bad.append(
                f"Coverage denominator {cov.total} ≠ 3 — rows without a transcript were moved out of the denominator,"
                "which is exactly a part impersonating the whole (R2)"
            )
        projected = bf.project_coverage(cov, 5)
        if projected.incremental != cov.incremental:
            bad.append("The backfill changed the 'incremental collection' cell — one number masked another number "
                "(6.3-2)")
        if projected.total != cov.total:
            bad.append("The backfill changed the denominator")
        line = cov.line()
        if not all(k in line for k in ("incremental collection", "historical backfill", "unattributed")):
            bad.append("The coverage presentation is missing one of the three — unattributed must be on the same "
                "screen as attributed")
        if "Total" in line or "Grand total" in line:
            bad.append("The coverage row gives a total — adding numbers of two metrics together is meaningless")

    return bad


def main() -> int:
    try:
        bad = check()
    except Exception as exc:  # noqa: BLE001 — if the machine check itself blows up it must say so clearly, not silently
    # pass
        print(f"❌ Machine check execution failed: {type(exc).__name__}: {exc}")
        return 1
    if bad:
        for line in bad:
            print(f"  - {line}")
        return 1
    print(
        "✅ Backfill red line passed: workflow_agents.tokens unchanged row by row (fingerprint) / "
        "coverage windows not merged / dry-run by default + journal required + re-running changes nothing"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
