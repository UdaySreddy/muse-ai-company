"""Sub-agent token attribution — closing the observability broken chain, batch (3).

Background: the agents table previously had only a 'context watermark' metric (ctx_tokens/ctx_pct), and **no billing
metric
collection at all**. On the workflow side, workflow_agents.tokens were also largely empty — of 170 measured rows with
model='opus',
170 rows had tokens of 0/NULL, and the distribution continues to the most recent (123 rows on 07-21 alone) — not an
early legacy.

The root cause is not broken collection but that **the data source holds only request specifications**: in the same
workflow JSON, 82 agents
only 'want a certain model' (model written as the alias opus verbatim, tokens null), and only 24 brought back telemetry.
the OS persists them as-is, so alias rows naturally have zero tokens.

Key finding: these agents' **transcripts exist in full**, and in the transcripts ``message.model``
**forever the full model number** (measured 'claude-opus-5' / 'claude-opus-4-8'), never an alias. So the real
model and tokens can both be losslessly re-collected from the transcript — the alias mapping table only needs to cover
rows whose transcript is already lost,
as a fallback, and **only resolves on the read side, never writing back to the model field** (the 2026-07-07 iron law:
when the model field is unknown it stays empty and is filled
in by observation, not hard-coded to a model number).

The accumulation algorithm is the most error-prone part of this item: one API call produces **multiple** assistant rows
(one per
content blocks), measured: only 17 unique requestIds among 79 rows; within the same requestId,
input/cache stay constant while output_tokens **increases** with streaming. So the correct algorithm is
**group by requestId, take the last snapshot in each group, then accumulate across groups**. Bare per-row addition would
seriously inflate:
measured input would rise from a true value of 12,185 to 72,556, and cache_read from 1,289,742 to 5,105,773.
"""

from __future__ import annotations

import json

from aiteam.services.token_attribution import (
    parse_transcript_usage,
    resolve_model_alias,
)


def _write(tmp_path, rows):
    p = tmp_path / "agent-x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _assistant(request_id, model, usage):
    return {"type": "assistant", "requestId": request_id, "message": {"model": model, "usage": usage}}


class TestUsageParsing:
    def test_streaming_rows_are_not_double_counted(self, tmp_path):
        """Multiple rows with the same requestId are snapshots of the same call; take the last one, not their sum."""
        rows = [
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 3,
                                                  "cache_read_input_tokens": 100,
                                                  "cache_creation_input_tokens": 50}),
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 200,
                                                  "cache_read_input_tokens": 100,
                                                  "cache_creation_input_tokens": 50}),
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 583,
                                                  "cache_read_input_tokens": 100,
                                                  "cache_creation_input_tokens": 50}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["output_tokens"] == 583, "Streaming increments were added row by row"
        assert got["input_tokens"] == 10
        assert got["cache_read_tokens"] == 100
        assert got["cache_creation_tokens"] == 50
        assert got["api_calls"] == 1

    def test_multiple_calls_accumulate(self, tmp_path):
        rows = [
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 5}),
            _assistant("req-2", "claude-opus-5", {"input_tokens": 7, "output_tokens": 11}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["input_tokens"] == 17
        assert got["output_tokens"] == 16
        assert got["api_calls"] == 2

    def test_model_comes_from_transcript_verbatim(self, tmp_path):
        """The transcript holds the full model number; collect it as is — this is 'backfilling by observation'."""
        rows = [_assistant("req-1", "claude-opus-4-8", {"input_tokens": 1, "output_tokens": 1})]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["model"] == "claude-opus-4-8"
        assert got["model_source"] == "transcript"

    def test_non_assistant_rows_ignored(self, tmp_path):
        rows = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "attachment"},
            _assistant("req-1", "claude-opus-5", {"input_tokens": 4, "output_tokens": 2}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["input_tokens"] == 4
        assert got["api_calls"] == 1

    def test_missing_file_yields_nothing_not_zero(self, tmp_path):
        """A missing file ≠ 0 tokens used. no-data must be distinguished from zero."""
        got = parse_transcript_usage(tmp_path / "nope.jsonl")
        assert got is None

    def test_synthetic_model_line_does_not_become_the_model(self, tmp_path):
        """A compact synthetic row's "<synthetic>" is not a model number and must not overwrite the real model.

        The way model is taken is 'a later one overwrites an earlier one', so as long as a transcript has, at its **very
        end**, one
        synthetic row, the whole attribution's model would be written as "<synthetic>". Sub-agent transcripts generally
        do not contain synthetic rows, so this defect was never bitten by it; a main-session transcript has one right at
        the start — a measured
        35.1 MB main session parsed back a model of exactly "<synthetic>".
        """
        rows = [
            _assistant("req-1", "claude-opus-5", {"input_tokens": 10, "output_tokens": 5}),
            _assistant("req-2", "<synthetic>", {"input_tokens": 2, "output_tokens": 1}),
        ]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["model"] == "claude-opus-5", "A synthetic row overwrote the real model"
        assert got["model_source"] == "transcript"
        # Only the model is filtered; usage is untouched: the two calls accumulate as usual (changing the metric is not
        # this task)
        assert got["input_tokens"] == 12
        assert got["api_calls"] == 2

    def test_all_synthetic_yields_unknown_model_not_a_fake_one(self, tmp_path):
        """When everything is synthetic rows, truthfully return 'model unknown'; do not fabricate one."""
        rows = [_assistant("req-1", "<synthetic>", {"input_tokens": 3, "output_tokens": 1})]
        got = parse_transcript_usage(_write(tmp_path, rows))
        assert got["model"] == ""
        assert got["model_source"] == "unknown"
        assert got["input_tokens"] == 3, "What is filtered is the model, not the usage"

    def test_synthetic_marker_is_shared_not_copied(self):
        """There is only one definition of the synthetic-row marker — copied into two, one of them would forget to
        skip it."""
        from aiteam.api import session_probe
        from aiteam.services import token_attribution

        assert token_attribution.SYNTHETIC_MODEL is session_probe.SYNTHETIC_MODEL

    def test_corrupt_lines_are_skipped(self, tmp_path):
        p = tmp_path / "agent-x.jsonl"
        p.write_text(
            "not json\n"
            + json.dumps(_assistant("r1", "claude-opus-5", {"input_tokens": 3, "output_tokens": 1}))
            + "\n",
            encoding="utf-8",
        )
        got = parse_transcript_usage(p)
        assert got["input_tokens"] == 3


class TestAliasLedger:
    def test_alias_resolves_with_effective_date(self):
        """Aliases are resolved by effective date — the same opus points to different models in different periods."""
        entry = resolve_model_alias("opus", at="2026-07-21")
        assert entry is not None
        assert entry["resolved"].startswith("claude-opus")
        assert entry["evidence"], "The mapping must carry an evidence source; it cannot be a gut call"

    def test_unknown_alias_returns_none(self):
        assert resolve_model_alias("no-such-alias", at="2026-07-21") is None

    def test_before_any_window_returns_none(self):
        """Before the effective date, do not guess blindly."""
        assert resolve_model_alias("opus", at="2020-01-01") is None

    def test_ledger_is_append_only_shaped(self):
        """Every entry must carry an effective start and evidence, otherwise it is impossible to reconstruct
        afterwards what was running at the time."""
        from aiteam.services.token_attribution import MODEL_ALIAS_LEDGER

        for entry in MODEL_ALIAS_LEDGER:
            assert entry["effective_from"]
            assert entry["evidence"]
            assert entry["resolved"] != entry["alias"]


class TestEndToEndAttribution:
    """Demo path: dispatch a sub-agent → SubagentStop → tokens are attributed to the agent row."""

    async def test_subagent_stop_attributes_tokens_to_agent(self, tmp_path):
        import pytest_asyncio  # noqa: F401

        from aiteam.api.hook_translator import HookTranslator
        from aiteam.storage.connection import close_db
        from aiteam.storage.repository import StorageRepository
        from aiteam.types import EventType

        class _Bus:
            async def emit(self, event_type, source, data, **_kw):
                EventType(event_type)

        repo = StorageRepository(db_url="sqlite+aiosqlite://")
        await repo.init_db()
        try:
            team = await repo.create_team(name="d1-token", mode="coordinate")
            # Production shape: agents.cc_tool_use_id stores exactly CC's agentId,
            # and also the transcript file name's infix — the demo path's association key.
            agent = await repo.create_agent(
                team_id=team.id,
                name="worker-1",
                role="worker",
                session_id="sess-1",
                cc_tool_use_id="aworker-1-deadbeef",
            )
            tpath = _write(
                tmp_path,
                [
                    _assistant("r1", "claude-opus-5", {"input_tokens": 12, "output_tokens": 3,
                                                       "cache_read_input_tokens": 900,
                                                       "cache_creation_input_tokens": 40}),
                    _assistant("r1", "claude-opus-5", {"input_tokens": 12, "output_tokens": 77,
                                                       "cache_read_input_tokens": 900,
                                                       "cache_creation_input_tokens": 40}),
                ],
            )
            tr = HookTranslator(repo=repo, event_bus=_Bus())
            await tr.handle_event(
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": "sess-1",
                    "agent_id": "aworker-1-deadbeef",
                    "agent_type": "worker-1",
                    "agent_transcript_path": str(tpath),
                }
            )
            got = await repo.get_agent(agent.id)
            assert got.output_tokens == 77, "A streaming snapshot was added row by row, or was not attributed"
            assert got.input_tokens == 12
            assert got.cache_read_tokens == 900
            assert got.tokens_measured_at is not None
            # The model is backfilled from observation, not hard-coded
            assert got.model == "claude-opus-5"
        finally:
            await close_db()
