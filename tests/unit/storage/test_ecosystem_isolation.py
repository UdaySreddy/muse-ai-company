"""Stage J — Ecosystem project isolation storage-layer unit tests.

Verifies that 5 data tables (profile / scan_run / deep_review / repo_tag / relation)
are completely isolated between different project_ids, and the EcosystemTag dictionary stays globally visible.

Design points:
- The same in-memory SQLite DB simulates the real environment (shared database + multi-project scopes).
- Tests two StorageRepository instances with project_scope=p1 / p2 respectively.
- Through both paths: an explicit project_id parameter + the implicit _project_scope.
- Includes an end-to-end verification scenario for backfill_ecosystem_to_project.
"""

from __future__ import annotations

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRelation,
    EcosystemRelationType,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemScanRun,
    EcosystemScanStrategy,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)

# Shared in-memory DB — use file::memory:?cache=shared so multiple connections see the same tables
# But the test simplifies: reuse the same db_url, and within the same process the EnginePool shares it.
SHARED_DB_URL = "sqlite+aiosqlite://"


@pytest_asyncio.fixture()
async def shared_db():
    """Initialize the shared in-memory DB and return three repos:
    - global_repo: no scope, used to set up base data + global queries
    - project_a / project_b: two independent project ids
    """
    # First create the schema + projects with global_repo
    global_repo = StorageRepository(db_url=SHARED_DB_URL)
    await global_repo.init_db()

    # Create the two projects
    project_a_obj = await global_repo.create_project(
        name="Project-A",
        root_path="/tmp/project-a",
        description="Test project A",
    )
    project_b_obj = await global_repo.create_project(
        name="Project-B",
        root_path="/tmp/project-b",
        description="Test project B",
    )

    # Create a scoped repo for each project (sharing the same db_url, because the EnginePool caches the engine)
    repo_a = StorageRepository(
        db_url=SHARED_DB_URL, project_scope=project_a_obj.id
    )
    repo_b = StorageRepository(
        db_url=SHARED_DB_URL, project_scope=project_b_obj.id
    )

    yield {
        "global": global_repo,
        "project_a_id": project_a_obj.id,
        "project_b_id": project_b_obj.id,
        "repo_a": repo_a,
        "repo_b": repo_b,
    }

    await close_db()


def _make_profile(repo_full_name: str = "test/repo") -> EcosystemRepoProfile:
    return EcosystemRepoProfile(
        repo_full_name=repo_full_name,
        name=repo_full_name.split("/")[-1],
        owner=repo_full_name.split("/")[0],
        description="iso-test",
        stars=100,
        topics=["claude"],
    )


# ------------------------------------------------------------------
# Profile isolation
# ------------------------------------------------------------------


async def test_profile_isolated_by_project_scope(shared_db) -> None:
    """Two projects upserting the same repository → each gets an independent row, mutually invisible."""
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    p_a = _make_profile("anthropic/sdk")
    p_b = _make_profile("anthropic/sdk")
    await repo_a.upsert_ecosystem_profile(p_a)
    await repo_b.upsert_ecosystem_profile(p_b)

    a_list = await repo_a.search_ecosystem_profiles(limit=100)
    b_list = await repo_b.search_ecosystem_profiles(limit=100)

    assert len(a_list) == 1
    assert len(b_list) == 1
    assert a_list[0].project_id == shared_db["project_a_id"]
    assert b_list[0].project_id == shared_db["project_b_id"]
    # The same repo_full_name, two independent ids
    assert a_list[0].id != b_list[0].id


async def test_get_profile_respects_project_scope(shared_db) -> None:
    """get_ecosystem_profile sees only its own project's rows under different scopes."""
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    await repo_a.upsert_ecosystem_profile(_make_profile("dummy/repo"))

    found_in_a = await repo_a.get_ecosystem_profile("dummy/repo")
    found_in_b = await repo_b.get_ecosystem_profile("dummy/repo")

    assert found_in_a is not None
    assert found_in_a.project_id == shared_db["project_a_id"]
    assert found_in_b is None  # Project B cannot see project A's repository


async def test_explicit_project_id_overrides_scope(shared_db) -> None:
    """An explicit project_id parameter takes priority over _project_scope."""
    repo_a = shared_db["repo_a"]
    pid_b = shared_db["project_b_id"]

    # repo_a writes into project B with an explicit project_id=pid_b
    await repo_a.upsert_ecosystem_profile(
        _make_profile("forced/projectb"), project_id=pid_b
    )

    # repo_a itself (scope=A) cannot find it
    assert await repo_a.get_ecosystem_profile("forced/projectb") is None
    # Querying with an explicit project_id=pid_b is OK
    found = await repo_a.get_ecosystem_profile(
        "forced/projectb", project_id=pid_b
    )
    assert found is not None
    assert found.project_id == pid_b


# ------------------------------------------------------------------
# ScanRun isolation
# ------------------------------------------------------------------


async def test_scan_run_isolated(shared_db) -> None:
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    await repo_a.create_scan_run(
        EcosystemScanRun(strategy=EcosystemScanStrategy.INCREMENTAL)
    )
    await repo_a.create_scan_run(
        EcosystemScanRun(strategy=EcosystemScanStrategy.FULL)
    )
    await repo_b.create_scan_run(
        EcosystemScanRun(strategy=EcosystemScanStrategy.INCREMENTAL)
    )

    a_list = await repo_a.list_scan_runs(limit=100)
    b_list = await repo_b.list_scan_runs(limit=100)
    assert len(a_list) == 2
    assert len(b_list) == 1
    assert all(r.project_id == shared_db["project_a_id"] for r in a_list)
    assert b_list[0].project_id == shared_db["project_b_id"]


# ------------------------------------------------------------------
# DeepReview isolation
# ------------------------------------------------------------------


async def test_deep_review_isolated(shared_db) -> None:
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    # A writes a profile + a deep review
    p_a = _make_profile("dr/test")
    await repo_a.upsert_ecosystem_profile(p_a)
    profile_a = (await repo_a.search_ecosystem_profiles(limit=10))[0]

    await repo_a.create_deep_review(
        EcosystemDeepReview(
            repo_id=profile_a.id,
            status=EcosystemDeepReviewStatus.QUEUED,
        )
    )

    # B cannot see it
    a_reviews = await repo_a.list_deep_reviews(limit=100)
    b_reviews = await repo_b.list_deep_reviews(limit=100)
    assert len(a_reviews) == 1
    assert a_reviews[0].project_id == shared_db["project_a_id"]
    assert len(b_reviews) == 0


# ------------------------------------------------------------------
# RepoTag + Relation isolation
# ------------------------------------------------------------------


async def test_repo_tag_isolated(shared_db) -> None:
    global_repo = shared_db["global"]
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    # The global tag dictionary (project_id NULL)
    tag = EcosystemTag(
        name="memory_system",
        category=EcosystemTagCategory.CAPABILITY,
        description="long-term memory",
    )
    await global_repo.upsert_tag(tag)

    # Each of the two projects has one repository
    await repo_a.upsert_ecosystem_profile(_make_profile("a-team/repo"))
    await repo_b.upsert_ecosystem_profile(_make_profile("b-team/repo"))
    profile_a = (await repo_a.search_ecosystem_profiles(limit=10))[0]
    profile_b = (await repo_b.search_ecosystem_profiles(limit=10))[0]
    saved_tag = await global_repo.get_tag_by_name("memory_system")
    assert saved_tag is not None

    # Tag each one
    await repo_a.add_repo_tag(
        EcosystemRepoTag(
            repo_id=profile_a.id,
            tag_id=saved_tag.id,
            source=EcosystemTagSource.MANUAL,
        )
    )
    await repo_b.add_repo_tag(
        EcosystemRepoTag(
            repo_id=profile_b.id,
            tag_id=saved_tag.id,
            source=EcosystemTagSource.MANUAL,
        )
    )

    a_rt = await repo_a.list_repo_tags(limit=100)
    b_rt = await repo_b.list_repo_tags(limit=100)
    assert len(a_rt) == 1
    assert len(b_rt) == 1
    assert a_rt[0].project_id == shared_db["project_a_id"]
    assert b_rt[0].project_id == shared_db["project_b_id"]

    # The tag dictionary stays global: both projects can find the same tag dictionary row
    assert (await global_repo.list_tags(limit=10))[0].name == "memory_system"


async def test_relation_isolated(shared_db) -> None:
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    # Project A: two repositories + one relation
    await repo_a.upsert_ecosystem_profile(_make_profile("rel/x"))
    await repo_a.upsert_ecosystem_profile(_make_profile("rel/y"))
    a_profiles = await repo_a.search_ecosystem_profiles(limit=10)
    assert len(a_profiles) == 2
    pa_x = next(p for p in a_profiles if p.repo_full_name == "rel/x")
    pa_y = next(p for p in a_profiles if p.repo_full_name == "rel/y")

    await repo_a.add_relation(
        EcosystemRelation(
            from_repo_id=pa_x.id,
            to_repo_id=pa_y.id,
            relation_type=EcosystemRelationType.INSPIRED_BY,
        )
    )

    a_rels = await repo_a.list_relations(limit=100)
    b_rels = await repo_b.list_relations(limit=100)
    assert len(a_rels) == 1
    assert a_rels[0].project_id == shared_db["project_a_id"]
    assert len(b_rels) == 0


# ------------------------------------------------------------------
# Backfill scenario
# ------------------------------------------------------------------


async def test_backfill_to_project(shared_db) -> None:
    """Legacy rows (project_id=NULL) are migrated to the specified project via backfill;
    after migration, repo_a's search can see them and repo_b cannot."""
    global_repo = shared_db["global"]
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    # Simulate legacy data: write a NULL project_id using global_repo (no project_scope)
    legacy_profile = _make_profile("legacy/repo-1")
    legacy_profile.project_id = None  # Explicitly ensure NULL
    await global_repo.upsert_ecosystem_profile(legacy_profile)

    legacy_run = EcosystemScanRun(strategy=EcosystemScanStrategy.INCREMENTAL)
    await global_repo.create_scan_run(legacy_run)

    # Before migration, neither repo_a nor repo_b can see it
    assert len(await repo_a.search_ecosystem_profiles(limit=100)) == 0
    assert len(await repo_b.search_ecosystem_profiles(limit=100)) == 0

    # Backfill to project A
    counts = await global_repo.backfill_ecosystem_to_project(
        shared_db["project_a_id"]
    )
    assert counts["ecosystem_repo_profiles"] == 1
    assert counts["ecosystem_scan_runs"] == 1

    # After migration, repo_a sees it and repo_b does not
    a_after = await repo_a.search_ecosystem_profiles(limit=100)
    b_after = await repo_b.search_ecosystem_profiles(limit=100)
    assert len(a_after) == 1
    assert a_after[0].repo_full_name == "legacy/repo-1"
    assert a_after[0].project_id == shared_db["project_a_id"]
    assert len(b_after) == 0

    # A second backfill is idempotent: the NULL rows no longer exist, so nothing new is added
    counts_again = await global_repo.backfill_ecosystem_to_project(
        shared_db["project_a_id"]
    )
    assert all(v == 0 for v in counts_again.values())


async def test_backfill_rejects_unknown_project(shared_db) -> None:
    """A non-existent project_id should raise an exception."""
    import pytest

    global_repo = shared_db["global"]
    with pytest.raises(ValueError):
        await global_repo.backfill_ecosystem_to_project("not-a-real-id")


# ------------------------------------------------------------------
# Cross-project scope override
# ------------------------------------------------------------------


async def test_search_extended_scoped(shared_db) -> None:
    """search_ecosystem_profiles_extended also respects the scope."""
    repo_a = shared_db["repo_a"]
    repo_b = shared_db["repo_b"]

    await repo_a.upsert_ecosystem_profile(_make_profile("ext/in-a-1"))
    await repo_a.upsert_ecosystem_profile(_make_profile("ext/in-a-2"))
    await repo_b.upsert_ecosystem_profile(_make_profile("ext/in-b"))

    rows_a, total_a = await repo_a.search_ecosystem_profiles_extended(limit=100)
    rows_b, total_b = await repo_b.search_ecosystem_profiles_extended(limit=100)

    assert total_a == 2
    assert total_b == 1
    assert all(r.project_id == shared_db["project_a_id"] for r in rows_a)
    assert rows_b[0].project_id == shared_db["project_b_id"]
