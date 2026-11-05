"""Cross-domain reference extractor — knowledge layer P1a (docs/knowledge-layer-design.md).

Extract OS-native ID references from memo/report text using pure regex and zero LLM (GBrain's "convention is the edge"
route, with entities replaced by OS IDs — language-agnostic, and Chinese content has zero impact). It is attached to the
API route write
path (the convergence point of the task_memo_add / report_save dual entry points); failures are silent and never block
the main write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Hit-point context window (evidence snapshot ±120 characters; link_type inference ±100 characters —
# widened from 60 when the codebase went English-only: an English cue phrase spans roughly twice the
# characters of the equivalent Chinese, so the original radius silently cut cues off mid-word)
_CONTEXT_CHARS = 120
_TYPE_WINDOW = 100

# wf_id: wf_ + 8 hex, optionally with a -xxx suffix (the observation layer's run numbering format)
_WF_RE = re.compile(r"\bwf_[0-9a-f]{8}(?:-[0-9a-f]{2,4})?\b")
# Standard UUID (task/report id)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
# CC memory link [[slug]]
_MEMORY_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]{2,60})\]\]")
# Short commit hash: 7-10 hex — high false-positive risk; only counts when a commit context word is present
_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,10}\b")
_COMMIT_CUES = re.compile(r"commit|\u63d0\u4ea4|\u4fee\u590d|\u4fee|fix|feat|refactor|release|\u7248\u672c", re.I)
# fixes semantic cues (when one appears within a ±60-character window, the edge type is upgraded to fixes)
_FIX_CUES = re.compile(
    r"\u4fee\u590d|\u4fee\u597d|\u6839\u6cbb|\u6839\u56e0|"
    r"fix(?:ed|es)?\b|\u5df2\u4fee|\u4fee\u6389",
    re.I,
)


@dataclass
class ExtractedRef:
    to_kind: str  # run / task / commit / memory
    to_id: str
    link_type: str  # references / fixes
    context: str  # Hit-point evidence snapshot


def _window(text: str, start: int, end: int, radius: int) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def extract_refs(text: str) -> list[ExtractedRef]:
    """Extract all cross-domain references from the text (deduplicated by (kind, id), keeping the context of the
    first hit)."""
    if not text:
        return []
    refs: dict[tuple[str, str], ExtractedRef] = {}

    def _add(kind: str, ref_id: str, m_start: int, m_end: int) -> None:
        key = (kind, ref_id)
        if key in refs:
            return
        type_win = _window(text, m_start, m_end, _TYPE_WINDOW)
        link_type = "fixes" if _FIX_CUES.search(type_win) else "references"
        refs[key] = ExtractedRef(
            to_kind=kind,
            to_id=ref_id,
            link_type=link_type,
            context=_window(text, m_start, m_end, _CONTEXT_CHARS).strip(),
        )

    for m in _WF_RE.finditer(text):
        _add("run", m.group(0), m.start(), m.end())
    for m in _UUID_RE.finditer(text):
        _add("task", m.group(0), m.start(), m.end())
    for m in _MEMORY_RE.finditer(text):
        _add("memory", m.group(1), m.start(), m.end())
    for m in _COMMIT_RE.finditer(text):
        # Skip fragments already hit by wf_/uuid; skip when there is no commit context word (prevents bare hex from
        # being falsely extracted)
        frag = m.group(0)
        if any(frag in k[1] for k in refs):
            continue
        cue_win = _window(text, m.start(), m.end(), _TYPE_WINDOW)
        if not _COMMIT_CUES.search(cue_win):
            continue
        _add("commit", frag, m.start(), m.end())

    return list(refs.values())
