"""v1.5.0-A: DeepReview stage_status helpers unit tests.

Coverage:
- update_deep_review_stage progression + automatic timestamp writing
- list_deep_reviews_by_stage filtering
- debate_meeting_id / integration_task_id / integration_md writing
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repository for testing."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def sample_repo_id(repo: StorageRepository) -> str:
    """First create an EcosystemRepoProfile and return its id."""
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        stars=50000,
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("anthropics/claude-code")
    assert fetched is not None
    return fetched.id


@pytest.mark.asyncio
async def test_update_deep_review_stage_promotes_shallow(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """SHALLOW_DONE should write the shallow_completed_at timestamp."""
    review = await repo.create_deep_review(
        EcosystemDeepReview(
            repo_id=sample_repo_id,
            status=EcosystemDeepReviewStatus.QUEUED,
        )
    )
    assert review.stage_status == EcosystemStageStatus.QUEUED
    assert review.shallow_completed_at is None

    updated = await repo.update_deep_review_stage(
        review.id, EcosystemStageStatus.SHALLOW_DONE
    )
    assert updated is not None
    assert updated.stage_status == EcosystemStageStatus.SHALLOW_DONE
    assert updated.shallow_completed_at is not None
    assert updated.architecture_completed_at is None  # Should not write other stages' timestamps


@pytest.mark.asyncio
async def test_update_deep_review_stage_promotes_architecture(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """ARCHITECTURE_DONE should write architecture_completed_at."""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id, EcosystemStageStatus.ARCHITECTURE_DONE
    )
    assert updated.stage_status == EcosystemStageStatus.ARCHITECTURE_DONE
    assert updated.architecture_completed_at is not None
    assert updated.shallow_completed_at is None


@pytest.mark.asyncio
async def test_update_deep_review_stage_with_meeting_id(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """DEBATED should write debated_at + debate_meeting_id."""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id,
        EcosystemStageStatus.DEBATED,
        debate_meeting_id="mtg-001",
    )
    assert updated.stage_status == EcosystemStageStatus.DEBATED
    assert updated.debated_at is not None
    assert updated.debate_meeting_id == "mtg-001"


@pytest.mark.asyncio
async def test_update_deep_review_stage_integration(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """INTEGRATED should write stage3_completed_at + integration_task_id + integration_md."""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id,
        EcosystemStageStatus.INTEGRATED,
        integration_task_id="task-int-1",
        integration_md="## Integration plan\n\nStep one: ...",
    )
    assert updated.stage_status == EcosystemStageStatus.INTEGRATED
    assert updated.stage3_completed_at is not None
    assert updated.integration_task_id == "task-int-1"
    assert updated.integration_md.startswith("## Integration plan")


@pytest.mark.asyncio
async def test_update_deep_review_stage_referenced(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """REFERENCED should also write stage3_completed_at."""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id, EcosystemStageStatus.REFERENCED
    )
    assert updated.stage_status == EcosystemStageStatus.REFERENCED
    assert updated.stage3_completed_at is not None


@pytest.mark.asyncio
async def test_update_deep_review_stage_accepts_string(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Passing a string can also advance normally (an API boundary scenario)."""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(review.id, "shallow_done")
    assert updated.stage_status == EcosystemStageStatus.SHALLOW_DONE


@pytest.mark.asyncio
async def test_list_deep_reviews_by_stage_filters(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """list_deep_reviews_by_stage should return only rows matching the stage."""
    r1 = await repo.create_deep_review(EcosystemDeepReview(repo_id=sample_repo_id))
    r2 = await repo.create_deep_review(EcosystemDeepReview(repo_id=sample_repo_id))
    r3 = await repo.create_deep_review(EcosystemDeepReview(repo_id=sample_repo_id))

    await repo.update_deep_review_stage(r1.id, EcosystemStageStatus.SHALLOW_DONE)
    await repo.update_deep_review_stage(r2.id, EcosystemStageStatus.DEBATED)
    # r3 keeps QUEUED

    shallow = await repo.list_deep_reviews_by_stage(EcosystemStageStatus.SHALLOW_DONE)
    debated = await repo.list_deep_reviews_by_stage(EcosystemStageStatus.DEBATED)
    queued = await repo.list_deep_reviews_by_stage("queued")

    assert {r.id for r in shallow} == {r1.id}
    assert {r.id for r in debated} == {r2.id}
    assert {r.id for r in queued} == {r3.id}


@pytest.mark.asyncio
async def test_explicit_completed_at_respected(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """An explicitly passed completed_at should be adopted, to facilitate backfilling."""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    target_ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    updated = await repo.update_deep_review_stage(
        review.id,
        EcosystemStageStatus.SHALLOW_DONE,
        completed_at=target_ts,
    )
    assert updated.shallow_completed_at == target_ts
