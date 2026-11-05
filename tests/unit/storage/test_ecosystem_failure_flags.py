"""v1.5.0-A: Profile failure/activity state helpers unit tests.

Coverage:
- mark_profile_deleted / mark_profile_private / mark_profile_fetch_failure
- clear_profile_failure (resurrection mechanism §3.3)
- update_profile_active_set
- update_profile_shallow_summary
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def sample_repo_id(repo: StorageRepository) -> str:
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/skills",
        name="skills",
        owner="anthropics",
        stars=12000,
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("anthropics/skills")
    assert fetched is not None
    return fetched.id


@pytest.mark.asyncio
async def test_mark_profile_deleted_sets_flags(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """The deletion marker should set is_deleted=True / is_active=False / and write an error message."""
    updated = await repo.mark_profile_deleted(
        sample_repo_id, error_message="GitHub returned 404"
    )
    assert updated is not None
    assert updated.is_deleted is True
    assert updated.is_active is False
    assert updated.last_fetch_error == "GitHub returned 404"


@pytest.mark.asyncio
async def test_mark_profile_private_sets_flags(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """The private marker sets is_private_now=True / is_active=False."""
    updated = await repo.mark_profile_private(sample_repo_id)
    assert updated.is_private_now is True
    assert updated.is_active is False
    assert "403" in updated.last_fetch_error


@pytest.mark.asyncio
async def test_mark_profile_fetch_failure_increments_count(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """An ordinary fetch failure accumulates fetch_failure_count and does not toggle deleted/private/active."""
    u1 = await repo.mark_profile_fetch_failure(
        sample_repo_id, error_message="connection reset"
    )
    assert u1.fetch_failure_count == 1
    assert u1.is_deleted is False
    assert u1.is_active is True

    u2 = await repo.mark_profile_fetch_failure(
        sample_repo_id, error_message="timeout"
    )
    assert u2.fetch_failure_count == 2
    assert u2.last_fetch_error == "timeout"


@pytest.mark.asyncio
async def test_clear_profile_failure_resurrects(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Resurrection mechanism: deletion/private markers + the failure count are cleared, and is_active returns to
    True."""
    await repo.mark_profile_deleted(sample_repo_id, error_message="404")
    await repo.mark_profile_fetch_failure(sample_repo_id, error_message="404 again")

    cleared = await repo.clear_profile_failure(sample_repo_id)
    assert cleared.is_deleted is False
    assert cleared.is_private_now is False
    assert cleared.is_active is True
    assert cleared.last_fetch_error == ""
    assert cleared.fetch_failure_count == 0


@pytest.mark.asyncio
async def test_update_profile_active_set(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Refreshing the active set writes is_active + active_rank."""
    updated = await repo.update_profile_active_set(
        sample_repo_id, is_active=True, active_rank=42
    )
    assert updated.is_active is True
    assert updated.active_rank == 42

    deactivated = await repo.update_profile_active_set(
        sample_repo_id, is_active=False, active_rank=None
    )
    assert deactivated.is_active is False
    assert deactivated.active_rank is None


@pytest.mark.asyncio
async def test_update_profile_shallow_summary_writes_timestamp(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Writing a shallow-scan summary automatically updates last_shallow_refreshed_at."""
    before = utc_now()
    updated = await repo.update_profile_shallow_summary(
        sample_repo_id,
        shallow_summary="This is the shallow-scan summary of anthropics/skills...",
    )
    assert updated.shallow_summary.startswith("This is")
    assert updated.last_shallow_refreshed_at is not None
    assert updated.last_shallow_refreshed_at >= before


@pytest.mark.asyncio
async def test_update_profile_shallow_summary_explicit_timestamp(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """An explicit refreshed_at should be adopted."""
    target = datetime(2025, 6, 1, tzinfo=UTC)
    updated = await repo.update_profile_shallow_summary(
        sample_repo_id,
        shallow_summary="Historical backfill summary",
        refreshed_at=target,
    )
    assert updated.last_shallow_refreshed_at == target


@pytest.mark.asyncio
async def test_mark_nonexistent_repo_returns_none(repo: StorageRepository) -> None:
    """A non-existent repo_id returns None without raising."""
    result = await repo.mark_profile_deleted("does-not-exist")
    assert result is None
