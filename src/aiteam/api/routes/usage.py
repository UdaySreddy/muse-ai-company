"""AI Team OS — Read-only endpoints for token usage attribution (docs/token-attribution-v1-design.md stages 2 / 5).

The coverage and attribution endpoints, one MCP tool, and the ``os_health_check`` summary line all go through
``StorageRepository.aggregate_token_attribution`` / ``usage_coverage_report``
this same pair of methods. If the denominator ever gains a second implementation, the two places will quietly give different coverage numbers after some change,
and nothing will raise an error about it.

Stage 5 adds two more; both are structural needs of the `/usage` page, not convenience endpoints tacked on:

* ``/scopes`` —— drill-down candidate enumeration (§5.2 ③). It only returns dispatch counts, **not a single token number**:
  a list carrying usage figures would become a leaderboard without a denominator.
* ``/probe`` —— the sole data source for the single-measurement card (§5.2 ④). It parses a transcript on the spot,
  **does not read the agents table's five token columns** — that card is exempt from the coverage gate, and the price is that its data can only come from
  a single parse; taking data from an aggregated view would turn the exemption into a backdoor around the gate.

**Read-only**: this module writes no table and triggers no collection. Triggered on demand, with zero new daemons (P3).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api import usage_probe
from aiteam.api.deps import get_repository
from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AttributionScope,
    DispatchPopulation,
    TokenAttribution,
    TokenMetric,
    UsageCoverageReport,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _window(days: int) -> tuple[object, object]:
    """Fold days into (since, until); 0 = no window (all history).

    The window lands on ``created_at`` (see repository._apply_created_window) — windowing by measurement time
    would make unmeasured rows disappear from the denominator and coverage would always equal 100%.
    """
    if days <= 0:
        return None, None
    now = utc_now()
    return now - timedelta(days=days), now


@router.get("/coverage")
async def get_usage_coverage(
    days: int = Query(0, ge=0, le=3650, description="Lookback days; 0 = all history"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Coverage vector — the matrix rows + the per-hop C_hop. Zero token values, only numerators and denominators.

    Deliberately **does not consume the implicit project scope**: the agents table is shared across projects, while coverage answers the question "is this
    observation chain healthy"; quietly slicing by the current cwd would give different answers to the same question in different directories.
    To view by project, use ``/attribution?scope=project&scope_id=...``.
    """
    since, until = _window(days)
    report: UsageCoverageReport = await repo.usage_coverage_report(since=since, until=until)
    return {"success": True, "data": report.model_dump(mode="json")}


@router.get("/attribution")
async def get_token_attribution(
    metric: str = Query(
        TokenMetric.USAGE_SUM.value,
        description="Token caliber; currently only usage_sum is supported (ctx_last has no four-layer breakdown)",
    ),
    scope: str = Query(
        AttributionScope.PROJECT.value,
        description="Attribution level: project / session / workflow_run / agent / task",
    ),
    scope_id: str = Query("", description="The id of that level; leave empty = do not filter by this dimension (whole database)"),
    population: str = Query(
        DispatchPopulation.SUBAGENT.value,
        description="Dispatch path: subagent / leader_session (the two differ by orders of magnitude and must be listed separately)",
    ),
    days: int = Query(0, ge=0, le=3650, description="Lookback days; 0 = all history"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Aggregate the four-layer usage by scope, returned at the same level as the numerator, denominator, and unattributed categories.

    The response body **has no total field**; under no parameter combination can an isolated total be obtained: 95.6% of the measured four layers is
    cache_read, so reporting only a total would equal reporting only the cache-read amount (§1.2 / §2.5).
    """
    try:
        parsed_metric = TokenMetric(metric)
        parsed_scope = AttributionScope(scope)
        parsed_population = DispatchPopulation(population)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since, until = _window(days)
    try:
        result: TokenAttribution = await repo.aggregate_token_attribution(
            metric=parsed_metric,
            scope=parsed_scope,
            scope_id=scope_id,
            population=parsed_population,
            since=since,
            until=until,
        )
    except ValueError as exc:
        # Requests like ctx_last / workflow_self_report, which "structurally cannot get in", are stopped here,
        # and the error message itself carries the reason — putting knowledge on the rejection path makes it harder to bypass than putting it in a comment.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"success": True, "data": result.model_dump(mode="json")}


@router.get("/scopes")
async def list_usage_scopes(
    scope: str = Query(
        AttributionScope.PROJECT.value,
        description="The level to enumerate: project / session / workflow_run / agent / task",
    ),
    parent_scope: str = Query("", description="Parent level; used in pair with parent_id; leave empty = no parent restriction"),
    parent_id: str = Query("", description="The parent level's id"),
    population: str = Query(
        DispatchPopulation.SUBAGENT.value,
        description="Dispatch path: subagent / leader_session (they differ by orders of magnitude and must be listed separately)",
    ),
    days: int = Query(0, ge=0, le=3650, description="Lookback days; 0 = all history"),
    limit: int = Query(30, ge=1, le=200, description="Maximum number of candidates to return"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Drill-down candidate list — each item has only id, name, dispatch count, and measured count, **with no token values at all**.

    The four-layer usage for the one selected scope_id must be fetched separately via ``/attribution``; this is a structural division of labor
    rather than a shortcut: once a sortable list carries usage figures, it will be read as a leaderboard, and a leaderboard has no
    denominator (§2.5).
    """
    try:
        parsed_scope = AttributionScope(scope)
        parsed_parent = AttributionScope(parent_scope) if parent_scope else None
        parsed_population = DispatchPopulation(population)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since, until = _window(days)
    try:
        rows = await repo.list_attribution_scopes(
            scope=parsed_scope,
            parent_scope=parsed_parent,
            parent_id=parent_id,
            population=parsed_population,
            since=since,
            until=until,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": rows}


@router.get("/unattributed")
async def sample_unattributed_dispatches(
    population: str = Query(
        DispatchPopulation.SUBAGENT.value,
        description="Dispatch path: subagent / leader_session",
    ),
    days: int = Query(0, ge=0, le=3650, description="Lookback days; 0 = all history"),
    scan_limit: int = Query(400, ge=1, le=2000, description="Maximum number of unattributed dispatch rows to scan for sampling"),
    per_reason: int = Query(5, ge=1, le=50, description="Maximum number of sample rows to return per reason code"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Drill-down samples from the unattributed drawer — a few real rows per reason code, used to judge "whether they can be recovered".

    The row counts here are **not the denominator**: only the most recent ``scan_limit`` rows are scanned. The counts per category follow the full scan of ``/coverage``;
    the ``scanned`` field in the response exists so the page can mark this boundary.
    """
    try:
        parsed_population = DispatchPopulation(population)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since, until = _window(days)
    try:
        data = await repo.sample_unattributed(
            population=parsed_population,
            since=since,
            until=until,
            scan_limit=scan_limit,
            per_reason=per_reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": data}


@router.get("/probe")
async def probe_single_dispatch(
    agent_id: str = Query(..., description="The dispatch to measure (agents.id)"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Single measurement — parse this one agent's transcript on the spot and return the four-layer usage plus head/tail summaries.

    **Does not read the agents table's five token columns**: those are ledger values (written during backfill or collection), whereas this endpoint's
    promise to keep is "I just counted it myself". The coverage-gate exemption §5.2 ④ grants this card costs exactly
    this — its data can only come from a single transcript parse.

    The response has no total field and does not pass through the parser's internal ``total_tokens`` (§1.2).
    """
    agent = await repo.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    if not agent.transcript_path:
        # This is not an error; it is a formal reason code from §3.4. Stating it clearly lets the reader know who to ask for rescue.
        raise HTTPException(
            status_code=404,
            detail=f"agent {agent_id} has no registered transcript path (no_transcript_path) — cannot perform a single measurement",
        )

    payload = usage_probe.probe_dispatch(agent.transcript_path)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"the transcript is no longer on disk or contains no parseable usage lines (transcript_gone)"
                f": {agent.transcript_path}"
            ),
        )
    payload["agent_id"] = agent.id
    payload["agent_name"] = agent.name
    return {"success": True, "data": payload}
