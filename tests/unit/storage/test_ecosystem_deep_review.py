"""Ecosystem repository deep-scan report storage-layer unit tests — create / get / update / list coverage."""

from __future__ import annotations

import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    DemoResult,
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    IntegrationRecommendation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repository for testing."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def sample_repo_id(repo: StorageRepository) -> str:
    """First create an EcosystemRepoProfile and return its id for use as an FK."""
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        stars=50000,
        last_scanned_at=utc_now(),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("anthropics/claude-code")
    assert fetched is not None
    return fetched.id


def _make_review(
    repo_id: str,
    agent_id: str | None = None,
    **extra,
) -> EcosystemDeepReview:
    # D5: the status is derived by create_deep_review from stage_status and is no longer passed in by the caller;
    # tests that need a specific status now pass stage_status instead (see the STAGE_TO_STATUS mapping).
    return EcosystemDeepReview(
        repo_id=repo_id,
        agent_id=agent_id,
        summary_md="# Summary",
        architecture_md="Architecture description",
        risks_md="Risks",
        learnings_md="Experience",
        **extra,
    )


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


async def test_create_deep_review_persists(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """After creating a deep-scan report it can be retrieved by id."""
    review = _make_review(sample_repo_id)
    await repo.create_deep_review(review)

    fetched = await repo.get_deep_review(review.id)
    assert fetched is not None
    assert fetched.repo_id == sample_repo_id
    assert fetched.status == EcosystemDeepReviewStatus.QUEUED


async def test_get_deep_review_missing_returns_none(repo: StorageRepository) -> None:
    """A non-existent id returns None."""
    fetched = await repo.get_deep_review("non-existent-id")
    assert fetched is None


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_deep_review_status_transition(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """The status can be updated step by step: queued -> running -> completed."""
    review = _make_review(sample_repo_id)
    await repo.create_deep_review(review)

    updated = await repo.update_deep_review(
        review.id, status=EcosystemDeepReviewStatus.RUNNING
    )
    assert updated is not None
    assert updated.status == EcosystemDeepReviewStatus.RUNNING

    completed = await repo.update_deep_review(
        review.id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        completed_at=utc_now(),
        duration_seconds=120.5,
        integration_recommendation=IntegrationRecommendation.INTEGRATE,
        demo_result=DemoResult.SUCCESS,
    )
    assert completed is not None
    assert completed.status == EcosystemDeepReviewStatus.COMPLETED
    assert completed.duration_seconds == 120.5
    assert completed.integration_recommendation == IntegrationRecommendation.INTEGRATE
    assert completed.demo_result == DemoResult.SUCCESS


async def test_update_deep_review_missing_returns_none(repo: StorageRepository) -> None:
    """Updating a non-existent review returns None."""
    result = await repo.update_deep_review(
        "missing-id", status=EcosystemDeepReviewStatus.RUNNING
    )
    assert result is None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_deep_reviews_filter_by_repo_id(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Filtering by repo_id returns only that repository's reports."""
    # Create a second repo
    second_profile = EcosystemRepoProfile(
        repo_full_name="other/repo",
        name="repo",
        owner="other",
        stars=8000,
        last_scanned_at=utc_now(),
    )
    await repo.upsert_ecosystem_profile(second_profile)
    second = await repo.get_ecosystem_profile("other/repo")
    assert second is not None

    await repo.create_deep_review(_make_review(sample_repo_id))
    await repo.create_deep_review(_make_review(sample_repo_id))
    await repo.create_deep_review(_make_review(second.id))

    rows_a = await repo.list_deep_reviews(repo_id=sample_repo_id)
    rows_b = await repo.list_deep_reviews(repo_id=second.id)
    assert len(rows_a) == 2
    assert len(rows_b) == 1
    assert all(r.repo_id == sample_repo_id for r in rows_a)


async def test_list_deep_reviews_filter_by_status(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Filtering by status returns only reports matching the status.

    D5: create_deep_review forcibly derives the status from stage_status (the single source of truth,
    types.STAGE_TO_STATUS), so each status value is constructed via a stage —
    queued→queued / shallow_done→completed / shallow_failed→failed。
    """
    from aiteam.types import EcosystemStageStatus

    await repo.create_deep_review(
        _make_review(sample_repo_id, stage_status=EcosystemStageStatus.QUEUED)
    )
    await repo.create_deep_review(
        _make_review(sample_repo_id, stage_status=EcosystemStageStatus.SHALLOW_DONE)
    )
    await repo.create_deep_review(
        _make_review(sample_repo_id, stage_status=EcosystemStageStatus.SHALLOW_FAILED)
    )

    queued = await repo.list_deep_reviews(status="queued")
    completed = await repo.list_deep_reviews(status="completed")
    assert len(queued) == 1
    assert len(completed) == 1
    assert queued[0].status == EcosystemDeepReviewStatus.QUEUED
    assert completed[0].status == EcosystemDeepReviewStatus.COMPLETED
