"""Memory and knowledge MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all memory-related MCP tools."""

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def memory_search(
        query: str = "",
        scope: str = "global",
        scope_id: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search the memory store in AI Team OS.

        Args:
            query: Search keywords
            scope: Memory scope, default "global"
            scope_id: Scope ID; when **left empty**, the server derives it from the context (global→system,
                user→user, project→the current project or an unregistered directory's fingerprint temporary bucket).
                Pass it explicitly only when
                a precise cross-scope specification is needed (e.g. a team's scope_id).
            limit: Maximum number of results, default 10

        Returns:
            List of matching memories
        """
        params_dict: dict[str, Any] = {"scope": scope, "query": query, "limit": limit}
        # When empty, do not send scope_id at all, letting the server's get_scoped_repository derive it from
        # X-Project-Dir —
        # otherwise the old default "system" would make an unregistered directory's memory_search unable to find its
        # own temporary bucket.
        if scope_id:
            params_dict["scope_id"] = scope_id
        params = urllib.parse.urlencode(params_dict)
        return _api_call("GET", f"/api/memory?{params}")

    @mcp.tool()
    def memory_add(
        content: str,
        kind: str = "preference",
        scope: str = "global",
        supersedes: str | None = None,
        source_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a direction-layer memory — the team's shared, cross-task standing preferences.

        The direction layer = low-frequency, high value-density, cross-task long-lived
        preferences/corrections/constraints/design intent. Every dispatched
        agent is injected with the direction layer at birth, so preferences like "everything in Chinese" and "report
        as soon as done" no longer need to be hand-copied into prompts.

        Write test (soft threshold): **how many future tasks can this entry affect? One that affects only a single
        task → goes to
        task_memo_add (the episodic layer); do not write it here.**

        The volume red line is a **single axis: storage cap = injection budget**. The direction layer counts character
        quotas per bucket —
        global 1200 characters + each project 1500 characters + user 300 characters; one session actually inherits
        3000 characters; a single entry is still ≤ 400 characters. Whatever fits in storage is guaranteed to be
        delivered, and what cannot be written truly has no room:
        when over the limit, this tool returns that bucket's **full set of valid entries** (id / kind / character
        count / full text) + the usage gap,
        requiring you to first, **in the same round**, memory_invalidate (locatable via a content_match substring) or
        memory_reconcile_apply to free space, then retry this write.
        Rewrite over-long content into a **pointer entry** of the form "trigger condition + pointer to the
        authoritative file" (e.g.
        "When production/clusters/DB are involved, follow the read-only iron rule; see ~/.claude/CLAUDE.md"), with the
        body externalized.

        Write-side safety scan: direction-layer entries enter every dispatched agent's system prompt, so invisible
        Unicode, prompt-injection phrasings (overriding existing instructions / extracting the system prompt / forging
        conversation roles), and credential
        shapes are uniformly refused storage.

        The four kinds (which determine the injection truncation priority constraint>design>directive>preference):
        - constraint (prohibitions/guardrails): one sentence, machine-checkable, valid for life.
          E.g. "All output in Chinese", "Never automatically add an agent signature to git commits".
        - design (value ordering/design intent): the basis for trade-offs when no explicit instruction exists.
          E.g. "Technical decisions favor quality/simplicity/robustness/long-term maintainability, with little weight
          on development cost".
        - directive (methodology/working style): answers "how to do it".
          E.g. "On completion, report by problem → root cause → solution → verification; do not batch things up".
        - preference (format preferences): optional, e.g. "One sentence per line for easy diffing".

        Args:
            content: the memory content (a single entry ≤ 400 characters, and it must fit this bucket's character
            quota; rewrite over-long content as a pointer entry)
            kind: constraint / design / directive / preference
            scope: global / project (the current project) / user (user level).
                Before writing to global, ask yourself: **does this hold for any session in any directory?** Anything
                mentioning a specific
                project/repo/manuscript/task is uniformly scope=project — an unregistered directory falls into this
                directory's
                fingerprint temporary bucket ("dir:..."), inherited only by this directory's sessions and never
                broadcast as a global memory.
            supersedes: optional, the old memory id this entry replaces and invalidates (a changed preference = the
            new entry supersedes
                the old one; Zep invalidation semantics, no deletion)
            source_refs: optional, a list of provenance ids (pointing back to memos/reports/meetings; used at
            distillation/promotion time)

        Returns:
            The write result; when over the bucket quota, returns success=False + quota usage + bucket_entries
            (the full text of all valid entries in that bucket) + next_action; when the safety scan hits, returns the
            refusal reason
        """
        body: dict[str, Any] = {
            "content": content,
            "kind": kind,
            "scope": scope,
            "source_refs": source_refs or [],
        }
        if supersedes:
            body["supersedes"] = supersedes
        return _api_call("POST", "/api/memories", body)

    @mcp.tool()
    def memory_invalidate(memory_id: str = "", content_match: str = "") -> dict[str, Any]:
        """Invalidate a direction-layer memory — mark it invalid without deleting.

        Explicitly invalidate a direction-layer preference when it is outdated/overturned (Zep invalidation semantics:
        set invalid_at without deleting,
        preserving an auditable trail). After invalidation it no longer enters injection and by default does not
        appear in memory_list.

        Two locating methods, choose one: **precise location by memory_id**, or **location by content_match substring**
        (saving an id lookup when you only have the original text — exactly the situation when a quota bounce hands it
        back to you).
        The substring must uniquely hit a valid entry in the current context: on 0 or multiple hits, the data is not
        touched at all; on multiple hits,
        candidates are returned so you can give a more precise substring.

        Args:
            memory_id: the id of the direction-layer memory to invalidate (alternative to content_match)
            content_match: the uniquely locating substring, exactly matched against valid entries' bodies (alternative
            to memory_id)

        Returns:
            The entry after invalidation; no hit / multiple hits / nonexistent id returns an error
        """
        if memory_id and content_match:
            return {
                "success": False,
                "error": "Provide either memory_id or content_match, not both.",
            }
        if memory_id:
            return _api_call("POST", f"/api/memories/{memory_id}/invalidate", {})
        if content_match:
            return _api_call(
                "POST", "/api/memories/invalidate", {"content_match": content_match}
            )
        return {
            "success": False,
            "error": "One of memory_id or content_match is required to locate the entry to invalidate.",
        }

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def memory_list(
        kind: str = "",
        include_invalidated: bool = False,
    ) -> dict[str, Any]:
        """List direction-layer memories — valid entries by default, grouped by kind.

        Return the current context's direction-layer entries: global + user global entries + the current project's
        project-
        level entries, ordered by kind priority (constraint>design>directive>preference) + time descending.
        This is the same data source as the dual hooks' resident injection; use it to review "what dispatched agents
        will inherit".

        Args:
            kind: optional, filter by kind (constraint/design/directive/preference)
            include_invalidated: whether to include invalidated entries (default no)

        Returns:
            The list of direction-layer entries
        """
        params_dict: dict[str, Any] = {}
        if kind:
            params_dict["kind"] = kind
        if include_invalidated:
            params_dict["include_invalidated"] = "true"
        qs = urllib.parse.urlencode(params_dict)
        path = "/api/memories" + (f"?{qs}" if qs else "")
        return _api_call("GET", path)

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 800000})
    def memory_reconcile_candidates(
        scope_path: str = "",
        threshold: float = 0.45,
    ) -> dict[str, Any]:
        """On-demand organization · coarse screening: returns episodic-layer candidate groups + the direction-layer
        inventory + distillation material + operating instructions.

        Memory organization = an explicit on-demand in-session action (CC is not resident; there is no background
        organization process). This tool performs only
        **deterministic coarse screening (zero LLM)** — the OS has no independent LLM credentials, so the judgment is
        made by you (the in-session agent calling
        the tool); the tool is responsible only for candidate coarse screening and operation application ("the agent
        computes, the tool stores").

        Returns four blocks (project_id is automatically resolved from the current context):
        - candidate_groups: valid task_memos clustered by scope_path/task, with in-cluster BM25 pairwise
          similarity above the threshold paired into candidate groups (including each entry's full text + id). Make an
          LLM fine judgment per group:
          KEEP (keep all) / MERGE / INVALIDATE (contradiction invalidation) / NOOP (no action).
        - direction_inventory: the full text of all valid direction-layer entries — perform a **staleness check** on
        each (a referenced
          feature retired / version outdated / the world changed → propose invalidate).
        - promotion_candidates: high-frequency clusters recurring across tasks — material for distilling into
        direction-layer entries
          (the promote operation; source_refs point back to the source memos).
        - operation_guide: the four operations' semantics + the three reconcile rules (keep only the high-frequency
        and useful / point to the authoritative
          source rather than restating / prefer concise rewrites) + the hint to open ultracode when the volume is large.

        After judging, hand the confirmed operations to memory_reconcile_apply for batch application.

        Args:
            scope_path: organize only that path scope's memos (empty = the whole project's valid memos)
            threshold: the in-cluster BM25 similarity pairing threshold (0-1, default 0.45)

        Returns:
            candidate_groups / promotion_candidates / direction_inventory /
            operation_guide / stats (including ultracode_hint when the candidate-group volume is large)
        """
        params_dict: dict[str, Any] = {"threshold": threshold}
        if scope_path:
            params_dict["scope_path"] = scope_path
        qs = urllib.parse.urlencode(params_dict)
        return _api_call("GET", f"/api/memory/reconcile/candidates?{qs}")

    @mcp.tool()
    def memory_reconcile_apply(operations: list[dict[str, Any]]) -> dict[str, Any]:
        """On-demand organization · apply: batch-execute the operations confirmed by the LLM fine judgment
        (deterministic, idempotent).

        Each operation is a dict, dispatched by its op field (an unknown/missing field returns an error for that entry
        without blocking the rest):
        - merge: {op:"merge", content: the new merged content, memo_ids: [the entries being merged],
          memo_type?:"summary", scope_path?} — creates a new memo and sets each merged entry invalid,
          with invalidated_by pointing at the new entry (Zep invalidation semantics, no deletion).
        - invalidate: {op:"invalidate", memo_ids:[...]} — invalidates entry by entry (contradicted/overturned).
        - score: {op:"score", memo_id, quality_score:1-10, reason} — adds a quality score,
          with reason going into meta.
        - promote: {op:"promote", content, kind:constraint/design/directive/preference,
          scope?:"project"/global/user, source_refs?:[source memo ids]} — distills and promotes into a direction-layer
          entry; **the red lines apply as usual** (a single entry ≤400 characters + bucket character quotas global
          1200 / project
          1500 / user 300; over the limit, that entry returns an error with usage; the safety scan likewise applies).
        - keep / noop: no action (may be omitted).

        Idempotent: repeating invalidate/merge on an already-invalidated entry returns noop without error. After
        applying, it automatically refreshes
        the project's last_reconcile_at (the baseline for the volume-threshold soft hint).

        Args:
            operations: the operation list, one dict per entry, dispatched by the op field into
                merge / invalidate / score / promote / keep (see the tool description for each field).
                One call may mix multiple ops; a single entry's error returns only that entry's error and does not
                block the rest.

        Returns:
            results (per-entry status: applied/noop/error) + applied_count +
            last_reconcile_at
        """
        return _api_call(
            "POST", "/api/memory/reconcile/apply", {"operations": operations}
        )
