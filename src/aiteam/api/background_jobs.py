"""CC background job observation — reads ``~/.claude/jobs/<short>/state.json``, with zero registration dependency.

CC's `--bg` background sessions (daemon backend) never register into the OS through any hook: they have their own
sessionId and their own cwd, and can keep running after the user closes the foreground window. For the OS, this is an
entire
class of invisible in-flight work — the project summary says "nobody is working", while a daemon is actually running.

On-disk shape (measured on this machine)::

    {"state":"done","tempo":"idle","template":"bg","backend":"daemon",
     "sessionId":"44fbf725-…","resumeSessionId":"…","daemonShort":"44fbf725",
     "cwd":"/…","createdAt":"2026-07-15T04:09:27.012Z",
     "updatedAt":"…","firstTerminalAt":"2026-07-15T11:41:17.040Z",
     "respawnFlags":["--permission-mode","auto","--model","fable"],"intent":""}

★ The "still in flight" criterion is whether ``firstTerminalAt`` is absent; it **does not guess state strings**: on this
machine,
the four records' states are done / stopped, yet nowhere in the CC binary is there a job-state enumeration
listing these two values together — writing a whitelist from known values would, upon meeting an unseen terminal state,
treat a dead job
as if it were in flight. Absent means in flight and present means terminal; this criterion does not depend on the set of
values.

Read-only, writes nothing, caches nothing; any exception degrades to an empty list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiteam.clock import parse_utc


def _jobs_dir() -> Path:
    return Path.home() / ".claude" / "jobs"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return parse_utc(value)


@dataclass(frozen=True)
class BackgroundJob:
    job_id: str
    session_id: str
    state: str
    tempo: str
    backend: str
    cwd: str
    intent: str
    model: str
    created_at: datetime | None
    updated_at: datetime | None
    terminal_at: datetime | None

    @property
    def in_flight(self) -> bool:
        return self.terminal_at is None


def _model_from_flags(flags: object) -> str:
    """respawnFlags is a flat array like ['--model', 'fable', …]."""
    if not isinstance(flags, list):
        return ""
    for i, flag in enumerate(flags):
        if flag == "--model" and i + 1 < len(flags):
            return str(flags[i + 1])
    return ""


def read_jobs() -> list[BackgroundJob]:
    """Read all background job records, skipping corrupt files. In descending update-time order."""
    jobs: list[BackgroundJob] = []
    try:
        entries = sorted(_jobs_dir().glob("*/state.json"))
    except OSError:
        return jobs
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        jobs.append(
            BackgroundJob(
                job_id=str(data.get("daemonShort") or path.parent.name),
                session_id=str(data.get("sessionId") or ""),
                state=str(data.get("state") or ""),
                tempo=str(data.get("tempo") or ""),
                backend=str(data.get("backend") or ""),
                cwd=str(data.get("cwd") or ""),
                intent=str(data.get("intent") or ""),
                model=_model_from_flags(data.get("respawnFlags")),
                created_at=_parse_iso(data.get("createdAt")),
                updated_at=_parse_iso(data.get("updatedAt")),
                terminal_at=_parse_iso(data.get("firstTerminalAt")),
            )
        )
    jobs.sort(key=lambda j: j.updated_at or datetime.min, reverse=True)
    return jobs


def in_flight_jobs(root_path: str = "") -> list[BackgroundJob]:
    """Background jobs still running; when a project root is given, only those under that project are counted."""
    jobs = [j for j in read_jobs() if j.in_flight]
    if not root_path:
        return jobs
    root = root_path.rstrip("/")
    return [j for j in jobs if j.cwd == root or j.cwd.startswith(root + "/")]


def as_dicts(jobs: list[BackgroundJob]) -> list[dict]:
    return [
        {
            "job_id": j.job_id,
            "session_id": j.session_id,
            "state": j.state,
            "tempo": j.tempo,
            "backend": j.backend,
            "cwd": j.cwd,
            "intent": j.intent,
            "model": j.model,
            "in_flight": j.in_flight,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "updated_at": j.updated_at.isoformat() if j.updated_at else None,
        }
        for j in jobs
    ]
