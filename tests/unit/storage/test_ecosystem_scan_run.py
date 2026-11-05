"""Scan batch record storage-layer unit tests."""

from __future__ import annotations

import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemScanRun, EcosystemScanStrategy


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repository for testing."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


def _make_scan_run(
    strategy: EcosystemScanStrategy = EcosystemScanStrategy.INCREMENTAL,
    triggered_by: str = "manual",
) -> EcosystemScanRun:
    return EcosystemScanRun(
        strategy=strategy,
        triggered_by=triggered_by,
        notes="Test scan batch",
    )


async def test_create_scan_run_persists(repo: StorageRepository) -> None:
    """After creating a scan batch it can be retrieved by id."""
    run = _make_scan_run()
    await repo.create_scan_run(run)

    fetched = await repo.get_scan_run(run.id)
    assert fetched is not None
    assert fetched.strategy == EcosystemScanStrategy.INCREMENTAL
    assert fetched.triggered_by == "manual"


async def test_get_scan_run_missing_returns_none(repo: StorageRepository) -> None:
    """A non-existent id returns None."""
    assert await repo.get_scan_run("non-existent-id") is None


async def test_update_scan_run_completion_stats(repo: StorageRepository) -> None:
    """After the scan completes, update the statistics fields."""
    run = _make_scan_run()
    await repo.create_scan_run(run)

    completed_at = utc_now()
    updated = await repo.update_scan_run(
        run.id,
        completed_at=completed_at,
        duration_seconds=42.5,
        repos_added=10,
        repos_updated=20,
        repos_skipped=3,
        errors=["timeout: foo/bar"],
    )
    assert updated is not None
    assert updated.repos_added == 10
    assert updated.repos_updated == 20
    assert updated.repos_skipped == 3
    assert updated.duration_seconds == 42.5
    assert updated.errors == ["timeout: foo/bar"]


async def test_update_scan_run_missing_returns_none(repo: StorageRepository) -> None:
    """Updating a non-existent scan_run returns None."""
    assert await repo.update_scan_run("missing-id", repos_added=5) is None


async def test_list_scan_runs_filter_by_strategy(repo: StorageRepository) -> None:
    """Filtering by strategy returns only the corresponding batches."""
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.INCREMENTAL))
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.FULL))
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.TRENDING))

    inc = await repo.list_scan_runs(strategy="incremental")
    full = await repo.list_scan_runs(strategy="full")
    trending = await repo.list_scan_runs(strategy="trending")
    assert len(inc) == 1 and inc[0].strategy == EcosystemScanStrategy.INCREMENTAL
    assert len(full) == 1 and full[0].strategy == EcosystemScanStrategy.FULL
    assert len(trending) == 1 and trending[0].strategy == EcosystemScanStrategy.TRENDING


async def test_list_scan_runs_no_filter_returns_all(repo: StorageRepository) -> None:
    """Without a strategy, all batches are returned."""
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.INCREMENTAL))
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.TOPIC))

    rows = await repo.list_scan_runs()
    assert len(rows) >= 2
