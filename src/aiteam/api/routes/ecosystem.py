"""AI Team OS — Claude ecosystem repo profile API routes."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from aiteam.api.deps import get_scoped_repository
from aiteam.clock import ensure_utc, utc_now
from aiteam.services.ecosystem_deep_reviewer import EcosystemDeepReviewer
from aiteam.services.ecosystem_scanner import (
    EcosystemScanner,
    FilterConfig,
    default_gh_search,
)
from aiteam.services.ecosystem_summarizer import (
    TOP_N_SORT_OPTIONS,
    EcosystemSummarizer,
)
from aiteam.services.ecosystem_tagger import (
    DEFAULT_LLM_AGENT_TEMPLATE,
    EcosystemTagger,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    DataSourceKind,
    EcosystemDeepReview,
    EcosystemIndexDiff,
    EcosystemRepoEvent,
    EcosystemRepoProfile,
    EcosystemScanRun,
    EcosystemScanStrategy,
    EcosystemStatusChange,
    EcosystemTag,
    EcosystemTagCategory,
)

logger = logging.getLogger(__name__)

# Default ScanProfile template — used when no profile has been configured.
# v1.6.0-P1.A: stars is admission gate only; once admitted repos are permanent.
# Removed: active_definition / inactive_signals / archive_signals (no inactivity eviction).
_DEFAULT_SCAN_PROFILE: dict = {
    "popularity_floor": {
        "github": 1000,
        "huggingface": 200,
        "npm": 1000,
        "pypi": 1000,
    },
    "alert_thresholds": {
        "max_new_per_scan": 50,
    },
}

router = APIRouter(prefix="/api/ecosystem", tags=["ecosystem"])


class EcosystemProfileCreate(BaseModel):
    repo_full_name: str
    name: str
    owner: str
    description: str | None = None
    stars: int = 0
    language: str | None = None
    topics: list[str] = []
    homepage: str | None = None
    last_commit_at: str | None = None
    needs_deep_review: bool = False
    relevance_category: str | None = None
    relevance_score: int = 0
    one_line_summary: str | None = None
    last_scanned_at: str | None = None
    pushed_at: str | None = None
    is_archived: bool = False
    scan_run_id: str | None = None
    description_excerpt: str = ""


def _make_summary_excerpt(text: str | None, max_chars: int = 150) -> str:
    """Truncate long text to excerpt for list endpoints (P1.B layered return)."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_profile(data: EcosystemProfileCreate) -> EcosystemRepoProfile:
    """Convert API payload to Pydantic model."""
    last_commit_at = _parse_dt(data.last_commit_at)
    pushed_at = _parse_dt(data.pushed_at)

    now = utc_now()
    last_scanned_at = _parse_dt(data.last_scanned_at) or now

    return EcosystemRepoProfile(
        repo_full_name=data.repo_full_name,
        name=data.name,
        owner=data.owner,
        description=data.description,
        stars=data.stars,
        language=data.language,
        topics=data.topics,
        homepage=data.homepage,
        last_commit_at=last_commit_at,
        needs_deep_review=data.needs_deep_review,
        relevance_category=data.relevance_category,
        relevance_score=data.relevance_score,
        one_line_summary=data.one_line_summary,
        last_scanned_at=last_scanned_at,
        first_seen_at=now,
        pushed_at=pushed_at,
        is_archived=data.is_archived,
        scan_run_id=data.scan_run_id,
        description_excerpt=data.description_excerpt or "",
    )


def _profile_to_dict(
    p: EcosystemRepoProfile,
    stage_status: str | None = None,
    research_count: int = 0,
) -> dict[str, Any]:
    """Serialize a profile.

    v1.5.1: expose stage_status (taken from the latest deep_review; when there is no review, the caller passes "queued").
    research_count = the number of deep_review rows associated with this repo (the frontend uses it for the RepoCard research-count badge).
    """
    return {
        "id": p.id,
        "repo_full_name": p.repo_full_name,
        "name": p.name,
        "owner": p.owner,
        "description": p.description,
        "stars": p.stars,
        "language": p.language,
        "topics": p.topics,
        "homepage": p.homepage,
        "last_commit_at": p.last_commit_at.isoformat() if p.last_commit_at else None,
        "needs_deep_review": p.needs_deep_review,
        # v1.6.1: relevance_category/relevance_score deprecated (the hardcoded heuristic has no reference value); no longer exposed
        "one_line_summary": p.one_line_summary,
        "last_scanned_at": p.last_scanned_at.isoformat(),
        "first_seen_at": p.first_seen_at.isoformat(),
        "pushed_at": p.pushed_at.isoformat() if p.pushed_at else None,
        "is_archived": p.is_archived,
        "scan_run_id": p.scan_run_id,
        "description_excerpt": _make_summary_excerpt(p.description_excerpt, 150) if p.description_excerpt else None,
        # Fields added in v1.5.0-A (depended on by the frontend stage badge / failure hint / active-set tab)
        "shallow_summary": p.shallow_summary,
        "last_shallow_refreshed_at": (
            p.last_shallow_refreshed_at.isoformat()
            if p.last_shallow_refreshed_at
            else None
        ),
        "is_deleted": p.is_deleted,
        "is_private_now": p.is_private_now,
        "last_fetch_error": p.last_fetch_error,
        "fetch_failure_count": p.fetch_failure_count,
        "is_active": p.is_active,
        "active_rank": p.active_rank,
        # Added in v1.5.1: expose the progressive-funnel stage status + research count (so frontend badges render and totals are accurate)
        "stage_status": stage_status or "queued",
        "research_count": research_count,
        # v1.6.0-P1.A manual status fields
        "last_active_status": p.last_active_status,
        "manual_status": p.manual_status,
        # v1.6.1 multi-source
        "sources": p.sources or [],
        "primary_source": p.primary_source or "github",
        "canonical_id": p.canonical_id,
        "source_kind": p.source_kind,
    }


def _profile_to_list_dict(
    p: EcosystemRepoProfile,
    stage_status: str | None = None,
    research_count: int = 0,
) -> dict[str, Any]:
    """P1.B layered return: summary-only fields for list endpoints.

    Omits shallow_summary (long text) and other large fields.
    Use _profile_to_dict for full detail.
    """
    return {
        "id": p.id,
        "repo_full_name": p.repo_full_name,
        "name": p.name,
        "owner": p.owner,
        "stars": p.stars,
        "language": p.language,
        "topics": p.topics,
        # v1.6.1: relevance_category/relevance_score deprecated (the hardcoded heuristic has no reference value); no longer exposed
        "one_line_summary": p.one_line_summary,
        "description_excerpt": _make_summary_excerpt(p.description, 150),
        "is_archived": p.is_archived,
        "last_active_status": p.last_active_status,
        "manual_status": p.manual_status,
        "canonical_id": p.canonical_id,
        "source_kind": p.source_kind,
        # v1.6.1 multi-source: the frontend card renders multi-source badges from the sources list
        "sources": p.sources or [],
        "primary_source": p.primary_source or "github",
        "last_commit_at": p.last_commit_at.isoformat() if p.last_commit_at else None,
        "last_scanned_at": p.last_scanned_at.isoformat(),
        "pushed_at": p.pushed_at.isoformat() if p.pushed_at else None,
        "stage_status": stage_status or "queued",
        "research_count": research_count,
        "needs_deep_review": p.needs_deep_review,
    }


async def _build_stage_map(
    repo: StorageRepository,
    profile_ids: list[str],
    shallow_summary_map: dict[str, str | None] | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    """Batch-fetch each profile's latest deep_review.stage_status + research count.

    Returns (stage_map, count_map):
    - stage_map[repo_id] = "queued" / "shallow_done" / ...(the latest deep_review; when none, per
      the v1.6.0 SST rule: has shallow_summary -> "shallow_done"; otherwise "queued")
    - count_map[repo_id] = the number of times this repo has genuinely been researched (v1.5.2: only counts architecture_done+ rows;
      shallow-scan completion does not count — a shallow scan is a pre-screening action, not "being researched")

    shallow_summary_map: {repo_id: shallow_summary} — used as the fallback when there is no deep_review.
    Query all reviews in one go (by created_at desc) and aggregate on the Python side, avoiding an N+1 at the route layer.
    """
    # v1.5.2: The semantic boundary of "has been researched" — only counts after entering the architecture stage
    _researched_stages = {"architecture_done", "debated", "referenced", "integrated"}

    stage_map: dict[str, str] = {}
    count_map: dict[str, int] = {}
    if not profile_ids:
        return stage_map, count_map
    id_set = set(profile_ids)
    # list_deep_reviews does not support IN with multiple ids, but with a limit applied, Python-side filtering is sufficient (currently < a few thousand rows)
    reviews = await repo.list_deep_reviews(limit=10000)
    for r in reviews:
        if r.repo_id not in id_set:
            continue
        # Only count rows that were genuinely researched (excluding shallow-scan shallow_done / queued / *_failed)
        stage_value = (
            r.stage_status.value
            if hasattr(r.stage_status, "value")
            else (r.stage_status or "")
        )
        if stage_value in _researched_stages:
            count_map[r.repo_id] = count_map.get(r.repo_id, 0) + 1
        if r.repo_id not in stage_map:
            # list_deep_reviews is already in created_at desc order; the first row is the latest
            stage_map[r.repo_id] = stage_value or "queued"
    # v1.6.0 SST fallback: no deep_review → check shallow_summary before defaulting to "queued"
    for pid in profile_ids:
        if pid not in stage_map:
            summary = (shallow_summary_map or {}).get(pid)
            if summary and summary.strip():
                stage_map[pid] = "shallow_done"
            else:
                stage_map[pid] = "queued"
        count_map.setdefault(pid, 0)
    return stage_map, count_map


@router.post("/profiles")
async def upsert_profile(
    data: EcosystemProfileCreate,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Upsert an ecosystem repo profile (unique key: repo_full_name)."""
    existing = await repo.get_ecosystem_profile(data.repo_full_name)
    is_new = existing is None

    profile = _parse_profile(data)
    await repo.upsert_ecosystem_profile(profile)
    result = _profile_to_dict(profile)
    result["created"] = is_new
    return result


@router.get("/profiles")
async def search_profiles(
    keyword: str = Query(default=""),
    topic: str = Query(default=""),
    min_stars: int = Query(default=0),
    max_stars: int = Query(default=0),
    category: str = Query(default=""),
    language: str = Query(default=""),
    pushed_after: str = Query(default=""),
    is_archived: bool | None = Query(default=None),
    needs_deep_review: bool | None = Query(default=None),
    tags: str = Query(default=""),
    tag_match_mode: str = Query(default="all"),
    sort: str = Query(default="stars"),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    facet_counts: bool = Query(default=False),
    # Depended on by the v1.5.0-E frontend tab switching / failed filtering (ignored by the service layer if unrecognized)
    is_active: bool | None = Query(default=None),
    is_deleted: bool | None = Query(default=None),
    stage_status: str = Query(default=""),
    # P1.B: detail=true returns full fields (shallow_summary etc); default is summary-only
    detail: bool = Query(default=False),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Search the ecosystem repo profile list (Stage E extended version).

    - tags: comma-separated list of tag names; combined with tag_match_mode (all/any) for AND/OR filtering
    - sort: stars / recency (pushed_at desc)  [v1.6.1: relevance sorting deprecated]
    - facet_counts: when True, include the category/language/archived aggregate distributions
    - is_active / is_deleted / stage_status: v1.5.0-E frontend filters (active/full tabs, failed filter)
    - detail=false (default): return summary fields (P1.B layered return, without the long shallow_summary text)
    - detail=true: return full fields (including shallow_summary)
    """
    pushed_after_dt = _parse_dt(pushed_after) if pushed_after else None
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # v1.5.1: The stage_status filter does not depend on the stars limit — first compute stage_map over the whole DB to find wanted_ids
    id_filter: list[str] | None = None
    id_exclude: list[str] | None = None
    if stage_status:
        wanted_stages = {s.strip() for s in stage_status.split(",") if s.strip()}
        if wanted_stages:
            full_reviews = await repo.list_deep_reviews(limit=10000)
            full_stage_map: dict[str, str] = {}
            for r in full_reviews:
                if r.repo_id in full_stage_map:
                    continue  # latest_only (list_deep_reviews is already created_at desc)
                stg = (
                    r.stage_status.value
                    if hasattr(r.stage_status, "value")
                    else r.stage_status
                ) or "queued"
                full_stage_map[r.repo_id] = stg

            other_wanted = wanted_stages - {"queued"}
            wants_queued = "queued" in wanted_stages

            if wants_queued and not other_wanted:
                # Pure queued: exclude all repos that have reviews (regardless of stage)
                id_exclude = list(full_stage_map.keys())
            elif other_wanted and not wants_queued:
                # Pure non-queued: limit to repos matching the stages
                id_filter = [
                    rid for rid, stg in full_stage_map.items() if stg in other_wanted
                ]
            elif wants_queued and other_wanted:
                # queued + others: exclude repos that "have a review but whose stage does not match"
                id_exclude = [
                    rid for rid, stg in full_stage_map.items() if stg not in other_wanted
                ]

    profiles, total = await repo.search_ecosystem_profiles_extended(
        keyword=keyword,
        topic=topic,
        min_stars=min_stars,
        max_stars=max_stars if max_stars > 0 else None,
        needs_deep_review=needs_deep_review,
        category=category,
        language=language,
        pushed_after=pushed_after_dt,
        is_archived=is_archived,
        tags=tag_list or None,
        tag_match_mode=tag_match_mode,
        sort=sort,
        limit=limit,
        offset=offset,
        id_filter=id_filter,
        id_exclude=id_exclude,
    )

    # v1.5.0-E client-side secondary filtering: is_active / is_deleted
    # DEPRECATED v1.6.0 P1.A: the is_active field is deprecated; use last_active_status.
    # This parameter is kept only for backward compatibility and falls back internally to last_active_status='active'.
    if is_active is not None:
        def _is_active(p):
            status = getattr(p, 'last_active_status', None)
            if status is not None:
                return status == 'active'
            return bool(getattr(p, 'is_active', True))
        profiles = [p for p in profiles if _is_active(p) == is_active]
    if is_deleted is not None:
        profiles = [p for p in profiles if p.is_deleted == is_deleted]

    # v1.5.1: Batch-fetch stage_map + count_map in one go (eliminating the original N+1 queries)
    # v1.6.0 SST: pass shallow_summary so repos with summary but no deep_review → "shallow_done"
    _shallow_map = {p.id: p.shallow_summary for p in profiles}
    stage_map, count_map = await _build_stage_map(repo, [p.id for p in profiles], _shallow_map)

    # stage_status is already handled at the SQL layer with id_filter / id_exclude; the route layer no longer post-filters
    if is_active is not None or is_deleted is not None:
        total = len(profiles)

    # P1.B: use summary-only serializer by default; full fields only when detail=True
    _serialize_fn = _profile_to_dict if detail else _profile_to_list_dict
    payload: dict[str, Any] = {
        "profiles": [
            _serialize_fn(
                p,
                stage_status=stage_map.get(p.id, "queued"),
                research_count=count_map.get(p.id, 0),
            )
            for p in profiles
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
    }

    if facet_counts:
        facets = await repo.compute_ecosystem_facet_counts(
            keyword=keyword,
            min_stars=min_stars,
            max_stars=max_stars if max_stars > 0 else None,
            category=category,
            language=language,
            is_archived=is_archived,
        )
        payload["facet_counts"] = facets

    return payload


@router.get("/profiles/{repo_full_name:path}/full")
async def get_profile_full(
    repo_full_name: str,
    relations_limit: int = Query(default=50, le=200),
    deep_reviews_limit: int = Query(default=20, le=100),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get a repo's full details: profile + tags + deep_reviews + relations + scan_run.

    repo_full_name looks like "owner/repo". Passing a repo id directly is also supported (the id form has no "/").
    """
    if "/" in repo_full_name:
        result = await repo.get_ecosystem_profile_full(
            repo_full_name=repo_full_name,
            relations_limit=relations_limit,
            deep_reviews_limit=deep_reviews_limit,
        )
    else:
        # treat it as a primary-key id
        result = await repo.get_ecosystem_profile_full(
            repo_id=repo_full_name,
            relations_limit=relations_limit,
            deep_reviews_limit=deep_reviews_limit,
        )

    if result is None:
        raise HTTPException(status_code=404, detail="ecosystem repo not found")

    return _serialize_full(result)


class ManualStatusBody(BaseModel):
    """Request body for setting manual status on a repo."""

    status: str | None = None  # 'no_value' to mark; null/None to clear
    reason: str = ""
    set_by: str = "user"


@router.post("/repos/{repo_id}/manual_status")
async def set_repo_manual_status(
    repo_id: str,
    body: ManualStatusBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Set or clear manual status on a repo (e.g. mark as no_value after deep review).

    POST body: {status: 'no_value', reason: 'low quality content', set_by: 'user'}
    To clear: {status: null, reason: ''}

    When status='no_value', the repo's last_active_status will reflect 'manual_archived'
    on the next index_update run.
    """
    # Validate status value — allowed: 'no_value', 'pinned', null
    allowed = {None, "no_value", "pinned"}
    if body.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: no_value, pinned, null. Got: {body.status!r}",
        )

    found = await repo.update_repo_manual_status(
        repo_id=repo_id,
        manual_status=body.status,
        reason=body.reason,
        set_by=body.set_by,
    )
    if not found:
        raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")

    # Update last_active_status based on new manual_status
    if body.status == "no_value":
        await repo.update_repo_active_status(repo_id, new_status="manual_archived")
    elif body.status == "pinned":
        # pinned repos stay active — ensure last_active_status reflects this
        await repo.update_repo_active_status(repo_id, new_status="active")
    elif body.status is None:
        # Clearing manual status — restore to 'active' unless github archived
        profile = await repo.get_ecosystem_profile_by_id(repo_id)
        if profile and not profile.is_archived:
            await repo.update_repo_active_status(repo_id, new_status="active")

    return {
        "success": True,
        "repo_id": repo_id,
        "manual_status": body.status,
        "reason": body.reason,
        "set_by": body.set_by,
        "message": (
            f"Marked as no_value: {body.reason}" if body.status == "no_value"
            else f"Pinned: {body.reason}" if body.status == "pinned"
            else "Manual status cleared"
        ),
    }


@router.get("/search/by_capability")
async def search_by_capability(
    tags: str = Query(default=""),
    match_mode: str = Query(default="all"),
    min_stars: int = Query(default=0),
    max_stars: int = Query(default=0),
    sort: str = Query(default="stars"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Reverse-look-up repos by capability tags.

    tags are comma-separated. match_mode: all (AND) / any (OR).
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if not tag_list:
        return {"profiles": [], "total": 0, "limit": limit, "offset": offset}

    profiles, total = await repo.search_ecosystem_profiles_extended(
        min_stars=min_stars,
        max_stars=max_stars if max_stars > 0 else None,
        tags=tag_list,
        tag_match_mode=match_mode,
        sort=sort,
        limit=limit,
        offset=offset,
    )

    return {
        "profiles": [_profile_to_dict(p) for p in profiles],
        "total": total,
        "limit": limit,
        "offset": offset,
        "matched_tags": tag_list,
        "match_mode": match_mode,
    }


# ============================================================
# Deep review endpoints (Stage F)
# ============================================================


class DeepReviewRequestBody(BaseModel):
    """Request a new deep-scan report (by repo_id)."""

    repo_id: str
    priority: str = "medium"
    timeout_minutes: int = Field(default=45, ge=5, le=180)
    agent_id: str | None = None


class DeepReviewLinkBody(BaseModel):
    """The link entry point used by the hook: write an existing report_id onto the deep_review.

    K5: after the hook parses the 5-section markdown, it backfills the structured fields into the row together.
    If any field is None, the row's original value is kept (backward compatible with old hooks).
    """

    report_id: str
    summary_md: str | None = None
    architecture_md: str | None = None
    risks_md: str | None = None
    learnings_md: str | None = None
    integration_md: str | None = None
    demo_result: str | None = None
    demo_log_excerpt: str | None = None
    integration_recommendation: str | None = None


def _deep_review_to_dict(dr: EcosystemDeepReview) -> dict[str, Any]:
    """Serialize a deep-review row for the public API."""
    return {
        "id": dr.id,
        "repo_id": dr.repo_id,
        "status": dr.status.value if hasattr(dr.status, "value") else dr.status,
        "agent_id": dr.agent_id,
        "summary_md": dr.summary_md,
        "architecture_md": dr.architecture_md,
        "demo_result": (
            dr.demo_result.value
            if dr.demo_result is not None and hasattr(dr.demo_result, "value")
            else dr.demo_result
        ),
        "demo_log_excerpt": dr.demo_log_excerpt,
        "risks_md": dr.risks_md,
        "learnings_md": dr.learnings_md,
        "integration_recommendation": (
            dr.integration_recommendation.value
            if dr.integration_recommendation is not None
            and hasattr(dr.integration_recommendation, "value")
            else dr.integration_recommendation
        ),
        "report_id": dr.report_id,
        "dispatch_prompt": getattr(dr, "dispatch_prompt", "") or "",
        "started_at": dr.started_at.isoformat() if dr.started_at else None,
        "completed_at": dr.completed_at.isoformat() if dr.completed_at else None,
        "duration_seconds": dr.duration_seconds,
        "created_at": dr.created_at.isoformat() if dr.created_at else None,
        # Fields added in v1.5.0-A (depended on by the frontend research-history timeline)
        "stage_status": (
            dr.stage_status.value
            if hasattr(dr.stage_status, "value")
            else dr.stage_status
        ),
        "integration_md": getattr(dr, "integration_md", "") or "",
        "shallow_completed_at": (
            dr.shallow_completed_at.isoformat() if dr.shallow_completed_at else None
        ),
        "architecture_completed_at": (
            dr.architecture_completed_at.isoformat()
            if dr.architecture_completed_at
            else None
        ),
        "debated_at": dr.debated_at.isoformat() if dr.debated_at else None,
        "stage3_completed_at": (
            dr.stage3_completed_at.isoformat() if dr.stage3_completed_at else None
        ),
        "debate_meeting_id": dr.debate_meeting_id,
        "integration_task_id": dr.integration_task_id,
    }


def _get_reviewer(repo: StorageRepository) -> EcosystemDeepReviewer:
    """Build an EcosystemDeepReviewer bound to the request's repository."""
    return EcosystemDeepReviewer(repo=repo)


@router.post("/deep_reviews")
async def request_deep_review(
    body: DeepReviewRequestBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Enqueue a deep-scan report; returns the stage_status=queued row + the dispatch prompt.

    D5 convergence: the status column is a derived read-only view of stage_status; new rows have status='queued'
    (historical behavior returned 'running'; no longer written since v1.6.2).
    """
    reviewer = _get_reviewer(repo)
    try:
        review = await reviewer.request(
            repo_id=body.repo_id,
            priority=body.priority,
            timeout_minutes=body.timeout_minutes,
            agent_id=body.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _deep_review_to_dict(review)


@router.get("/deep_reviews")
async def list_deep_reviews(
    status: str = Query(default=""),
    limit: int = Query(default=20, le=100),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List deep-scan reports filtered by status (newest-first)."""
    reviewer = _get_reviewer(repo)
    rows = await reviewer.list_reviews(status=status, limit=limit)
    return {
        "reviews": [_deep_review_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.get("/deep_reviews/by_repo/{repo_id}")
async def deep_review_status(
    repo_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get the status of the latest deep-scan report for the specified repo; returns 404 if not found."""
    reviewer = _get_reviewer(repo)
    review = await reviewer.status(repo_id=repo_id)
    if review is None:
        raise HTTPException(status_code=404, detail="no deep review for repo_id")
    return _deep_review_to_dict(review)


@router.post("/deep_reviews/{deep_review_id}/cancel")
async def cancel_deep_review(
    deep_review_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Cancel a queued / in-progress deep-scan report."""
    reviewer = _get_reviewer(repo)
    review = await reviewer.cancel(deep_review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="deep review not found")
    return _deep_review_to_dict(review)


@router.post("/deep_reviews/{deep_review_id}/backfill")
async def backfill_deep_review(
    deep_review_id: str,
    body: DeepReviewLinkBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Backfill 5-section fields on an already-linked deep_review row.

    Same payload as link_report but bypasses the "report_id already set"
    short-circuit. Used by scripts/backfill_deep_review_sections.py to
    populate rows created before the K5 hook parser shipped.
    """
    review = await repo.get_deep_review(deep_review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="deep review not found")

    update_fields: dict[str, Any] = {"report_id": body.report_id}
    if body.summary_md is not None:
        update_fields["summary_md"] = body.summary_md
    if body.architecture_md is not None:
        update_fields["architecture_md"] = body.architecture_md
    if body.risks_md is not None:
        update_fields["risks_md"] = body.risks_md
    if body.learnings_md is not None or body.integration_md is not None:
        learnings = body.learnings_md or ""
        if body.integration_md is not None:
            sep = "\n\n" if learnings else ""
            learnings = (
                f"{learnings}{sep}## 5. Integration suggestions\n{body.integration_md.strip()}"
            )
        update_fields["learnings_md"] = learnings
    if body.demo_log_excerpt is not None:
        update_fields["demo_log_excerpt"] = body.demo_log_excerpt
    if body.demo_result is not None:
        update_fields["demo_result"] = body.demo_result
    if body.integration_recommendation is not None:
        update_fields["integration_recommendation"] = body.integration_recommendation

    updated = await repo.update_deep_review(deep_review_id, **update_fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="deep review not found")
    return _deep_review_to_dict(updated)


@router.post("/deep_reviews/{deep_review_id}/link_report")
async def link_deep_review_report(
    deep_review_id: str,
    body: DeepReviewLinkBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Associate report_id with deep_review_id and advance the status to completed.

    Used by the PostToolUse hook. Repeated calls are idempotent: if report_id is already bound, return directly.
    The hook also backfills the fields parsed from the 5 sections (summary_md / architecture_md / ...).
    """
    reviewer = _get_reviewer(repo)
    review = await reviewer.link_report(
        deep_review_id,
        body.report_id,
        summary_md=body.summary_md,
        architecture_md=body.architecture_md,
        risks_md=body.risks_md,
        learnings_md=body.learnings_md,
        integration_md=body.integration_md,
        demo_result=body.demo_result,
        demo_log_excerpt=body.demo_log_excerpt,
        integration_recommendation=body.integration_recommendation,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep review not found")
    return _deep_review_to_dict(review)


def _serialize_full(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert dict containing Pydantic models into JSON-friendly dict."""
    profile = payload["profile"]
    deep_reviews = payload.get("deep_reviews") or []
    scan_run = payload.get("scan_run")

    # v1.6.0 SST: derive stage_status using same logic as list endpoint (_build_stage_map)
    # Priority: deep_review.stage_status > non-empty shallow_summary → 'shallow_done' > 'queued'
    stage_status = "queued"
    latest_dr = deep_reviews[0] if deep_reviews else None  # the repository return should be in reverse chronological order
    if latest_dr and getattr(latest_dr, "stage_status", None):
        stage_status = latest_dr.stage_status
    elif profile.shallow_summary and profile.shallow_summary.strip():
        stage_status = "shallow_done"

    serialized: dict[str, Any] = {
        "profile": _profile_to_dict(profile, stage_status=stage_status, research_count=len(deep_reviews)),
        "tags": payload.get("tags") or [],
        "deep_reviews": [_deep_review_to_dict(dr) for dr in deep_reviews],
        "relations_from": payload.get("relations_from") or [],
        "relations_to": payload.get("relations_to") or [],
        "scan_run": (
            {
                "id": scan_run.id,
                "strategy": (
                    scan_run.strategy.value
                    if hasattr(scan_run.strategy, "value")
                    else scan_run.strategy
                ),
                "started_at": (
                    scan_run.started_at.isoformat()
                    if getattr(scan_run, "started_at", None)
                    else None
                ),
                "completed_at": (
                    scan_run.completed_at.isoformat()
                    if getattr(scan_run, "completed_at", None)
                    else None
                ),
                "duration_seconds": scan_run.duration_seconds,
                "repos_added": scan_run.repos_added,
                "repos_updated": scan_run.repos_updated,
                "repos_skipped": scan_run.repos_skipped,
                "metadata_changed_count": getattr(scan_run, "metadata_changed_count", 0),
                "errors": scan_run.errors,
                "notes": scan_run.notes,
                "triggered_by": scan_run.triggered_by,
                "agent_id": scan_run.agent_id,
            }
            if scan_run is not None
            else None
        ),
    }
    return serialized


# ============================================================
# Stage C: Scan Run endpoints
# ============================================================


def _scan_run_to_dict(run: EcosystemScanRun) -> dict[str, Any]:
    """Serialise an EcosystemScanRun into a JSON-friendly dict."""
    return {
        "id": run.id,
        "strategy": run.strategy.value if hasattr(run.strategy, "value") else run.strategy,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_seconds": run.duration_seconds,
        "repos_added": run.repos_added,
        "repos_updated": run.repos_updated,
        "repos_skipped": run.repos_skipped,
        # v1.6.1 Phase 2: real metadata change count
        "metadata_changed_count": getattr(run, "metadata_changed_count", 0),
        "errors": run.errors,
        "notes": run.notes,
        "triggered_by": run.triggered_by,
        "agent_id": run.agent_id,
    }


class ScanRunCreate(BaseModel):
    strategy: str = "incremental"
    triggered_by: str = "manual"
    notes: str = ""
    agent_id: str | None = None


class ScanRunComplete(BaseModel):
    duration_seconds: float = 0.0
    repos_added: int = 0
    repos_updated: int = 0
    repos_skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    completed_at: str | None = None
    notes: str | None = None


class ScanRunExecute(BaseModel):
    strategy: str = "incremental"
    min_stars: int = 1000
    triggered_by: str = "manual"
    notes: str = ""
    agent_id: str | None = None


def _coerce_strategy(value: str) -> EcosystemScanStrategy:
    """Map a strategy string to the enum, defaulting to INCREMENTAL on bad input."""
    try:
        return EcosystemScanStrategy(value)
    except ValueError:
        return EcosystemScanStrategy.INCREMENTAL


@router.post("/scan-runs")
async def create_scan_run(
    body: ScanRunCreate,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Create a new EcosystemScanRun row in OPEN state.

    Used by the legacy ecosystem_scan tool to bracket each invocation.
    """
    run = EcosystemScanRun(
        strategy=_coerce_strategy(body.strategy),
        triggered_by=body.triggered_by or "manual",
        notes=body.notes or "",
        agent_id=body.agent_id,
    )
    await repo.create_scan_run(run)
    return _scan_run_to_dict(run)


@router.get("/scan-runs")
async def list_scan_runs(
    strategy: str = Query(default=""),
    limit: int = Query(default=10, le=100, ge=1),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List recent scan runs, ordered by started_at DESC."""
    rows = await repo.list_scan_runs(strategy=strategy, limit=limit)
    return {
        "runs": [_scan_run_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.get("/scan-runs/{run_id}")
async def get_scan_run(
    run_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get a single scan run by id."""
    row = await repo.get_scan_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="scan run not found")
    return _scan_run_to_dict(row)


@router.post("/scan-runs/{run_id}/complete")
async def complete_scan_run(
    run_id: str,
    body: ScanRunComplete,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Update a scan run with completion stats."""
    completed_at = _parse_dt(body.completed_at) or utc_now()
    fields: dict[str, Any] = {
        "completed_at": completed_at,
        "duration_seconds": body.duration_seconds,
        "repos_added": body.repos_added,
        "repos_updated": body.repos_updated,
        "repos_skipped": body.repos_skipped,
        "errors": body.errors,
    }
    if body.notes is not None:
        fields["notes"] = body.notes
    updated = await repo.update_scan_run(run_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="scan run not found")
    return _scan_run_to_dict(updated)


@router.post("/scan-runs/execute")
async def execute_scan_run(
    body: ScanRunExecute,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Trigger a scanner run synchronously and return the resulting summary.

    This drives the EcosystemScanner service end-to-end: open a ScanRun,
    fan out gh queries, apply secondary filters, upsert profiles, close
    the ScanRun. Errors are collected (graceful degradation).
    """
    config = FilterConfig.from_env()
    config.min_stars = body.min_stars or config.min_stars
    scanner = EcosystemScanner(repo=repo, gh_search=default_gh_search, config=config)
    strategy = _coerce_strategy(body.strategy)
    result = await scanner.scan(
        strategy=strategy,
        triggered_by=body.triggered_by or "manual",
        agent_id=body.agent_id,
        notes=body.notes or "",
    )
    return {
        "run_id": result.run_id,
        "strategy": result.strategy,
        "scanned": result.scanned,
        "new_profiles": result.new_profiles,
        "updated_profiles": result.updated_profiles,
        "skipped": result.skipped,
        "archived_marked": result.archived_marked,
        "errors": result.errors,
        "duration_seconds": result.duration_seconds,
        "per_query_stats": result.per_query_stats,
        "category_distribution": result.category_distribution,
    }


class EcosystemRefreshBody(BaseModel):
    """On-demand incremental refresh request body."""

    notes: str = ""
    triggered_by: str = "manual"


async def _default_repo_fetcher(full_name: str) -> dict[str, Any]:
    """Probe a single repo via the gh CLI, producing the GhFetcher response agreed with EcosystemRefresher.

    Returns {http_status, pushed_at, stars, is_archived[, error_message,
    rate_limit_remaining]}; gh unavailable/timeout is classified as http_status=0 (transient).
    """
    import asyncio
    import json as _json
    import subprocess

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(  # noqa: S603, S607 — fixed command; the only argument is the repo name
                ["gh", "api", f"repos/{full_name}"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"http_status": 0, "error_message": str(exc)[:200]}
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if "404" in err or "Not Found" in err:
                return {"http_status": 404, "error_message": err[:200]}
            if "rate limit" in err.lower():
                return {
                    "http_status": 403,
                    "rate_limit_remaining": 0,
                    "error_message": err[:200],
                }
            if "403" in err:
                return {"http_status": 403, "error_message": err[:200]}
            return {"http_status": 0, "error_message": err[:200]}
        try:
            d = _json.loads(proc.stdout)
        except ValueError as exc:
            return {"http_status": 0, "error_message": f"bad json: {exc}"}
        return {
            "http_status": 200,
            "pushed_at": d.get("pushed_at"),
            "stars": int(d.get("stargazers_count") or 0),
            "is_archived": bool(d.get("archived")),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


@router.post("/refresh")
async def run_shallow_refresh(
    body: EcosystemRefreshBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Incrementally refresh the active set on demand (user ruling 2026-07-10: the periodic cron is retired in favor of manual triggering).

    Probe each repo in the project's active set (top_n by stars) via gh: write status snapshots;
    repos with an updated pushed_at get their Stage 0 shallow scan re-queued; 404/403 are marked deleted/private.
    The weekly emit_event registered by the original `_ensure_ecosystem_weekly_cron` had no consumer,
    and the refresh capability was actually unreachable before — this endpoint is its first real entry point.
    """
    from aiteam.services.ecosystem_refresher import EcosystemRefresher
    from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker

    worker = EcosystemShallowQueueWorker(repo=repo)
    refresher = EcosystemRefresher(repo, worker, gh_fetcher=_default_repo_fetcher)
    r = await refresher.shallow_refresh(
        triggered_by=body.triggered_by or "manual", notes=body.notes
    )
    resp: dict[str, Any] = {
        "success": True,
        "project_id": r.project_id,
        "active_total": r.active_total,
        "refreshed": r.refreshed,
        "skipped_no_diff": r.skipped_no_diff,
        "snapshots_written": r.snapshots_written,
        "marked_deleted": r.marked_deleted,
        "marked_private": r.marked_private,
        "transient_errors": r.transient_errors,
        "errors": r.errors[:20],
        "scan_run_id": r.scan_run_id,
    }
    if r.refreshed:
        resp["hint"] = (
            f"{r.refreshed} repos have new pushes and have been re-queued for shallow scan."
            "To run shallow scans, enable ultracode and orchestrate with a Workflow;"
            "write the results back via ecosystem_apply_shallow_summary (see the skill /os-workflow for the template)."
        )
    return resp


# ============================================================
# Stage D — Tag dictionary + tagger endpoints
# ============================================================


def _tag_to_dict(t: EcosystemTag) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "aliases": t.aliases,
        "category": t.category.value if hasattr(t.category, "value") else t.category,
        "description": t.description,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


class TagApplyBatchRequest(BaseModel):
    repo_ids: list[str] = Field(default_factory=list, description="Leave empty to tag all profiles in the database")
    repo_full_names: list[str] = Field(default_factory=list)
    agent_id: str | None = "ecosystem-tagger"
    limit: int = Field(default=200, ge=1, le=1000)
    replace_auto: bool = Field(
        default=False,
        description=(
            "When True, first delete each repo's old tags with source in {github_topic, auto_rule}, then insert the newly computed results."
            "manual/auto_llm tags are left untouched. Used to clean up stale false positives after a rule upgrade. Defaults to False (append mode) for backward compatibility."
        ),
    )


class TagApplyLLMResultRequest(BaseModel):
    repo_id: str
    tags: list[dict[str, Any]] = Field(
        default_factory=list,
        description='[{"name": str, "confidence": float}, ...]',
    )
    agent_id: str | None = None


class ManualTagRequest(BaseModel):
    repo_id: str
    tag_name: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    agent_id: str | None = None


class LLMDispatchPlanRequest(BaseModel):
    repo_ids: list[str] = Field(default_factory=list)
    agent_template: str = DEFAULT_LLM_AGENT_TEMPLATE
    max_concurrency: int = Field(default=20, ge=1, le=50)


@router.get("/tags")
async def list_tags(
    category: str = Query(default=""),
    limit: int = Query(default=200, le=500),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List the full tag dictionary; can be filtered by category."""
    tags = await repo.list_tags(category=category, limit=limit)
    return {"tags": [_tag_to_dict(t) for t in tags], "total": len(tags)}


@router.post("/tags")
async def upsert_tag(
    payload: dict[str, Any],
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Add or update a tag (unique key: name)."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    category_raw = (payload.get("category") or "").strip()
    try:
        category = EcosystemTagCategory(category_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid category '{category_raw}'. "
                f"Allowed: {[c.value for c in EcosystemTagCategory]}"
            ),
        ) from exc

    existing = await repo.get_tag_by_name(name)
    is_new = existing is None
    tag = EcosystemTag(
        name=name,
        aliases=payload.get("aliases") or [],
        category=category,
        description=payload.get("description") or "",
    )
    if existing is not None:
        tag.id = existing.id
    await repo.upsert_tag(tag)
    saved = await repo.get_tag_by_name(name)
    if saved is None:
        raise HTTPException(status_code=500, detail="tag not persisted")
    out = _tag_to_dict(saved)
    out["created"] = is_new
    return out


@router.get("/repos/{repo_id}/tags")
async def list_repo_tags(
    repo_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List all tag associations of a repo, with tag details."""
    rows = await repo.list_repo_tags(repo_id=repo_id, limit=200)
    out: list[dict[str, Any]] = []
    for r in rows:
        tag = await repo.get_tag(r.tag_id)
        out.append(
            {
                "repo_tag_id": r.id,
                "repo_id": r.repo_id,
                "tag_id": r.tag_id,
                "tag_name": tag.name if tag else None,
                "tag_category": (
                    tag.category.value
                    if tag and hasattr(tag.category, "value")
                    else None
                ),
                "confidence": r.confidence,
                "source": r.source.value if hasattr(r.source, "value") else r.source,
                "agent_id": r.agent_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return {"repo_id": repo_id, "tags": out, "total": len(out)}


async def _resolve_profiles_for_batch(
    repo: StorageRepository,
    repo_ids: list[str],
    repo_full_names: list[str],
    limit: int,
) -> list[EcosystemRepoProfile]:
    profiles: list[EcosystemRepoProfile] = []
    if repo_ids:
        for rid in repo_ids:
            p = await repo.get_ecosystem_profile_by_id(rid)
            if p is not None:
                profiles.append(p)
    elif repo_full_names:
        for name in repo_full_names:
            p = await repo.get_ecosystem_profile(name)
            if p is not None:
                profiles.append(p)
    else:
        rows, _ = await repo.search_ecosystem_profiles_extended(limit=limit)
        profiles = list(rows)
    return profiles


@router.post("/tags/apply")
async def apply_tags_batch(
    req: TagApplyBatchRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Run Layer 1+2 automatic tagging on a batch of repos (GitHub topics + rules).

    - If both repo_ids and repo_full_names are empty, process the first `limit` profiles in the database.
    - Returns the per-repo hit distribution + which repos need the Layer 3 LLM fallback.
    """
    profiles = await _resolve_profiles_for_batch(
        repo, req.repo_ids, req.repo_full_names, req.limit
    )

    repo_dicts = [
        {
            "id": p.id,
            "repo_full_name": p.repo_full_name,
            "name": p.name,
            "owner": p.owner,
            "description": p.description,
            "topics": p.topics,
            "language": p.language,
        }
        for p in profiles
    ]

    tagger = EcosystemTagger(repo)
    stats = await tagger.tag_repos_batch(
        repo_dicts,
        agent_id=req.agent_id,
        replace_auto=req.replace_auto,
    )
    return stats.to_dict()


@router.post("/tags/llm/dispatch_plan")
async def build_llm_dispatch_plan(
    req: LLMDispatchPlanRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Generate a Layer 3 LLM sub-agent dispatch plan for the specified repo set."""
    profiles: list[EcosystemRepoProfile] = []
    for rid in req.repo_ids:
        p = await repo.get_ecosystem_profile_by_id(rid)
        if p is not None:
            profiles.append(p)
    if not profiles:
        return {
            "agent_template": req.agent_template,
            "max_concurrency": req.max_concurrency,
            "total_requested": 0,
            "dispatched": 0,
            "skipped_due_to_limit": 0,
            "dispatch": [],
            # Same plan shape as the real branch - nothing is spawned, so nothing to warn about.
            "template_warnings": [],
            "instructions": "no repos to dispatch",
        }

    repo_dicts = [
        {
            "id": p.id,
            "repo_full_name": p.repo_full_name,
            "description": p.description,
            "topics": p.topics,
            "language": p.language,
        }
        for p in profiles
    ]

    tagger = EcosystemTagger(repo)
    plan = await tagger.build_llm_dispatch_plan(
        repo_dicts,
        agent_template=req.agent_template,
        max_concurrency=req.max_concurrency,
    )
    return plan


@router.post("/tags/llm/result")
async def apply_llm_tag_result(
    req: TagApplyLLMResultRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """A sub-agent submits Layer 3 LLM tagging results."""
    profile = await repo.get_ecosystem_profile_by_id(req.repo_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="repo not found")

    tagger = EcosystemTagger(repo)
    result = await tagger.apply_llm_tags(
        repo_id=req.repo_id,
        repo_full_name=profile.repo_full_name,
        llm_output_tags=req.tags,
        agent_id=req.agent_id,
    )
    return result.to_dict()


@router.post("/tags/manual")
async def manual_tag(
    req: ManualTagRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Manually tag a repo."""
    profile = await repo.get_ecosystem_profile_by_id(req.repo_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="repo not found")

    tagger = EcosystemTagger(repo)
    rt = await tagger.manual_tag(
        repo_id=req.repo_id,
        tag_name=req.tag_name,
        confidence=req.confidence,
        agent_id=req.agent_id,
    )
    if rt is None:
        raise HTTPException(
            status_code=404, detail=f"tag '{req.tag_name}' not registered"
        )
    return {
        "repo_id": rt.repo_id,
        "tag_id": rt.tag_id,
        "tag_name": req.tag_name,
        "confidence": rt.confidence,
        "source": rt.source.value if hasattr(rt.source, "value") else rt.source,
    }


@router.delete("/repos/{repo_id}/tags/{tag_name}")
async def remove_tag(
    repo_id: str,
    tag_name: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Remove a tag association from a repo."""
    tagger = EcosystemTagger(repo)
    removed = await tagger.remove_tag(repo_id, tag_name)
    if not removed:
        raise HTTPException(status_code=404, detail="tag not associated with repo")
    return {"removed": True, "repo_id": repo_id, "tag_name": tag_name}


# ============================================================
# Stage G — summarizer endpoints
# ============================================================


def _get_summarizer(repo: StorageRepository) -> EcosystemSummarizer:
    return EcosystemSummarizer(repo=repo)


@router.get("/summary/weekly")
async def summary_weekly(
    window_days: int = Query(default=7, ge=1, le=90),
    top_movers_limit: int = Query(default=5, ge=1, le=20),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Generate the ecosystem briefing markdown for the past N days.

    Returns {markdown: str, window_days, generated_at}. The MCP layer is responsible for report_save.
    """
    summarizer = _get_summarizer(repo)
    md = await summarizer.weekly_summary(
        window_days=window_days,
        top_movers_limit=top_movers_limit,
    )
    return {
        "markdown": md,
        "window_days": window_days,
        "top_movers_limit": top_movers_limit,
        "generated_at": utc_now().isoformat(),
    }


@router.get("/summary/by_tag")
async def summary_by_tag(
    tag: str = Query(..., min_length=1),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=500),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List the markdown of all repos in a direction, by tag."""
    summarizer = _get_summarizer(repo)
    md = await summarizer.by_tag_summary(
        tag,
        include_archived=include_archived,
        limit=limit,
    )
    return {
        "markdown": md,
        "tag": tag,
        "include_archived": include_archived,
        "generated_at": utc_now().isoformat(),
    }


@router.get("/summary/top_n")
async def summary_top_n(
    category: str = Query(default=""),
    n: int = Query(default=10, ge=1, le=100),
    sort: str = Query(default="stars"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Top N ranking markdown table. sort: stars / pushed_at / scan_freshness."""
    if sort not in TOP_N_SORT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"sort must be one of {list(TOP_N_SORT_OPTIONS)}",
        )
    summarizer = _get_summarizer(repo)
    md = await summarizer.top_n_summary(category=category, n=n, sort=sort)
    return {
        "markdown": md,
        "category": category,
        "n": n,
        "sort": sort,
        "generated_at": utc_now().isoformat(),
    }


@router.get("/summary/health")
async def summary_health(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Platform self-check markdown: repo count / scans / deep scans / tag coverage / inactivation rate."""
    summarizer = _get_summarizer(repo)
    md = await summarizer.health_summary()
    return {
        "markdown": md,
        "generated_at": utc_now().isoformat(),
    }


# ============================================================
# v1.5.0-B Stage 0 shallow-scan queue endpoints
# ============================================================


class ApplyShallowSummaryBody(BaseModel):
    """Payload from a Stage 0 ai-engineer sub-agent reporting its summary."""

    repo_id: str
    shallow_summary: str = ""
    deep_review_id: str | None = None
    error_kind: str = ""
    error_message: str = ""
    http_status: int | None = None
    rate_limit_remaining: int | None = None


def _get_shallow_worker(
    repo: StorageRepository,
):
    """Construct an EcosystemShallowQueueWorker bound to repo's project."""
    from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker

    return EcosystemShallowQueueWorker(repo=repo)


@router.post("/shallow_queue/apply_summary")
async def apply_shallow_summary(
    body: ApplyShallowSummaryBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Apply a Stage 0 shallow summary or report a failure.

    On success path:
      - update_profile_shallow_summary
      - update_deep_review_stage to SHALLOW_DONE
    On failure path:
      - delegate to worker.report_failure (handles classification, profile
        flag updates, and self-learning bookkeeping).
    """
    from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker
    from aiteam.types import EcosystemStageStatus

    worker = EcosystemShallowQueueWorker(repo=repo)

    if body.error_kind:
        decision = await worker.report_failure(
            body.repo_id,
            error_kind=body.error_kind,
            http_status=body.http_status,
            error_message=body.error_message,
            rate_limit_remaining=body.rate_limit_remaining,
            deep_review_id=body.deep_review_id,
        )
        return {
            "success": False,
            "failure_class": decision.failure_class,
            "immediate_retry": decision.immediate_retry,
            "retry_delay_seconds": decision.retry_delay_seconds,
            "marked_deleted": decision.mark_deleted,
            "marked_private": decision.mark_private,
        }

    summary = (body.shallow_summary or "").strip()
    if not summary:
        raise HTTPException(
            status_code=400,
            detail="shallow_summary required when error_kind not set",
        )

    profile = await repo.update_profile_shallow_summary(
        body.repo_id, shallow_summary=summary
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="repo profile not found")

    review = None
    if body.deep_review_id:
        # D5 convergence: stage_status is the single authoritative axis; update_deep_review_stage's
        # choke point derives status=completed, fills in completed_at, and releases the claim,
        # and the v1.6.1 status backfill patch block was deleted along with it.
        review = await repo.update_deep_review_stage(
            body.deep_review_id,
            EcosystemStageStatus.SHALLOW_DONE,
        )

    return {
        "success": True,
        "repo_id": profile.id,
        "shallow_summary_length": len(profile.shallow_summary),
        "stage_status": review.stage_status.value if review else None,
    }


@router.get("/shallow_queue/status")
async def shallow_queue_status(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return Stage 0 queue metrics for the active project."""
    worker = _get_shallow_worker(repo)
    return await worker.queue_status()


@router.post("/shallow_queue/tick")
async def shallow_queue_tick(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Run one Stage 0 worker tick on demand (Leader-driven)."""
    worker = _get_shallow_worker(repo)
    result = await worker.tick()
    return {
        "queued": result.queued,
        "dispatched": result.dispatched,
        "skipped_inflight": result.skipped_inflight,
        "errors": result.errors,
        "intents": [
            {
                "repo_id": i.repo_id,
                "repo_full_name": i.repo_full_name,
                "deep_review_id": i.deep_review_id,
                "prompt": i.prompt,
                "timeout_seconds": i.timeout_seconds,
            }
            for i in result.intents
        ],
    }


# ============================================================
# v1.5.0-C — Stage 1 / 2 / 3 lifecycle trigger routes
# ============================================================


class DeepReviewRequestBatchBody(BaseModel):
    """Stage 1 batch request payload (POST /api/ecosystem/lifecycle/request_batch)."""

    tags: list[str] = Field(default_factory=list)
    min_stars: int | None = None
    limit: int = 20
    research_goal: str = ""


class ApplyArchitectureMdBody(BaseModel):
    """Stage 1 writeback payload."""

    deep_review_id: str
    architecture_md: str = ""
    agent_id: str | None = None
    error_message: str = ""  # when non-empty, enter the architecture_failed path


class TriggerDebateBody(BaseModel):
    """Stage 2 debate trigger payload."""

    repo_ids: list[str] = Field(default_factory=list)
    research_goal: str = ""
    suggested_advocate: str = "backend-architect"
    suggested_critic: str = "code-reviewer"
    suggested_judge: str = "team-lead"


class LinkDebateMeetingBody(BaseModel):
    """Stage 2 link meeting payload (called after debate_start)."""

    review_ids: list[str] = Field(default_factory=list)
    meeting_id: str


class ApplyDebateResultBody(BaseModel):
    """Stage 2 result writeback payload."""

    deep_review_id: str
    risks_md: str = ""
    learnings_md: str = ""
    integration_md: str = ""
    integration_recommendation: str = ""
    agent_id: str | None = None


class MarkAsReferenceBody(BaseModel):
    """Stage 3 reference path payload."""

    deep_review_id: str
    agent_id: str | None = None
    confidence: float = 1.0


class StartIntegrationBody(BaseModel):
    """Stage 3 integrate path — request task payload."""

    deep_review_id: str
    title: str = ""
    description: str = ""
    priority: str = "high"
    horizon: str = "mid"
    extra_tags: list[str] = Field(default_factory=list)


class LinkIntegrationTaskBody(BaseModel):
    """Stage 3 link task payload (called after task_create)."""

    deep_review_id: str
    task_id: str


def _get_lifecycle_service(repo: StorageRepository):
    """Construct an EcosystemLifecycleService bound to repo's project scope."""
    from aiteam.services.ecosystem_lifecycle import EcosystemLifecycleService

    return EcosystemLifecycleService(repo)


@router.post("/lifecycle/request_batch")
async def lifecycle_request_batch(
    body: DeepReviewRequestBatchBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 1: queue a batch of deep_review rows for tag-filtered candidates.

    Returns dispatch intents for backend-architect sub-agents. Leader is
    responsible for actually spawning each agent via the Agent tool.
    """
    service = _get_lifecycle_service(repo)
    try:
        intents = await service.request_deep_review_batch(
            tags=body.tags,
            min_stars=body.min_stars,
            limit=body.limit,
            research_goal=body.research_goal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "dispatched": len(intents),
        "intents": [
            {
                "repo_id": i.repo_id,
                "repo_full_name": i.repo_full_name,
                "deep_review_id": i.deep_review_id,
                "prompt": i.prompt,
                "timeout_seconds": i.timeout_seconds,
                "project_id": i.project_id,
            }
            for i in intents
        ],
    }


@router.post("/lifecycle/apply_architecture_md")
async def lifecycle_apply_architecture_md(
    body: ApplyArchitectureMdBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 1 writeback: agent submits architecture_md or reports failure."""
    from aiteam.types import EcosystemStageStatus

    service = _get_lifecycle_service(repo)

    # Failure path — empty architecture_md + non-empty error_message
    if not body.architecture_md.strip() and body.error_message:
        review = await repo.get_deep_review(body.deep_review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="deep_review not found")
        # D5: only appends a risks_md note; status=failed / completed_at are filled in by the subsequent
        # ARCHITECTURE_FAILED stage advancement derivation.
        await repo.update_deep_review(
            body.deep_review_id,
            risks_md=(review.risks_md or "")
            + f"\n\n[architecture failed] {body.error_message}",
        )
        await repo.update_deep_review_stage(
            body.deep_review_id,
            EcosystemStageStatus.ARCHITECTURE_FAILED,
        )
        return {
            "success": False,
            "stage_status": EcosystemStageStatus.ARCHITECTURE_FAILED.value,
            "error": body.error_message,
        }

    try:
        review = await service.apply_architecture_md(
            body.deep_review_id,
            architecture_md=body.architecture_md,
            agent_id=body.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "stage_status": review.stage_status.value,
        "architecture_md_length": len(review.architecture_md),
    }


@router.post("/lifecycle/trigger_debate")
async def lifecycle_trigger_debate(
    body: TriggerDebateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 2: build a debate dispatch intent (caller must invoke debate_start)."""
    service = _get_lifecycle_service(repo)
    try:
        intent = await service.trigger_debate(
            repo_ids=body.repo_ids,
            research_goal=body.research_goal,
            suggested_advocate=body.suggested_advocate,
            suggested_critic=body.suggested_critic,
            suggested_judge=body.suggested_judge,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "review_ids": intent.review_ids,
        "repo_full_names": intent.repo_full_names,
        "research_goal": intent.research_goal,
        "suggested_topic": intent.suggested_topic,
        "suggested_advocate": intent.suggested_advocate,
        "suggested_critic": intent.suggested_critic,
        "suggested_judge": intent.suggested_judge,
        "project_id": intent.project_id,
        "next_action": (
            "Call debate_start(topic=suggested_topic, advocate=..., critic=..., "
            "judge=...) to create a meeting, then call /lifecycle/link_debate_meeting "
            "to write meeting.id back into review_ids."
        ),
    }


# ============================================================
# v1.7.0: shallow-scan batch management endpoints
# ============================================================


class CreateShallowBatchBody(BaseModel):
    triggered_by: str = "user"  # 'cron' / 'manual' / 'user'
    trigger_reason: str | None = None


class ApproveBatchBody(BaseModel):
    approved_by: str = "user"


@router.post("/shallow_batches")
async def create_shallow_batch(
    body: CreateShallowBatchBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Create a new shallow-scan batch — discover candidate repos and freeze a snapshot; status=pending_approval, awaiting human approval.

    Candidate repo logic: among active repos (is_deleted=False, last_active_status != 'archived'),
    repos whose shallow_summary is empty or stale (not refreshed for > 30 days).
    """
    import json
    import uuid
    from datetime import timedelta

    from aiteam.types import EcosystemShallowBatch

    # One clock, one comparison: every timestamp is UTC and the ORM returns it
    # timezone-aware, so the 30-day cutoff needs no normalisation. (This spot used
    # to carry a hand-rolled zone dance because the DB ran two wall clocks — the
    # ageing here fired 8 hours early on UTC+8 until that was unified.)
    now = utc_now()
    cutoff = now - timedelta(days=30)

    # Discover candidate repos: read all active profiles and filter those needing a shallow scan
    all_profiles = await repo.search_ecosystem_profiles(limit=1000)

    candidates = []
    for p in all_profiles:
        # Exclude deleted / private / archived
        if p.is_deleted or p.is_private_now:
            continue
        if p.last_active_status in ("archived", "manual_archived"):
            continue
        # Candidate condition: no shallow-scan summary, or the summary is over 30 days old
        ts = ensure_utc(p.last_shallow_refreshed_at)
        needs_refresh = not p.shallow_summary or ts is None or ts < cutoff
        if needs_refresh:
            candidates.append(p.id)

    batch = EcosystemShallowBatch(
        id=str(uuid.uuid4()),
        project_id=repo._project_scope or None,
        triggered_by=body.triggered_by,
        trigger_reason=body.trigger_reason,
        candidates_count=len(candidates),
        candidates_snapshot_json=json.dumps(candidates),
        status="pending_approval",
        created_at=now,
        updated_at=now,
    )
    created = await repo.create_shallow_batch(batch)
    return {
        "success": True,
        "batch_id": created.id,
        "candidates_count": created.candidates_count,
        "status": created.status,
        "message": f"Batch created; found {created.candidates_count} candidate repos, awaiting approval",
    }


@router.get("/shallow_batches")
async def list_shallow_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List shallow-scan batches (by created_at descending)."""
    batches, total = await repo.list_shallow_batches(limit=limit, offset=offset)
    return {
        "batches": [_batch_to_dict(b) for b in batches],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/shallow_batches/{batch_id}")
async def get_shallow_batch(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Query the details of a single shallow-scan batch."""
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return {"batch": _batch_to_dict(batch)}


@router.get("/shallow_batches/{batch_id}/items")
async def get_shallow_batch_items(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Query the candidate repo list within a batch."""
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    items = await repo.get_batch_items(batch_id)
    return {
        "batch_id": batch_id,
        "items": items,
        "total": len(items),
    }


@router.post("/shallow_batches/{batch_id}/approve")
async def approve_shallow_batch(
    batch_id: str,
    body: ApproveBatchBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Approve a batch — create DR rows for the candidate repos in the snapshot and **return the dispatch intents**.

    The dispatch intents (including sub-agent prompts) are the product of this chain: approval itself does not make any agent
    run; only once the caller receives ``intents`` can Stage 0 actually be dispatched. The historical implementation here
    hand-rolled ``EcosystemDeepReview`` inserts; the created rows had no ``dispatch_prompt``, and the subsequent
    ``tick()`` then skipped the whole batch because "this repo already has a queued row" — after approval nothing could run,
    and tick's return value was discarded entirely. Now everything is uniformly delegated to
    ``EcosystemShallowQueueWorker.dispatch_batch`` (row creation + prompt rendering + deduplication).
    """
    import json

    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if batch.status != "pending_approval":
        raise HTTPException(
            status_code=400,
            detail=f"batch status is '{batch.status}', only pending_approval can be approved",
        )

    now = utc_now()

    # Read the candidate snapshot. A corrupted snapshot must fail explicitly: silently treating it as empty and approving would advance the batch to running
    # while dispatching no repo at all — equivalent to permanently losing the order.
    candidate_ids: list[str] = []
    if batch.candidates_snapshot_json:
        try:
            candidate_ids = json.loads(batch.candidates_snapshot_json)
        except (ValueError, TypeError) as exc:
            logger.error(
                "approve_shallow_batch: corrupt candidates_snapshot_json batch_id=%s: %s",
                batch_id,
                exc,
            )
            raise HTTPException(
                status_code=500,
                detail=f"batch candidates snapshot is not valid JSON: {exc}",
            ) from exc

    worker = _get_shallow_worker(repo)
    try:
        intents, skipped = await worker.dispatch_batch(
            candidate_ids, batch_id=batch_id
        )
    except Exception as exc:
        logger.exception(
            "approve_shallow_batch: dispatch failed batch_id=%s", batch_id
        )
        raise HTTPException(
            status_code=500, detail=f"failed to dispatch batch: {exc}"
        ) from exc

    if skipped:
        logger.warning(
            "approve_shallow_batch: %d/%d candidates skipped (in-flight or "
            "unavailable) batch_id=%s",
            len(skipped),
            len(candidate_ids),
            batch_id,
        )

    # Update the batch status
    updated = await repo.update_shallow_batch(
        batch_id,
        status="running",
        approved_by=body.approved_by,
        approved_at=now,
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="failed to update batch status")

    return {
        "success": True,
        "batch_id": batch_id,
        "status": "running",
        "dr_created": len(intents),
        "skipped_count": len(skipped),
        "intents": [
            {
                "repo_id": i.repo_id,
                "repo_full_name": i.repo_full_name,
                "deep_review_id": i.deep_review_id,
                "prompt": i.prompt,
                "timeout_seconds": i.timeout_seconds,
            }
            for i in intents
        ],
        "message": (
            f"Batch approved; created {len(intents)} shallow-scan tasks"
            + (f", skipped {len(skipped)} (in-flight or unavailable)" if skipped else "")
            + "; see intents for the dispatch instructions"
        ),
    }


@router.post("/shallow_batches/{batch_id}/cancel")
async def cancel_shallow_batch(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Cancel a batch — mark the DR rows (if any) as cancelled."""
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if batch.status in ("completed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"batch is already '{batch.status}', cannot cancel",
        )

    # Update the batch status
    updated = await repo.update_shallow_batch(batch_id, status="cancelled")
    if updated is None:
        raise HTTPException(status_code=500, detail="failed to cancel batch")

    return {
        "success": True,
        "batch_id": batch_id,
        "status": "cancelled",
        "message": "Batch cancelled",
    }


@router.post("/shallow_batches/{batch_id}/rollback")
async def rollback_shallow_batch(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Roll back a batch — the inverse operation of approve.

    Delete this batch's shallow-scan task rows that have not started yet (stage_status='queued' and not claimed),
    claimed/completed rows are kept; the batch is set to cancelled. Removed repos will be rediscovered the next time a batch is created
    (candidates are computed in real time by the "not refreshed for 30 days" rule).
    """
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    deleted = await repo.delete_unstarted_batch_reviews(batch_id)
    await repo.update_shallow_batch(batch_id, status="cancelled")
    return {
        "success": True,
        "batch_id": batch_id,
        "deleted_queued": deleted,
        "status": "cancelled",
        "message": f"Batch rolled back; removed {deleted} not-yet-started tasks",
    }


def _batch_to_dict(b: Any) -> dict:
    """Convert an EcosystemShallowBatch into a JSON-safe dict."""
    return {
        "id": b.id,
        "project_id": b.project_id,
        "triggered_by": b.triggered_by,
        "trigger_reason": b.trigger_reason,
        "candidates_count": b.candidates_count,
        "status": b.status,
        "approved_by": b.approved_by,
        "approved_at": b.approved_at.isoformat() if b.approved_at else None,
        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
        "new_repos_count": b.new_repos_count,
        "updated_repos_count": b.updated_repos_count,
        "metadata_changed_count": b.metadata_changed_count,
        "failed_count": b.failed_count,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


# ============================================================
# Worker pool claim endpoints (v1.5.3)
# ============================================================


class ShallowQueueClaimBody(BaseModel):
    worker_id: str


class ReviewQueueClaimBody(BaseModel):
    worker_id: str
    min_stars: int = 0


class ApplyQualityReviewBody(BaseModel):
    dr_id: str
    quality_score: int = Field(..., ge=0, le=100)
    quality_notes: str = ""
    recommendation: str = ""


class ReleaseClaimBody(BaseModel):
    dr_id: str
    reason: str = ""


@router.post("/shallow_queue/claim")
async def shallow_queue_claim(
    body: ShallowQueueClaimBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Claim the next repo awaiting shallow scan (stage_status='queued', claimed_by IS NULL).

    Atomic operation: SELECT + UPDATE WHERE claimed_by IS NULL; concurrency-safe.
    v1.7.0: additionally returns repo_full_name/topics/description/owner/stars/last_commit_at,
            saving the worker one ecosystem_repo_get call per repo.
    Returns claimed deep_review row or {"claimed": false} when queue is empty.
    """
    review = await repo.claim_next_shallow_repo(worker_id=body.worker_id)
    if review is None:
        return {"claimed": False}

    # Fetch the repo profile's basic fields
    profile = await repo.get_ecosystem_profile_by_id(review.repo_id)
    extra: dict[str, Any] = {}
    if profile is not None:
        extra = {
            "repo_full_name": profile.repo_full_name,
            "topics": profile.topics,
            "description": profile.description,
            "owner": profile.owner,
            "stars": profile.stars,
            "last_commit_at": profile.last_commit_at.isoformat() if profile.last_commit_at else None,
        }

    return {
        "claimed": True,
        "dr_id": review.id,
        "repo_id": review.repo_id,
        "claimed_by": review.claimed_by,
        "claimed_at": review.claimed_at.isoformat() if review.claimed_at else None,
        "stage_status": review.stage_status.value
        if hasattr(review.stage_status, "value")
        else str(review.stage_status),
        **extra,
    }


@router.post("/review_queue/claim")
async def review_queue_claim(
    body: ReviewQueueClaimBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Claim the next row awaiting quality review (stage_status='shallow_done', quality_score IS NULL, claimed_by IS NULL).

    Also returns the associated repo profile's shallow_summary for the reviewer to assess.
    Returns claimed review + profile or {"claimed": false} when queue is empty.
    """
    result = await repo.claim_next_review_repo(
        worker_id=body.worker_id,
        min_stars=body.min_stars,
    )
    if result is None:
        return {"claimed": False}
    review, profile = result
    return {
        "claimed": True,
        "dr_id": review.id,
        "repo_id": review.repo_id,
        "claimed_by": review.claimed_by,
        "claimed_at": review.claimed_at.isoformat() if review.claimed_at else None,
        "stage_status": review.stage_status.value
        if hasattr(review.stage_status, "value")
        else str(review.stage_status),
        "repo_full_name": profile.repo_full_name,
        "stars": profile.stars,
        "shallow_summary": profile.shallow_summary,
    }


@router.post("/review_queue/apply")
async def review_queue_apply(
    body: ApplyQualityReviewBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Write the quality review result and release the claim lock.

    Writes quality_score / quality_notes / reviewed_by / reviewed_at,
    and clears claimed_by to release the claim lock.
    """
    review = await repo.apply_quality_review(
        dr_id=body.dr_id,
        quality_score=body.quality_score,
        quality_notes=body.quality_notes,
        recommendation=body.recommendation,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "dr_id": review.id,
        "quality_score": review.quality_score,
        "quality_notes": review.quality_notes,
        "reviewed_by": review.reviewed_by,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "claimed_by": review.claimed_by,
    }


@router.post("/claims/release")
async def claims_release(
    body: ReleaseClaimBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Release the claim lock (the worker gives up); clear claimed_by so other workers can claim it again.

    The reason is recorded into quality_notes for traceability.
    """
    review = await repo.release_claim(dr_id=body.dr_id, reason=body.reason)
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "dr_id": review.id,
        "claimed_by": review.claimed_by,
        "quality_notes": review.quality_notes,
    }


@router.post("/lifecycle/link_debate_meeting")
async def lifecycle_link_debate_meeting(
    body: LinkDebateMeetingBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 2 helper: link meeting_id to review rows after debate_start."""
    service = _get_lifecycle_service(repo)
    updated = await service.link_debate_meeting(
        review_ids=body.review_ids,
        meeting_id=body.meeting_id,
    )
    return {
        "success": True,
        "linked": updated,
        "meeting_id": body.meeting_id,
    }


@router.post("/lifecycle/apply_debate_result")
async def lifecycle_apply_debate_result(
    body: ApplyDebateResultBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 2 writeback: agent submits debate conclusion → debated."""
    service = _get_lifecycle_service(repo)
    try:
        review = await service.apply_debate_result(
            body.deep_review_id,
            risks_md=body.risks_md,
            learnings_md=body.learnings_md,
            integration_md=body.integration_md,
            integration_recommendation=body.integration_recommendation,
            agent_id=body.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "stage_status": review.stage_status.value,
        "debated_at": review.debated_at.isoformat() if review.debated_at else None,
    }


@router.post("/lifecycle/mark_as_reference")
async def lifecycle_mark_as_reference(
    body: MarkAsReferenceBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 3 reference: add the lifecycle:reference tag + advance to referenced."""
    service = _get_lifecycle_service(repo)
    review = await service.mark_as_reference(
        body.deep_review_id,
        agent_id=body.agent_id,
        confidence=body.confidence,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "stage_status": review.stage_status.value,
        "stage3_completed_at": (
            review.stage3_completed_at.isoformat()
            if review.stage3_completed_at
            else None
        ),
    }


@router.post("/lifecycle/start_integration")
async def lifecycle_start_integration(
    body: StartIntegrationBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 3 integrate: build a task dispatch intent."""
    service = _get_lifecycle_service(repo)
    try:
        intent = await service.start_integration(
            body.deep_review_id,
            title=body.title,
            description=body.description,
            priority=body.priority,
            horizon=body.horizon,
            extra_tags=body.extra_tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "review_id": intent.review_id,
        "repo_id": intent.repo_id,
        "repo_full_name": intent.repo_full_name,
        "task_payload": {
            "title": intent.title,
            "description": intent.description,
            "priority": intent.priority,
            "horizon": intent.horizon,
            "tags": intent.tags,
        },
        "project_id": intent.project_id,
        "next_action": (
            "Call POST /api/projects/{project_id}/tasks to create a task, then call "
            "/lifecycle/link_integration_task to write task.id back into the review."
        ),
    }


@router.post("/lifecycle/link_integration_task")
async def lifecycle_link_integration_task(
    body: LinkIntegrationTaskBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 3 helper: link task_id to review row after task_create."""
    service = _get_lifecycle_service(repo)
    review = await service.link_integration_task(
        deep_review_id=body.deep_review_id,
        task_id=body.task_id,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "integration_task_id": review.integration_task_id,
        "stage_status": review.stage_status.value,
    }


# ============================================================
# Project Settings (v1.5.0-E decision 12.1: the Ecosystem settings tab of the project details page)
# ============================================================


class ProjectSettingsBody(BaseModel):
    """Project-level ecosystem configuration (PUT input)."""

    min_stars: int = Field(default=1000, ge=0)
    top_n: int = Field(default=200, ge=1, le=1000)
    refresh_interval_days: int = Field(default=7, ge=1, le=90)
    auto_shallow_on_archive: bool = True
    focus_topics: list[str] = Field(default_factory=list)
    focus_languages: list[str] = Field(default_factory=list)
    shallow_concurrency: int = Field(default=5, ge=1, le=20)
    deep_concurrency: int = Field(default=3, ge=1, le=10)
    # v1.6.1 Phase 2: migrated from scan_profile.alert_thresholds
    alert_max_new_per_scan: int = Field(default=50, ge=1, le=10000)


def _settings_to_dict(s: Any) -> dict[str, Any]:
    """Serialize EcosystemProjectSettings for API."""
    return {
        "project_id": s.project_id,
        "min_stars": s.min_stars,
        "top_n": s.top_n,
        "refresh_interval_days": s.refresh_interval_days,
        "auto_shallow_on_archive": s.auto_shallow_on_archive,
        "focus_topics": s.focus_topics,
        "focus_languages": s.focus_languages,
        "shallow_concurrency": s.shallow_concurrency,
        "deep_concurrency": s.deep_concurrency,
        "alert_max_new_per_scan": getattr(s, "alert_max_new_per_scan", 50),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/projects/{project_id}/settings")
async def get_project_settings(
    project_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get a project's ecosystem configuration (create it with defaults if it does not exist)."""
    settings = await repo.ensure_ecosystem_project_settings(project_id)
    return _settings_to_dict(settings)


@router.put("/projects/{project_id}/settings")
async def update_project_settings(
    project_id: str,
    body: ProjectSettingsBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Update a project's ecosystem configuration."""
    from aiteam.types import EcosystemProjectSettings as _Settings

    payload = _Settings(
        project_id=project_id,
        min_stars=body.min_stars,
        top_n=body.top_n,
        refresh_interval_days=body.refresh_interval_days,
        auto_shallow_on_archive=body.auto_shallow_on_archive,
        focus_topics=body.focus_topics,
        focus_languages=body.focus_languages,
        shallow_concurrency=body.shallow_concurrency,
        deep_concurrency=body.deep_concurrency,
        alert_max_new_per_scan=body.alert_max_new_per_scan,
    )
    saved = await repo.upsert_ecosystem_project_settings(payload)
    return _settings_to_dict(saved)


# ============================================================
# Failed repo retry (v1.5.0-E decision: the failed-repo immediate-retry button)
# ============================================================


class RetryFailedRepoBody(BaseModel):
    """Reset a repo's failure state to trigger the next round of Stage 0 shallow scan."""

    reason: str = "manual_retry"


@router.post("/profiles/{repo_id}/retry")
async def retry_failed_repo(
    repo_id: str,
    body: RetryFailedRepoBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Reset fetch_failure_count + last_fetch_error so the repo re-enters the shallow-scan queue.

    Used by the frontend "Retry now" button.
    """
    profile = await repo.get_ecosystem_profile_by_id(repo_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="ecosystem repo not found")

    # Clear the failure count + error message (do not touch is_deleted/is_private_now; the scanner re-determines them)
    profile.fetch_failure_count = 0
    profile.last_fetch_error = ""
    await repo.upsert_ecosystem_profile(profile)
    return {
        "success": True,
        "repo_id": repo_id,
        "repo_full_name": profile.repo_full_name,
        "reason": body.reason,
        "next_action": "The Stage 0 shallow-scan queue will re-dispatch an agent on the next tick",
    }


# ============================================================
# v1.6.0 P0.2: DataSource + ScanProfile + QuickSetup endpoints
# ============================================================


def _get_project_id_from_repo(repo: StorageRepository, request: Any = None) -> str:
    """Extract project_id from repository scope."""
    return repo._project_scope or ""


class DataSourceCreateBody(BaseModel):
    kind: str
    name: str
    config: dict = Field(default_factory=dict)


class DataSourceUpdateBody(BaseModel):
    name: str | None = None
    config: dict | None = None
    enabled: bool | None = None


class ScanProfileUpdateBody(BaseModel):
    profile: dict


class QuickSetupBody(BaseModel):
    sources: list[str] = Field(default=["github"])
    queries: list[str] = Field(default_factory=list)
    use_defaults: bool = True
    custom_profile: dict | None = None


class IndexUpdateBody(BaseModel):
    dry_run: bool = True


@router.post("/data_sources")
async def create_data_source(
    body: DataSourceCreateBody,
    request: Any = Depends(lambda: None),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Create a new data source configuration for the current project."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    try:
        kind = DataSourceKind(body.kind)
    except ValueError:
        valid = [k.value for k in DataSourceKind]
        raise HTTPException(
            status_code=422, detail=f"Invalid kind '{body.kind}'. Valid: {valid}"
        )

    ds = await repo.create_data_source(
        project_id=project_id,
        kind=kind,
        name=body.name,
        config=body.config,
    )
    return {
        "success": True,
        "data_source": {
            "id": ds.id,
            "project_id": ds.project_id,
            "kind": ds.kind.value,
            "name": ds.name,
            "config": ds.config,
            "enabled": ds.enabled,
            "version": ds.version,
            "created_at": ds.created_at.isoformat(),
        },
    }


@router.get("/data_sources")
async def list_data_sources(
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List all data sources for the current project (P1.B: paginated, config truncated).

    config field returns only top-level keys (not full JSON) to prevent token explosion.
    Use GET /data_sources/{id} for full config.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    sources = await repo.list_data_sources(project_id)
    paged = sources[offset: offset + limit]
    return {
        "success": True,
        "data_sources": [
            {
                "id": ds.id,
                "project_id": ds.project_id,
                "kind": ds.kind.value,
                "name": ds.name,
                # P1.B: return only config key names, not full JSON
                "config_keys": list((ds.config or {}).keys()),
                "queries_count": len((ds.config or {}).get("queries", [])),
                "enabled": ds.enabled,
                "version": ds.version,
                "created_at": ds.created_at.isoformat(),
            }
            for ds in paged
        ],
        "total": len(sources),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < len(sources),
    }


@router.put("/data_sources/{ds_id}")
async def update_data_source(
    ds_id: str,
    body: DataSourceUpdateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Update a data source configuration."""
    from aiteam.api.exceptions import NotFoundError

    try:
        ds = await repo.update_data_source(
            ds_id,
            name=body.name,
            config=body.config,
            enabled=body.enabled,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"DataSource {ds_id} not found")

    return {
        "success": True,
        "data_source": {
            "id": ds.id,
            "project_id": ds.project_id,
            "kind": ds.kind.value,
            "name": ds.name,
            "config": ds.config,
            "enabled": ds.enabled,
            "version": ds.version,
            "updated_at": ds.updated_at.isoformat(),
        },
    }


@router.get("/scan_profile")
async def get_scan_profile(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get the active scan profile for the current project.

    Returns the default profile with is_default=True when no profile is configured.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profile = await repo.get_active_scan_profile(project_id)
    if profile is None:
        return {
            "success": True,
            "is_default": True,
            "scan_profile": {
                "id": None,
                "project_id": project_id,
                "version": 0,
                "profile": _DEFAULT_SCAN_PROFILE,
                "is_active": True,
                "created_at": None,
            },
        }

    return {
        "success": True,
        "is_default": False,
        "scan_profile": {
            "id": profile.id,
            "project_id": profile.project_id,
            "version": profile.version,
            "profile": profile.profile,
            "is_active": profile.is_active,
            "created_at": profile.created_at.isoformat(),
        },
    }


@router.put("/scan_profile")
async def update_scan_profile(
    body: ScanProfileUpdateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Create a new scan profile version (old version is deactivated)."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profile = await repo.create_or_update_scan_profile(project_id, body.profile)
    return {
        "success": True,
        "scan_profile": {
            "id": profile.id,
            "project_id": profile.project_id,
            "version": profile.version,
            "profile": profile.profile,
            "is_active": profile.is_active,
            "created_at": profile.created_at.isoformat(),
        },
    }


@router.post("/quick_setup")
async def quick_setup(
    body: QuickSetupBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Minimal 3-question setup wizard — creates data sources + scan profile in one call."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    created_sources = []
    for source_kind_str in body.sources:
        try:
            kind = DataSourceKind(source_kind_str)
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"Unknown source kind: {source_kind_str}"
            )
        ds_config: dict = {}
        if body.queries:
            ds_config["queries"] = body.queries
        ds = await repo.create_data_source(
            project_id=project_id,
            kind=kind,
            name=f"{kind.value} default",
            config=ds_config,
        )
        created_sources.append(ds.id)

    # Build profile: use defaults or merge with custom overrides
    profile_dict = dict(_DEFAULT_SCAN_PROFILE)
    if not body.use_defaults and body.custom_profile:
        profile_dict.update(body.custom_profile)

    profile = await repo.create_or_update_scan_profile(project_id, profile_dict)

    return {
        "success": True,
        "data_source_ids": created_sources,
        "scan_profile_id": profile.id,
        "scan_profile_version": profile.version,
        "next_step": "Call POST /api/ecosystem/index_update?dry_run=true to preview changes",
    }


@router.post("/index_update")
async def index_update(
    body: IndexUpdateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Trigger a real ecosystem index update scan with diff calculation.

    Flow:
      1. Resolve scan config from ecosystem_project_settings.
      2. Check gh CLI auth status — return missing_setup when not logged in.
      3. Run EcosystemScanner over the configured queries (GitHub, single source).
      4. Classify each fresh repo (stars gate + archived/manual_status).
      5. Diff against DB: new / updated / archived-flip / absent-from-query.
      6. Check the alert threshold — stop + return alert when exceeded.
      7. dry_run=False: upsert profiles + write index_diffs + write status_changes.
    """
    import subprocess

    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    # ── Step 1: resolve scan config from settings ─────────────────────────────
    # After v1.6.1 rejected multi-source expansion, the data_sources / scan_profiles tables were zeroed out (their write entry points
    # had long been no-ops), yet this endpoint still hard-validated their existence, so it forever returned only missing_setup —
    # dragging down index_diff_latest / diff / queries_recap, the downstream consumers of its output,
    # with it. The configuration source of truth is changed to ecosystem_project_settings (Q5 ruling, consistent with the single-source decision):
    #   min_stars               → star threshold for archiving
    #   alert_max_new_per_scan  → per-scan new-additions alert threshold (migrated from scan_profile in v1.6.1)
    #   focus_topics            → query set; when empty, falls back to DEFAULT_QUERIES
    settings = await repo.ensure_ecosystem_project_settings(project_id)

    # ── Step 2: check gh CLI auth ─────────────────────────────────────────────
    try:
        auth_check = await asyncio.to_thread(
            subprocess.run,
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if auth_check.returncode != 0:
            return {
                "success": False,
                "dry_run": body.dry_run,
                "missing_setup": ["gh_auth"],
                "message": (
                    "GitHub CLI is not authenticated. "
                    "Run `gh auth login` then retry."
                ),
            }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {
            "success": False,
            "dry_run": body.dry_run,
            "missing_setup": ["gh_cli"],
            "message": "GitHub CLI (gh) not found. Install from https://cli.github.com/ and run `gh auth login`.",
        }

    # ── Step 3a: capture pre-scan state for diff calculation ──────────────────
    # Must be done BEFORE scanner upserts new repos, so we can identify truly new ones.
    pre_scan_profiles, _ = await repo.search_ecosystem_profiles_extended(
        limit=5000,
        offset=0,
    )
    pre_scan_map: dict[str, EcosystemRepoProfile] = {
        p.repo_full_name: p for p in pre_scan_profiles
    }

    # ── Step 3b: run the GitHub scanner ──────────────────────────────────────
    from aiteam.services.ecosystem_scanner import DEFAULT_QUERIES

    min_stars_gh = settings.min_stars
    max_new_per_scan = settings.alert_max_new_per_scan or 50

    # focus_topics are stored as bare topic names; empty list = scan the built-in
    # Claude-ecosystem query set.
    scanner_queries: list[tuple[str, tuple[str, ...]]] = [
        ("", (topic,)) for topic in settings.focus_topics if topic
    ] or list(DEFAULT_QUERIES)

    all_fresh: dict[str, dict[str, Any]] = {}  # repo_full_name → repo_dict from scanner

    if not body.dry_run:
        # Real run: let scanner upsert into ecosystem_repo_profiles
        filter_cfg = FilterConfig(min_stars=min_stars_gh)
        scanner = EcosystemScanner(
            repo=repo,
            gh_search=default_gh_search,
            config=filter_cfg,
            project_id=project_id,
        )
        await scanner.scan(
            strategy=EcosystemScanStrategy.FULL,
            queries=tuple(scanner_queries),
            triggered_by="index_update",
        )

    # Collect fresh repo list via gh_search (for both dry_run and real run).
    # dry_run: this is the ONLY data collection path — scanner.scan is skipped.
    # real run: scanner already upserted; we re-fetch to build all_fresh for diff.
    query_errors: list[str] = []
    for keyword, topics in scanner_queries:
        try:
            items = await default_gh_search(keyword, min_stars_gh, list(topics))
            for item in items:
                fn = item.get("repo_full_name", "")
                if fn and fn not in all_fresh:
                    all_fresh[fn] = item
        except Exception as exc:
            # One bad query must not sink the whole scan, but a silent swallow
            # used to make a half-empty diff indistinguishable from a real one.
            logger.warning(
                "index_update: query keyword=%r topics=%s failed: %s",
                keyword,
                list(topics),
                exc,
            )
            query_errors.append(f"{keyword or list(topics)}: {exc}")

    # ── Step 4: classify active status (P1.A simplified) ──────────────────────
    # Stars-only gate: repos in all_fresh already passed min_stars filter.
    # Status logic: github_archived=True → archived; manual_status='no_value' → manual_archived
    # Everything else → active (permanent; no rank eviction, no pushed_at stale).
    total = len(all_fresh)

    # repo_full_name → computed_status
    fresh_statuses: dict[str, str] = {}
    for fn, repo_data in all_fresh.items():
        stars = repo_data.get("stars", 0)
        if stars < min_stars_gh:
            # fetcher should have pre-filtered, but guard here
            fresh_statuses[fn] = "not_collected"
            continue
        if repo_data.get("is_archived", False):
            fresh_statuses[fn] = "archived"
        else:
            fresh_statuses[fn] = "active"

    # ── Step 5: diff against DB ───────────────────────────────────────────────
    existing_map = pre_scan_map

    new_repos: list[str] = []
    updated_repos: list[str] = []
    github_archived_changed: list[str] = []
    removed_from_query: list[str] = []  # in DB but not in this scan — preserve, just log
    status_changes: list[EcosystemStatusChange] = []

    for fn, computed_status in fresh_statuses.items():
        if computed_status == "not_collected":
            continue
        existing = existing_map.get(fn)

        if existing is None:
            # Truly new repo
            new_repos.append(fn)
            if not body.dry_run:
                upserted = await repo.get_ecosystem_profile(fn, project_id=project_id)
                if upserted:
                    # Determine effective status: manual_status takes priority
                    if upserted.manual_status == "no_value":
                        effective_status = "manual_archived"
                    elif upserted.manual_status == "pinned":
                        effective_status = "active"
                    else:
                        effective_status = computed_status
                    await repo.update_repo_active_status(upserted.id, new_status=effective_status)
                    status_changes.append(EcosystemStatusChange(
                        repo_id=upserted.id,
                        project_id=project_id,
                        from_status=None,
                        to_status=effective_status,
                        reason="new_repo",
                    ))
        else:
            updated_repos.append(fn)
            prev_status = existing.last_active_status

            # Determine effective status: manual_status takes priority
            if existing.manual_status == "no_value":
                effective_status = "manual_archived"
            elif existing.manual_status == "pinned":
                # Pinned repos stay active regardless of scan result
                effective_status = "active"
            else:
                effective_status = computed_status

            # Track GitHub archived status changes
            if effective_status == "archived" and prev_status != "archived":
                github_archived_changed.append(fn)
            elif effective_status != "archived" and prev_status == "archived":
                github_archived_changed.append(fn)  # unarchived

            if effective_status != prev_status:
                if not body.dry_run:
                    await repo.update_repo_active_status(existing.id, new_status=effective_status)
                    status_changes.append(EcosystemStatusChange(
                        repo_id=existing.id,
                        project_id=project_id,
                        from_status=prev_status,
                        to_status=effective_status,
                        reason="github_archived_change" if "archived" in (effective_status, prev_status or "")
                               else "status_change",
                    ))

    # Repos in DB but absent from this scan → preserve status permanently (P1.A)
    # P1.C-2: pinned repos are excluded from removed_from_query count
    fresh_fn_set = set(fn for fn, s in fresh_statuses.items() if s != "not_collected")
    for fn, existing in existing_map.items():
        if fn not in fresh_fn_set:
            if existing.manual_status == "pinned":
                # Pinned repos never count as removed — they stay active regardless
                pass
            else:
                removed_from_query.append(fn)
            # No status change — repos are permanent once admitted

    # ── Step 6: check alert thresholds ────────────────────────────────────────
    alerted = False
    alert_message = ""
    if len(new_repos) > max_new_per_scan:
        alerted = True
        alert_message = (
            f"Alert: {len(new_repos)} new repos exceeds max_new_per_scan={max_new_per_scan}. "
            "No changes committed. Raise alert_max_new_per_scan in the project's "
            "ecosystem settings, or re-run with dry_run=False once the list is reviewed."
        )

    if alerted:
        diff = EcosystemIndexDiff(
            project_id=project_id,
            diff_type="incremental",
            new_count=len(new_repos),
            reactivated_count=0,
            deactivated_count=0,
            stale_count=0,
            archived_count=0,
            github_archived_changed_count=len(github_archived_changed),
            removed_from_query_count=len(removed_from_query),
            details_json={
                "new": new_repos[:50],
                "github_archived_changed": github_archived_changed[:50],
                "removed_from_query": removed_from_query[:50],
            },
            markdown_summary=alert_message,
            alerted=True,
        )
        if not body.dry_run:
            await repo.create_index_diff(diff)
        return {
            "success": True,
            "dry_run": body.dry_run,
            "alerted": True,
            "message": alert_message,
            "diff": {
                "new_count": diff.new_count,
                "updated_count": len(updated_repos),
                "github_archived_changed_count": diff.github_archived_changed_count,
                "removed_from_query_count": diff.removed_from_query_count,
            },
        }

    # ── Step 7: persist diff + status changes (dry_run=False only) ────────────
    markdown_summary = _build_diff_markdown_p1(
        new_repos=new_repos,
        github_archived_changed=github_archived_changed,
        removed_from_query=removed_from_query,
        dry_run=body.dry_run,
        total_scanned=total,
        min_stars=min_stars_gh,
        query_count=len(scanner_queries),
    )

    diff = EcosystemIndexDiff(
        project_id=project_id,
        diff_type="initial" if not existing_map else "incremental",
        new_count=len(new_repos),
        reactivated_count=0,
        deactivated_count=0,
        stale_count=0,
        archived_count=0,
        github_archived_changed_count=len(github_archived_changed),
        removed_from_query_count=len(removed_from_query),
        details_json={
            "new": new_repos[:200],
            "github_archived_changed": github_archived_changed[:200],
            "removed_from_query": removed_from_query[:200],
        },
        markdown_summary=markdown_summary,
        alerted=False,
    )

    if not body.dry_run:
        await repo.create_index_diff(diff)
        if status_changes:
            await repo.bulk_create_status_changes(status_changes)
            # v1.6.0: also write status_changed events to ecosystem_repo_events
            status_events = [
                EcosystemRepoEvent(
                    repo_id=sc.repo_id,
                    project_id=sc.project_id,
                    event_type="status_changed",
                    payload_json={"from": sc.from_status, "to": sc.to_status},
                    source="api",
                    from_status=sc.from_status,
                    to_status=sc.to_status,
                    reason=sc.reason or None,
                )
                for sc in status_changes
            ]
            if status_events:
                await repo.bulk_create_repo_events(status_events)

    diff_summary = (
        f"{diff.new_count} new, {len(updated_repos)} updated, "
        f"{diff.github_archived_changed_count} archive changes"
    )
    return {
        "success": True,
        "dry_run": body.dry_run,
        "alerted": False,
        "min_stars": min_stars_gh,
        "query_count": len(scanner_queries),
        "query_errors": query_errors,
        "total_scanned": total,
        "diff": {
            "id": diff.id,
            "new_count": diff.new_count,
            "updated_count": len(updated_repos),
            "github_archived_changed_count": diff.github_archived_changed_count,
            "removed_from_query_count": diff.removed_from_query_count,
            "markdown_summary": diff.markdown_summary,
            "summary": diff_summary,
        },
        "message": (
            "Dry-run complete — no changes written." if body.dry_run
            else f"Index updated: {diff.new_count} new, {len(updated_repos)} updated metadata, "
                 f"{diff.archived_count} github_archived changes."
        ),
    }


def _build_diff_markdown_p1(
    new_repos: list[str],
    github_archived_changed: list[str],
    removed_from_query: list[str],
    dry_run: bool,
    total_scanned: int,
    min_stars: int,
    query_count: int,
) -> str:
    """Build human-readable markdown summary for P1.A simplified diff."""
    mode = "Dry-run preview" if dry_run else "Index update"
    lines = [
        f"## {mode} — min_stars {min_stars}, {query_count} queries",
        "",
        f"Total repos in fresh scan: **{total_scanned}**",
        "",
        "| Category | Count |",
        "|----------|-------|",
        f"| New | {len(new_repos)} |",
        f"| GitHub archived status changed | {len(github_archived_changed)} |",
        f"| Absent from this query (preserved) | {len(removed_from_query)} |",
    ]
    if new_repos:
        lines += ["", "### New repos (first 10)"]
        for fn in new_repos[:10]:
            lines.append(f"- {fn}")
    if github_archived_changed:
        lines += ["", "### GitHub archived status changed (first 10)"]
        for fn in github_archived_changed[:10]:
            lines.append(f"- {fn}")
    return "\n".join(lines)


@router.get("/index_diffs/latest")
async def get_latest_index_diff(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return the most recent index diff for the current project."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    diff = await repo.get_latest_index_diff(project_id)
    if diff is None:
        return {"success": True, "diff": None, "message": "No index diffs found yet."}

    return {
        "success": True,
        "diff": {
            "id": diff.id,
            "diff_type": diff.diff_type,
            "new_count": diff.new_count,
            "reactivated_count": diff.reactivated_count,
            "deactivated_count": diff.deactivated_count,
            "stale_count": diff.stale_count,
            "archived_count": diff.archived_count,
            # new fields (P1 hotfix): read new columns, fallback to old for pre-hotfix rows
            "github_archived_changed_count": diff.github_archived_changed_count or diff.archived_count,
            "removed_from_query_count": diff.removed_from_query_count,
            "markdown_summary": diff.markdown_summary,
            "alerted": diff.alerted,
            "generated_at": diff.generated_at.isoformat(),
        },
    }


@router.get("/index_diffs/history")
async def get_index_diff_history(
    limit: int = Query(default=10, ge=1, le=20),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return recent index diffs for the current project (P1.B: details truncated to first 5).

    Full details_json available in /index_diffs/latest.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    diffs = await repo.list_index_diffs(project_id, limit=limit)

    def _truncate_details(details: dict | None) -> dict:
        if not details:
            return {}
        return {k: v[:5] if isinstance(v, list) else v for k, v in details.items()}

    return {
        "success": True,
        "diffs": [
            {
                "id": d.id,
                "diff_type": d.diff_type,
                "new_count": d.new_count,
                "reactivated_count": d.reactivated_count,
                "deactivated_count": d.deactivated_count,
                "stale_count": d.stale_count,
                "archived_count": d.archived_count,
                "alerted": d.alerted,
                "generated_at": d.generated_at.isoformat(),
                # P1.B: only first 5 items per category to prevent token explosion
                "details_summary": _truncate_details(d.details_json),
            }
            for d in diffs
        ],
        "total": len(diffs),
    }


@router.get("/repos/{repo_id}/events")
async def get_repo_events(
    repo_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return event history for a single repo, newest first (v1.6.0 event sourcing).

    Returns:
        {success: True, repo_id, events: [{id, event_type, payload_json, source, triggered_at, ...}], total}
    """
    events = await repo.list_repo_events(repo_id, limit=limit)
    return {
        "success": True,
        "repo_id": repo_id,
        "events": [
            {
                "id": ev.id,
                "event_type": ev.event_type,
                "payload_json": ev.payload_json,
                "source": ev.source,
                "scan_run_id": ev.scan_run_id,
                "from_status": ev.from_status,
                "to_status": ev.to_status,
                "reason": ev.reason,
                "triggered_at": ev.triggered_at.isoformat(),
            }
            for ev in events
        ],
        "total": len(events),
    }


def _event_to_summary(ev: EcosystemRepoEvent) -> str:
    """Translate event_type + payload into a single human-readable sentence."""
    t = ev.event_type
    p = ev.payload_json or {}
    if t == "discovered":
        return "First-archival shallow scan"
    if t == "rescanned":
        return "Shallow scan: no change"
    if t == "topics_changed":
        before = p.get("before", [])
        after = p.get("after", [])
        return f"topics changed: {len(before)} → {len(after)}"
    if t == "stars_jumped":
        before = p.get("before", p.get("old_stars", "?"))
        after = p.get("after", p.get("new_stars", "?"))
        pct = p.get("pct", "")
        pct_str = f" (+{pct}%)" if pct else ""
        return f"stars: {before} → {after}{pct_str}"
    if t == "status_changed":
        from_s = p.get("from", ev.from_status or "?")
        to_s = p.get("to", ev.to_status or "?")
        return f"{from_s} → {to_s}"
    if t == "manual_pinned":
        return "Manually locked"
    if t == "manual_unpinned":
        return "Manual lock removed"
    if t == "archived":
        return "Archived"
    if t == "removed_from_query":
        return "Removed from query results"
    return t


@router.get("/repos/{repo_id}/scan_history")
async def get_repo_scan_history(
    repo_id: str,
    limit: int = Query(default=50, le=200),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Merge events + deep_reviews in reverse chronological order and return the scan/research-history timeline.

    Returns:
        {
          success, repo_id, total,
          entries: [
            {kind: 'event'|'deep_review', timestamp, type, summary, payload, expandable_md}
          ]
        }
    """
    events = await repo.list_repo_events(repo_id, limit=limit * 2)
    deep_reviews = await repo.list_deep_reviews(repo_id=repo_id, limit=limit * 2)

    entries: list[dict[str, Any]] = []

    for ev in events:
        entries.append({
            "kind": "event",
            "type": ev.event_type,
            "timestamp": ev.triggered_at.isoformat(),
            "source": ev.source,
            "scan_run_id": ev.scan_run_id,
            "payload": ev.payload_json,
            "summary": _event_to_summary(ev),
        })

    for dr in deep_reviews:
        # v1.6.0 fix: Skip the v1.5 stage-tracking empty placeholder rows (no actual content, only stage='shallow_done' recorded)
        # These rows are stage markers automatically created when a v1.5 shallow scan completes, not real deep scans. All 265 old repos have such rows.
        has_real_content = bool(
            (dr.summary_md or "").strip()
            or (dr.architecture_md or "").strip()
            or (dr.risks_md or "").strip()
            or (dr.learnings_md or "").strip()
            or (dr.integration_md or "").strip()
        )
        if not has_real_content:
            continue

        ts = (dr.completed_at or dr.created_at)
        stage_val = (
            dr.stage_status.value if hasattr(dr.stage_status, "value") else (dr.stage_status or "unknown")
        )
        # v1.6.0 fix: Distinguish shallow-scan/deep-scan semantics by stage; no longer call everything a "deep scan"
        # shallow_done stage (only summary_md has content) → shallow-scan record
        # architecture_done and later stages (with architecture_md etc.) → deep-scan record
        is_deep = bool((dr.architecture_md or "").strip())
        kind_label = "Deep scan" if is_deep else "Shallow scan"
        entry_kind = "deep_review" if is_deep else "shallow_scan"

        entries.append({
            "kind": entry_kind,
            "type": f"{entry_kind}_{stage_val}",
            "timestamp": ts.isoformat() if ts else dr.created_at.isoformat(),
            "stage_status": stage_val,
            "review_id": dr.id,
            "summary": f"{kind_label} ({stage_val})",
            "expandable_md": {
                "summary": dr.summary_md or "",
                "architecture": dr.architecture_md or "",
                "risks": dr.risks_md or "",
                "learnings": dr.learnings_md or "",
                "integration": dr.integration_md or "",
            },
            "integration_recommendation": (
                dr.integration_recommendation.value
                if hasattr(dr.integration_recommendation, "value")
                else dr.integration_recommendation
            ),
        })

    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    trimmed = entries[:limit]

    return {
        "success": True,
        "repo_id": repo_id,
        "total": len(trimmed),
        "entries": trimmed,
    }


@router.get("/diff")
async def get_diff_period(
    from_date: str = Query(..., alias="from", description="ISO date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="ISO date YYYY-MM-DD"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return a time-period diff computed dynamically from the events table (v1.6.0 event sourcing).

    Groups events by event_type to produce new / topics_changed / stars_jumped / status_changed counts.

    Returns:
        {success, from, to, summary: {new, topics_changed, stars_jumped, status_changed, ...},
         events_count, events_by_type: {event_type: count}}
    """
    from datetime import datetime

    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    try:
        from_dt = datetime.fromisoformat(from_date).replace(tzinfo=UTC)
        to_dt = datetime.fromisoformat(to_date).replace(hour=23, minute=59, second=59, tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")

    if from_dt > to_dt:
        raise HTTPException(status_code=400, detail="from must be <= to")

    events = await repo.query_events_in_period(project_id, from_dt, to_dt)

    by_type: dict[str, int] = {}
    for ev in events:
        by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1

    return {
        "success": True,
        "from": from_date,
        "to": to_date,
        "summary": {
            "new": by_type.get("discovered", 0),
            "topics_changed": by_type.get("topics_changed", 0),
            "stars_jumped": by_type.get("stars_jumped", 0),
            "status_changed": by_type.get("status_changed", 0),
            "archived": by_type.get("archived", 0),
            "manual_pinned": by_type.get("manual_pinned", 0),
            "manual_unpinned": by_type.get("manual_unpinned", 0),
        },
        "events_count": len(events),
        "events_by_type": by_type,
    }


@router.get("/queries_recap")
async def get_queries_recap(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return a recap of all search queries that have discovered repos in this project.

    Scans discovered_via_queries across all profiles and aggregates counts.

    Returns:
        {queries: list[str], by_query: {query: count}, total_repos: N}
    """

    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profiles, total_repos = await repo.search_ecosystem_profiles_extended(
        limit=5000,
        offset=0,
    )

    by_query: dict[str, int] = {}
    for p in profiles:
        for q in p.discovered_via_queries:
            by_query[q] = by_query.get(q, 0) + 1

    queries_sorted = sorted(by_query.keys(), key=lambda q: by_query[q], reverse=True)

    return {
        "success": True,
        "queries": queries_sorted,
        "by_query": by_query,
        "total_repos": total_repos,
        "repos_with_query_data": sum(1 for p in profiles if p.discovered_via_queries),
    }


@router.get("/repos/pinned")
async def get_pinned_repos(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List all repos with manual_status='pinned' for the current project.

    Pinned repos are permanently active regardless of scan results.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profiles, total = await repo.list_pinned_repos()

    return {
        "success": True,
        "pinned_repos": [_profile_to_list_dict(p) for p in profiles],
        "total": total,
    }
