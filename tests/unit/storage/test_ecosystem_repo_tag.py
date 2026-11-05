"""Repository-tag many-to-many association storage-layer unit tests."""

from __future__ import annotations

import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repository for testing."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def fixtures(repo: StorageRepository) -> dict[str, str]:
    """Pre-seed a repo_profile + two tags and return the id mapping."""
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        stars=50000,
        last_scanned_at=utc_now(),
    )
    await repo.upsert_ecosystem_profile(profile)
    p = await repo.get_ecosystem_profile("anthropics/claude-code")
    assert p is not None

    await repo.upsert_tag(
        EcosystemTag(name="memory_system", category=EcosystemTagCategory.CAPABILITY)
    )
    await repo.upsert_tag(
        EcosystemTag(name="python", category=EcosystemTagCategory.TECH_STACK)
    )
    tag_a = await repo.get_tag_by_name("memory_system")
    tag_b = await repo.get_tag_by_name("python")
    assert tag_a is not None and tag_b is not None

    return {"repo_id": p.id, "tag_a": tag_a.id, "tag_b": tag_b.id}


async def test_add_repo_tag_creates_association(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """After adding a new association it can be seen in the list."""
    rt = EcosystemRepoTag(
        repo_id=fixtures["repo_id"],
        tag_id=fixtures["tag_a"],
        confidence=0.9,
        source=EcosystemTagSource.AUTO_LLM,
    )
    await repo.add_repo_tag(rt)

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    assert len(rows) == 1
    assert rows[0].tag_id == fixtures["tag_a"]
    assert rows[0].confidence == 0.9
    assert rows[0].source == EcosystemTagSource.AUTO_LLM


async def test_add_repo_tag_idempotent_updates(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """Adding the same (repo_id, tag_id) again updates confidence / source rather than adding a new one."""
    rt = EcosystemRepoTag(
        repo_id=fixtures["repo_id"],
        tag_id=fixtures["tag_a"],
        confidence=0.5,
        source=EcosystemTagSource.AUTO_RULE,
    )
    await repo.add_repo_tag(rt)

    updated = EcosystemRepoTag(
        repo_id=fixtures["repo_id"],
        tag_id=fixtures["tag_a"],
        confidence=0.95,
        source=EcosystemTagSource.MANUAL,
        agent_id="reviewer-1",
    )
    await repo.add_repo_tag(updated)

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    assert len(rows) == 1
    assert rows[0].confidence == 0.95
    assert rows[0].source == EcosystemTagSource.MANUAL
    assert rows[0].agent_id == "reviewer-1"


async def test_remove_repo_tag(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """Removing an association means list no longer contains it; removing again returns False."""
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=fixtures["repo_id"],
            tag_id=fixtures["tag_a"],
        )
    )

    removed = await repo.remove_repo_tag(fixtures["repo_id"], fixtures["tag_a"])
    assert removed is True

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    assert len(rows) == 0

    removed_again = await repo.remove_repo_tag(fixtures["repo_id"], fixtures["tag_a"])
    assert removed_again is False


async def test_list_repo_tags_filter_by_tag_id(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """Filtering by tag_id can locate all repos associated with a certain tag."""
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_a"])
    )
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_b"])
    )

    rows_a = await repo.list_repo_tags(tag_id=fixtures["tag_a"])
    rows_b = await repo.list_repo_tags(tag_id=fixtures["tag_b"])
    assert len(rows_a) == 1 and rows_a[0].tag_id == fixtures["tag_a"]
    assert len(rows_b) == 1 and rows_b[0].tag_id == fixtures["tag_b"]


async def test_multiple_tags_per_repo(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """A single repository can be associated with multiple different tags."""
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_a"])
    )
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_b"])
    )

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    tag_ids = {r.tag_id for r in rows}
    assert tag_ids == {fixtures["tag_a"], fixtures["tag_b"]}
