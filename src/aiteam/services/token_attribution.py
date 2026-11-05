"""Sub-agent token attribution — collects billing-caliber usage from transcripts.

The agents table previously had only the "context watermark" caliber (ctx_tokens/ctx_pct) and **no billing-caliber
collection at all**; on the workflow side, workflow_agents.tokens was also empty over large areas (measured: all 170
rows with model='opus' have token=NULL, and it kept occurring until recently — not an early legacy artifact).

The root cause is not broken collection but **the data source holding only request specs**: in the same workflow JSON,
82 agents
wrote only "which model is wanted" (model was the alias ``opus`` verbatim, tokens null), and only 24 brought back
telemetry, which the OS persisted as-is.

But these agents' **transcripts exist in full**, and a transcript's ``message.model``
**is always the full model name** (measured ``claude-opus-5`` / ``claude-opus-4-8``), never an alias.
So both the real model and the tokens can be re-collected losslessly from the transcript — exactly what "the model field
is backfilled by observation"
should look like. The alias mapping table serves only as a fallback for rows whose transcripts are lost, **parsed only
on the read side and never
written back into the model field** (the 2026-07-07 iron rule: when unknown, leave it empty; never hard-code a model).

The accumulation algorithm is the one place in this module that is easy to get wrong; see :func:`parse_transcript_usage`
for its dedicated explanation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# The **sole** definition of the synthetic-row marker lives in session_probe — the earliest place that handled this
# correctly
# (read_session_model has always explicitly skipped them). This place deliberately imports it instead of copying the
# constant again:
# copying one literal into two places would, when one of them forgets to skip, grow two kinds of "model identification"
# behavior — and that
# is exactly the defect §1.3 named in this function.
from aiteam.api.session_probe import SYNTHETIC_MODEL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alias mapping: an append-only ledger, read-side fallback only
# ---------------------------------------------------------------------------
# Aliases (opus / sonnet / haiku) are **floating**: the same "opus" points at different models in different periods.
# Therefore the mapping must carry effective intervals and evidence sources, and forever only append, never modify
# historical entries — otherwise it would be
# impossible to reconstruct afterwards "what that record actually ran at the time". ``effective_until=None`` means still
# in effect.
#
# Usage boundary: only when a transcript is lost and cannot be verified, used as a fallback parse for
# **display/statistics**.
# Under no circumstances is the parse result written back to agents.model / workflow_agents.model.
MODEL_ALIAS_LEDGER: list[dict[str, Any]] = [
    {
        "alias": "opus",
        "resolved": "claude-opus-4-8",
        "effective_from": "2026-07-01",
        "effective_until": "2026-07-27",
        "evidence": "Measured in workflow transcripts: contemporary agent-*.jsonl message.model is always "
                    "claude-opus-4-8",
    },
    {
        "alias": "opus",
        "resolved": "claude-opus-5",
        "effective_from": "2026-07-28",
        "effective_until": None,
        "evidence": "Measured in this machine's subagent transcripts: message.model = claude-opus-5",
    },
]


def resolve_model_alias(alias: str, at: str) -> dict[str, Any] | None:
    """Resolve a floating alias to the model it named on date ``at`` (YYYY-MM-DD).

    Returns the ledger entry (so callers keep the evidence trail), or None when
    the alias is unknown or the date predates every recorded window — guessing
    outside a recorded window is exactly how a wrong model gets baked in.
    """
    for entry in MODEL_ALIAS_LEDGER:
        if entry["alias"] != alias:
            continue
        if at < entry["effective_from"]:
            continue
        until = entry["effective_until"]
        if until is not None and at > until:
            continue
        return entry
    return None


# ---------------------------------------------------------------------------
# transcript parsing
# ---------------------------------------------------------------------------

_USAGE_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
}


def parse_transcript_usage(path: str | Path) -> dict[str, Any] | None:
    """Sum a subagent transcript's billed token usage.

    **Algorithm essentials (getting this wrong badly inflates the numbers)**: one API call produces **multiple**
    assistant rows
    (one per content block); measured, 79 rows correspond to only 17 unique ``requestId``s. Within one
    ``requestId``, input/cache stay constant while ``output_tokens`` **grows** with streaming
    (3 → 3 → … → 583). So one must **group by requestId, take each group's last snapshot, then accumulate across
    groups**.
    Naively adding row by row measurably lifts input from 12,185 to 72,556 and cache_read from 1,289,742
    to 5,105,773.

    Returns None when the file is absent — "no data" and "used 0 tokens" are two different things,
    and the former must never be written as the latter (Council discipline: no-data ≠ zero).
    """
    p = Path(path)
    if not p.exists():
        return None

    # requestId -> that call's last usage snapshot
    snapshots: dict[str, dict[str, int]] = {}
    order: list[str] = []
    model = ""

    try:
        with p.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue  # a single corrupted line should not ruin the whole attribution
                if row.get("type") != "assistant":
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                # A compact synthetic row's model is "<synthetic>", not any real model.
                # Sub-agent transcripts generally contain no synthetic rows, so this never bit before; a main session's
                # transcript has them right away — measured: a 35.1 MB main session parsed back a
                # model of exactly "<synthetic>" (§1.3). The skipping logic is from the same origin and uses the same
                # constant as session_probe's
                # .read_session_model.
                raw_model = message.get("model")
                if raw_model and str(raw_model) != SYNTHETIC_MODEL:
                    model = str(raw_model)
                # With no requestId, fall back to the line number, ensuring each row forms its own group (never wrongly
                # merged)
                req = str(row.get("requestId") or f"_line{lineno}")
                if req not in snapshots:
                    order.append(req)
                snapshots[req] = {
                    out: int(usage.get(src) or 0) for src, out in _USAGE_MAP.items()
                }
    except OSError:
        logger.debug("token attribution: transcript unreadable %s", p, exc_info=True)
        return None

    if not snapshots:
        return None

    totals = {name: 0 for name in _USAGE_MAP.values()}
    for req in order:
        for name, value in snapshots[req].items():
            totals[name] += value

    totals["api_calls"] = len(snapshots)
    totals["total_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["cache_creation_tokens"]
        + totals["cache_read_tokens"]
    )
    totals["model"] = model
    # Record how the model was obtained: transcript-verified vs alias fallback, auditable afterwards.
    totals["model_source"] = "transcript" if model else "unknown"
    return totals
