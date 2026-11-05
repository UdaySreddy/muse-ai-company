"""Transcript path deriver — reads the first four segments of the attribution chain out of a file path.

CC places every transcript in a **structured** location, and the path itself encodes the attribution chain:

    ~/.claude/projects/<slug>/<session_id>.jsonl                              main session
    ~/.claude/projects/<slug>/<session_id>/subagents/agent-<cc_id>.jsonl      directly dispatched sub-agent
    ~/.claude/projects/<slug>/<session_id>/subagents/workflows/wf_<id>/agent-<cc_id>.jsonl

That is, the four segments ``project_slug / session_id / wf_id / cc_agent_id`` **need no new
collection** — parsing the path yields them. This is from the same origin as the trick
``hook_translator._extract_workflow_run_id`` already uses
— that place took only the wf_id; this module uses the same information to the full.

Two boundaries that must be written in stone:

* **The slug serves only for cross-checking, never for judging project ownership.** The slug is the product of
``re.sub(r'[^a-zA-Z0-9]',
  '-', root_path)`` and is **lossy**: two different Chinese directory names under ``~/Desktop/``
  would collapse into the same ``-Users-x-Desktop---``. Measured in the production DB, 2 of 6 slugs are exactly this
  kind of non-
  ASCII collapsed shape (525 rows + 191 rows). Ownership uniformly follows ``agents.project_id``; the slug
  is used only to mark, in the backfill report, the rows whose "path does not match the registered project", for human
  review.
* **When it cannot be parsed, return None.** No guessing, no fallback, no default filling — no-data and zero must be
distinguishable
  (Council discipline 1).

Pure functions, no IO: this module looks only at strings and never touches the disk. Whether a file still exists is
judged by the caller as needed,
so the parser can be pinned by unit tests and can also run on machines without those files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The session directory name is CC's session uuid. It is written as a loose 36-character hex/hyphen shape rather than
# a strict UUID v4: recent CC session ids already include v7-style ones like ``019f8b2f-1617-71d1-...``
# (measured in the production DB); gating on the v4 version bits would condemn them.
_SESSION_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# The complete shape for sub-agents (including workflow fan-out). The wf segment is optional.
_SUBAGENT_RE = re.compile(
    r"/projects/(?P<slug>[^/]+)"
    rf"/(?P<session_id>{_SESSION_RE})"
    r"/subagents/"
    r"(?:workflows/(?P<wf_id>wf_[^/]+)/)?"
    r"agent-(?P<cc_agent_id>[^/]+)\.jsonl$"
)

# Main session: <slug>/<session_id>.jsonl, hung directly at the top level, with no subagents segment.
_MAIN_RE = re.compile(
    r"/projects/(?P<slug>[^/]+)"
    rf"/(?P<session_id>{_SESSION_RE})\.jsonl$"
)


@dataclass(frozen=True)
class TranscriptRef:
    """The path-derivation result of one transcript.

    ``kind`` distinguishes main sessions from sub-agents — the two's usage magnitudes differ by two to three orders of
    magnitude (a single main session measured
    850M vs a sub-agent median in the millions); mixed into one leaderboard, sub-agents would be completely drowned, so
    the caliber
    must be able to separate them (design §3.3).
    """

    kind: str  # "main" | "subagent"
    project_slug: str
    session_id: str
    wf_id: str | None  # only workflow fan-out sub-agents have a value
    cc_agent_id: str | None  # only sub-agents have a value


def parse_transcript_path(path: str | None) -> TranscriptRef | None:
    """Derive (slug, session_id, wf_id, cc_agent_id) from a transcript path.

    Recognizes three shapes (see the module docstring). Returns None when none match — including empty strings,
    paths not containing ``/projects/``, and paths whose session segment is not uuid-shaped.

    Windows backslashes are first normalized to forward slashes before matching (the same handling as
    ``_extract_workflow_run_id``).
    The path **need not** be under ``~/.claude/projects``: it only needs the relative shape containing
    ``/projects/<slug>/...``,
    so tests and replica directories can both be parsed.
    """
    if not path:
        return None
    norm = str(path).replace("\\", "/")

    m = _SUBAGENT_RE.search(norm)
    if m:
        return TranscriptRef(
            kind="subagent",
            project_slug=m.group("slug"),
            session_id=m.group("session_id"),
            wf_id=m.group("wf_id"),
            cc_agent_id=m.group("cc_agent_id"),
        )

    m = _MAIN_RE.search(norm)
    if m:
        return TranscriptRef(
            kind="main",
            project_slug=m.group("slug"),
            session_id=m.group("session_id"),
            wf_id=None,
            cc_agent_id=None,
        )

    return None


def derive_session_id(path: str | None) -> str | None:
    """A narrow entry point for session_id only (used when SubagentStop writes this column on the side)."""
    ref = parse_transcript_path(path)
    return ref.session_id if ref else None


def slug_matches_root(slug: str, root_path: str | None) -> bool:
    """Cross-check: whether the slug in the path matches the registered project's root_path.

    **Answers only "does it match", not "whose is it".** The slug is a lossy mapping: multiple different directories can
    collapse into the same slug, so True does not constitute ownership evidence; False is the informative one — it shows
    this row's
    ``project_id`` disagrees with its transcript's actual landing spot and is worth a human look (in the backfill report
    it is
    marked with ``slug_mismatch``, but no ownership column is rewritten because of it).

    The slug algorithm is **not rewritten here**: the function lazily imports ``session_probe.project_slug``,
    because when CC's naming rules change it must change with them, and two separately written copies inevitably drift
    (§1.3 already suffered the same
    on synthetic-row filtering). The lazy import exists so the services layer does not drag the whole api layer in at
    import time.

    """
    if not slug or not root_path:
        return False
    from aiteam.api.session_probe import project_slug

    return slug == project_slug(root_path)
