"""Governance leader lease unit tests — D3 phase C (audit M50).

Verifies the DB atomic claim semantics: a new lease can be obtained, a claim is rejected while another holds it, the
same holder can renew,
after expiry it can be taken over, and the single-row table never proliferates.
"""

from __future__ import annotations

import asyncio

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


async def test_fresh_acquire(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True


async def test_other_holder_blocked_while_valid(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is False


async def test_same_holder_renews(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True


async def test_expired_lease_taken_over(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)  # Make now strictly later than expires_at (ISO strings compare lexicographically)
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True
    # After the takeover, the original holder cannot grab it back within the new lease's validity period
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is False


async def test_single_row_no_growth(repo: StorageRepository) -> None:
    from sqlalchemy import text

    from aiteam.storage.connection import get_session

    for holder in ("a", "b", "a", "c"):
        await repo.try_acquire_governance_lease(holder, ttl_seconds=0)
        await asyncio.sleep(0.005)
    async with get_session(repo._db_url) as session:
        result = await session.execute(text("SELECT COUNT(*) FROM governance_lease"))
        assert result.scalar_one() == 1


async def test_release_by_holder_frees_lease(repo: StorageRepository) -> None:
    """After the holder voluntarily releases, another instance can take over immediately without waiting for the TTL
    (the 77eb342 exit-path release)."""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=600) is True
    assert await repo.release_governance_lease("api-111") is True
    # With no waiting at all, another instance grabs it directly
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True


async def test_release_by_non_holder_is_noop(repo: StorageRepository) -> None:
    """Release by a non-holder is a no-op: one must not hand over someone else's lease (as with the residue of 89513
    versus 82516)."""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=600) is True
    assert await repo.release_governance_lease("api-999") is False
    # The original holder's lease is still valid; others still cannot grab it
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is False


async def test_release_when_no_lease_row(repo: StorageRepository) -> None:
    """Releasing on an empty table does not blow up and returns False (best-effort semantics on the exit path)."""
    assert await repo.release_governance_lease("api-111") is False


# ================================================================
# A2-obs: takeover tracing (debate 503e07f1 topic A ruling; observe for two weeks, then discuss A2-impl again)
#
# Add no columns and change no schema — only on the single branch of 'the predecessor is still in place but the lease
# has expired and is taken by another'
# emit one event. The criterion is 'whether an alternation really happened'; with no event, this path has never been
# walked,
# and A2-impl (an epoch column) need not be built.
# ================================================================


async def _takeover_events(repo: StorageRepository) -> list:
    return await repo.list_events(event_type="governance.lease_taken_over", limit=50)


async def test_takeover_from_expired_holder_emits_event(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True

    # Read back across the persistence boundary: EventType is a StrEnum, and the read end's EventType(x) raises for
    # absent members
    # ValueError (29287f88 caught the same kind of latent crash); only a real database query can prove the enum member
    # is present.
    events = await _takeover_events(repo)
    assert len(events) == 1
    data = events[0].data
    assert data["previous_holder"] == "api-111"
    assert data["new_holder"] == "api-222"
    assert data["previous_expires_at"]  # The expiry moment must be kept so the alternation interval can be computed


async def test_enum_member_present(repo: StorageRepository) -> None:
    """The enum member must really be in EventType — an event type string that does not match is a latent ValueError."""
    from aiteam.types import EventType

    assert EventType("governance.lease_taken_over") is EventType.GOVERNANCE_LEASE_TAKEN_OVER


async def test_fresh_acquire_emits_nothing(repo: StorageRepository) -> None:
    """An unclaimed acquisition is not a takeover: with no predecessor, there is no alternation."""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await _takeover_events(repo) == []


async def test_same_holder_renewal_emits_nothing(repo: StorageRepository) -> None:
    """A renewal is not a takeover — otherwise there would be an event every 60s and the observation surface would be
    drowned by itself."""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await _takeover_events(repo) == []


async def test_losing_attempt_emits_nothing(repo: StorageRepository) -> None:
    """The side that failed to grab it leaves no trace: the event describes an accomplished fact, not an intent."""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is False
    assert await _takeover_events(repo) == []


async def test_acquire_after_release_emits_nothing(repo: StorageRepository) -> None:
    """Taking over after a voluntary yield is a handover, not a takeover — the holder was already emptied and nobody
    was dispossessed."""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=600) is True
    assert await repo.release_governance_lease("api-111") is True
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True
    assert await _takeover_events(repo) == []


async def test_event_failure_never_blocks_the_lease(repo: StorageRepository, caplog) -> None:
    """If observability breaks it must not drag governance down: the lease is still obtained, but a WARNING must be
    left; it must not be silently swallowed."""
    import logging
    from unittest import mock

    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)

    async def boom(*args, **kwargs):
        raise RuntimeError("events ledger down")

    with mock.patch.object(repo, "create_event", side_effect=boom):
        with caplog.at_level(logging.WARNING):
            assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True

    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    # The lease body is unaffected: the new holder really is in place
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is False
