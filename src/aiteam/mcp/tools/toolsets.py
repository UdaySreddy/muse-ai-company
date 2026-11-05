"""Tool grouping switches + read-only tier parsing (tool progressive loading P2, registration-time static gating).

Two orthogonal dimensions, both taking effect in the ``register_all(mcp)`` registration loop:

1. ``AITEAM_TOOLSETS`` — select which modules to register, grouped by capability domain. Values:
   - absent / ``all`` → all tools (backward compatible, equal to the historical behavior);
   - ``default`` → the core set collection :data:`DEFAULT_TOOLSETS` (a hard cap of ≤50 tools after registration);
   - a comma-separated list of set names, which may mix in ``default`` for increments (e.g. ``"default,ecosystem"``);
   - an unknown set name → a stderr warning and the item is ignored; a configuration error never prevents the server
   from starting.

2. ``AITEAM_READONLY=1`` — orthogonally overlaid on grouping; after registration it removes all write-class tools,
   keeping only read-class ones. Write tools are judged by the :data:`WRITE_TOOLS` **explicit list** (not guessed by
   naming pattern,
   avoiding hard-to-trace false hits); the generation method is noted beside the list for maintainability.

Design spec: docs/tool-loading-design.md §P2 and §2.5.
"""

from __future__ import annotations

import sys

# ============================================================
# Capability-domain grouping: module short name -> toolset name
# The grouping keys are by capability domain rather than verb or physical origin (consistent with
# Composio/JARVIS/ToolBench industry practice).
# Set names equal module names (the historical git_ops→git / error_budget_tool→error_budget two
# merge-renames disappeared together with the two modules' retirement on 2026-07-27).
# ============================================================
MODULE_TOOLSET: dict[str, str] = {
    "team": "team",
    "agent": "agent",
    "meeting": "meeting",
    "task": "task",
    "project": "project",
    "analytics": "analytics",
    "links": "links",
    "reports": "reports",
    "briefing": "briefing",
    "task_analysis": "task_analysis",
    "memory": "memory",
    "infra": "infra",
    "channels": "channels",
    "watchdog": "watchdog",
    "ecosystem": "ecosystem",
    "workflows": "workflows",
}

# All valid set names (AITEAM_TOOLSETS=all expands to this set).
ALL_TOOLSETS: frozenset[str] = frozenset(MODULE_TOOLSET.values())

# ------------------------------------------------------------
# The default set = the core capability domains actually used in every session.
# Hard cap ≤50 tools (AnyTool 64 / JARVIS top-5 / the official 30-50 inflection point — a same-origin universal
# guardrail).
# Current member tool count: task8 + team5 + memory6 + infra7 + reports3 = 29 (21 headroom left).
# project / agent and the like mount incrementally as "default,project" on demand, not entering default, to keep from
# breaking the cap.
# ------------------------------------------------------------
DEFAULT_TOOLSETS: frozenset[str] = frozenset(
    {"task", "team", "memory", "infra", "reports"}
)

# ============================================================
# The explicit list of write-class tools (removed when AITEAM_READONLY=1).
#
# Generation method (re-checkable for maintenance): look tool by tool at the backend HTTP verb it calls —
#   POST / PUT / DELETE = write; GET = read. On top of that, manually correct two edge classes:
#   (a) GET but with side effects are added to the write list: os_restart_api (restarts the process);
#   (b) POST but purely analytical with no persistence stays on the read side: diagnose_task_failure (returns only a
# diagnosis).
# Anything not listed is read-class (*_list/*_get/*_search/*_read/*_status/*_check/*_trace/
#   *_query/*_summary/*_recommend, etc.), kept in the read-only tier.
# Grouped by module for maintenance against tools/*.py.
# ============================================================
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # agent
        "agent_update_status",
        "fleet_dispatch",  # drives a headless resume subprocess — write/side-effecting
        # briefing
        "briefing_add",
        "briefing_resolve",
        "briefing_dismiss",
        # channels
        "channel_send",
        # ecosystem (scan/apply/tag/claim/pin/mark etc. are all writes; summary/search/status are reads)
        "ecosystem_scan",
        "ecosystem_scan_periodic",
        "ecosystem_refresh",
        "ecosystem_deep_review_request",
        "ecosystem_deep_review_request_batch",
        "ecosystem_deep_review_cancel",
        "ecosystem_tag_apply_batch",
        "ecosystem_tag_dispatch_llm",
        "ecosystem_tag_apply_llm_result",
        "ecosystem_apply_shallow_summary",
        "ecosystem_apply_architecture_md",
        "ecosystem_apply_debate_result",
        "ecosystem_apply_quality_review",
        "ecosystem_trigger_debate",
        "ecosystem_link_debate_meeting",
        "ecosystem_link_integration_task",
        "ecosystem_start_integration",
        "ecosystem_mark_as_reference",
        "ecosystem_repo_manual_status",
        "ecosystem_claim_shallow",
        "ecosystem_claim_review",
        "ecosystem_release_claim",
        "ecosystem_quick_setup",
        "ecosystem_index_update",
        # infra (os_restart_api goes through GET but restarts the process; explicitly added)
        "os_restart_api",
        "model_config_set",
        # meeting
        "meeting_create",
        "meeting_send_message",
        "meeting_conclude",
        "meeting_update",
        "debate_start",
        "debate_code_review",
        # memory
        "memory_add",
        "memory_invalidate",
        "memory_reconcile_apply",
        # project
        "project_create",
        "project_update",
        "project_delete",
        "dismiss_project_registration",
        # reports
        "report_save",
        # task
        "task_run",
        "task_create",
        "task_update",
        "task_memo_add",
        # task_analysis (failure_analysis precipitates defense rules/training cases, a write;
        #   diagnose_task_failure returns only a diagnosis, staying on the read side)
        "failure_analysis",
        # team
        "team_close",
        "team_delete",
        # watchdog (verify_completion records the verification result, a write)
        "verify_completion",
        # workflows
        "workflow_reconcile",
    }
)

# The truth set the read-only tier recognizes.
_READONLY_TRUE = frozenset({"1", "true", "yes", "on"})


def _warn(msg: str) -> None:
    """Print a warning to stderr — a stdio server's stdout is the MCP protocol channel and must never be polluted."""
    print(f"[aiteam.toolsets] {msg}", file=sys.stderr)


def resolve_toolsets(raw: str | None) -> set[str]:
    """Parse AITEAM_TOOLSETS, returning the set of enabled toolset names.

    - None / "" / "all" → all sets (equal to the historical full behavior);
    - "default" → :data:`DEFAULT_TOOLSETS`;
    - a comma list: ``default`` expands to the core sets, ``all`` expands to everything, the rest are taken in by set
    name;
      an unknown set name → a stderr warning and it is ignored;
    - all tokens invalid → warn and fall back to all sets (guaranteeing the server starts as usual; pure functionality
    gain).
    """
    if raw is None:
        return set(ALL_TOOLSETS)
    text = raw.strip().lower()
    if text in ("", "all"):
        return set(ALL_TOOLSETS)

    enabled: set[str] = set()
    for token in text.split(","):
        name = token.strip()
        if not name:
            continue
        if name == "all":
            enabled |= set(ALL_TOOLSETS)
        elif name == "default":
            enabled |= set(DEFAULT_TOOLSETS)
        elif name in ALL_TOOLSETS:
            enabled.add(name)
        else:
            _warn(f"Unknown toolset «{name}» ignored; valid set names: {sorted(ALL_TOOLSETS)}")

    if not enabled:
        _warn("AITEAM_TOOLSETS has no valid set names; falling back to all sets.")
        return set(ALL_TOOLSETS)
    return enabled


def resolve_readonly(raw: str | None) -> bool:
    """Parse AITEAM_READONLY; the values 1/true/yes/on (case-insensitive) mean the read-only tier."""
    if raw is None:
        return False
    return raw.strip().lower() in _READONLY_TRUE


def module_enabled(module_shortname: str, enabled_toolsets: set[str]) -> bool:
    """Judge whether a module should register; unmapped modules fail open (always register), preserving the full
    fallback."""
    toolset = MODULE_TOOLSET.get(module_shortname)
    if toolset is None:
        return True
    return toolset in enabled_toolsets
