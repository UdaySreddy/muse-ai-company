"""Session probing — read directly from the file source of truth, with zero registration dependency.

User ruling (2026-07-07): the Leader is "the CC session started under this project directory",
and its model/active status should be auto-detected by the backend, rather than the leader registering into the DB via
the hook chain before being displayed.
The data has been in the filesystem all along:

    ~/.claude/projects/<slug>/<session-uuid>.jsonl   ← main session transcript

- File mtime = last active time (CC updates it as each message is written to disk)
- the message.model of the last assistant line at the tail = the current real model
  (/model switches at any time are tracked; compact synthetic lines "<synthetic>" are excluded)
- Sub-agent / workflow journals live inside the <slug>/<session-uuid>/ subdirectory,
  so a top-level glob("*.jsonl") naturally hits only main sessions, with no further distinction needed.

The hook registration chain continues to handle the activity stream and events, but the display layer's Leader identity
no longer depends on it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from aiteam.api import agent_context
from aiteam.clock import from_timestamp, utc_now

# Marker for the synthetic assistant lines CC writes in compact and similar scenarios; not a real model
SYNTHETIC_MODEL = "<synthetic>"

# Consistent with the observation layer's caliber: any write to disk within 15 minutes counts as "in progress"
LIVE_WINDOW = timedelta(minutes=15)

_TAIL_BYTES = 200_000


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def project_slug(root_path: str) -> str:
    """Character-for-character identical to CC's directory naming (non-ASCII characters, including Chinese, are all
    replaced with '-')."""
    import re

    return re.sub(r"[^a-zA-Z0-9]", "-", root_path)


def session_last_active(root_path: str, session_id: str) -> datetime | None:
    """Return the transcript file mtime for one specific session, or None if absent.

    File truth source: the main-session transcript at
    ``~/.claude/projects/<slug>/<session_id>.jsonl`` has its mtime bumped on every
    message CC writes. This exposes a per-session liveness read so the fleet layer
    can judge session death by file mtime (more authoritative than process liveness,
    since ``claude --resume`` spins up a fresh process anyway). Returns None on any
    failure so callers never break on a probe.
    """
    try:
        if not root_path or not session_id:
            return None
        p = _claude_projects_dir() / project_slug(root_path) / f"{session_id}.jsonl"
        if not p.is_file():
            return None
        return from_timestamp(p.stat().st_mtime)
    except Exception:  # noqa: BLE001 — probe failure must not affect callers
        return None


def read_session_model(transcript_path: str) -> str:
    """Tail-read a main session transcript and take the model of the last real assistant message.

    Scan backwards covering the last 200KB; skip compact synthetic lines (model="<synthetic>").
    On failure or absence, always return an empty string; never raise.
    """
    try:
        if not transcript_path:
            return ""
        p = Path(transcript_path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > _TAIL_BYTES:
                f.seek(size - _TAIL_BYTES)
            data = f.read().decode("utf-8", errors="replace")
        model = ""
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001 — the first line truncated by seek, etc.
                continue
            if d.get("type") == "assistant":
                m = (d.get("message") or {}).get("model")
                if m and str(m) != SYNTHETIC_MODEL:
                    model = str(m)
        return model
    except Exception:  # noqa: BLE001
        return ""


# Preset CEO English name list (user ruling 2026-07-10: when multiple sessions of the same project run in parallel, each
# session
# is displayed as CEO-<English name> rather than a single "Leader"; names are drawn from the list without repetition).
CEO_NAMES = [
    "Atlas", "Nova", "Orion", "Vega", "Lyra", "Miles", "Iris", "Felix",
    "Luna", "Hugo", "Cleo", "Jasper", "Wren", "Silas", "Freya", "Kai",
    "Elara", "Rowan", "Thea", "Ezra", "Selene", "Otto", "Nadia", "Remy",
]


def _assign_ceo_names(session_ids: list[str]) -> dict[str, str]:
    """Deterministically assign non-repeating CEO names: md5(session_id) mapped onto the list + open addressing to avoid
    collisions.

    Processed in session_id lexicographic order, so the assignment result for a batch of sessions is stable (refreshing
    does not change names),
    with no persistent state needed. When the list is exhausted (>24 parallel sessions), it degrades to the session
    prefix.
    """
    import hashlib

    taken: set[str] = set()
    result: dict[str, str] = {}
    n = len(CEO_NAMES)
    for sid in sorted(session_ids):
        h = int(hashlib.md5(sid.encode()).hexdigest(), 16)
        name = ""
        for i in range(n):
            cand = CEO_NAMES[(h + i) % n]
            if cand not in taken:
                name = cand
                break
        if not name:
            name = sid[:6]
        taken.add(name)
        result[sid] = name
    return result


def detect_live_sessions(root_path: str) -> list[dict]:
    """Probe all active CC main sessions under a project directory (within a 15min window), in descending mtime order.

    With multiple parallel sessions there is one entry per session; when all are silent, return the latest one
    (live=False),
    preserving the "the project page can always see the most recent session" semantics. Each entry carries a
    deterministic CEO English name.
    Pure filesystem reads; no DB queries and no dependence on hook registration. Returns an empty list when nothing is
    found.

    It also attaches the ``cc_*`` fields read from the CC session registry (``~/.claude/sessions/<pid>.json``):
    the real pid, CC's own idle/busy status, whether the process is still alive, and the session name and kind.
    **``live``
    is still decided by mtime alone** — the registry path is a parallel observation (C13); its fields are surfaced first
    for people to
    cross-check, while the criterion itself stays unchanged. When a session is not registered (old session / test /
    workflow injection), these fields are left empty.
    """
    from aiteam.api import session_registry

    try:
        if not root_path:
            return []
        try:
            registry = {r.session_id: r for r in session_registry.read_sessions() if r.session_id}
        except Exception:  # noqa: BLE001 — a registry read failure does not affect the main probe
            registry = {}
        pdir = _claude_projects_dir() / project_slug(root_path)
        if not pdir.is_dir():
            return []
        entries: list[tuple[Path, float]] = []
        for f in pdir.glob("*.jsonl"):
            try:
                entries.append((f, f.stat().st_mtime))
            except OSError:
                continue
        if not entries:
            return []
        entries.sort(key=lambda e: e[1], reverse=True)
        now = utc_now()
        chosen = [
            e for e in entries
            if (now - from_timestamp(e[1])) < LIVE_WINDOW
        ] or entries[:1]
        names = _assign_ceo_names([f.stem for f, _ in chosen])
        result = []
        for f, mt in chosen:
            last_active = from_timestamp(mt)
            # Main session context watermark (fleet layer P2 observation, see docs/fleet-layer-design.md §6.2):
            # Reuses the read_ctx_tokens extracted in agent-reuse batch 1B, applying the same caliber to main session
            # transcripts (previously it only covered sub-agents). Read failures are always left empty and do not affect
            # session probing itself.
            ctx_tokens = agent_context.read_ctx_tokens(f)
            ctx_window: int | None = None
            ctx_pct: float | None = None
            if ctx_tokens is not None:
                ctx_window, ctx_pct = agent_context.compute_window_pct(ctx_tokens)
            reg = registry.get(f.stem)
            result.append({
                "session_id": f.stem,
                "name": names[f.stem],
                "model": read_session_model(str(f)),
                "last_active_at": last_active.isoformat(),
                "live": (now - last_active) < LIVE_WINDOW,
                "ctx_tokens": ctx_tokens,
                "ctx_window": ctx_window,
                "ctx_pct": ctx_pct,
                "cc_pid": reg.pid if reg else None,
                "cc_status": reg.status if reg else "",
                "cc_name": reg.name if reg else "",
                "cc_kind": reg.kind if reg else "",
                "cc_process_alive": (
                    session_registry.process_alive(reg.pid) if reg else None
                ),
            })
        return result
    except Exception:  # noqa: BLE001 — a probe failure does not affect the caller
        return []


def detect_live_session(root_path: str) -> dict | None:
    """Single-session compatibility entry point: the first entry of the plural version (the latest session)."""
    sessions = detect_live_sessions(root_path)
    return sessions[0] if sessions else None
