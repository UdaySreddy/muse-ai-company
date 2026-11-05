"""AI Team OS — standalone mode tests (no Claude Code, no network, no API key)."""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core", reason="requires ai-team-os[langgraph] extra")
pytest.importorskip("langgraph", reason="requires ai-team-os[langgraph] extra")

from aiteam.orchestrator.llm_factory import build_llm, resolve_llm
from aiteam.types import TaskStatus


class TestBuildLlm:
    async def test_fake_provider_cycles_responses(self) -> None:
        llm = build_llm("fake", responses=["one", "two"])
        assert (await llm.ainvoke("hi")).content == "one"
        assert (await llm.ainvoke("hi")).content == "two"

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            build_llm("bogus")

    def test_anthropic_without_key_is_a_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("langchain_anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            build_llm("anthropic")

    def test_resolve_prefers_injected_llm(self) -> None:
        sentinel = object()
        assert resolve_llm({"llm": sentinel}) is sentinel

    async def test_resolve_builds_from_provider(self) -> None:
        llm = resolve_llm({"llm_provider": "fake"})
        assert (await llm.ainvoke("hi")).content  # default canned response


class TestCompanyTeam:
    async def test_company_roles_include_coo_and_cto(self, db_repository) -> None:  # type: ignore[no-untyped-def]
        from aiteam.orchestrator.team_manager import TeamManager
        from aiteam.standalone import COMPANY_ROLES, ensure_company_team

        names = {name for name, _role, _prompt in COMPANY_ROLES}
        assert {"coo", "cto", "qa-engineer"} <= names

        manager = TeamManager(repository=db_repository, memory=None)
        await ensure_company_team(manager)
        agents = await manager.list_agents("muse-company")
        assert names == {a.name for a in agents}

        # Idempotent: a second call adds nobody twice.
        await ensure_company_team(manager)
        agents = await manager.list_agents("muse-company")
        assert len(agents) == len(names)

    async def test_run_company_task_offline_fake(self, db_repository) -> None:  # type: ignore[no-untyped-def]
        """Full CEO -> COO/CTO/engineers/QA -> CEO loop with the fake provider.

        db_repository points StorageRepository at in-memory SQLite; the
        standalone runner must accept the same URL so this test exercises the
        real run_company_task wiring, not a copy of it.
        """
        from aiteam.standalone import run_company_task

        result = await run_company_task(
            "Draft a launch plan",
            provider="fake",
            db_url="sqlite+aiosqlite://",
            responses=["plan", "coo-out", "cto-out", "be-out", "fe-out", "qa-out", "final"],
        )
        assert result.status == TaskStatus.COMPLETED
        assert result.result == "final"
        assert set(result.agent_outputs) == {
            "coo",
            "cto",
            "backend-engineer",
            "frontend-engineer",
            "qa-engineer",
        }
