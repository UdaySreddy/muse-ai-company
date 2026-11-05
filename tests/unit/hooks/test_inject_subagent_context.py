"""inject_subagent_context P0 rewiring unit tests (audit fix, 2026-07-14).

Core invariants:
- The trigger key comes directly from the dispatch prompt (payload first, the transcript's first user message as
fallback);
- task_id recognizes only explicit styles; it does not mistake a bare uuid from repo_id/deep_review_id for a task;
- The dead pipeline detection has been deleted (there is no longer any _fetch_pipeline_context).
"""

from __future__ import annotations

import importlib
import json

inject = importlib.import_module("aiteam.hooks.inject_subagent_context")

UUID_A = "1279bdd9-35da-4b20-b44d-7de60282f1c0"
UUID_B = "7f38dbbf-7ea7-48ef-aff9-db5ba53165fa"


class TestExtractTaskContext:
    def test_explicit_task_id_from_prompt(self):
        payload = {"prompt": f"When done, call task_memo_add(task_id={UUID_A}) to write back progress"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == UUID_A
        assert "task_memo_add" in prompt

    def test_chinese_task_id_marker(self):
        payload = {"prompt": f"Your task ID: {UUID_A}, please read task_memo_read first"}
        task_id, _ = inject._extract_task_context(payload)
        assert task_id == UUID_A

    def test_bare_uuid_not_mistaken_as_task(self):
        # A bare uuid from repo_id / deep_review_id must not be mistaken for a task key
        payload = {"prompt": f"For the repository repo_id={UUID_B}, please produce a shallow-scan summary"}
        task_id, _ = inject._extract_task_context(payload)
        assert task_id == ""

    def test_empty_payload_gives_empty_context(self):
        task_id, prompt = inject._extract_task_context({})
        assert task_id == ""
        assert prompt == ""

    def test_parent_transcript_is_never_read(self, tmp_path):
        """Batch 3 (3) (rewritten from the old test_transcript_fallback; the old assertion locked in the wrong
        behavior).

        The transcript_path in a SubagentStart payload points to the **parent session** (the Leader's)
        transcript: its first user message is 'what the user said to the Leader', not this dispatch's
        prompt. The old fallback treated it as dispatch text → task_id extraction / memo fetching / pattern retrieval,
        all three segments
        worked dynamically injected against the wrong object (it would inject a task number the user mentioned in
        passing onto an unrelated agent).
        Now the transcript is not read at all.
        """
        transcript = tmp_path / "parent-session.jsonl"
        rec = {
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"The overall task {UUID_A} is on the wall; starting implementation"}
                ],
            }
        }
        transcript.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        payload = {"prompt": "", "transcript_path": str(transcript)}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == ""
        assert UUID_A not in prompt
        assert "starting implementation" not in prompt

    def test_transcript_reader_removed(self):
        assert not hasattr(inject, "_first_user_message")

    def test_missing_transcript_is_silent(self):
        payload = {"transcript_path": "/nonexistent/agent.jsonl"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == ""

    def test_description_used_when_prompt_absent(self):
        payload = {"description": f"Fix the bug, task ID: {UUID_A}"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == UUID_A
        assert "Fix the bug" in prompt

    def test_agent_type_and_cwd_are_the_retrieval_key(self):
        """With no dispatch text, the retrieval key = agent_type + cwd (the minimal signal under which pattern
        retrieval is still meaningful)."""
        payload = {"agent_type": "testing-bug-fixer", "cwd": "/repo/ai-team-os"}
        task_id, prompt = inject._extract_task_context(payload)
        assert task_id == ""
        assert "testing-bug-fixer" in prompt
        assert "/repo/ai-team-os" in prompt


class TestDeadPipelineRemoved:
    def test_pipeline_fetcher_gone(self):
        assert not hasattr(inject, "_fetch_pipeline_context")

    def test_no_retired_tool_mentions_in_source(self):
        import inspect

        src = inspect.getsource(inject)
        assert "pipeline_advance" not in src

    def test_report_format_block_removed(self):
        import inspect

        src = inspect.getsource(inject)
        assert "## Report format" not in src
