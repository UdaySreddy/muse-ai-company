"""MCP server instructions — the only server-level text a tool-search client sees.

CC no longer ships 112 tool schemas up front; it searches tool text and loads what
matches. This paragraph is therefore what decides whether the tools get found at
all, so it is organised as "capability → entry tools" (the way someone phrases a
need) rather than as a list of nouns.

Kept in its own module because it is data, not logic: a long Chinese literal that
would otherwise force an E501 blanket exemption onto server.py.
Constraints are enforced by tests/unit/test_mcp_server_surface.py — <= 2KB (it
rides along in every session), core capabilities covered, and every tool named
here must actually exist.
"""

from __future__ import annotations

INSTRUCTIONS = """AI Team OS — persistent governance for multi-agent teams: cross-session task ledger, agent observability, attribution and failure analysis, memory governance, project isolation, report knowledge base. CC sessions are temporary; tasks, memories, reports and observations here persist across sessions.

Core capabilities:
- Cross-session task ledger: tasks on the wall, status flow, memos; resume after crashes/session switches → task_create / task_status / task_update / task_memo_add / task_memo_read
- Agent observation and attribution: who works, how long, stuck where, failure causes, effective prompts → agent_list / agent_activity_query / task_execution_trace / diagnose_task_failure / failure_analysis / prompt_effectiveness / verify_completion
- Memory governance: episodic layer (task memos) + direction layer (constraints/preferences/decisions); search, invalidate, merge-distill → memory_search / memory_add / memory_list / memory_invalidate / memory_reconcile_candidates / memory_reconcile_apply
- Project isolation: auto-ownership by working directory; no cross-project mixing → context_resolve / project_list
- Report knowledge base: research/design outputs stored, searchable, not scattered md files → report_save / report_list
- Team collaboration: create/dispatch teams, channel broadcast, meetings, structured debates → team_status / fleet_dispatch / channel_send / channel_read / meeting_create / meeting_send_message
- Provenance search: reference network over tasks/reports/commits/workflows; global search → link_trace / unified_search
- Open-source ecosystem archive: Claude ecosystem repo index, capability tags, deep scans, weekly briefs → ecosystem_search / ecosystem_search_by_capability / ecosystem_repo_get, all ecosystem_*
- The rest: workflow_* = CC Workflow runs, briefing_* = pending briefings, find_skill = skill lookup, os_health_check self-check.

Conventions: confirm the project with context_resolve first; research outputs via report_save, not files; lasting conclusions via memory_add."""
