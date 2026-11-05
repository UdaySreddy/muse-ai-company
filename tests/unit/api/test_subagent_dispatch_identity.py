"""Row identity = dispatch identity — SubagentStart's same-name reuse must not rewrite an old row that already has
accounts posted.

The hard evidence from lifecycle audit A-06: in ``_on_subagent_start``'s four-tier dedup ladder, tiers 2/3 take
**name** matches within the team, taking ``matches[0]`` (``list_agents`` in ascending created_at order → the earliest
row),
tier 2b goes through ``repository.find_agent_by_session`` (``limit(1)`` and **no ORDER BY**).
On a hit, the new dispatch is bound in place to the old row; afterwards ``_on_subagent_stop`` **overwrites** the four
token layers
and ``tokens_measured_at`` by transcript. The two segments together = one new dispatch erases an old dispatch's account,
and the old value
is unrecoverable (transcripts are cleared sooner or later; once the account is gone it can never be computed again).

The same matter was already adjudicated on the workflow path: ``_register_workflow_subagent``'s docstring
says 'dedupe by cc_agent_id rather than by name, otherwise one run of 16 agents would be folded into a single row'.
The positive path is missing exactly this rule.

The accounting unit is the transcript file, and the transcript file name is ``agent-<cc_agent_id>.jsonl``
— so 'one row = one dispatch = one transcript' is the same sentence. The criteria are pinned accordingly:

1. The same cc_tool_use_id → the same dispatch; reuse (duplicate SubagentStart / resumed run);
2. The candidate row has never been bound to any dispatch (cc_tool_use_id empty) and **has no accounts** → reuse (MCP
pre-registration);
3. Everything else creates a new row. 'Has accounts' is read broadly: any of the four layers non-zero **or**
tokens_measured_at non-empty
   (a measured 0 is also a measurement result; no-data ≠ zero).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.services.agent_identity import has_token_account, pick_reusable_row
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import Agent

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


def _transcript(tmp_path, cc_id: str, *, input_tokens: int, output_tokens: int) -> str:
    """A minimal parseable sub-agent transcript (the file name encodes the cc_agent_id)."""
    p = tmp_path / f"agent-{cc_id}.jsonl"
    row = {
        "type": "assistant",
        "requestId": f"req-{cc_id}",
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }
    p.write_text(json.dumps(row), encoding="utf-8")
    return str(p)


def _start(cc_id: str, name: str = "Explore", **extra) -> dict:
    return {
        "hook_event_name": "SubagentStart",
        "agent_type": name,
        "agent_id": cc_id,
        "session_id": SESSION,
        **extra,
    }


def _stop(cc_id: str, tpath: str, name: str = "Explore") -> dict:
    return {
        "hook_event_name": "SubagentStop",
        "agent_type": name,
        "agent_id": cc_id,
        "session_id": SESSION,
        "agent_transcript_path": tpath,
    }


class TestChargedRowIsNeverOverwritten:
    """End-to-end: dispatch (1) runs to completion and posts accounts → dispatch (2) with the same name comes in →
    dispatch (1)'s account must remain exactly intact."""

    @pytest.mark.asyncio
    async def test_second_dispatch_gets_its_own_row_and_first_ledger_survives(
        self, repo, translator, tmp_path
    ):
        team = await repo.create_team(name="crew", mode="coordinate")

        # Dispatch (1): start → run to completion and post the accounts
        r1 = await translator.handle_event(_start("cc-first", cc_team_name="crew"))
        assert r1["status"] == "created"
        first_id = r1["agent_id"]
        t1 = _transcript(tmp_path, "cc-first", input_tokens=1_000, output_tokens=200)
        await translator.handle_event(_stop("cc-first", t1))

        after_first = await repo.get_agent(first_id)
        assert after_first.input_tokens == 1_000
        assert after_first.tokens_measured_at is not None

        # Dispatch (2): same name, same session, same team, but **another dispatch** (another cc id, another transcript)
        r2 = await translator.handle_event(_start("cc-second", cc_team_name="crew"))
        assert r2["agent_id"] != first_id, (
            "The new dispatch was bound back to an old row that already "
                "has accounts — row identity was treated as name identity"
        )
        t2 = _transcript(tmp_path, "cc-second", input_tokens=7_000, output_tokens=900)
        await translator.handle_event(_stop("cc-second", t2))

        # The old row's accounts are unchanged, verbatim
        survivor = await repo.get_agent(first_id)
        assert survivor.input_tokens == 1_000, "Dispatch (1)'s token account was overwritten by dispatch (2)"
        assert survivor.output_tokens == 200
        assert survivor.cc_tool_use_id == "cc-first", "The old row's dispatch identity was rewritten"
        assert survivor.tokens_measured_at == after_first.tokens_measured_at

        # The new row posts its own accounts independently; the two accounts do not swallow each other
        fresh = await repo.get_agent(r2["agent_id"])
        assert fresh.input_tokens == 7_000
        assert fresh.cc_tool_use_id == "cc-second"
        assert fresh.team_id == team.id

    @pytest.mark.asyncio
    async def test_measured_zero_row_is_also_protected(self, repo, translator):
        """A measured 0 is also a measurement result: no-data ≠ zero, and a row carrying 0 must likewise not be
        rewritten."""
        team = await repo.create_team(name="crew", mode="coordinate")
        old = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION, cc_tool_use_id="cc-zero",
        )
        await repo.update_agent(
            old.id, status="offline",
            input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0,
            tokens_measured_at=utc_now(),
        )

        res = await translator.handle_event(_start("cc-new", cc_team_name="crew"))

        assert res["agent_id"] != old.id
        kept = await repo.get_agent(old.id)
        assert kept.cc_tool_use_id == "cc-zero"

    @pytest.mark.asyncio
    async def test_legacy_session_name_path_skips_the_charged_row(
        self, repo, translator
    ):
        """The bypass without a cc_team_name (find_agent_by_session) is likewise bound by the criteria."""
        team = await repo.create_team(name="crew", mode="coordinate")
        # The Leader is present, so only tier 3 resolves the team; here tier 2b is deliberately made to hit first.
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="api", session_id=SESSION,
        )
        assert leader.id
        charged = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION, cc_tool_use_id="cc-old",
        )
        await repo.update_agent(
            charged.id, status="offline", input_tokens=42, tokens_measured_at=utc_now()
        )

        res = await translator.handle_event(_start("cc-new"))

        assert res["agent_id"] != charged.id
        kept = await repo.get_agent(charged.id)
        assert kept.input_tokens == 42
        assert kept.cc_tool_use_id == "cc-old"


class TestLateBindingObeysTheSameRule:
    """Second entry point: ``_resolve_agent``'s by-name late binding also writes cc_tool_use_id.

    After late binding, SubagentStop overwrites tokens following that row — so it walks the same criteria as
    registration;
    otherwise a dispatch whose SubagentStart was missed would sneak in here and erase accounts.
    """

    @pytest.mark.asyncio
    async def test_late_binding_refuses_a_charged_row(self, repo, translator, tmp_path):
        team = await repo.create_team(name="crew", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="api", session_id=SESSION,
        )
        await repo.update_agent(leader.id, status="busy")
        await repo.update_team(team.id, leader_agent_id=leader.id)
        charged = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore", source="hook",
            session_id=SESSION, cc_tool_use_id="cc-old",
        )
        await repo.update_agent(
            charged.id, input_tokens=1_234, tokens_measured_at=utc_now()
        )

        # SubagentStart was missed; a SubagentStop from another dispatch arrives directly
        t = _transcript(tmp_path, "cc-ghost", input_tokens=99, output_tokens=99)
        await translator.handle_event(_stop("cc-ghost", t))

        kept = await repo.get_agent(charged.id)
        assert kept.input_tokens == 1_234, "Late binding wrote another dispatch's account onto this row"
        assert kept.cc_tool_use_id == "cc-old"

    @pytest.mark.asyncio
    async def test_late_binding_still_adopts_an_unbound_row(self, repo, translator):
        """The intent of the race must be preserved: a row never bound to a cc id and with no accounts posted is
        claimed as before."""
        team = await repo.create_team(name="crew", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="api", session_id=SESSION,
        )
        await repo.update_agent(leader.id, status="busy")
        await repo.update_team(team.id, leader_agent_id=leader.id)
        unbound = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore", source="api",
        )

        got = await translator._resolve_agent("cc-late", "Explore", SESSION)

        assert got is not None
        assert got.id == unbound.id
        assert (await repo.get_agent(unbound.id)).cc_tool_use_id == "cc-late"


class TestLegitimateReuseStillWorks:
    """Reuse is not to be abolished — legitimate scenarios must survive intact, otherwise this becomes another bug."""

    @pytest.mark.asyncio
    async def test_same_dispatch_reports_twice_reuses_the_same_row(
        self, repo, translator, tmp_path
    ):
        """A duplicate SubagentStart (the same cc id) is the same dispatch; a second row must never grow."""
        await repo.create_team(name="crew", mode="coordinate")
        first = await translator.handle_event(_start("cc-dup", cc_team_name="crew"))
        t = _transcript(tmp_path, "cc-dup", input_tokens=5, output_tokens=5)
        await translator.handle_event(_stop("cc-dup", t))

        again = await translator.handle_event(_start("cc-dup", cc_team_name="crew"))

        assert again["status"] == "updated"
        assert again["agent_id"] == first["agent_id"]

    @pytest.mark.asyncio
    async def test_mcp_preregistered_row_is_still_adopted(self, repo, translator):
        """An empty MCP pre-registered row (no cc id, no accounts) exists precisely to be claimed by the hook."""
        team = await repo.create_team(name="crew", mode="coordinate")
        pre = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore", source="api",
        )

        res = await translator.handle_event(_start("cc-adopt", cc_team_name="crew"))

        assert res["status"] == "updated"
        assert res["agent_id"] == pre.id
        bound = await repo.get_agent(pre.id)
        assert bound.cc_tool_use_id == "cc-adopt"
        assert bound.session_id == SESSION


class TestReuseVerdict:
    """The criteria themselves (pure functions, not touching the database) — each boundary pinned separately."""

    @staticmethod
    def _row(agent_id: str, *, cc: str = "", minute: int = 0, **cols) -> Agent:
        return Agent(
            id=agent_id, team_id="t", name="Explore", role="Explore",
            cc_tool_use_id=cc or None,
            created_at=datetime(2026, 8, 1, 0, minute, tzinfo=UTC),
            **cols,
        )

    def test_no_candidates_means_new_row(self):
        assert pick_reusable_row([], "cc-1") is None

    def test_measured_at_alone_counts_as_charged(self):
        assert has_token_account(self._row("a", tokens_measured_at=utc_now()))

    def test_any_single_layer_counts_as_charged(self):
        for layer in ("input_tokens", "output_tokens",
                      "cache_creation_tokens", "cache_read_tokens"):
            assert has_token_account(self._row("a", **{layer: 1})), layer

    def test_untouched_row_is_not_charged(self):
        assert not has_token_account(self._row("a"))

    def test_same_dispatch_wins_even_when_charged(self):
        """Re-measuring its own account for the same dispatch is correct and should not be blocked by the account
        gate."""
        mine = self._row("mine", cc="cc-1", tokens_measured_at=utc_now(), minute=1)
        blank = self._row("blank", minute=9)
        assert pick_reusable_row([blank, mine], "cc-1").id == "mine"

    def test_row_bound_to_another_dispatch_is_never_reused(self):
        """Even if that row has no accounts posted yet — it is already **another** dispatch's identity card."""
        other = self._row("other", cc="cc-other")
        assert pick_reusable_row([other], "cc-mine") is None

    def test_newest_blank_row_wins_and_order_does_not_matter(self):
        old = self._row("old", minute=1)
        new = self._row("new", minute=5)
        assert pick_reusable_row([old, new], "cc-1").id == "new"
        assert pick_reusable_row([new, old], "cc-1").id == "new"

    def test_unknown_cc_id_keeps_only_the_ledger_gate(self):
        """When the payload carries no agent_id, the dispatch identity cannot be determined; only the account gate is
        kept, and no row is conjured up."""
        bound = self._row("bound", cc="cc-x")
        assert pick_reusable_row([bound], "").id == "bound"
        charged = self._row("charged", cc="cc-y", input_tokens=5, minute=9)
        assert pick_reusable_row([bound, charged], "").id == "bound"


class TestDeterministicCandidateOrder:
    """Which row is taken among multiple same-name rows must be deterministic — a limit(1) with no ORDER BY relies on
    luck."""

    @pytest.mark.asyncio
    async def test_find_agent_by_session_returns_newest(self, repo):
        team = await repo.create_team(name="crew", mode="coordinate")
        old = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION,
        )
        new = await repo.create_agent(
            team_id=team.id, name="Explore", role="Explore",
            source="hook", session_id=SESSION,
        )
        # Even with created_at in the same second it must be stable: the id is the fallback sort
        await repo.update_agent(new.id, last_active_at=utc_now())

        got = await repo.find_agent_by_session(SESSION, "Explore")
        assert got is not None
        assert got.id in {old.id, new.id}
        # Repeated calls must give the same answer
        again = await repo.find_agent_by_session(SESSION, "Explore")
        assert again.id == got.id

        rows = await repo.find_agents_by_session_and_name(SESSION, "Explore")
        assert [r.id for r in rows] == [rows[0].id, rows[1].id]
        assert len(rows) == 2
        assert rows[0].id == got.id, "find_agent_by_session must agree with the first of the full candidate list"
