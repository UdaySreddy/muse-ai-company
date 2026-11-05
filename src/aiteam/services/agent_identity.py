"""Row identity = dispatch identity — the reuse criterion for ``agents`` rows (lifecycle audit A-06).

**Why a hard criterion is needed**: three rungs of ``SubagentStart``'s dedup ladder match by **name**, and
a hit binds the new dispatch in place to the old row; then ``SubagentStop`` **overwrites** that row's four-layer
tokens and ``tokens_measured_at`` by transcript. So the single coincidence of "the same name" suffices for a new
dispatch to erase the previous
dispatch's ledger — and the token ledger is **unrebuildable** derived data (transcripts get cleaned sooner or later, and
once the ledger is gone it can never
be computed back) — exactly the rule laid down by the 2026-08-01 container-team cleanup incident: before deleting data,
ask "if deleted, can it
be rebuilt".

**The bookkeeping unit is the transcript file**, and a transcript's file name is
``agent-<cc_agent_id>.jsonl``. So "one row = one dispatch = one transcript" is a single statement,
and the criterion can only stand on ``cc_tool_use_id``, never on the name. The same matter was already judged on the
workflow path
(``_register_workflow_subagent``: dedup by cc_agent_id rather than name — otherwise a single run of 16
agents would be folded into one row) — this place merely adds the same discipline to the main path.

This module is **pure judgment**: it touches neither the DB nor files, and depends only on ``aiteam.types``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from aiteam.types import Agent

# The four-layer usage columns. Any one non-zero means "this row left bookkeeping entries".
TOKEN_LAYERS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def has_token_account(agent: Agent) -> bool:
    """Whether this row already carries a token ledger. The criterion is deliberately **loose**.

    A non-empty ``tokens_measured_at`` counts as having a ledger, even if all four layers are 0 — measuring 0 is still a
    measurement result, and
    ``no-data != zero`` is a caliber this project has established repeatedly. Overwriting it equals rewriting "measured
    and 0" into
    "measured and someone else's number"; neither error may be committed.
    """
    if getattr(agent, "tokens_measured_at", None) is not None:
        return True
    return any(int(getattr(agent, layer, None) or 0) != 0 for layer in TOKEN_LAYERS)


def _sort_key(agent: Agent) -> tuple[datetime, str]:
    created = getattr(agent, "created_at", None) or _EPOCH
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (created, agent.id)


def pick_reusable_row(candidates: Iterable[Agent], cc_agent_id: str) -> Agent | None:
    """Pick one row from the same-named candidate rows for this dispatch to reuse; return None when none can be picked
    (the caller creates a new row).

    There are **only two** legitimate reuse cases; everything else creates a new row:

    1. **The same dispatch**: the candidate row's ``cc_tool_use_id`` is exactly this dispatch's cc id. A duplicate
       ``SubagentStart``, a resumed run, or measuring the same transcript once more all go through here, and overwriting
       one's own ledger
       is correct.
    2. **Never bound to any dispatch and has no ledger**: an empty row with an empty ``cc_tool_use_id``, typically
    sourced from an MCP
       pre-registration (``source="api"``) — it is a placeholder row waiting to be claimed by the hook.

    Candidates are traversed in ``created_at`` descending order (ties fall back to id), so the answer is independent of
    the input order and of
    SQL's row return order: **determinism is a hard requirement**, and the original implementation's ``limit(1)``
    without ORDER BY was
    relying on luck. At the same time, a "newer" empty row is more likely to be the row just created for this dispatch.

    When ``cc_agent_id`` is empty (the payload carried no agent_id), the dispatch identity cannot be judged; at that
    point only the ledger
    gate is kept: do not pick a row that already carries a ledger, but picking a ledger-less row bound to another
    dispatch is allowed — rather reuse conservatively than fabricate
    a row from nothing.
    """
    ordered = sorted(candidates, key=_sort_key, reverse=True)

    if cc_agent_id:
        for agent in ordered:
            if (agent.cc_tool_use_id or "") == cc_agent_id:
                return agent

    for agent in ordered:
        if has_token_account(agent):
            continue
        bound = agent.cc_tool_use_id or ""
        if bound and cc_agent_id and bound != cc_agent_id:
            # This row is **another** dispatch's identity card; even if it has no ledger yet, it cannot be substituted
            continue
        return agent
    return None
