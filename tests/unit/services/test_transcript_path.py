"""Transcript path deriver — three sample classes + boundaries.

The samples follow the acceptance requirements of design §7 phase 1: **no wf segment / a worktree path / a non-ASCII
slug**,
three forms that have all really appeared (or will inevitably appear) in production:

* No wf: a directly dispatched sub-agent; of 1,923 registered paths in the production database, 106 are these;
* worktree: the cwd of a worktree-isolated agent is under ``.claude/worktrees/...``, so the slug
  encodes the whole worktree path into it, with far more segments than an ordinary project;
* Non-ASCII slug: directories like ``~/Desktop/文档`` are collapsed by ``[^a-zA-Z0-9] -> '-'`` into a string of
  hyphens, and multiple different directories collapse into **the same** slug (measured in production:
  ``-Users-dev-Desktop----``
  525 rows / ``-Users-dev-Desktop---`` 191 rows). This is exactly the origin of the rule 'the slug is only for
  cross-checking; ownership follows
  agents.project_id is authoritative" — so a dedicated set of assertions pins it down.
"""

from __future__ import annotations

import pytest

from aiteam.services.transcript_path import (
    derive_session_id,
    parse_transcript_path,
    slug_matches_root,
)

HOME = "/Users/dev"
SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


class TestSubagentPaths:
    def test_plain_subagent_has_no_wf_id(self):
        path = (
            f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
            f"{SESSION}/subagents/agent-a672b51dd77cd8dd0.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.kind == "subagent"
        assert ref.project_slug == "-Users-dev-Desktop-AI-team-OS"
        assert ref.session_id == SESSION
        assert ref.wf_id is None
        assert ref.cc_agent_id == "a672b51dd77cd8dd0"

    def test_workflow_subagent_carries_wf_id(self):
        path = (
            f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
            f"{SESSION}/subagents/workflows/wf_8cd4fced-95a/agent-af2d08f9a2862fe29.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.wf_id == "wf_8cd4fced-95a"
        assert ref.cc_agent_id == "af2d08f9a2862fe29"
        assert ref.session_id == SESSION

    def test_named_agent_id_with_dashes_survives(self):
        """cc_agent_id is not pure hex — production has ``agent-autc-final-gate-<hex>.jsonl``."""
        path = (
            f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
            f"{SESSION}/subagents/agent-autc-final-gate-4ada3682116587f3.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.cc_agent_id == "autc-final-gate-4ada3682116587f3"


class TestWorktreePaths:
    """An agent with worktree isolation: the cwd is deeper, the slug is longer, but the structure is unchanged."""

    SLUG = "-Users-dev-Desktop-AI-team-OS--claude-worktrees-agent-a45e819443a34dfb9"

    def test_worktree_slug_parses_and_keeps_session(self):
        path = (
            f"{HOME}/.claude/projects/{self.SLUG}/"
            f"{SESSION}/subagents/agent-ae25a929059984b04.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.project_slug == self.SLUG
        assert ref.session_id == SESSION
        assert ref.cc_agent_id == "ae25a929059984b04"

    def test_worktree_slug_cross_check_against_its_own_root(self):
        root = "/Users/dev/Desktop/AI team OS/.claude/worktrees/agent-a45e819443a34dfb9"
        assert slug_matches_root(self.SLUG, root) is True
        # It does not match the main checkout — this is exactly the scenario where a mismatch should be flagged
        assert slug_matches_root(self.SLUG, "/Users/dev/Desktop/AI team OS") is False


class TestNonAsciiSlug:
    def test_non_ascii_directory_collapses_but_still_parses(self):
        # ~/Desktop/文档 → -Users-dev-Desktop--- (each Chinese character becomes one hyphen)
        slug = "-Users-dev-Desktop---"
        path = f"{HOME}/.claude/projects/{slug}/{SESSION}/subagents/agent-abc123.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.project_slug == slug
        assert ref.session_id == SESSION

    def test_two_different_roots_collapse_to_one_slug(self):
        """Ownership cannot rely on the slug: different directories collapse into the same slug, and the reverse
        lookup is many-to-one."""
        slug = "-Users-dev-Desktop---"
        assert slug_matches_root(slug, "/Users/dev/Desktop/文档") is True
        assert slug_matches_root(slug, "/Users/dev/Desktop/资料") is True

    def test_mismatch_is_the_only_informative_answer(self):
        slug = "-Users-dev-Desktop-AI-team-OS"
        assert slug_matches_root(slug, "/Volumes/ext-disk/other-project") is False
        assert slug_matches_root(slug, None) is False
        assert slug_matches_root("", "/Users/dev") is False


class TestMainSessionPaths:
    def test_main_session_transcript(self):
        path = f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/{SESSION}.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.kind == "main"
        assert ref.session_id == SESSION
        assert ref.wf_id is None
        assert ref.cc_agent_id is None

    def test_uuid_v7_style_session_id_is_accepted(self):
        """A new CC session id has appeared in v7 form (measured in production: 019f8b2f-…-71d1-…); it must not be
        hard-blocked as v4."""
        sid = "019f8b2f-1617-71d1-a5fa-7f828d177065"
        path = f"{HOME}/.claude/projects/-Users-dev/{sid}.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.session_id == sid


class TestRefusals:
    @pytest.mark.parametrize(
        "path",
        [
            "",
            None,
            "/tmp/random.jsonl",
            # Missing the /projects/ segment
            f"{HOME}/.claude/-Users-dev/{SESSION}/subagents/agent-x.jsonl",
            # The session segment is not in uuid form
            f"{HOME}/.claude/projects/-slug/not-a-session/subagents/agent-x.jsonl",
            # Not a .jsonl
            f"{HOME}/.claude/projects/-slug/{SESSION}/subagents/agent-x.txt",
        ],
    )
    def test_unparseable_returns_none_never_guesses(self, path):
        assert parse_transcript_path(path) is None
        assert derive_session_id(path) is None


class TestPurity:
    def test_parsing_touches_no_disk(self):
        """Pure function: it parses even when the file the path points to does not exist at all."""
        path = f"/nowhere/projects/-slug/{SESSION}/subagents/agent-ghost.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None and ref.cc_agent_id == "ghost"

    def test_windows_backslashes_are_normalized(self):
        path = rf"C:\Users\x\.claude\projects\-slug\{SESSION}\subagents\agent-w1.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.session_id == SESSION
        assert ref.cc_agent_id == "w1"

    def test_repeat_parse_is_identical(self):
        path = f"{HOME}/.claude/projects/-slug/{SESSION}/subagents/agent-a1.jsonl"
        assert parse_transcript_path(path) == parse_transcript_path(path)


class TestDeriveSessionIdShortcut:
    def test_matches_full_parse(self):
        path = (
            f"{HOME}/.claude/projects/-slug/{SESSION}"
            "/subagents/workflows/wf_abc-1/agent-a1.jsonl"
        )
        assert derive_session_id(path) == SESSION
