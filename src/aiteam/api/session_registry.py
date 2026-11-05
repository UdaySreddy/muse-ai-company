"""CC session registry bridge — reads ``~/.claude/sessions/<pid>.json``, with zero registration dependency.

In CC v2.1.219, each process maintains its own session registration:

    ~/.claude/sessions/<pid>.json
    {"pid":32220,"sessionId":"80d0cc5e-…","cwd":"/…/AI team OS",
     "startedAt":1785116991072,"procStart":"Mon Jul 27 01:49:50 2026",
     "version":"2.1.219","kind":"interactive","entrypoint":"cli",
     "name":"ai-team-os-18","status":"busy","updatedAt":…,"statusUpdatedAt":…}

It is more authoritative than both of the OS's existing session signals:

- Newer than the ``leadSessionId`` in ``~/.claude/teams/<team>/config.json``. A team directory,
  once stamped at team-creation time, is **never renamed afterwards**, while when the same process switches sessions
  (reopened after ending, resume),
  only this file updates its ``sessionId`` accordingly. Measured on pid 32220: the team directory still says
  ``session-0def8f84``, while the registry already has ``80d0cc5e``.
- More direct than transcript mtime. mtime can only infer "whether anything was written to disk recently", while this
  file carries
  ``status`` (idle/busy) and the real ``pid``, so you can ask directly whether the process is dead or alive.

This module is read-only, writes nothing, and caches nothing; any exception degrades to "unknown" (None / False),
and a probe failure never affects the caller in return.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiteam.clock import from_timestamp, parse_utc

# When CC writes sessions/<pid>.json, it has two formats for the time fields (two code paths coexist in the binary):
# epoch milliseconds (updatedAt: Date.now()) and ISO strings (updatedAt: new Date().toISOString()).
# Both must be recognized; leave the value empty when it is unrecognizable.
_EPOCH_MS_MIN = 10**11


def _sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def _parse_ts(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)) and value > _EPOCH_MS_MIN:
            return from_timestamp(value / 1000)
        if isinstance(value, str) and value:
            return parse_utc(value)
    except (ValueError, OSError, OverflowError):
        return None
    return None


@dataclass(frozen=True)
class SessionRecord:
    """One CC session registration. ``procStart`` is deliberately not parsed — it is a UTC-rendered string
    that differs from ``ps -o lstart=``'s local time by one timezone, so comparing them directly would inevitably
    produce false mismatches
    (measured on the same process: registry "Mon Jul 27 01:49:50", ps "Mon Jul 27 09:49:50").
    For the start time, use ``started_at`` (parsed from epoch milliseconds, which lines up with ps)."""

    pid: int
    session_id: str
    cwd: str
    status: str
    kind: str
    name: str
    version: str
    entrypoint: str
    job_id: str
    started_at: datetime | None
    updated_at: datetime | None
    status_updated_at: datetime | None

    @property
    def is_background(self) -> bool:
        return self.kind == "bg"


def process_alive(pid: int) -> bool:
    """Whether the process is still around.

    ⚠️ The criterion is ``os.kill(pid, 0)``, which **does not guard against pid reuse**: after the process exits, when
    the pid is reassigned by the system
    to another program, this will falsely judge it to be alive. The session registry currently serves only as a parallel
    observation (it takes part in no
    offline decision), and this precision suffices; before switching it into the main criterion, process identity
    verification must be added first
    (see the dual-track switching preconditions in the docs).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but does not belong to this user
    except OSError:
        return False
    return True


def read_sessions() -> list[SessionRecord]:
    """Read all session registrations, skipping corrupt files. Returned in ascending pid order; results are stable
    and comparable."""
    records: list[SessionRecord] = []
    try:
        entries = sorted(_sessions_dir().glob("*.json"))
    except OSError:
        return records
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            pid = int(data.get("pid") or path.stem)
        except (TypeError, ValueError):
            continue
        records.append(
            SessionRecord(
                pid=pid,
                session_id=str(data.get("sessionId") or ""),
                cwd=str(data.get("cwd") or ""),
                status=str(data.get("status") or ""),
                kind=str(data.get("kind") or ""),
                name=str(data.get("name") or ""),
                version=str(data.get("version") or ""),
                entrypoint=str(data.get("entrypoint") or ""),
                job_id=str(data.get("jobId") or ""),
                started_at=_parse_ts(data.get("startedAt")),
                updated_at=_parse_ts(data.get("updatedAt")),
                status_updated_at=_parse_ts(data.get("statusUpdatedAt")),
            )
        )
    return records


def find_session(session_id: str) -> SessionRecord | None:
    """Find a registration by CC session id. When the same id appears under multiple pids, prefer the one that is
    alive."""
    if not session_id:
        return None
    matches = [r for r in read_sessions() if r.session_id == session_id]
    if not matches:
        return None
    return next((r for r in matches if process_alive(r.pid)), matches[0])


def sessions_for_cwd(root_path: str) -> list[SessionRecord]:
    """Session registrations under a project root directory (including subdirectory sessions), in descending
    start-time order."""
    if not root_path:
        return []
    root = root_path.rstrip("/")
    out = [
        r
        for r in read_sessions()
        if r.cwd == root or r.cwd.startswith(root + "/")
    ]
    out.sort(key=lambda r: r.started_at or datetime.min, reverse=True)
    return out


def pid_for_session(session_id: str) -> int | None:
    """Which CC process a session belongs to; None when the registry has no such entry.

    Only this one **exact key match** in the registry is recognized (``sessionId`` equality); deliberately no
    approximate kinship matching by cwd / start-time window is performed: measured locally, pids 32147 and 32220 started
    under the same cwd
    1.0 seconds apart, and the team directory's createdAt falls exactly between the two — approximate matching on real
    data is simply ambiguous (the user ruling of 2026-07-27 rejected that route).

    Note that it can only answer for the **current** session: CC keeps one registration per process, and when the
    process switches sessions the file is rewritten in place
    (``sessionId``), leaving no trace of the old value. Historical sessions are therefore never found here, and callers
    must treat
    None as "unknown" rather than "belongs to no process".
    """
    record = find_session(session_id)
    return record.pid if record is not None else None


def session_alive(session_id: str) -> bool | None:
    """Whether a session is alive: True/False = the registry decides; None = the registry has no such entry at all.

    None and False must be kept separate — "not registered" usually means an old session or a non-CC source (tests,
    workflow injection), not "already dead"; callers should not treat it as evidence of death.
    """
    record = find_session(session_id)
    if record is None:
        return None
    return process_alive(record.pid)
