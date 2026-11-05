"""Unit tests for the slimmed projection views of list tools (views.py).

Spec: docs/tool-loading-design.md §4. Core invariants:
- Keys for later calls, such as id, are always preserved in full;
- Semantic content is downgraded to a truncated summary rather than deleted;
- The fields parameter recognizes only compact/all (including the full alias); anything else errors explicitly.
"""

from __future__ import annotations

from aiteam.mcp.tools.views import (
    ECO_LIST_HINT,
    EVENT_HINT,
    TASK_WALL_HINT,
    compact_event_row,
    compact_profile_row,
    compact_task_row,
    excerpt,
    resolve_view,
)


class TestResolveView:
    def test_default_and_compact(self):
        assert resolve_view("") == "compact"
        assert resolve_view("compact") == "compact"
        assert resolve_view(" Compact ") == "compact"

    def test_full_aliases(self):
        assert resolve_view("all") == "all"
        assert resolve_view("full") == "all"
        assert resolve_view("ALL") == "all"

    def test_unknown_rejected(self):
        assert resolve_view("summary") is None
        assert resolve_view("min") is None


class TestExcerpt:
    def test_short_text_unchanged(self):
        assert excerpt("short text", 80) == "short text"

    def test_long_text_truncated_with_ellipsis(self):
        text = "x" * 100
        out = excerpt(text, 80)
        assert len(out) == 81
        assert out.endswith("…")

    def test_none_and_empty(self):
        assert excerpt(None, 80) == ""
        assert excerpt("", 80) == ""


class TestCompactTaskRow:
    def _task(self, **overrides):
        base = {
            "id": "1279bdd9-35da-4b20-b44d-7de60282f1c0",
            "title": "Discussion (10): RangeID fencing prevents double writes",
            "description": "Discussion only, no implementation." + "mechanism details" * 50,
            "status": "pending",
            "priority": "high",
            "horizon": "mid",
            "score": 90.0,
            "assigned_to": None,
            "tags": ["discussion"],
            "depends_on": [],
            "config": {},
            "team_id": None,
            "result": None,
            "subtasks": [],
        }
        base.update(overrides)
        return base

    def test_id_kept_intact(self):
        row = compact_task_row(self._task())
        assert row["id"] == "1279bdd9-35da-4b20-b44d-7de60282f1c0"

    def test_description_truncated_not_dropped(self):
        row = compact_task_row(self._task())
        assert row["desc"].startswith("Discussion only, no implementation.")
        assert row["desc"].endswith("…")
        assert len(row["desc"]) <= 81

    def test_internal_fields_dropped(self):
        row = compact_task_row(self._task())
        for key in ("config", "team_id", "depth", "order", "parent_id"):
            assert key not in row

    def test_sparse_signals_only_when_present(self):
        plain = compact_task_row(self._task())
        assert "result" not in plain
        assert "depends_on" not in plain
        assert "subtask_count" not in plain
        rich = compact_task_row(
            self._task(
                result="Fix complete" * 40,
                depends_on=["dep-1"],
                subtasks=[{"id": "s1"}, {"id": "s2"}],
            )
        )
        assert rich["result"].endswith("…")
        assert rich["depends_on"] == ["dep-1"]
        assert rich["subtask_count"] == 2

    def test_empty_tags_normalised(self):
        assert compact_task_row(self._task(tags=None))["tags"] == []


class TestCompactEventRow:
    def test_summary_prefers_intent_summary(self):
        row = compact_event_row(
            {
                "id": "e1",
                "type": "intent.agent_working",
                "source": "agent:x",
                "timestamp": "2026-07-14T10:00:00",
                "data": {
                    "intent_summary": "Using Bash",
                    "tool_input_summary": "Do not pick me",
                },
                "entity_id": None,
                "state_snapshot": None,
            }
        )
        assert row["summary"] == "Using Bash"
        assert row["ts"] == "2026-07-14T10:00:00"
        # Always-empty placeholder fields do not enter the slimmed row
        assert "entity_id" not in row
        assert "state_snapshot" not in row

    def test_unknown_payload_falls_back_to_json_excerpt(self):
        row = compact_event_row(
            {"id": "e2", "type": "custom", "source": "s", "data": {"foo": "bar"}}
        )
        assert "foo" in row["summary"]

    def test_empty_payload_gives_empty_summary(self):
        row = compact_event_row({"id": "e3", "type": "t", "source": "s", "data": None})
        assert row["summary"] == ""


class TestCompactProfileRow:
    def test_projection_and_language_normalised(self):
        row = compact_profile_row(
            {
                "id": "p1",
                "repo_full_name": "n8n-io/n8n",
                "stars": 195894,
                "language": None,
                "stage_status": "queued",
                "one_line_summary": "Visual workflow automation platform",
                "topics": ["a"] * 20,
                "canonical_id": "github/n8n-io/n8n",
            }
        )
        assert row == {
            "repo": "n8n-io/n8n",
            "stars": 195894,
            "lang": "",
            "status": "queued",
            "summary": "Visual workflow automation platform",
        }

    def test_summary_falls_back_to_excerpt_then_description(self):
        row = compact_profile_row(
            {"repo_full_name": "a/b", "description_excerpt": "excerpt version"}
        )
        assert row["summary"] == "excerpt version"
        row2 = compact_profile_row({"repo_full_name": "a/b", "description": "desc version"})
        assert row2["summary"] == "desc version"


class TestHints:
    def test_hints_declare_not_missing_and_escape_hatch(self):
        for hint in (TASK_WALL_HINT, EVENT_HINT, ECO_LIST_HINT):
            assert "not a missing field" in hint
            assert 'fields="all"' in hint
