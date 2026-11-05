"""The server-side half of the mirror contract: idempotency + correct placement of CC-dependent ids.

CC may report completion for the same task repeatedly (reruns, session resumes), so the task-creation end must be
idempotent on
``cc_task_id`` — otherwise duplicate rows would grow on the wall. And a CC ``blockedBy`` holds
**CC's own task ids**; stuffing them directly into the OS ``depends_on`` would make the blocking determination look up a
row that
does not exist at all, so only upstreams that have already been mirrored are converted into depends_on; the rest are
preserved as evidence in their original form.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def client(repo):
    from fastapi.testclient import TestClient

    from aiteam.api import deps
    from aiteam.api.app import create_app

    app = create_app()
    app.dependency_overrides[deps.get_repository] = lambda: repo
    app.dependency_overrides[deps.get_scoped_repository] = lambda: repo
    with TestClient(app) as c:
        yield c


@pytest_asyncio.fixture()
async def project(repo):
    return await repo.create_project(name="p", root_path="/tmp/p")


def _mirror(client, project_id, **body):
    payload = {
        "title": "t",
        "description": "d",
        "tags": ["cc-task"],
        "status": "completed",
        **body,
    }
    return client.post(f"/api/projects/{project_id}/tasks", json=payload)


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_same_cc_task_never_creates_a_second_row(self, client, project, repo):
        first = _mirror(client, project.id, cc_task_id="20").json()
        second = _mirror(client, project.id, cc_task_id="20", title="Changed title").json()

        assert second["data"]["id"] == first["data"]["id"]
        assert second["data"]["title"] == "t"  # An existing row is not overwritten
        assert len(await repo.list_tasks_by_project(project.id)) == 1

    @pytest.mark.asyncio
    async def test_different_cc_tasks_still_create_separate_rows(self, client, project, repo):
        _mirror(client, project.id, cc_task_id="20")
        _mirror(client, project.id, cc_task_id="21")
        assert len(await repo.list_tasks_by_project(project.id)) == 2

    @pytest.mark.asyncio
    async def test_plain_tasks_without_a_cc_id_are_unaffected(self, client, project, repo):
        _mirror(client, project.id)
        _mirror(client, project.id)
        assert len(await repo.list_tasks_by_project(project.id)) == 2


class TestCompletionLedgerFields:
    @pytest.mark.asyncio
    async def test_owner_and_completion_are_recorded(self, client, project):
        data = _mirror(client, project.id, cc_task_id="20", assigned_to="alice").json()["data"]
        assert data["assigned_to"] == "alice"
        assert data["status"] == "completed"
        assert data["completed_at"] is not None
        assert data["cc_task_id"] == "20"

    @pytest.mark.asyncio
    async def test_default_creation_is_still_a_pending_task(self, client, project):
        body = {"title": "Ordinary task", "description": ""}
        data = client.post(f"/api/projects/{project.id}/tasks", json=body).json()["data"]
        assert data["status"] == "pending"
        assert data["completed_at"] is None
        assert data["cc_task_id"] is None


class TestDependencyResolution:
    @pytest.mark.asyncio
    async def test_mirrored_upstream_becomes_a_real_depends_on(self, client, project):
        upstream = _mirror(client, project.id, cc_task_id="19").json()["data"]
        downstream = _mirror(
            client, project.id, cc_task_id="20", cc_blocked_by=["19"]
        ).json()["data"]

        assert downstream["depends_on"] == [upstream["id"]]
        assert downstream["config"]["cc_blocked_by_unresolved"] == []

    @pytest.mark.asyncio
    async def test_unmirrored_upstream_is_kept_as_provenance_not_a_fake_id(
        self, client, project
    ):
        """A CC id must never impersonate an OS task id and sneak into depends_on."""
        data = _mirror(
            client, project.id, cc_task_id="20", cc_blocked_by=["19", "18"]
        ).json()["data"]

        assert data["depends_on"] == []
        assert data["config"]["cc_blocked_by"] == ["19", "18"]
        assert data["config"]["cc_blocked_by_unresolved"] == ["19", "18"]

    @pytest.mark.asyncio
    async def test_partially_mirrored_chain_splits_correctly(self, client, project):
        upstream = _mirror(client, project.id, cc_task_id="19").json()["data"]
        data = _mirror(
            client, project.id, cc_task_id="20", cc_blocked_by=["19", "18"]
        ).json()["data"]

        assert data["depends_on"] == [upstream["id"]]
        assert data["config"]["cc_blocked_by_unresolved"] == ["18"]
