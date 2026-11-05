"""Multiple container teams from the same CC process must be able to be grouped together at the display layer.

CC's team directory stamps ``leadSessionId`` at the moment the team is created and **never updates it**, while the same
process changing sessions
(ending and reopening, resuming) only updates ``sessionId`` in ``~/.claude/sessions/<pid>.json``.
So one live process leaves multiple container teams on the OS side: the old completed ones, the new active ones. User
ruling
(2026-07-27): the ownership logic is not touched at all; they are only merged into one group at the display layer.

Merging only recognizes **provable** relationships, never claimed kinship:
- Tier (1): at team creation, ``config.cc_pid`` stamped from the registry by an exact session_id match;
- Tier (2): read the registry live; owner_session_id happens to be some process's current session;
- If neither holds → unknown, a row of its own.

Deliberately no cwd + startup-time-window matching: local measurements show pid 32147 and 32220 starting under **the
same cwd**
1.0 seconds apart, with the team directory's createdAt falling right between the two; approximate matching on real data
is simply
ambiguous.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps, session_registry
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

LIVE_PID = 32220
SESSION_NOW = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
SESSION_OLD = "0def8f84-3b72-4b09-ae42-b13365ffeb65"


def _write_registry(tmp_path, monkeypatch, records: list[dict]) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for record in records:
        (sessions / f"{record['pid']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    monkeypatch.setattr(session_registry, "_sessions_dir", lambda: sessions)


def _record(pid: int, session_id: str) -> dict:
    return {
        "pid": pid,
        "sessionId": session_id,
        "cwd": "/Users/dev/Desktop/AI team OS",
        "startedAt": 1785116991072,
        "version": "2.1.219",
        "kind": "interactive",
        "entrypoint": "cli",
        "name": "ai-team-os-18",
        "status": "busy",
    }


@pytest.fixture()
def client():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = EventBus(repo=repo)
    deps._manager = TeamManager(repository=repo, memory=memory)

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    yield TestClient(app), repo

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None


class TestBirthStamp:
    """When creating a container team, record the pid at that moment — the registry can only answer for the current
    session; asking afterwards is too late."""

    @pytest.mark.asyncio
    async def test_container_team_records_the_owning_process(
        self, db_repository, tmp_path, monkeypatch
    ):
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        translator = HookTranslator(repo=db_repository, event_bus=EventBus(repo=db_repository))

        team = await translator._find_or_create_session_team(SESSION_NOW, {})

        assert team.config["cc_pid"] == LIVE_PID
        assert team.config["owner_session_id"] == SESSION_NOW

    @pytest.mark.asyncio
    async def test_unregistered_session_is_left_blank_never_guessed(
        self, db_repository, tmp_path, monkeypatch
    ):
        """If the session is not in the registry (an old session / a test / a workflow injection), leave it empty;
        guessing is not allowed."""
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        translator = HookTranslator(repo=db_repository, event_bus=EventBus(repo=db_repository))

        team = await translator._find_or_create_session_team(SESSION_OLD, {})

        assert "cc_pid" not in team.config

    @pytest.mark.asyncio
    async def test_registry_failure_does_not_block_team_creation(
        self, db_repository, monkeypatch
    ):
        def boom():
            raise OSError("registry unreadable")

        monkeypatch.setattr(session_registry, "read_sessions", boom)
        translator = HookTranslator(repo=db_repository, event_bus=EventBus(repo=db_repository))

        team = await translator._find_or_create_session_team(SESSION_NOW, {})

        assert team.name == "session-80d0cc5e"
        assert "cc_pid" not in team.config


class TestListResolution:
    def test_stamped_pid_is_reported(self, client, tmp_path, monkeypatch):
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [])  # The registry no longer has this process; rely on the stamp that
        # was made
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-0def8f84",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_OLD, "cc_pid": LIVE_PID},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] == LIVE_PID

    def test_live_session_resolves_through_the_registry(
        self, client, tmp_path, monkeypatch
    ):
        """A legacy team was never stamped, but its session is exactly some process's current session — that also
        counts as evidence."""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-80d0cc5e",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_NOW},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] == LIVE_PID

    def test_two_containers_of_one_process_share_a_pid(
        self, client, tmp_path, monkeypatch
    ):
        """Exactly the phenomenon to fix: one live process, two container teams, merged into the same group."""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            repo.create_team(
                name="session-0def8f84",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_OLD, "cc_pid": LIVE_PID},
            )
        )
        loop.run_until_complete(
            repo.create_team(
                name="session-80d0cc5e",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_NOW},
            )
        )

        teams = client.get("/api/teams").json()["data"]

        assert {t["cc_pid"] for t in teams} == {LIVE_PID}

    def test_unprovable_container_stands_alone(
        self, client, tmp_path, monkeypatch
    ):
        """A historical session cannot be found in the registry (when a process changes session it is rewritten in
        place, the old value leaves no trace) — leave it empty."""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-0def8f84",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_OLD},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] is None

    def test_non_container_teams_are_not_resolved(
        self, client, tmp_path, monkeypatch
    ):
        """Each workflow team forms its own collection per run and does not participate in process merging."""
        client, repo = client
        _write_registry(tmp_path, monkeypatch, [_record(LIVE_PID, SESSION_NOW)])
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="workflow-wf_374d39b3",
                mode="coordinate",
                config={"kind": "workflow", "owner_session_id": SESSION_NOW},
            )
        )

        (team,) = client.get("/api/teams").json()["data"]

        assert team["cc_pid"] is None

    def test_registry_failure_still_lists_teams(self, client, monkeypatch):
        client, repo = client
        def boom():
            raise OSError("registry unreadable")

        monkeypatch.setattr(session_registry, "read_sessions", boom)
        asyncio.get_event_loop().run_until_complete(
            repo.create_team(
                name="session-80d0cc5e",
                mode="coordinate",
                config={"kind": "session", "owner_session_id": SESSION_NOW},
            )
        )

        resp = client.get("/api/teams")

        assert resp.status_code == 200
        assert resp.json()["data"][0]["cc_pid"] is None
