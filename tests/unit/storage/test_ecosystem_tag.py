"""Ecosystem tag dictionary storage-layer unit tests — upsert / get / list coverage."""

from __future__ import annotations

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemTag, EcosystemTagCategory


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repository for testing."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


def _make_tag(
    name: str = "memory_system",
    category: EcosystemTagCategory = EcosystemTagCategory.CAPABILITY,
    aliases: list[str] | None = None,
    description: str = "Memory system",
) -> EcosystemTag:
    return EcosystemTag(
        name=name,
        category=category,
        aliases=aliases or [],
        description=description,
    )


async def test_upsert_new_tag_persists(repo: StorageRepository) -> None:
    """A new tag, once upserted, can be searched by name."""
    tag = _make_tag()
    await repo.upsert_tag(tag)

    fetched = await repo.get_tag_by_name("memory_system")
    assert fetched is not None
    assert fetched.category == EcosystemTagCategory.CAPABILITY
    assert fetched.description == "Memory system"


async def test_upsert_tag_updates_existing(repo: StorageRepository) -> None:
    """Upserting a tag with the same name again updates aliases / category / description."""
    await repo.upsert_tag(_make_tag())

    updated = _make_tag(aliases=["mem", "long-term"], description="New description")
    await repo.upsert_tag(updated)

    fetched = await repo.get_tag_by_name("memory_system")
    assert fetched is not None
    assert set(fetched.aliases) == {"mem", "long-term"}
    assert fetched.description == "New description"


async def test_get_tag_by_name_missing_returns_none(repo: StorageRepository) -> None:
    """An unregistered name returns None."""
    assert await repo.get_tag_by_name("non-existent") is None


async def test_list_tags_filter_by_category(repo: StorageRepository) -> None:
    """Filtering by category returns only tags of the corresponding category."""
    await repo.upsert_tag(_make_tag("python", EcosystemTagCategory.TECH_STACK))
    await repo.upsert_tag(_make_tag("rust", EcosystemTagCategory.TECH_STACK))
    await repo.upsert_tag(_make_tag("framework", EcosystemTagCategory.POSITIONING))

    tech = await repo.list_tags(category="tech_stack")
    pos = await repo.list_tags(category="positioning")
    assert {t.name for t in tech} == {"python", "rust"}
    assert {t.name for t in pos} == {"framework"}


async def test_list_tags_no_filter_returns_all(repo: StorageRepository) -> None:
    """Without a category, all tags are returned."""
    await repo.upsert_tag(_make_tag("a", EcosystemTagCategory.CAPABILITY))
    await repo.upsert_tag(_make_tag("b", EcosystemTagCategory.MATURITY))

    rows = await repo.list_tags()
    names = {t.name for t in rows}
    assert {"a", "b"} <= names
