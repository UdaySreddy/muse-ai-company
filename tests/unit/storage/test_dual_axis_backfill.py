"""D5 dual-axis convergence tests — the derivation mapping's source of truth / choke point / the R1-F3 backfill matrix
and idempotency.

Coverage (corresponding to the D5 test plan T1-T6):
- T1: full coverage of types.STAGE_TO_STATUS + the derive_status_from_stage pure-function contract;
- T2: the create_deep_review choke point forcibly derives (ignoring the status passed by the caller);
- T3: the update_deep_review_stage choke point derivation + claim clearing + completed_at
      COALESCE (an existing value is never overwritten; history cannot be rewritten);
- T4: the backfill_deep_review_dual_axis real combination matrix (each of the R1/R2/F1/F2/F3 rules'
      rowcounts and final states), with an embedded ordering argument (R1 before F3, so (completed,queued)
      legacy rows are never wrongly reset back to queued);
- T5: backfill idempotency — on a second run, every rule's rowcount is constantly 0 and rows are unchanged;
- T6: project isolation — an explicit project_id touches only this project's rows (the current paradigm of red line 3).

The claim-gap concurrency regression (tick creating rows that atomically carry claimed_by) is in
tests/unit/services/test_ecosystem_shallow_queue.py::test_tick_dispatched_row_cannot_be_double_claimed。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    STAGE_TO_STATUS,
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    EcosystemStageStatus,
    derive_status_from_stage,
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


async def _make_legacy_row(
    repo: StorageRepository,
    repo_id: str,
    *,
    status: EcosystemDeepReviewStatus,
    stage: EcosystemStageStatus = EcosystemStageStatus.QUEUED,
    project_id: str | None = None,
    **extra_fields,
) -> EcosystemDeepReview:
    """Construct a historical dirty-data row: first go through create (which forcibly derives), then write the dirty
    status directly with the generic setter.

    After D5, create_deep_review is a choke point, so dirty combinations can only be reproduced via the low-level setter
    —
    equivalent to the storage path of real historical rows (the old reviewer back then wrote the status column
    directly).
    """
    row = EcosystemDeepReview(
        project_id=project_id,
        repo_id=repo_id,
        stage_status=stage,
    )
    await repo.create_deep_review(row, project_id=project_id)
    updated = await repo.update_deep_review(
        row.id,
        _project_id=project_id,
        status=status,
        **extra_fields,
    )
    assert updated is not None
    return updated


# ---------------------------------------------------------------------------
# T1: Mapping source of truth — STAGE_TO_STATUS / derive_status_from_stage
# ---------------------------------------------------------------------------


def test_stage_to_status_covers_every_stage() -> None:
    """STAGE_TO_STATUS must cover all members of EcosystemStageStatus (the single source of truth, with no blind
    spots)."""
    assert set(STAGE_TO_STATUS.keys()) == set(EcosystemStageStatus)


def test_stage_to_status_mapping_values() -> None:
    """The mapping pinned by the spec: queued→queued; the 5 completion states→completed; the 3 failure states→failed."""
    expected = {
        EcosystemStageStatus.QUEUED: EcosystemDeepReviewStatus.QUEUED,
        EcosystemStageStatus.SHALLOW_DONE: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.ARCHITECTURE_DONE: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.DEBATED: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.REFERENCED: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.INTEGRATED: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.SHALLOW_FAILED: EcosystemDeepReviewStatus.FAILED,
        EcosystemStageStatus.ARCHITECTURE_FAILED: EcosystemDeepReviewStatus.FAILED,
        EcosystemStageStatus.DEBATED_FAILED: EcosystemDeepReviewStatus.FAILED,
    }
    assert STAGE_TO_STATUS == expected


def test_derive_status_accepts_enum_and_str() -> None:
    """derive_status_from_stage accepts both enums and strings (normalizing first)."""
    assert (
        derive_status_from_stage(EcosystemStageStatus.DEBATED)
        is EcosystemDeepReviewStatus.COMPLETED
    )
    assert (
        derive_status_from_stage("shallow_failed")
        is EcosystemDeepReviewStatus.FAILED
    )
    assert derive_status_from_stage("queued") is EcosystemDeepReviewStatus.QUEUED


def test_derive_status_rejects_unknown_stage() -> None:
    """An unknown stage string raises ValueError (the same contract as EcosystemStageStatus())."""
    with pytest.raises(ValueError):
        derive_status_from_stage("not-a-stage")


# ---------------------------------------------------------------------------
# T2: create_deep_review choke point — forced derivation, ignoring the caller's status
# ---------------------------------------------------------------------------


async def test_create_forces_derived_status(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """At row creation the status is forcibly derived; the RUNNING passed in by the caller is ignored."""
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,  # Forged by the caller; should be ignored
        stage_status=EcosystemStageStatus.QUEUED,
    )
    await repo.create_deep_review(row)

    fetched = await repo.get_deep_review(row.id)
    assert fetched is not None
    assert fetched.status == EcosystemDeepReviewStatus.QUEUED  # Derived from the stage


async def test_create_derives_completed_for_shallow_done(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """The lifecycle row-creation paradigm: stage=shallow_done → status derived as completed."""
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,
    )
    await repo.create_deep_review(row)

    fetched = await repo.get_deep_review(row.id)
    assert fetched is not None
    assert fetched.status == EcosystemDeepReviewStatus.COMPLETED


# ---------------------------------------------------------------------------
# T3: update_deep_review_stage choke point — derivation + claim clearing + COALESCE timestamp filling
# ---------------------------------------------------------------------------


async def test_stage_advance_derives_failed_and_fills_completed_at(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Advancing to SHALLOW_FAILED: the status is derived as failed, the claim is released, and completed_at is
    filled in."""
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        stage_status=EcosystemStageStatus.QUEUED,
        claimed_by="tick:deadbeef",
        claimed_at=utc_now(),
    )
    await repo.create_deep_review(row)

    advanced = await repo.update_deep_review_stage(
        row.id, EcosystemStageStatus.SHALLOW_FAILED
    )
    assert advanced is not None
    assert advanced.status == EcosystemDeepReviewStatus.FAILED
    assert advanced.claimed_by is None
    assert advanced.claimed_at is None
    assert advanced.completed_at is not None


async def test_stage_advance_never_overwrites_completed_at(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """COALESCE semantics: when a row already has a completed_at, stage progression never overwrites it (history
    cannot be rewritten)."""
    historic_ts = datetime(2025, 1, 1, tzinfo=UTC)
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,
    )
    await repo.create_deep_review(row)
    await repo.update_deep_review(row.id, completed_at=historic_ts)

    advanced = await repo.update_deep_review_stage(
        row.id, EcosystemStageStatus.ARCHITECTURE_DONE
    )
    assert advanced is not None
    assert advanced.status == EcosystemDeepReviewStatus.COMPLETED
    # SQLite storage strips tzinfo; compare by naive wall time: the original value is preserved, not overwritten with
    # now
    assert advanced.completed_at is not None
    assert advanced.completed_at.replace(tzinfo=None) == historic_ts.replace(
        tzinfo=None
    )


# ---------------------------------------------------------------------------
# T4: Backfill matrix — every real combination has an owner, and per-rule rowcounts can be reconciled
# ---------------------------------------------------------------------------


async def test_backfill_matrix_covers_real_combinations(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Running the backfill once over 8 classes of real historical combinations: R1=1 R2=1 F1=2 F2=1 F3=2, with clean
    rows untouched."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)

    # R1: an old reviewer's completed row (completed, queued) → the stage advances to shallow_done
    r1_row = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        stage=EcosystemStageStatus.QUEUED,
        completed_at=ts,
    )
    # R2: an old reviewer's failed row (failed, queued) with dispatch evidence → shallow_failed
    r2_row = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
        started_at=ts,
        dispatch_prompt="dispatched once",
    )
    # F3(b): (failed, queued) with no dispatch evidence at all → does not pass the R2 gate; reset to truly queued
    f3_no_evidence = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
    )
    # F1(a): the v1.5.2 main lesion (running, shallow_done) → status=completed
    f1_running = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
    )
    # F1(b): (queued, architecture_done) — a row that only advanced the stage and never touched the status
    f1_queued = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.QUEUED,
        stage=EcosystemStageStatus.ARCHITECTURE_DONE,
    )
    # F2: (running, shallow_failed) — a row where report_failure only advanced the stage
    f2_row = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_FAILED,
    )
    # F3(a): (running, queued) — an in-flight/dead row at the drift birthplace
    f3_running = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.QUEUED,
    )
    # A clean row (queued, queued) with dual defaults → a whole-chain no-op
    clean = EcosystemDeepReview(
        repo_id=sample_repo_id, stage_status=EcosystemStageStatus.QUEUED
    )
    await repo.create_deep_review(clean)

    counts = await repo.backfill_deep_review_dual_axis()
    assert counts == {"R1": 1, "R2": 1, "F1": 2, "F2": 1, "F3": 2}

    # R1 final state: the stage moves away from queued; shallow_completed_at is COALESCEd from completed_at
    r1_after = await repo.get_deep_review(r1_row.id)
    assert r1_after is not None
    assert r1_after.stage_status == EcosystemStageStatus.SHALLOW_DONE
    assert r1_after.status == EcosystemDeepReviewStatus.COMPLETED  # F1 no-op
    # SQLite storage strips tzinfo; compare the COALESCE result by naive wall time
    assert r1_after.shallow_completed_at is not None
    assert r1_after.shallow_completed_at.replace(tzinfo=None) == ts.replace(
        tzinfo=None
    )

    # R2 final state: stage=shallow_failed, the status stays failed (F2 no-op)
    r2_after = await repo.get_deep_review(r2_row.id)
    assert r2_after is not None
    assert r2_after.stage_status == EcosystemStageStatus.SHALLOW_FAILED
    assert r2_after.status == EcosystemDeepReviewStatus.FAILED

    # F1 final state: status=completed + completed_at filled in (COALESCE created_at)
    for fixed in (f1_running, f1_queued):
        after = await repo.get_deep_review(fixed.id)
        assert after is not None
        assert after.status == EcosystemDeepReviewStatus.COMPLETED
        assert after.completed_at is not None

    # F2 final state: status=failed
    f2_after = await repo.get_deep_review(f2_row.id)
    assert f2_after is not None
    assert f2_after.status == EcosystemDeepReviewStatus.FAILED
    assert f2_after.completed_at is not None

    # F3 final state: the status is reset to queued, the stage stays queued (it can be claimed again)
    for reset in (f3_running, f3_no_evidence):
        after = await repo.get_deep_review(reset.id)
        assert after is not None
        assert after.status == EcosystemDeepReviewStatus.QUEUED
        assert after.stage_status == EcosystemStageStatus.QUEUED

    # Clean rows are untouched
    clean_after = await repo.get_deep_review(clean.id)
    assert clean_after is not None
    assert clean_after.status == EcosystemDeepReviewStatus.QUEUED
    assert clean_after.stage_status == EcosystemStageStatus.QUEUED


async def test_backfill_order_r1_before_f3_protects_completed_rows(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """The ordering iron law: a (completed, queued) legacy row must be pushed away from queued by R1 and must never fall
    into F3 and
    be wrongly reset to (queued, queued) — the direct regression for the whole chain's most dangerous trap."""
    legacy = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        stage=EcosystemStageStatus.QUEUED,
        completed_at=datetime(2025, 3, 1, tzinfo=UTC),
    )

    await repo.backfill_deep_review_dual_axis()

    after = await repo.get_deep_review(legacy.id)
    assert after is not None
    # If the order were reversed (F3 runs first), the status would be reset to queued — this assertion must go red
    assert after.status == EcosystemDeepReviewStatus.COMPLETED
    assert after.stage_status == EcosystemStageStatus.SHALLOW_DONE


async def test_backfill_r2_gate_requires_dispatch_evidence(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """R2 gate: a report_id as the sole evidence can also advance to shallow_failed; with all three evidences empty
    it takes F3."""
    with_report = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
        report_id="rep-1",
    )
    no_evidence = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
    )

    counts = await repo.backfill_deep_review_dual_axis()
    assert counts["R2"] == 1
    assert counts["F3"] == 1

    a = await repo.get_deep_review(with_report.id)
    b = await repo.get_deep_review(no_evidence.id)
    assert a is not None and b is not None
    assert a.stage_status == EcosystemStageStatus.SHALLOW_FAILED
    assert a.status == EcosystemDeepReviewStatus.FAILED
    assert b.stage_status == EcosystemStageStatus.QUEUED
    assert b.status == EcosystemDeepReviewStatus.QUEUED


# ---------------------------------------------------------------------------
# T5: Idempotency — on a second run, every rowcount is constantly 0
# ---------------------------------------------------------------------------


async def test_backfill_is_idempotent(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """Every WHERE excludes its own post-write state: on the second run, all rules have rowcount=0 and rows are
    unchanged."""
    await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        stage=EcosystemStageStatus.QUEUED,
    )
    await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
    )
    await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.QUEUED,
        started_at=utc_now() - timedelta(days=30),
    )

    first = await repo.backfill_deep_review_dual_axis()
    assert sum(first.values()) == 3

    snapshot = {
        r.id: (r.status, r.stage_status, r.completed_at, r.shallow_completed_at)
        for r in await repo.list_deep_reviews(limit=100)
    }

    second = await repo.backfill_deep_review_dual_axis()
    assert second == {"R1": 0, "R2": 0, "F1": 0, "F2": 0, "F3": 0}

    resnap = {
        r.id: (r.status, r.stage_status, r.completed_at, r.shallow_completed_at)
        for r in await repo.list_deep_reviews(limit=100)
    }
    assert resnap == snapshot


async def test_backfill_empty_table_returns_zero(repo: StorageRepository) -> None:
    """Backfilling an empty database (the local Win→Mac unmigrated-data scenario) safely returns all zeros."""
    counts = await repo.backfill_deep_review_dual_axis()
    assert counts == {"R1": 0, "R2": 0, "F1": 0, "F2": 0, "F3": 0}


# ---------------------------------------------------------------------------
# T6: Project isolation — an explicit project_id touches only this project's rows (the current paradigm of red line 3)
# ---------------------------------------------------------------------------


async def test_backfill_respects_project_scope(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """backfill(project_id='p1') fixes only p1's dirty rows; a global run then collects p2."""
    dirty_p1 = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
        project_id="p1",
    )
    dirty_p2 = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
        project_id="p2",
    )

    counts = await repo.backfill_deep_review_dual_axis(project_id="p1")
    assert counts["F1"] == 1

    p1_after = await repo.get_deep_review(dirty_p1.id, project_id="p1")
    p2_after = await repo.get_deep_review(dirty_p2.id, project_id="p2")
    assert p1_after is not None and p2_after is not None
    assert p1_after.status == EcosystemDeepReviewStatus.COMPLETED
    assert p2_after.status == EcosystemDeepReviewStatus.RUNNING  # Untouched

    global_counts = await repo.backfill_deep_review_dual_axis()
    assert global_counts["F1"] == 1
    p2_final = await repo.get_deep_review(dirty_p2.id, project_id="p2")
    assert p2_final is not None
    assert p2_final.status == EcosystemDeepReviewStatus.COMPLETED
