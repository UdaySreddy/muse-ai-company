"""Single-dispatch measurement — the **only** data source for the `/usage` page's ④ single-measurement card (design §5.2
④).

This card is explicitly exempt from the coverage gate: it is a single measurement that settles the truth, not the full
ledger, and the two chains are decoupled. But the exemption has
a price, and the price falls on the way this module exists — **card data can only come from a single transcript parse,
**fetching data from aggregated views is forbidden**. Otherwise the exemption becomes a backdoor around the gate: a
number without a denominator, once labeled
with the words "single measurement", could get onto the page.

So this module deliberately does not touch the ``agents`` table's five token columns (those are **ledger** values
written in by backfill/collection),
and instead reads that one transcript on the spot and recomputes. The two usually agree numerically but differ in source
— what this card promises is
"I just counted it myself"; ledger values cannot give that promise.

The numeric algorithm does not live in this module: everything goes through
:func:`aiteam.services.token_attribution.parse_transcript_usage`,
that one implementation (group by requestId, take the last snapshot in each group, then sum across groups; naively
adding row by row would inflate the result 5x). This module only
additionally does two presentation-layer things: excerpting the head and tail text, and measuring elapsed time from the
transcript timestamps.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from aiteam.services import token_attribution
from aiteam.types import TOKEN_LAYERS, TokenMetric

logger = logging.getLogger(__name__)

# Summary truncation length. The card is a "dedicated channel for external samples"; what matters is being able to tell
# at a glance what this dispatch was doing,
# not moving the entire prompt onto the page.
EXCERPT_LIMIT = 600


def _text_of(message: Any) -> str:
    """Flatten one CC message's content into plain text; non-text blocks (tool_use / image) are skipped."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p).strip()


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= EXCERPT_LIMIT else text[:EXCERPT_LIMIT] + "…"


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def read_transcript_sample(path: str | Path) -> dict[str, Any] | None:
    """Excerpt a transcript's first input, last output, and head-to-tail time span.

    Returning None means the file is absent — "no data" and "used 0" are two different things; the former must never be
    written as the latter.
    """
    p = Path(path)
    if not p.exists():
        return None

    first_input = ""
    last_output = ""
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue  # a single corrupted line should not ruin the whole summary
                ts = _parse_ts(row.get("timestamp"))
                if ts is not None:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                kind = row.get("type")
                if kind == "user" and not first_input:
                    # The first user entry = the prompt handed in at dispatch time (later user lines are mostly tool
                    # results fed back in)
                    first_input = _text_of(row.get("message"))
                elif kind == "assistant":
                    text = _text_of(row.get("message"))
                    if text:
                        last_output = text
    except OSError:
        logger.debug("usage probe: transcript unreadable %s", p, exc_info=True)
        return None

    duration_ms: int | None = None
    if first_ts is not None and last_ts is not None and last_ts >= first_ts:
        duration_ms = int((last_ts - first_ts).total_seconds() * 1000)

    return {
        "prompt_excerpt": _clip(first_input),
        "result_excerpt": _clip(last_output),
        "duration_ms": duration_ms,
        "started_at": first_ts.isoformat() if first_ts else None,
        "ended_at": last_ts.isoformat() if last_ts else None,
    }


def probe_dispatch(transcript_path: str) -> dict[str, Any] | None:
    """Parse a transcript on the spot and return all the fields the single-measurement card needs.

    **Deliberately discards the ``total_tokens`` returned by the parse**: 95.6% of the measured four layers is
    cache_read, so any
    "total" is a synonym for "cache-read amount"; putting it on the presentation surface means handing people a number
    that systematically skews
    comparisons for them to compare with (§1.2). Anyone who wants a total adds it up themselves and bears the
    interpretation responsibility themselves.
    """
    usage = token_attribution.parse_transcript_usage(transcript_path)
    if usage is None:
        return None
    sample = read_transcript_sample(transcript_path) or {}

    payload: dict[str, Any] = {name: int(usage.get(name) or 0) for name in TOKEN_LAYERS}
    payload.update(
        {
            # The caliber shares its source with the aggregation surface: this card and the /usage attribution card are
            # both usage_sum and can be cross-checked against each other.
            "metric": TokenMetric.USAGE_SUM.value,
            "api_calls": int(usage.get("api_calls") or 0),
            # The model name is **obtained by observation**, not read from configuration: the transcript always holds
            # the full model name,
            # while `model: opus` in the templates is a tier-level alias; the two are not interchangeable.
            "model": str(usage.get("model") or ""),
            "model_source": str(usage.get("model_source") or "unknown"),
            "transcript_path": transcript_path,
        }
    )
    payload.update(sample)
    return payload
