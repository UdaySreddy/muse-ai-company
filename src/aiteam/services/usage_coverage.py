"""The coverage denominator definition and the unattributed classification — token usage attribution v1 §4.1 / §3.4.

**Why a separate module**: these two things must be directly referenceable by the storage layer, while the same-domain
``services/token_attribution.py`` imports ``api.session_probe`` to share the synthetic-row constants
(deliberate in stage 0, see the comments there), and following the api layer back imports ``storage.repository``,
forming a cycle. The denominator definition should not become a lazy import just to live in the file that "looks more
right" — within this set of
things, it is the segment that most needs to be visible at a glance.

This module depends only on ``pathlib`` and ``aiteam.types``, and can be safely imported at any time.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aiteam.types import UnattributedReason

# ---------------------------------------------------------------------------
# Denominator (§4.1)
# ---------------------------------------------------------------------------
# The denominator is the only place that can be quietly faked (R2): move the no-data rows out of the denominator and
# coverage is permanently 100%,
# the numbers look better and it all sounds entirely plausible. So it is defined only once, here, and referenced by the
# repository's aggregation methods,
# with unit tests pinning each value.
#
# Criterion: ``agents.role`` not equal to this literal counts as one dispatch. Measured across the whole DB: 2,451 rows,
# of which
# 2,343 rows (96%) are the workflow-derived ``workflow-subagent``.
#
# **Deliberately not adopted**: ``api/agent_reuse.py``'s ``_EXCLUDED_ROLES`` (which also
# excludes ``workflow-subagent``): that is the "who may be reused" governance caliber, excluding rows not managed
# by people. In the attribution caliber, a workflow dispatch **is a dispatch** — the tokens it burns are no fewer than
# a direct dispatch's. Moving the group that makes up 96% out of the denominator makes coverage jump from 78% to a very
# pretty number at once — and that is exactly
# the shape R2 names as the thing to guard against.
LEADER_ROLE = "leader"


# ---------------------------------------------------------------------------
# Unattributed classification (§3.4)
# ---------------------------------------------------------------------------


def transcript_exists(path: str) -> bool:
    """Whether the transcript file is still on disk today.

    It is a separate function so tests can replace it: the unattributed classification must distinguish "recoverable"
    from "unrecoverable", and this
    distinction depends entirely on the file system's instantaneous state — not testing this branch equals not testing
    this table.
    """
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def classify_unattributed(
    transcript_path: str | None,
    *,
    file_probe: Callable[[str], bool] = transcript_exists,
) -> str:
    """Which of §3.4's four unattributed classes a dispatch whose usage was not measured belongs to.

    It answers only "why was it not measured", not "should it be measured" — the caller has already confirmed this row's
    ``tokens_measured_at`` is NULL before asking here.

    The three return values are handled completely differently, which is exactly why this table exists (§3.4: turning
    "coverage 78%" into
    one actionable sentence). ``NOT_YET_MEASURED`` is made up by one re-collection run; the other two cannot be made up,
    and ``TRANSCRIPT_GONE`` only grows over time — the re-collection window is closing (R6). Merging them
    into a single "unattributed" number leaves the reader unable to judge whether to act now.
    """
    if not transcript_path:
        return UnattributedReason.NO_TRANSCRIPT_PATH.value
    if not file_probe(transcript_path):
        return UnattributedReason.TRANSCRIPT_GONE.value
    return UnattributedReason.NOT_YET_MEASURED.value
