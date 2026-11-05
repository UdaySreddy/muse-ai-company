"""Unit tests for the v1.5.0-B EcosystemShallowQueueWorker — dispatch path."""

from __future__ import annotations

from datetime import datetime

import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.services.ecosystem_shallow_queue import (
    STAGE0_TIMEOUT_SECONDS,
    DispatchIntent,
    EcosystemShallowQueueWorker,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReviewStatus,
    EcosystemProjectSettings,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)

# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _make_profile(
    repo: StorageRepository,
    full_name: str,
    *,
    stars: int = 5000,
    project_id: str = "proj-test",
    is_active: bool = True,
    shallow_summary: str = "",
    last_shallow_refreshed_at: datetime | None = None,
) -> str:
    """Insert a profile and return its id."""
    profile = EcosystemRepoProfile(
        project_id=project_id,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=stars,
        is_active=is_active,
        shallow_summary=shallow_summary,
        # After the Bug 2 fix: skip only when there is a summary and pushed_at <= last_shallow_refreshed_at.
        # To simulate 'refresh completed' in a test, pass a last_shallow_refreshed_at newer than pushed_at.
        last_shallow_refreshed_at=last_shallow_refreshed_at,
        last_scanned_at=utc_now(),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=project_id)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=project_id)
    assert fetched is not None
    return fetched.id


async def _seed_settings(
    repo: StorageRepository,
    project_id: str = "proj-test",
    *,
    min_stars: int = 1000,
    top_n: int = 50,
    concurrency: int = 5,
) -> None:
    settings = EcosystemProjectSettings(
        project_id=project_id,
        min_stars=min_stars,
        top_n=top_n,
        shallow_concurrency=concurrency,
    )
    await repo.upsert_ecosystem_project_settings(settings)


# ============================================================
# Dispatch path
# ============================================================


async def test_tick_dispatches_active_profiles_missing_summary(
    repo: StorageRepository,
) -> None:
    """tick() picks active profiles with empty shallow_summary and dispatches.

    Bug 2 semantics after the fix:
    - owner/a, owner/b: no summary → candidates
    - owner/c: has a summary and last_shallow_refreshed_at set to just now (> pushed_at=None),
      _has_new_push returns False → skip
    """
    await _seed_settings(repo)
    repo_a = await _make_profile(repo, "owner/a", stars=10000)
    repo_b = await _make_profile(repo, "owner/b", stars=5000)
    # It has a summary and the freshest refresh timestamp → no new push → skip
    await _make_profile(
        repo,
        "owner/c",
        stars=4000,
        shallow_summary="Existing summary",
        last_shallow_refreshed_at=utc_now(),
    )

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()

    assert result.dispatched == 2
    assert result.queued == 2  # only candidates, owner/c was filtered
    dispatched_ids = {i.repo_id for i in result.intents}
    assert dispatched_ids == {repo_a, repo_b}

    # Each dispatch should have created an in-flight deep_review row —
    # D5: stage=queued + claimed_by='tick:*'; status derives to 'queued'.
    for intent in result.intents:
        assert intent.repo_full_name in {"owner/a", "owner/b"}
        assert intent.timeout_seconds == STAGE0_TIMEOUT_SECONDS
        review = await repo.get_deep_review(
            intent.deep_review_id, project_id="proj-test"
        )
        assert review is not None
        assert review.status == EcosystemDeepReviewStatus.QUEUED
        assert review.claimed_by is not None
        assert review.claimed_by.startswith("tick:")
        assert review.dispatch_prompt
        assert intent.repo_full_name in review.dispatch_prompt


async def test_tick_dispatches_all_candidates_no_budget_cap(
    repo: StorageRepository,
) -> None:
    """Bug 3 fix: tick() dispatches all candidates at once, no longer limited by the concurrency cap.

    The concurrency setting is kept, but throttling moves to the worker claim stage; the dispatch stage has no cap.
    """
    await _seed_settings(repo, concurrency=2)
    for i in range(5):
        await _make_profile(repo, f"owner/r{i}", stars=2000 + i)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    # All 5 candidates are enqueued; no longer only the first concurrency=2
    assert result.dispatched == 5
    assert result.queued == 5


async def test_tick_skips_inflight_repo(repo: StorageRepository) -> None:
    """Re-running tick does not duplicate dispatch for in-flight repos."""
    await _seed_settings(repo)
    _ = await _make_profile(repo, "owner/x", stars=8000)
    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    first = await worker.tick()
    assert first.dispatched == 1

    second = await worker.tick()
    assert second.dispatched == 0
    assert second.skipped_inflight == 1


async def test_tick_skips_inactive_or_failed_profiles(
    repo: StorageRepository,
) -> None:
    """is_active=False / is_deleted / is_private_now repos are excluded."""
    await _seed_settings(repo)
    inactive = await _make_profile(
        repo, "owner/inactive", stars=2000, is_active=False
    )
    deleted = await _make_profile(repo, "owner/deleted", stars=2000)
    await repo.mark_profile_deleted(deleted, project_id="proj-test")
    private = await _make_profile(repo, "owner/private", stars=2000)
    await repo.mark_profile_private(private, project_id="proj-test")
    alive = await _make_profile(repo, "owner/alive", stars=2000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    assert result.dispatched == 1
    assert result.intents[0].repo_id == alive
    assert inactive not in {i.repo_id for i in result.intents}


async def test_enqueue_repo_dispatches_one(repo: StorageRepository) -> None:
    """Scanner hook entry point dispatches a single repo."""
    await _seed_settings(repo)
    rid = await _make_profile(repo, "owner/new", stars=3000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    intent = await worker.enqueue_repo(rid)
    assert isinstance(intent, DispatchIntent)
    assert intent.repo_id == rid
    assert "owner/new" in intent.prompt


async def test_enqueue_repo_skips_already_summarized(
    repo: StorageRepository,
) -> None:
    """enqueue_repo no-ops on profiles that already have a summary."""
    await _seed_settings(repo)
    rid = await _make_profile(
        repo, "owner/done", stars=3000, shallow_summary="Scanned"
    )

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    intent = await worker.enqueue_repo(rid)
    assert intent is None


async def test_prompt_injects_lessons_from_pattern_searcher(
    repo: StorageRepository,
) -> None:
    """When pattern_searcher returns failure patterns they are injected."""
    await _seed_settings(repo)
    await _make_profile(repo, "owner/repo", stars=4000)

    async def fake_pattern_searcher(query: str, top_k: int = 3):
        return [
            {
                "type": "failure",
                "error": "description too short",
                "lesson": "fall back to README",
            }
        ]

    worker = EcosystemShallowQueueWorker(
        repo,
        project_id="proj-test",
        pattern_searcher=fake_pattern_searcher,
    )
    result = await worker.tick()
    assert result.dispatched == 1
    prompt = result.intents[0].prompt
    assert "fall back to README" in prompt
    assert "description too short" in prompt


async def test_settings_auto_created_when_missing(
    repo: StorageRepository,
) -> None:
    """Worker auto-creates default settings when none exist."""
    await _make_profile(repo, "owner/r", stars=1500)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    # Default settings (min_stars=1000, top_n=100) so the 1500-star repo qualifies.
    assert result.dispatched == 1
    settings = await repo.get_ecosystem_project_settings("proj-test")
    assert settings is not None
    assert settings.min_stars == 1000


async def test_queue_status_returns_metrics(repo: StorageRepository) -> None:
    """queue_status() reports accurate counts from DR table (Bug 4 fix).

    owner/a: no summary → taken in by _find_candidates → after tick() the DR row has stage=queued → pending=1
    owner/b: has a summary + the freshest last_shallow_refreshed_at → skipped → 0 DR rows → pending unchanged
    owner/del: deleted → counted as deleted, does not participate in DR counting
    """
    await _seed_settings(repo)
    await _make_profile(repo, "owner/a", stars=2000)  # No summary → candidate
    await _make_profile(
        repo,
        "owner/b",
        stars=2000,
        shallow_summary="done",
        last_shallow_refreshed_at=utc_now(),  # Freshest timestamp → skip
    )
    deleted = await _make_profile(repo, "owner/del", stars=2000)
    await repo.mark_profile_deleted(deleted, project_id="proj-test")

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")

    # Run tick once first so owner/a enters the DR table (stage=queued)
    await worker.tick()

    status = await worker.queue_status()
    # After the Bug 4 fix: pending comes from DR rows with stage_status='queued' AND claimed_by IS NULL.
    # After the D5 convergence, tick creates rows that atomically carry claimed_by → dispatched rows count directly as
    # in_progress
    # (semantic correction: dispatched ≠ awaiting claim), and pending returns to 0.
    assert status["pending"] == 0
    assert status["in_progress"] == 1
    assert status["pending_shallow"] == 0  # Backward-compatible alias
    assert status["in_flight"] == 1  # Backward-compatible alias
    assert status["deleted"] == 1
    assert status["concurrency"] == 5


async def test_no_project_id_returns_empty_tick(repo: StorageRepository) -> None:
    """Worker without project_id has no settings -> no work."""
    await _make_profile(repo, "owner/a", stars=2000, project_id="proj-x")

    worker = EcosystemShallowQueueWorker(repo, project_id="")
    result = await worker.tick()
    assert result.dispatched == 0
    assert result.queued == 0


# ============================================================
# Bug 2 second fix: stage_status determination tests (v1.6.1)
# ============================================================


async def test_tick_dispatches_shallow_done_repo_with_new_push(
    repo: StorageRepository,
) -> None:
    """Bug 2 second fix: a repo with stage='shallow_done' + a new push should be re-dispatched.

    Simulated scenario: the profile has a shallow_summary and pushed_at is newer than last_shallow_refreshed_at
    (i.e. _has_new_push returns True). tick should create a new DR row with dispatch=1, skipped=0.
    The existing shallow_done DR row should not block a new round of enqueueing.
    """
    from datetime import timedelta

    await _seed_settings(repo)
    # pushed_at is now; last_shallow_refreshed_at is in the past (simulating a new push)
    old_refresh_time = utc_now() - timedelta(days=7)
    rid = await _make_profile(
        repo,
        "owner/old-with-new-push",
        stars=3000,
        shallow_summary="Old summary",
        last_shallow_refreshed_at=old_refresh_time,
    )
    # Manually set pushed_at newer than last_shallow_refreshed_at (simulating a new GitHub push)
    # Directly update the profile's pushed_at to now (newer than last_shallow_refreshed_at)
    from aiteam.types import EcosystemDeepReview, EcosystemDeepReviewStatus, EcosystemStageStatus

    # First create a historical shallow_done DR row. After D5, create_deep_review forcibly derives
    # the status; historical dirty data (status='running') must be constructed by writing directly via the generic
    # setter.
    history_dr = EcosystemDeepReview(
        project_id="proj-test",
        repo_id=rid,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,  # Completed stage
    )
    await repo.create_deep_review(history_dr, project_id="proj-test")
    await repo.update_deep_review(
        history_dr.id,
        _project_id="proj-test",
        status=EcosystemDeepReviewStatus.RUNNING,  # Historical dirty data
    )

    # Update the profile's pushed_at to a time newer than last_shallow_refreshed_at
    profile = await repo.get_ecosystem_profile_by_id(rid, project_id="proj-test")
    assert profile is not None
    new_pushed_at = utc_now()  # New push time
    await repo.update_profile_shallow_summary(
        rid,
        shallow_summary=profile.shallow_summary or "Old summary",
        refreshed_at=old_refresh_time,
        project_id="proj-test",
    )
    # Set pushed_at directly to the new time
    from aiteam.storage.connection import get_session
    from aiteam.storage.models import EcosystemRepoProfileModel
    async with get_session(repo._db_url) as session:
        from sqlalchemy import update as sa_update
        stmt = sa_update(EcosystemRepoProfileModel).where(
            EcosystemRepoProfileModel.id == rid
        ).values(pushed_at=new_pushed_at)
        await session.execute(stmt)
        await session.flush()

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()

    # An old repo with a new push (stage=shallow_done) should be re-dispatched, not skipped
    assert result.dispatched == 1, (
        f"expected dispatched=1, got dispatched={result.dispatched}, "
        f"skipped={result.skipped_inflight}"
    )
    assert result.skipped_inflight == 0


async def test_tick_skips_queued_stage_dr_row(repo: StorageRepository) -> None:
    """Bug 2 second fix: a DR row with stage='queued' should be skipped (truly in-flight).

    Simulated scenario: the profile has no summary; tick creates a DR row with stage=queued.
    The second tick should recognize the DR row with stage=queued and skip it, without dispatching again.
    """
    await _seed_settings(repo)
    await _make_profile(repo, "owner/inflight", stars=4000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    first = await worker.tick()
    assert first.dispatched == 1  # First enqueueing succeeds normally

    # Keep the just-created DR row's stage_status as queued (it is queued by default), simulating incompletion
    second = await worker.tick()
    assert second.dispatched == 0
    assert second.skipped_inflight == 1  # stage=queued is correctly skipped


async def test_backfill_shallow_done_status_completed(repo: StorageRepository) -> None:
    """The backfill method corrects rows with stage='shallow_done' + status='running' to 'completed'.

    1. Create a dirty-data row with stage=shallow_done + status=running (a historical scenario)
    2. Create a clean row with stage=shallow_done + status=completed (should not be changed)
    3. Run the backfill
    4. Verify the dirty row becomes completed and the clean row is unchanged
    """
    from aiteam.types import EcosystemDeepReview, EcosystemDeepReviewStatus, EcosystemStageStatus

    await _seed_settings(repo)
    rid = await _make_profile(repo, "owner/backfill-test", stars=2000)

    # Create a dirty-data row: stage=shallow_done + status=running.
    # After D5, create_deep_review forcibly derives the status; historical dirty data must be constructed by writing
    # directly via the generic setter.
    dirty_dr = EcosystemDeepReview(
        project_id="proj-test",
        repo_id=rid,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,
    )
    await repo.create_deep_review(dirty_dr, project_id="proj-test")
    await repo.update_deep_review(
        dirty_dr.id,
        _project_id="proj-test",
        status=EcosystemDeepReviewStatus.RUNNING,
    )

    # Create a clean row: stage=shallow_done + status=completed (a different repo_id to distinguish them)
    rid2 = await _make_profile(repo, "owner/backfill-clean", stars=2000)
    clean_dr = EcosystemDeepReview(
        project_id="proj-test",
        repo_id=rid2,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,  # create derives status=completed
    )
    await repo.create_deep_review(clean_dr, project_id="proj-test")

    # Run the backfill
    fixed_count = await repo.backfill_shallow_done_status_completed(project_id="proj-test")
    assert fixed_count == 1, f"expected 1 row fixed, got {fixed_count}"

    # Verify the dirty-data row has been corrected
    dirty_dr_updated = await repo.get_deep_review(dirty_dr.id, project_id="proj-test")
    assert dirty_dr_updated is not None
    assert dirty_dr_updated.status == EcosystemDeepReviewStatus.COMPLETED

    # Verify the clean row was not modified a second time (still completed; rowcount is not counted)
    clean_dr_check = await repo.get_deep_review(clean_dr.id, project_id="proj-test")
    assert clean_dr_check is not None
    assert clean_dr_check.status == EcosystemDeepReviewStatus.COMPLETED


# ============================================================
# D5 convergence: tick/claim double-claim gap regression (v1.6.2)
# ============================================================


async def test_tick_dispatched_row_cannot_be_double_claimed(
    repo: StorageRepository,
) -> None:
    """D5 claim-gap regression: a tick-dispatched row carries claimed_by atomically from row creation, so the race
    window is 0.

    Historical gap: after INSERT(stage=queued, claimed_by=NULL) the row permanently satisfied
    claim_next_shallow_repo's candidate conditions, and tick dispatch and claim could both grab the same row.
    After the fix, claim's candidate SELECT (queued, unclaimed) can at no moment see
    a row created by tick → returns None.
    """
    await _seed_settings(repo)
    await _make_profile(repo, "owner/no-double-claim", stars=8000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    assert result.dispatched == 1
    dr_id = result.intents[0].deep_review_id

    review = await repo.get_deep_review(dr_id, project_id="proj-test")
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.QUEUED
    assert review.claimed_by is not None and review.claimed_by.startswith("tick:")

    # A claim worker must at this moment never be able to grab a row already dispatched by tick
    claimed = await repo.claim_next_shallow_repo(
        worker_id="rival-worker", project_id="proj-test"
    )
    assert claimed is None


async def test_stage_advance_releases_tick_claim(
    repo: StorageRepository,
) -> None:
    """D5 supporting release: stage progression (SHALLOW_DONE) uniformly clears claimed_by/claimed_at,
    and derives status=completed + fills in completed_at, preventing a dangling claim from occupying it."""
    await _seed_settings(repo)
    await _make_profile(repo, "owner/release-on-advance", stars=8000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    dr_id = result.intents[0].deep_review_id

    advanced = await repo.update_deep_review_stage(
        dr_id, EcosystemStageStatus.SHALLOW_DONE, project_id="proj-test"
    )
    assert advanced is not None
    assert advanced.claimed_by is None
    assert advanced.claimed_at is None
    assert advanced.status == EcosystemDeepReviewStatus.COMPLETED  # Derived
    assert advanced.completed_at is not None  # Filled in by the choke point
