"""AI Team OS — Workflow observability ingest (I3a).

The pure ingestion module of the CC ultracode/Workflow observation layer (a single copy in the API layer, not the hook
dual-copy, avoiding red line 5's
synchronization pitfalls and install.py registration drift). Three trigger points (PostToolUse receipt, reaper polling,
SessionStart reconciliation) share the pure functions here:

- ``parse_workflow_receipt(text)``: extracts the four keys from a PostToolUse(Workflow) receipt with regexes.
- ``ingest_run_from_file(repo, event_bus, wf_json_path)``: reads the ``wf_<id>.json`` rich snapshot
  → upsert run + batch upsert agents → stamps team_id/os_agent_id → writes back team.completed_at
  → emits ``workflow.completed``. Idempotent, fully try/except-wrapped.
- ``reconcile(repo, event_bus, project_dir=None, session_id=None)``: proj-slug glob
  ``~/.claude/projects/<slug>/*/workflows/wf_*.json`` and ingests file by file.

Key semantics: hooks only drive "timing + correlation anchors + lifecycle events"; the file is the "full telemetry
source of truth"
(tokens/durations/per-agent return values exist only in ``wf_<id>.json.workflowProgress[]``). The two projection tables
are
"rebuildable caches of immutable files", advanced monotonically by UPSERT on natural keys, never deleting rows (red line
3 append-only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat as stat_module
from datetime import datetime
from pathlib import Path
from typing import Any

from aiteam.api.event_bus import EventBus
from aiteam.clock import from_timestamp, parse_utc, utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent, WorkflowRun

logger = logging.getLogger(__name__)

# — Phase2 live-tracking constants (calibrated from measurements; any change must re-derive from the agent-row interval
# distribution, see design §10.3) —
WF_STALL_SECONDS = 900  # interrupted stillness threshold: the longest measured silence of a healthy agent is 173.8s
# (p99=77.6s, n=3378 intervals); 900s ≈ 5.2× the maximum observed value — rather judge late than wrong
# the window for continued re-checks after interrupted (terminal-state self-healing
# channel); past the window it ages out of the reaper's view and steady state returns to
# zero stats
WF_INTERRUPTED_RECHECK_HOURS = 24
WF_LIVE_TAIL_MAX_RUNS = 10  # per-tick cap on live tail runs (_reap_cycle 30s hard-timeout protection)
WF_AGENT_TAIL_BYTES = 65536  # agent jsonl tail window (a single assistant row is far smaller than 64KB)

# Terminal-state set (aligned with repository._WF_STATUS_RANK's same-rank semantics; red line 8 does not change the rank
# body)
_WF_TERMINAL_STATUSES = frozenset({"completed", "killed", "failed"})

# WP10 event deduplication: a per-wf_id in-process lock serializing the "upsert → judge became_*" critical section of
# the same run.
# The three drivers (reaper reconciliation / SessionStart reconciliation / hook receipt ingest) run on the same process
# and the same event loop,
# so they can interleave freely at await points — pure in-transaction judging relies on snapshot-conflict aborts under a
# cross-connection file WAL as a fallback, but under
# a single-connection topology (e.g. an in-memory DB) the three coroutines can simultaneously read the old status and
# each judge became_completed=True →
# duplicate emits. This lock makes the critical section strictly serial, guaranteeing exactly-once regardless of
# connection topology. Cross-API-process cases are additionally
# covered by the reaper governance lease + WAL snapshot aborts. The dict is memoized by wf_id and grows boundedly with
# the process lifecycle
# (one lightweight Lock per run) and is cleared on process restart — no background cleanup daemon (following the "no
# timers" discipline).
_WF_INGEST_LOCKS: dict[str, asyncio.Lock] = {}


def _wf_ingest_lock(wf_id: str) -> asyncio.Lock:
    """Return the per-run ingest lock, creating it on first use (single-threaded loop)."""
    lock = _WF_INGEST_LOCKS.get(wf_id)
    if lock is None:
        lock = asyncio.Lock()
        _WF_INGEST_LOCKS[wf_id] = lock
    return lock

# wf_<id> run id (same semantics as hook_translator._WF_RUN_ID_RE).
# Bounded to a single optional dash-suffix, not unbounded `*` — see hook_translator.py's
# _WF_RUN_ID_RE comment for the worktree-suffix over-match this fixes (task f8207497).
_WF_RUN_ID_RE = re.compile(r"wf_[0-9a-z]+(?:-[0-9a-z]+)?", re.IGNORECASE)
# Receipt per-line fields (each field occupies its own line; .+ captures to end of line then strips, accommodating
# Summaries containing spaces).
_TASK_ID_RE = re.compile(r"Task ID:\s*(\S+)")
_SUMMARY_RE = re.compile(r"Summary:\s*(.+)")
_TRANSCRIPT_RE = re.compile(r"Transcript dir:\s*(.+)")
_SCRIPT_RE = re.compile(r"Script file:\s*(.+)")


# ============================================================
# Pure utilities
# ============================================================


def _to_int(v: Any) -> int:
    """Robustly convert str/int/float/None (snapshot numbers are mostly strings) to int; 0 on failure."""
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _ms_to_dt(ms: int | None) -> datetime | None:
    """epoch milliseconds → local datetime; None for 0/None/out-of-range."""
    if not ms:
        return None
    try:
        return from_timestamp(ms / 1000)
    except (ValueError, OSError, OverflowError):
        return None


def _trim(s: str, n: int) -> str:
    """Truncate to n characters (anti-bloat)."""
    return s[:n] if s else ""


def _norm_phases(raw: Any) -> list[dict[str, Any]]:
    """Normalize file phases ([{title,detail}]) or plan phases ([str]) to [{index,title}]."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for i, ph in enumerate(raw, start=1):
            if isinstance(ph, dict):
                out.append(
                    {
                        "index": _to_int(ph.get("index")) or i,
                        "title": str(ph.get("title") or ""),
                    }
                )
            elif isinstance(ph, str):
                out.append({"index": i, "title": ph})
    return out


def _trim_result(result: Any, max_chars: int = 8000) -> dict[str, Any] | None:
    """Store terminal StructuredOutput results truncated (prevents oversized results from bloating the DB)."""
    if result is None:
        return None
    if not isinstance(result, dict):
        return {"_raw": str(result)[:max_chars]}
    try:
        s = json.dumps(result, ensure_ascii=False)
    except Exception:
        return {"_repr": str(result)[:max_chars]}
    if len(s) <= max_chars:
        return result
    return {"_truncated": True, "_preview": s[:max_chars]}


def _project_slug(path: str) -> str:
    """Reverse-resolve a project's root_path into the CC projects directory slug (every non-alphanumeric character →
    '-').

    Example: ``/Users/cronus/Desktop/AI team OS`` → ``-Users-cronus-Desktop-AI-team-OS``.
    CC does not collapse consecutive separators, so this also replaces character by character without collapsing.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", path or "")


def _claude_projects_dir() -> Path:
    """Root of ``~/.claude/projects`` (tests can monkeypatch this function to point at a temp directory)."""
    return Path.home() / ".claude" / "projects"


def _claude_tmp_dir() -> Path:
    """CC background Task landing directory ``/tmp/claude-<uid>`` (on macOS /tmp → /private/tmp).

    Only for ``enrich_from_task_output``'s fallback read of ``tasks/<taskId>.output``; cleared on restart,
    never a source of truth. Tests can monkeypatch.
    """
    return Path(f"/tmp/claude-{os.getuid()}")


def _iso_to_utc(s: str) -> datetime | None:
    """agent jsonl top-level timestamp (UTC ISO, e.g. 2026-06-24T06:54:31.209Z) → aware UTC.

    Same semantics as the other timestamps in the DB (see aiteam/clock.py).
    """
    return parse_utc(str(s)) if s else None


def _first_line_timestamp(path: Path) -> datetime | None:
    """First-line top-level timestamp of an agent jsonl → started_at approximation (journal rows carry no
    timestamps)."""
    try:
        with path.open("rb") as f:
            line = f.readline()
        obj = json.loads(line)
        if isinstance(obj, dict):
            return _iso_to_utc(str(obj.get("timestamp") or ""))
    except Exception:  # noqa: BLE001 — best-effort; on failure there is simply no started_at
        return None
    return None


def _first_user_prompt(path: Path, max_chars: int = 160) -> str:
    """Read the head of an agent transcript and extract the first user message's text as the running-period semantic
    label.

    A nested run's wf_<id>.json is written late (only at terminal state), so the label stays empty throughout the
    running period
    (3edd0dc1) — the prompt's first line is the only semantic information available during the active period. If not
    found within the first 64KB
    or the file is not ready, return an empty string and retry on the next tick.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
        for line in head.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("type") != "user":
                continue
            content = (d.get("message") or {}).get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    str(c.get("text") or "")
                    for c in content
                    if isinstance(c, dict)
                )
            else:
                continue
            text = " ".join(text.split())
            if text:
                return text[:max_chars]
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _last_assistant_ctx_tokens(path: Path) -> int | None:
    """Search the last 64KB tail window of an agent jsonl backwards for the last assistant row containing message.usage
    → lastCtx.

    D1 ruling semantics: the sum of the four fields input + cache_creation_input + cache_read_input + output
    (reconciles directly against the wf_<id>.json terminal-state per-agent tokens: error agents match exactly, done
    agents only 3~12% high). Cross-round accumulation was rejected (cache_read double-counting inflates ~445×).

    Returns:
        lastCtx int; no assistant usage in the tail window / read failure → None (the caller keeps the old value).
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - WF_AGENT_TAIL_BYTES))
            data = f.read()
    except OSError:
        return None
    for raw in reversed(data.split(b"\n")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001 — the tail window's first segment may be a half-line; skip it
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        return (
            _to_int(usage.get("input_tokens"))
            + _to_int(usage.get("cache_creation_input_tokens"))
            + _to_int(usage.get("cache_read_input_tokens"))
            + _to_int(usage.get("output_tokens"))
        )
    return None


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _name_from_script(script_path: str, wf_id: str) -> str:
    """Reverse-resolve the workflow name from the script file name: drop .js and the trailing -wf_<id>.

    Example: ``cnipa-xml-format-research-wf_8e92fe01-67c.js`` → ``cnipa-xml-format-research``.
    """
    if not script_path:
        return ""
    base = script_path.replace("\\", "/").rsplit("/", 1)[-1]
    if base.endswith(".js"):
        base = base[:-3]
    suffix = f"-{wf_id}"
    if wf_id and base.endswith(suffix):
        base = base[: -len(suffix)]
    return base


def parse_workflow_receipt(text: str) -> dict[str, Any]:
    """Extract the four keys (+ transcript_dir, name) from a PostToolUse(Workflow) launch receipt's plaintext.

    Receipt sample (about 1331 characters of plaintext; < 32KB so it is not trimmed by send_event._trim_payload):
        Workflow launched in background. Task ID: westwrtgj
        Summary: Multi-way parallel research...
        Transcript dir: /Users/.../subagents/workflows/wf_8e92fe01-67c
        Script file: /Users/.../workflows/scripts/<name>-wf_8e92fe01-67c.js

    Returns:
        {wf_id, cc_task_id, script_path, name, summary, transcript_dir}; empty strings where extraction fails.
    """
    text = text or ""
    task_m = _TASK_ID_RE.search(text)
    summary_m = _SUMMARY_RE.search(text)
    transcript_m = _TRANSCRIPT_RE.search(text)
    script_m = _SCRIPT_RE.search(text)

    transcript_dir = transcript_m.group(1).strip() if transcript_m else ""
    script_path = script_m.group(1).strip() if script_m else ""

    # wf_id: prefer extracting from transcript_dir, then from the whole text.
    wf_id = ""
    for src in (transcript_dir, script_path, text):
        m = _WF_RUN_ID_RE.search(src.replace("\\", "/"))
        if m:
            wf_id = m.group(0)
            break

    return {
        "wf_id": wf_id,
        "cc_task_id": task_m.group(1).strip() if task_m else "",
        "script_path": script_path,
        "name": _name_from_script(script_path, wf_id),
        "summary": summary_m.group(1).strip() if summary_m else "",
        "transcript_dir": transcript_dir,
    }


def run_json_path_from_transcript_dir(transcript_dir: str, wf_id: str) -> Path | None:
    """Derive the run JSON path from the receipt's Transcript dir.

    transcript_dir = ``<session>/subagents/workflows/wf_<id>``; the run JSON is its sibling
    ``<session>/workflows/wf_<id>.json`` (not under subagents).
    """
    if not transcript_dir or not wf_id:
        return None
    try:
        tdir = Path(transcript_dir)
        session_dir = tdir.parent.parent.parent
        return session_dir / "workflows" / f"{wf_id}.json"
    except Exception:
        return None


# ============================================================
# File ingestion (full telemetry source of truth)
# ============================================================


async def _upsert_agents_from_progress(
    repo: StorageRepository,
    wf_id: str,
    project_id: str,
    progress: Any,
    run_team_id: str | None = None,
) -> int:
    """workflowProgress[] type=workflow_agent entries → batch upsert workflow_agents.

    Shared pure function: used by both ``ingest_run_from_file`` (wf_<id>.json source of truth) and
    ``enrich_from_task_output`` (/tmp .output fallback, a 7-key subset missing runId).
    Also stamps os_agent_id to associate existing members (agents.cc_tool_use_id == cc_agent_id);
    when run_team_id is given, performs "corpse-collection migration": OS members hit by the authoritative roster that
    still linger in a
    workflow-session-* fallback team (killed mid-way, never promoted / arrived late and missed the receipt migration)
    are migrated into the run team (missed-migration record 2026-07-08, wf-a8bda693e5).
    """
    agent_entries = [
        x
        for x in (progress or [])
        if isinstance(x, dict) and x.get("type") == "workflow_agent"
    ]
    n = 0
    for a in agent_entries:
        cc_agent_id = str(a.get("agentId") or "").strip()
        os_agent_id = None
        if cc_agent_id:
            try:
                existing = await repo.find_agent_by_cc_id(cc_agent_id)
                os_agent_id = existing.id if existing else None
            except Exception:
                existing = None
                os_agent_id = None
            if (
                existing is not None
                and run_team_id
                and getattr(existing, "team_id", None)
                and existing.team_id != run_team_id
            ):
                try:
                    cur = await repo.get_team(existing.team_id)
                    if cur is not None and cur.name.startswith("workflow-session-"):
                        await repo.update_agent(existing.id, team_id=run_team_id)
                except Exception:  # noqa: BLE001 — a corpse-collection failure does not block the telemetry upsert
                    pass
        wa = WorkflowAgent(
            run_id=wf_id,
            wf_id=wf_id,
            project_id=project_id,
            cc_agent_id=cc_agent_id,
            os_agent_id=os_agent_id,
            label=str(a.get("label") or ""),
            phase_index=_to_int(a.get("phaseIndex")),
            phase_title=str(a.get("phaseTitle") or ""),
            model=str(a.get("model") or ""),
            state=str(a.get("state") or ""),
            tokens=_to_int(a.get("tokens")),
            tool_calls=_to_int(a.get("toolCalls")),
            duration_ms=_to_int(a.get("durationMs")) or None,
            last_tool_name=str(a.get("lastToolName") or ""),
            last_tool_summary=_trim(str(a.get("lastToolSummary") or ""), 500),
            prompt_preview=_trim(str(a.get("promptPreview") or ""), 2000),
            result_preview=_trim(str(a.get("resultPreview") or ""), 2000),
            started_at=_ms_to_dt(_to_int(a.get("startedAt"))),
            queued_at=_ms_to_dt(_to_int(a.get("queuedAt"))),
        )
        try:
            await repo.upsert_workflow_agent(wa)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow ingest: agent upsert failed wf=%s cc=%s: %s", wf_id, cc_agent_id, exc
            )
    return n


async def ingest_run_from_file(
    repo: StorageRepository,
    event_bus: EventBus,
    wf_json_path: str | Path,
) -> dict[str, Any]:
    """Read the ``wf_<id>.json`` rich snapshot → upsert run + agents → write back team → emit completed.

    Idempotent: can be re-run repeatedly (upsert by natural key; emit fires only on "newly completed", avoiding event
    doubling).
    Fully try/except-wrapped, never raises (for best-effort hook/reaper calls).
    """
    path = Path(wf_json_path)
    # Phase2 fingerprint: stat *before* read_text (TOCTOU-conservative: if the file changes after the read, the stored
    # fp is the old one → the next tick definitely misses → re-ingests; the direction is safe).
    fp = ""
    try:
        st = path.stat()
        fp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        fp = ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a file-read failure no longer imitates a bare except: pass; it logs
        logger.warning("workflow ingest: read/parse failed %s: %s", path, exc)
        return {"ok": False, "reason": "read_error", "path": str(path)}

    if not isinstance(data, dict):
        return {"ok": False, "reason": "bad_json", "path": str(path)}

    wf_id = str(data.get("runId") or "").strip()
    if not wf_id:
        return {"ok": False, "reason": "no_runId", "path": str(path)}

    # session_id reverse-resolved from the path: <session>/workflows/wf_<id>.json.
    session_id: str | None = None
    try:
        session_id = path.parent.parent.name or None
    except Exception:
        session_id = None

    # team / project association: prefer the existing workflow-<wf_id> team; if none, adopt this session's
    # workflow-session-<sid> fallback team (when the wf_id arrives late, all agents hang there — looking up by wf name
    # alone
    # would cause "one run, two teams" fragmentation); when neither exists during the OS's offline period, leave it
    # empty.
    team = None
    try:
        team = await repo.get_team_by_name(f"workflow-{wf_id}")
        if team is None and session_id:
            fallback = await repo.get_team_by_name(
                f"workflow-session-{session_id[:8]}"
            )
            fb_wf = (fallback.config or {}).get("workflow_run_id") if fallback else None
            if fallback is not None and fb_wf in (None, "", wf_id):
                team = fallback
    except Exception:
        team = None
    team_id = team.id if team else None
    project_id = (getattr(team, "project_id", None) or "") if team else ""

    # Ownership = the file source of truth (user decision 2026-07-07, abolishing the "absorb into the OS" strategy): the
    # slug of the run's landing
    # location ↔ the slug of a registered project's root must match exactly; on a match it overrides the team-inherited
    # value
    # (the team may have been absorbed into the wrong project by the historical strategy); on no match, keep the
    # original value / leave empty and never guess.
    try:
        _run_slug = path.parent.parent.parent.name
        for _p in await repo.list_projects():
            if _p.root_path and _project_slug(_p.root_path) == _run_slug:
                project_id = _p.id
                break
    except Exception:  # noqa: BLE001
        pass

    start_ms = _to_int(data.get("startTime"))
    dur_ms = _to_int(data.get("durationMs"))
    started_at = _ms_to_dt(start_ms)
    completed_at = _ms_to_dt(start_ms + dur_ms) if (start_ms and dur_ms) else None
    status = str(data.get("status") or "completed")
    cc_task_id_raw = str(data.get("taskId") or "").strip()

    run = WorkflowRun(
        wf_id=wf_id,
        project_id=project_id,
        team_id=team_id,
        session_id=session_id,
        cc_task_id=cc_task_id_raw or None,
        name=str(data.get("workflowName") or ""),
        status=status,
        source="file",
        phases=_norm_phases(data.get("phases")),
        agent_count=_to_int(data.get("agentCount")),
        total_tokens=_to_int(data.get("totalTokens")),
        total_tool_calls=_to_int(data.get("totalToolCalls")),
        duration_ms=dur_ms or None,
        summary=str(data.get("summary") or ""),
        result=_trim_result(data.get("result")),
        script_path=str(data.get("scriptPath") or ""),
        started_at=started_at,
        completed_at=completed_at,
        source_fingerprint=fp or None,  # None = no change (keep the old fp when stat fails)
    )

    # Event dedup guardrail: emit workflow.completed only on "this ingestion's first completion";
    # when killed/failed first enter a terminal state, emit workflow.run_ingested (Phase2; previously silent).
    # The "first transition" judgment is folded into the upsert transaction (read old status → write new status,
    # atomically returning became_*),
    # replacing the outside-transaction get → was_completed check-then-act; then a per-wf in-process lock serializes the
    # "upsert →
    # obtain became_*" critical section — only one driver can obtain the running→completed transition; the others,
    # sequenced after it,
    # necessarily see completed on upsert → became_completed=False. Emits happen outside the lock, each decided by its
    # own captured became_*
    # value, so exactly one truly emits (audit WP10: three unserialized drivers interleaving).
    try:
        async with _wf_ingest_lock(wf_id):
            upsert_res = await repo.upsert_workflow_run(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow ingest: run upsert failed wf=%s: %s", wf_id, exc)
        return {"ok": False, "reason": "run_upsert_failed", "wf_id": wf_id}

    # Batch upsert per-agent telemetry (type=workflow_agent), stamping os_agent_id to associate existing members
    # + and corpse-collect-migrate members lingering in the fallback team into the run team.
    n = await _upsert_agents_from_progress(
        repo, wf_id, project_id, data.get("workflowProgress"), run_team_id=team_id
    )

    # Also backfills a historical gap: team.completed_at is always None → write it back using startTime+durationMs;
    # writing to an existing nullable field, not a deletion — compliant (red line 3).
    if team is not None:
        try:
            updates: dict[str, Any] = {}
            if completed_at is not None and getattr(team, "completed_at", None) is None:
                updates["completed_at"] = completed_at
            if run.summary and not (getattr(team, "summary", "") or ""):
                updates["summary"] = run.summary[:500]
            # The workflow team's ownership follows the run's file source of truth (correcting projects wrongly absorbed
            # historically)
            if project_id and (getattr(team, "project_id", None) or "") != project_id:
                updates["project_id"] = project_id
            # Team status follows the run (record 2026-07-08): a bypass session's SessionEnd once killed the team of a
            # still-
            # running run into completed — while the run runs, revive to active (self-healing
            # historical mis-kills); at run terminal state, converge to completed. This must ship in the same batch as
            # SessionEnd's workflow-team
            # exemption (otherwise ingest revives ↔ SessionEnd kills again, ping-ponging).
            _t_status = str(getattr(team, "status", ""))  # an enum's str may carry a class-name prefix
            if status in _WF_TERMINAL_STATUSES and _t_status.endswith("active"):
                updates["status"] = "completed"
            elif status == "running" and _t_status.endswith("completed"):
                updates["status"] = "active"
            if updates:
                await repo.update_team(team.id, **updates)
            # Symmetric member wrap-up completion (confirmed 2026-07-10, wf_811593ec: two members stuck busy):
            # when SubagentStop is occasionally lost, nobody moves a terminal run's members to offline and they can only
            # wait for
            # the reaper's 15min heartbeat timeout. A terminal run means no one alive in the team — wrap them up
            # directly.
            # Excluding the leader: a historical Leader row may parasitize a workflow team (03fe7cae),
            # and its busy state belongs to a live session, not to this run's collection (a manual cleanup in round 35
            # mistakenly swept it, confirmed in practice).
            if status in _WF_TERMINAL_STATUSES:
                for member in await repo.list_agents(team.id):
                    if str(getattr(member, "role", "")) == "leader":
                        continue
                    if str(getattr(member, "status", "")).endswith(("busy", "waiting")):
                        await repo.update_agent(member.id, status="offline")
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow ingest: team backfill failed wf=%s: %s", wf_id, exc)

    emitted = False
    if status == "completed" and upsert_res.became_completed:
        try:
            await event_bus.emit(
                "workflow.completed",
                f"workflow:{wf_id}",
                {
                    "wf_id": wf_id,
                    "name": run.name,
                    "status": status,
                    "agent_count": run.agent_count,
                    "total_tokens": run.total_tokens,
                    "total_tool_calls": run.total_tool_calls,
                    "duration_ms": run.duration_ms,
                    "team_id": team_id,
                    "project_id": project_id,
                    "source": "file",
                },
                entity_id=wf_id,
                entity_type="workflow",
            )
            emitted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow ingest: emit completed failed wf=%s: %s", wf_id, exc)

    # Phase2: killed/failed first entering a terminal state emit workflow.run_ingested (the MVP was silent before;
    # workflow.completed's semantics stay untouched and the two are mutually exclusive — completed takes the branch
    # above).
    if status in ("killed", "failed") and upsert_res.became_terminal:
        try:
            await event_bus.emit(
                "workflow.run_ingested",
                f"workflow:{wf_id}",
                {
                    "wf_id": wf_id,
                    "name": run.name,
                    "status": status,
                    "agent_count": run.agent_count,
                    "total_tokens": run.total_tokens,
                    "duration_ms": run.duration_ms,
                    "team_id": team_id,
                    "project_id": project_id,
                    "source": "file",
                },
                entity_id=wf_id,
                entity_type="workflow",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow ingest: emit run_ingested failed wf=%s: %s", wf_id, exc)

    return {
        "ok": True,
        "wf_id": wf_id,
        "agents": n,
        "status": status,
        "emitted": emitted,
        "new_completion": emitted,
    }


# ============================================================
# Reconciliation (reaper fallback + SessionStart/manual acceleration, sharing the same ingest function)
# ============================================================


async def reconcile(
    repo: StorageRepository,
    event_bus: EventBus,
    project_dir: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Scan all ``workflows/wf_*.json`` under a proj-slug and ingest file by file (each file in its own try/except).

    Args:
        project_dir: limit to the slug of the project owning this directory; None scans all registered projects.
        session_id: limit to one session's ``<slug>/<session_id>/workflows/`` (used to accelerate hook traffic).

    Returns:
        {ingested (count newly completed this time), updated (count of already-completed re-reconciled), errors,
        scanned}.
    """
    ingested = 0
    updated = 0
    errors = 0
    scanned = 0

    try:
        projects = await repo.list_projects()
    except Exception:
        projects = []

    slug_set: set[str] = set()
    if project_dir:
        pd = _norm_path(project_dir)
        matched = None
        best = -1
        for p in projects:
            rp = _norm_path(p.root_path or "")
            if rp and (pd == rp or pd.startswith(rp + "/")) and len(rp) > best:
                matched = p
                best = len(rp)
        if matched and matched.root_path:
            slug_set.add(_project_slug(matched.root_path))
        else:
            # When no registered project matches, reverse-resolve the slug directly from the cwd (covering
            # not-yet-registered projects).
            slug_set.add(_project_slug(project_dir))
    else:
        for p in projects:
            if p.root_path:
                slug_set.add(_project_slug(p.root_path))

    # Steady-state cheap short-circuit (exactly one stat per file; Phase2 fingerprint first, mtime as fallback):
    # 1) The run row's source_fingerprint is non-empty → fp(mtime_ns:size) equal means skip; unequal means ingest
    # directly
    # (no longer falling to an mtime comparison: a same-second append has equal mtime but changed size and would be
    # wrongly
    # skipped by the mtime rule — the (mtime_ns, size) tuple is strictly stronger than pure mtime, only strengthening,
    # never regressing);
    # 2) fp empty (old rows ingested before Phase2) → keep the original mtime ≤ updated_at rule (an MVP byte-for-byte
    # regression test). Deliberately not using "terminal state means skip": resumeFromRunId rewrites the same-named
    # wf_<id>.json in place
    #    (killed→completed), and a newer fp/mtime naturally triggers re-ingestion.
    last_ingest: dict[str, tuple[datetime | None, str]] = {}
    try:
        for known in await repo.list_workflow_runs(limit=1000):
            last_ingest[known.wf_id] = (known.updated_at, known.source_fingerprint or "")
    except Exception:  # noqa: BLE001 — a preload failure degrades to a full ingest (still idempotent)
        last_ingest = {}

    inner = (
        f"{session_id}/workflows/wf_*.json" if session_id else "*/workflows/wf_*.json"
    )
    base = _claude_projects_dir()
    seen: set[str] = set()
    for slug in slug_set:
        proj_dir = base / slug
        if not proj_dir.exists():
            continue
        try:
            files = sorted(proj_dir.glob(inner))
        except Exception:
            files = []
        for jf in files:
            key = str(jf)
            if key in seen:
                continue
            seen.add(key)
            scanned += 1
            prev_time, prev_fp = last_ingest.get(jf.stem, (None, ""))
            if prev_time is not None or prev_fp:
                try:
                    st = jf.stat()
                except OSError:
                    st = None
                if st is not None:
                    if prev_fp:
                        # fingerprint first: equal = file unchanged, skip; unequal = ingest directly
                        if f"{st.st_mtime_ns}:{st.st_size}" == prev_fp:
                            continue
                    elif (
                        prev_time is not None
                        and from_timestamp(st.st_mtime) <= prev_time
                    ):
                        continue  # old-row mtime fallback: the file has not changed since the last ingestion
            try:
                res = await ingest_run_from_file(repo, event_bus, jf)
            except Exception as exc:  # noqa: BLE001 — a single file's failure is isolated and does not block the rest
                errors += 1
                logger.warning("reconcile: ingest raised %s: %s", jf, exc)
                continue
            if res.get("ok"):
                if res.get("emitted"):
                    ingested += 1
                else:
                    updated += 1
            else:
                errors += 1

    return {"ingested": ingested, "updated": updated, "errors": errors, "scanned": scanned}


# ============================================================
# Phase2 live tracking (attached to reaper ticks / manual reconcile; best-effort, single-run failure isolation)
# ============================================================


async def _candidate_slugs(repo: StorageRepository, run: WorkflowRun) -> list[str]:
    """A run's CC projects slug candidates: with a project_id, reverse-resolve its root_path; when missing/not hit,
    iterate all registered projects' slugs (aligned with reconcile's slug_set logic)."""
    slugs: list[str] = []
    if run.project_id:
        try:
            proj = await repo.get_project(run.project_id)
        except Exception:
            proj = None
        root = getattr(proj, "root_path", "") if proj else ""
        if root:
            slugs.append(_project_slug(root))
    if not slugs:
        try:
            projects = await repo.list_projects()
        except Exception:
            projects = []
        for p in projects:
            if p.root_path:
                s = _project_slug(p.root_path)
                if s not in slugs:
                    slugs.append(s)
    # Cross-project fix B (narrow fallback, for legacy rows only): when an old row has no transcript_dir, registered
    # projects'
    # slugs may none of them contain its session (a run of an unregistered project) — do one global reverse lookup of
    # the directory by session_id.
    # The observation set (running ∪ interrupted within the 24h window) is bounded and the cost controllable; new rows
    # take A's direct addressing
    # and never enter this path, so the steady-state zero-stat semantics do not regress.
    if run.session_id and not run.transcript_dir:
        try:
            for d in _claude_projects_dir().glob(f"*/{run.session_id}"):
                if d.is_dir():
                    s = d.parent.name
                    if s not in slugs:
                        slugs.append(s)
        except Exception:  # noqa: BLE001
            pass
    return slugs


def _find_wf_dir(
    base: Path, slugs: list[str], session_id: str | None, wf_id: str
) -> Path | None:
    """Locate the live transcript directory ``<slug>/<session>/subagents/workflows/<wf_id>/``.

    With session_id known, build the path directly; when missing/not hit, glob ``*/subagents/workflows/<wf_id>``.
    """
    for slug in slugs:
        proj_dir = base / slug
        if not proj_dir.exists():
            continue
        if session_id:
            cand = proj_dir / session_id / "subagents" / "workflows" / wf_id
            if cand.is_dir():
                return cand
        try:
            for cand in proj_dir.glob(f"*/subagents/workflows/{wf_id}"):
                if cand.is_dir():
                    return cand
        except Exception:  # noqa: BLE001 — directory races etc.; continue to the next slug
            continue
    return None


async def live_wf_dir(repo: StorageRepository, run: WorkflowRun) -> Path | None:
    """Live transcript directory location (the standalone entry of design §10.2 step 4-1; test/reuse friendly)."""
    slugs = await _candidate_slugs(repo, run)
    return _find_wf_dir(_claude_projects_dir(), slugs, run.session_id, run.wf_id)


def _find_terminal_json(
    base: Path,
    slugs: list[str],
    session_id: str | None,
    wf_id: str,
    wf_dir: Path | None,
) -> Path | None:
    """Terminal-state snapshot ``<session>/workflows/<wf_id>.json`` — returns the path only if it *exists*.

    When wf_dir is known, take its sibling directory directly (<session>/subagents/workflows/<wf_id> up three levels);
    otherwise build the path from slug+session / glob as a fallback.
    """
    if wf_dir is not None:
        try:
            cand = wf_dir.parent.parent.parent / "workflows" / f"{wf_id}.json"
            if cand.exists():
                return cand
        except Exception:  # noqa: BLE001
            pass
    for slug in slugs:
        proj_dir = base / slug
        if not proj_dir.exists():
            continue
        if session_id:
            cand = proj_dir / session_id / "workflows" / f"{wf_id}.json"
            if cand.exists():
                return cand
        try:
            for cand in proj_dir.glob(f"*/workflows/{wf_id}.json"):
                return cand
        except Exception:  # noqa: BLE001
            continue
    return None


async def tail_live_run(
    repo: StorageRepository,
    event_bus: EventBus,
    run: WorkflowRun,
) -> dict[str, Any]:
    """Perform one round of live collection for a single running / interrupted (within the 24h window) run (§10.2 step
    4).

    1) The terminal-state json has landed → ingest_run_from_file directly and return (D3: terminal state overrides
    live);
    2) journal.jsonl byte-offset incremental tail: consume only up to the last ``\\n`` (guarding against half-lines from
    concurrent
       writes); ``st_size < offset`` ⇒ the file was rewritten ⇒ reset to 0;
    3) Enrich per agent on top of the existing map: skip recomputation when mtime has not advanced; lastCtx tokens (D1),
       first-row timestamp → started_at; journal says started but no agent jsonl → cached
       record 0; overwrite only state/tokens/started_at/last_activity_at, keeping label/phase;
    4) Run-level watermark upsert (journal_offset / live_tokens / last_activity_at; status
       carrying an empty string does not participate in rank); **no change means no upsert** → updated_at freezes, and
       interrupted runs
       age out of the 24h re-check window naturally, returning steady state to zero stats;
    5) interrupted four-condition judgment (§10.5; marks only, never deletes rows; misjudgments self-heal via the
    terminal-state rank 2→3);
    6) Aggregated emits of workflow.agent_updated (one per run per tick) / workflow.run_ingested.
    """
    wf_id = run.wf_id
    base = _claude_projects_dir()
    # Cross-project fix A: the receipt-persisted transcript_dir takes priority for direct addressing — no dependence on
    # project registration,
    # zero globs; invalidation scenarios such as a cleaned-up directory fall back to the slug candidates (including B's
    # legacy-row session reverse lookup).
    wf_dir: Path | None = None
    slugs: list[str] = []
    if run.transcript_dir:
        try:
            _cand = Path(run.transcript_dir)
            wf_dir = _cand if _cand.is_dir() else None
        except Exception:  # noqa: BLE001
            wf_dir = None
    if wf_dir is None:
        slugs = await _candidate_slugs(repo, run)
        wf_dir = _find_wf_dir(base, slugs, run.session_id, wf_id)

    # 1. Terminal state first: wf_<id>.json exists → file values override all live approximations
    # (when transcript_dir is known, derive the sibling path directly; it still hits even if the transcript directory
    # has been cleaned up)
    json_path: Path | None = None
    if run.transcript_dir:
        _direct = run_json_path_from_transcript_dir(run.transcript_dir, wf_id)
        if _direct is not None and _direct.exists():
            json_path = _direct
    if json_path is None:
        json_path = _find_terminal_json(base, slugs, run.session_id, wf_id, wf_dir)
    if json_path is not None:
        res = await ingest_run_from_file(repo, event_bus, json_path)
        return {
            "ok": bool(res.get("ok")),
            "terminal": True,
            "status": str(res.get("status") or ""),
        }

    now = utc_now()
    file_mtimes: list[datetime] = []
    journal_states: dict[str, str] = {}
    prev_offset = run.journal_offset or 0
    new_offset: int | None = None  # None = this tick has no journal information; do not touch the watermark

    # 2. journal incremental tail
    if wf_dir is not None:
        jpath = wf_dir / "journal.jsonl"
        try:
            jst = jpath.stat()
        except OSError:
            jst = None
        if jst is not None:
            file_mtimes.append(from_timestamp(jst.st_mtime))
            offset = prev_offset
            if jst.st_size < offset:
                offset = 0  # the file was rewritten → reset and tail again
            new_offset = offset
            if jst.st_size > offset:
                chunk = b""
                try:
                    with jpath.open("rb") as f:
                        f.seek(offset)
                        chunk = f.read(jst.st_size - offset)
                except OSError:
                    chunk = b""
                nl = chunk.rfind(b"\n")
                if nl >= 0:
                    # consume only up to the last \n; a trailing segment without a newline is not parsed and the offset
                    # does not pass it
                    for raw in chunk[: nl + 1].split(b"\n"):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except Exception:  # noqa: BLE001 — a single corrupted line is skipped without interrupting
                            continue
                        if not isinstance(obj, dict):
                            continue
                        ccid = str(obj.get("agentId") or "").strip()
                        if not ccid:
                            continue
                        if obj.get("type") == "started":
                            journal_states[ccid] = "running"
                        elif obj.get("type") == "result":
                            journal_states[ccid] = "done"
                    new_offset = offset + nl + 1

    # agent-<ccid>.jsonl inventory (when files_known=False there is zero information; never apply the cached-record-0
    # rule)
    agent_files: dict[str, Path] = {}
    files_known = False
    if wf_dir is not None:
        try:
            for p in wf_dir.glob("agent-*.jsonl"):
                ccid = p.name[len("agent-") : -len(".jsonl")]
                if ccid:
                    agent_files[ccid] = p
            files_known = True
        except Exception:  # noqa: BLE001
            agent_files = {}

    # 3. Enrich per agent on top of existing (anti-zeroing: overwrite only fields newly computed this tick)
    try:
        existing = {
            a.cc_agent_id: a
            for a in await repo.list_workflow_agents(wf_id)
            if a.cc_agent_id
        }
    except Exception:  # noqa: BLE001
        existing = {}

    changed_agents = 0
    live_total = 0
    for ccid in set(existing) | set(journal_states) | set(agent_files):
        base_row = existing.get(ccid)
        fpath = agent_files.get(ccid)
        state_new = journal_states.get(ccid) or (base_row.state if base_row else "")
        tokens_new = (base_row.tokens if base_row else 0) or 0
        started_new = base_row.started_at if base_row else None
        act_new = base_row.last_activity_at if base_row else None
        prompt_new = (base_row.prompt_preview if base_row else "") or ""

        if fpath is not None:
            if not state_new:
                state_new = "running"  # having a transcript file means at least started
            try:
                fst = fpath.stat()
            except OSError:
                fst = None
            if fst is not None:
                fmt = from_timestamp(fst.st_mtime)
                file_mtimes.append(fmt)
                prev_act = base_row.last_activity_at if base_row else None
                if prev_act is None or fmt > prev_act:
                    # cheap per-agent watermark: skip token recomputation when mtime has not advanced
                    ctx = _last_assistant_ctx_tokens(fpath)
                    if ctx is not None:
                        tokens_new = ctx
                    if started_new is None:
                        started_new = _first_line_timestamp(fpath)
                    act_new = fmt
            # Running-period semantic label (3edd0dc1): the label only exists in the terminal-state wf json,
            # so during the active period the prompt's first line stands in; an existing label/preview is skipped at
            # zero cost.
            if not prompt_new and not (base_row.label if base_row else ""):
                prompt_new = _first_user_prompt(fpath)
        elif files_known and not tokens_new:
            # journal says started but no agent jsonl on disk → cross-run cache hit; live records 0
            # (D1 approximation); residual error is overridden by the terminal-state file (D3; per the merge rule, int 0
            # is written).
            # Guardrail (V2 review, major): a row with tokens>0 must come from the .output fallback enrich
            # (a true cache hit is always 0) — do not zero it, so a live tick does not erase already-enriched real
            # telemetry.
            tokens_new = 0

        live_total += tokens_new

        # —— Running-period team-member corpse collection (2026-07-10, user-confirmed three loss modes): SubagentStart
        # events occasionally lost → member rows missing; wf_id invisible at Start → rows lingering in the
        # workflow-session-* fallback team, with promote waiting for the next event, so during a long-running agent the
        # team page shows
        # "missing members / only one visible". The tail holds this run's authoritative ccid set and team_id and
        # corrects idempotently every tick;
        # the terminal receipt/reconciliation remains the final truth (here we only add rows / reassign teams, never
        # touching rows of non-fallback teams).
        if run.team_id and ccid:
            try:
                os_row = await repo.find_agent_by_cc_id(ccid)
                if os_row is None:
                    created = await repo.create_agent(
                        team_id=run.team_id,
                        name=f"wf-{ccid[:10]}",
                        # = hook_translator.WORKFLOW_AGENT_TYPE (a literal, to avoid a circular import)
                        role="workflow-subagent",
                        source="hook",
                        session_id=run.session_id or "",
                        cc_tool_use_id=ccid,
                        model="",
                    )
                    await repo.update_agent(
                        created.id,
                        status="busy" if state_new == "running" else "offline",
                        project_id=run.project_id or None,
                        last_active_at=act_new or now,
                    )
                else:
                    if os_row.team_id != run.team_id:
                        cur_team = (
                            await repo.get_team(os_row.team_id)
                            if os_row.team_id
                            else None
                        )
                        if cur_team is not None and str(
                            cur_team.name or ""
                        ).startswith("workflow-session-"):
                            await repo.update_agent(os_row.id, team_id=run.team_id)
                    # Liveness touch (2026-07-10, Wenge confirmed "WF 0 members"): during a workflow agent's
                    # long run nobody updates its heartbeat, and after 15min the reaper wrongly moves it offline — the
                    # journal
                    # not showing a terminal state means it is alive, so touch it back to busy + move last_active_at
                    # forward (after the agent truly dies,
                    # its mtime stalls and the reaper still collects it normally; no zombie immortality).
                    if state_new == "running" and str(
                        getattr(os_row, "status", "")
                    ) not in ("busy",):
                        await repo.update_agent(
                            os_row.id,
                            status="busy",
                            last_active_at=act_new or now,
                        )
            except Exception as exc:  # noqa: BLE001 — a corpse-collection failure does not affect the main projection chain
                logger.debug("live tail: member reap failed cc=%s: %s", ccid, exc)

        agent_changed = (
            base_row is None
            or state_new != (base_row.state or "")
            or tokens_new != (base_row.tokens or 0)
            or prompt_new != ((base_row.prompt_preview if base_row else "") or "")
            or (started_new is not None and base_row.started_at is None)
            or (
                act_new is not None
                and (
                    base_row.last_activity_at is None
                    or act_new > base_row.last_activity_at
                )
            )
        )
        if not agent_changed:
            continue
        wa = WorkflowAgent(
            run_id=wf_id,
            wf_id=wf_id,
            project_id=(base_row.project_id if base_row else "") or run.project_id or "",
            cc_agent_id=ccid,
            os_agent_id=base_row.os_agent_id if base_row else None,
            label=base_row.label if base_row else "",
            phase_index=base_row.phase_index if base_row else 0,
            phase_title=base_row.phase_title if base_row else "",
            model=base_row.model if base_row else "",
            state=state_new,
            tokens=tokens_new,
            tool_calls=(base_row.tool_calls if base_row else 0) or 0,
            duration_ms=base_row.duration_ms if base_row else None,
            last_tool_name=base_row.last_tool_name if base_row else "",
            last_tool_summary=base_row.last_tool_summary if base_row else "",
            prompt_preview=prompt_new,
            result_preview=base_row.result_preview if base_row else "",
            started_at=started_new,
            queued_at=base_row.queued_at if base_row else None,
            last_activity_at=act_new,
        )
        try:
            await repo.upsert_workflow_agent(wa)
            changed_agents += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: agent upsert failed wf=%s cc=%s: %s", wf_id, ccid, exc)

    # 4/5. run-level watermark + interrupted four-condition judgment (§10.5, all four required)
    file_activity = max(file_mtimes) if file_mtimes else None
    offset_changed = new_offset is not None and new_offset != prev_offset
    tokens_changed = files_known and live_total != (run.live_tokens or 0)
    activity_advanced = file_activity is not None and (
        run.last_activity_at is None or file_activity > run.last_activity_at
    )
    run_changed = offset_changed or tokens_changed or activity_advanced

    # Activity-judgment baseline: max(journal + agent jsonl mtime); when all files are missing, fall back to the stored
    # watermark /
    # started_at/created_at (right after launch, before files land, created_at is very fresh → condition 4 is not met).
    judge_activity = (
        file_activity or run.last_activity_at or run.started_at or run.created_at
    )
    stall_seconds = (
        int((now - judge_activity).total_seconds()) if judge_activity else 0
    )
    mark_interrupted = (
        run.status == "running"  # condition 1: the sole entry rank
        # condition 2: the terminal-state json does not exist (when it exists we already
        # returned above)
        and json_path is None
        and judge_activity is not None  # condition 3: the activity baseline is obtainable
        and stall_seconds > WF_STALL_SECONDS  # condition 4: stillness exceeds the threshold
    )

    if run_changed or mark_interrupted:
        try:
            await repo.upsert_workflow_run(
                WorkflowRun(
                    wf_id=wf_id,
                    project_id=run.project_id or "",
                    status="interrupted" if mark_interrupted else "",  # an empty string does not participate in rank
                    source="",  # an empty string keeps the original value after merging
                    journal_offset=new_offset,  # None = no change; explicit 0 = reset (watermark semantics)
                    live_tokens=live_total if files_known else None,
                    last_activity_at=file_activity,  # the repository side monotonically takes max
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: run upsert failed wf=%s: %s", wf_id, exc)
            mark_interrupted = False
            run_changed = False

    # 6. Aggregated events (at most one of each per run per tick; never emitted per agent)
    if changed_agents:
        try:
            await event_bus.emit(
                "workflow.agent_updated",
                f"workflow:{wf_id}",
                {
                    "wf_id": wf_id,
                    "started": sum(1 for s in journal_states.values() if s == "running"),
                    "done": sum(1 for s in journal_states.values() if s == "done"),
                    "changed": changed_agents,
                    "project_id": run.project_id or "",
                },
                entity_id=wf_id,
                entity_type="workflow",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: emit agent_updated failed wf=%s: %s", wf_id, exc)

    if run_changed or mark_interrupted:
        payload: dict[str, Any] = {
            "wf_id": wf_id,
            "status": "interrupted" if mark_interrupted else run.status,
            "journal_offset": new_offset,
            "live_tokens": live_total if files_known else None,
            "last_activity_at": file_activity.isoformat() if file_activity else None,
            "project_id": run.project_id or "",
        }
        if mark_interrupted:
            payload["stall_seconds"] = stall_seconds
        try:
            await event_bus.emit(
                "workflow.run_ingested",
                f"workflow:{wf_id}",
                payload,
                entity_id=wf_id,
                entity_type="workflow",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("live tail: emit run_ingested failed wf=%s: %s", wf_id, exc)

    return {
        "ok": True,
        "terminal": False,
        "status": "interrupted" if mark_interrupted else run.status,
        "marked_interrupted": mark_interrupted,
        "agents_changed": changed_agents,
        "journal_offset": new_offset,
        "live_tokens": live_total if files_known else None,
    }


async def enrich_from_task_output(
    repo: StorageRepository,
    run: WorkflowRun,
) -> dict[str, Any]:
    """``/tmp/claude-<uid>/<slug>/*/tasks/<cc_task_id>.output`` fallback enrichment (§10.6).

    A real .output is a 7-key subset missing runId — **never feed it to ingest_run_from_file** (the runId
    gate would certainly return no_runId); reuse ``_upsert_agents_from_progress`` to enrich only the agents
    (label/phase/tokens — exactly the swimlane data the journal cannot provide) and the run-level live_tokens /
    summary; status / total_tokens / agent_count are never touched (terminal-state columns belong solely to the
    wf-json).

    Shunting (measured pitfall): a symlink = an ordinary Task's transcript jsonl and must be skipped (lexists+lstat+
    S_ISLNK; opening a dangling symlink raises); 0 bytes = task still running, skip; non-JSON = plain-text log, skip.
    /private/tmp is cleared on restart (best-effort within a single boot cycle only), fully try/except-wrapped.
    """
    wf_id = run.wf_id
    task_id = (run.cc_task_id or "").strip()
    if not task_id:
        return {"ok": False, "reason": "no_cc_task_id", "wf_id": wf_id}

    base = _claude_tmp_dir()
    slugs = await _candidate_slugs(repo, run)
    data: dict[str, Any] | None = None
    for slug in slugs:
        proj_dir = base / slug
        try:
            candidates = sorted(proj_dir.glob(f"*/tasks/{task_id}.output"))
        except Exception:  # noqa: BLE001
            candidates = []
        for cand in candidates:
            sp = str(cand)
            try:
                if not os.path.lexists(sp):
                    continue
                lst = os.lstat(sp)
                if stat_module.S_ISLNK(lst.st_mode):
                    continue  # a symlink = an ordinary Task transcript; mis-parsing it would pollute the data
                if lst.st_size == 0:
                    continue  # the task is running; the snapshot has not landed
                parsed = json.loads(Path(sp).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — a plain-text sub-task log / half-written JSON; skip
                continue
            if isinstance(parsed, dict) and parsed.get("workflowProgress"):
                data = parsed
                break
        if data is not None:
            break
    if data is None:
        return {"ok": False, "reason": "no_output", "wf_id": wf_id}

    n = await _upsert_agents_from_progress(
        repo, wf_id, run.project_id or "", data.get("workflowProgress")
    )

    total = _to_int(data.get("totalTokens"))
    summary = str(data.get("summary") or "")
    if total > 0 or summary:
        try:
            await repo.upsert_workflow_run(
                WorkflowRun(
                    wf_id=wf_id,
                    project_id=run.project_id or "",
                    status="",  # do not touch status
                    source="",
                    live_tokens=total if total > 0 else None,
                    summary=summary,  # merge rule: overwrite only when non-empty
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("output enrich: run upsert failed wf=%s: %s", wf_id, exc)

    return {"ok": True, "wf_id": wf_id, "agents": n}
