"""Meeting MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.clock import utc_now
from aiteam.mcp._base import _api_call, _resolve_team_id
from aiteam.services.agent_template_registry import (
    FALLBACK_SUBAGENT_TYPE,
    check_subagent_types,
)

# ============================================================
# Prompt builder (pure function — easy to unit test)
# ============================================================


def _build_participation_prompt(
    name: str,
    role: str,
    meeting_id: str,
    title: str,
    round_rule: str,
    materials: list[str],
    context_files: list[str],
    expected_output: str,
) -> str:
    """Build a ready-to-use participation prompt for a meeting Agent.

    Args:
        name: Participant agent name
        role: One-line role description
        meeting_id: Meeting ID
        title: Meeting topic/title
        round_rule: Rule string for round 1 (from rounds[0].rule)
        materials: Global materials all participants must read
        context_files: Per-participant context files
        expected_output: Expected output format description

    Returns:
        Complete prompt string to paste into Agent tool's prompt parameter
    """
    all_files = list(dict.fromkeys(materials + context_files))  # deduplicated, order preserved
    read_steps = ""
    if all_files:
        file_list = "\n".join(f"   - Read `{f}`" for f in all_files)
        read_steps = f"\n**Step 1 — Read required materials (MANDATORY):**\n{file_list}\n"

    output_hint = expected_output or "clear paragraph format, including stance, reasoning, and recommendations"

    return f"""You are {name}, {role}

## Meeting Info
- meeting_id: `{meeting_id}`
- Topic: {title}
- Current round: Round 1
{read_steps}
**Step 2 — Speaking rules:**
{round_rule or "Clearly express your stance, basis, and recommendations."}

**Step 3 — Expected output format:**
{output_hint}

**Step 4 — Operations you must perform (in order, none skippable):**

1. Complete all the Read operations above
2. Generate your speaking content with your own LLM (copying others' content or leaving it empty is forbidden)
3. Call `meeting_send_message`:
   ```
   meeting_send_message(
       meeting_id="{meeting_id}",
       agent_id="{name}",
       agent_name="{name}",
       round_number=1,
       content=<the speaking content you generated>
   )
   ```
4. After speaking, call `SendMessage`:
   ```
   SendMessage(to="team-lead", summary="Speaking completed", message="round 1 completed for meeting {meeting_id}")
   ```
5. **Exit immediately. Do nothing outside the meeting. Do not speak on behalf of other participants.**

## Warnings
- Do not modify meeting_id (you must use `{meeting_id}`)
- Do not speak in place of others
- The speaking content must be generated independently by your LLM
- Stop immediately after completing step 4; do not continue operating
"""


# ============================================================
# Dispatch plan builder
# ============================================================


def _build_dispatch_plan(
    meeting_id: str,
    title: str,
    participants_raw: list[str | dict],
    rounds: list[dict],
    materials: list[str],
    team_name: str,
) -> tuple[list[dict], list[str], list[str], list[dict]]:
    """Build dispatch_plan and expected_participants list.

    Supports both legacy string participants and new structured MeetingParticipant dicts.

    Every subagent_type that will be spawned is checked against the template catalogue
    before the plan is returned. A miss is *reported*, never corrected: templates are an
    accelerator, not a whitelist, and CC's built-in types cannot be enumerated - see
    aiteam.services.agent_template_registry.

    Returns:
        (dispatch_plan list, expected_participants list, legacy_warnings list,
         template_warnings list)
    """
    round_rule = rounds[0].get("rule", "") if rounds else ""
    dispatch_plan: list[dict] = []
    expected: list[str] = []
    warnings: list[str] = []
    spawn_types: list[tuple[str, str]] = []

    for p in participants_raw:
        if isinstance(p, str):
            # Legacy string — minimal dispatch item, no launch_call
            expected.append(p)
            dispatch_plan.append({
                "participant": p,
                "launch_call": {},
                "ready_to_paste": False,
                "warning": "Please use the new structured parameters (MeetingParticipant) to get the complete "
                           "launch_call",
            })
            warnings.append(p)
            continue

        # Structured participant dict
        name = p.get("name", "")
        agent_template = p.get("agent_template", "")
        role = p.get("role", "")
        ctx_files = p.get("context_files", [])
        expected_output = p.get("expected_output", "")

        expected.append(name)
        prompt = _build_participation_prompt(
            name=name,
            role=role,
            meeting_id=meeting_id,
            title=title,
            round_rule=round_rule,
            materials=materials,
            context_files=ctx_files,
            expected_output=expected_output,
        )

        # Three corrections (2026-07-27 CC tool-surface alignment):
        #   ① removed team_name — CC v2.1.219 marks it Deprecated and ignores it (a session has a single implicit team),
        #      and OS-side ownership is handled by SubagentStart's automatic enrollment;
        #   ② the fallback template software-engineer **does not exist** among the 25 plugin/agents templates,
        #      so spawning with it would certainly fail — switched to CC's built-in general-purpose (always available);
        #   ③ explicit model=opus — the dispatch charter: Fable orchestrates, Opus executes; do not let attending agents
        #      mindlessly inherit the main session's high-tier model.
        subagent_type = agent_template or FALLBACK_SUBAGENT_TYPE
        spawn_types.append((subagent_type, name))
        launch_call = {
            "tool": "Agent",
            "params": {
                "subagent_type": subagent_type,
                "name": name,
                "model": "opus",
                "description": role or f"{name}'s meeting speaking task",
                "prompt": prompt,
            },
        }
        dispatch_plan.append({
            "participant": name,
            "launch_call": launch_call,
            "ready_to_paste": True,
        })

    # Report-only: the plan above already carries exactly what the caller asked for.
    template_warnings = check_subagent_types(spawn_types)
    return dispatch_plan, expected, warnings, template_warnings


def register(mcp):
    """Register all meeting-related MCP tools."""

    @mcp.tool()
    def meeting_create(
        topic: str,
        team_id: str = "",
        participants: list | None = None,
        template: str = "free",
        rounds: list | None = None,
        materials: list[str] | None = None,
        team_name: str = "",
    ) -> dict[str, Any]:
        """Create a team meeting and return a ready-to-use dispatch_plan for spawning participant Agents.

        Supports two participant formats:
        1. Legacy (strings): participants=["arch-lead", "backend-arch"]
           Returns dispatch_plan with empty launch_call + deprecation warning.
        2. Structured (dicts): participants=[{"name": "arch-lead", "agent_template": "software-architect",
           "role": "Responsible for evaluating architecture proposals", "context_files": ["docs/arch.md"],
           "expected_output": "three-paragraph form"}]
           Returns dispatch_plan with fully populated launch_call.params ready to paste into Agent tool.

        Available templates: brainstorm / decision / review / retrospective / standup / debate /
                  lean_coffee / council / free (auto-recommends based on topic)

        Args:
            topic: Meeting discussion topic
            team_id: Team ID or name (optional, auto-uses active team if empty)
            participants: Participant list — strings (legacy) or structured dicts (recommended)
            template: Meeting template, default "free"
            rounds: Custom round structure e.g. [{"topic": "Stance", "rule": "3 paragraphs per person"}]
            materials: Global materials all participants must read (file paths)
            team_name: the name of the team the meeting belongs to (only for OS-side ownership resolution); not
            written into the launch_call
                — CC Agent's team_name parameter is deprecated and ignored

        Returns:
            Meeting info + dispatch_plan + attendance_check_command + template_warnings.
            template_warnings lists any subagent_type absent from the template catalogue.
            Nothing is blocked or rewritten: an unknown type may well be a CC built-in.
            Read it, then decide whether it was a typo.
        """
        from aiteam.meeting.templates import TEMPLATE_ROUNDS, recommend_template

        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "No active team found; provide a team_id or create a team first"}

        participants_raw: list = participants or []
        materials_list: list[str] = materials or []

        # Build expected_participants for meta_json (store string names only)
        expected_names = [
            p if isinstance(p, str) else p.get("name", "")
            for p in participants_raw
        ]
        meta_json = {
            "expected_participants": expected_names,
            "round_started_at": utc_now().isoformat(),
        }

        # Pass only string participant names to the API (backward compatible)
        result = _api_call(
            "POST",
            f"/api/teams/{resolved}/meetings",
            {
                "topic": topic,
                "participants": expected_names,
                "meta_json": meta_json,
            },
        )

        if not result.get("success", True):
            return result

        # Template resolution
        auto_selected = False
        if template == "free" and topic:
            recommended, reason = recommend_template(topic)
            if recommended != "brainstorm" or "brainstorm" in topic.lower():
                template = recommended
                auto_selected = True
                result["_auto_selected"] = {"template": recommended, "reason": reason}

        effective_rounds: list[dict] = rounds or []
        if template and template != "free" and template in TEMPLATE_ROUNDS:
            result["_template"] = {
                "name": template,
                "auto_selected": auto_selected,
                **TEMPLATE_ROUNDS[template],
            }
            if not effective_rounds:
                effective_rounds = TEMPLATE_ROUNDS[template].get("rounds", [])
        else:
            result["_template"] = {
                "name": "free",
                "description": "Free discussion — no preset structure; multiple rounds as needed",
                "total_rounds": None,
                "rounds": [],
            }

        # Build dispatch plan
        meeting_id = (result.get("data") or {}).get("id", "")
        resolved_team_name = team_name or team_id or resolved
        dispatch_plan, expected, legacy_warnings, template_warnings = _build_dispatch_plan(
            meeting_id=meeting_id,
            title=topic,
            participants_raw=participants_raw,
            rounds=effective_rounds,
            materials=materials_list,
            team_name=resolved_team_name,
        )

        dispatch_instructions = (
            "The Leader calls Agent(**dispatch_plan[i].launch_call.params) in order to spawn each participant."
            " Items with ready_to_paste=True can be used directly; items with False need the structured parameters "
            "filled in."
        )
        if legacy_warnings:
            dispatch_instructions += (
                f"\nWarning: {legacy_warnings} used the legacy string format and cannot generate a launch_call."
                " Please upgrade to the structured participants parameter."
            )
        for w in template_warnings:
            # Top level, not buried inside dispatch_plan[i] - the reader decides.
            dispatch_instructions += f"\n⚠️ {w['message']}"

        result["dispatch_plan"] = dispatch_plan
        result["dispatch_instructions"] = dispatch_instructions
        result["template_warnings"] = template_warnings
        result["attendance_check_command"] = f"meeting_attendance_check(meeting_id='{meeting_id}')"
        result["expected_participants"] = expected

        return result

    @mcp.tool()
    def meeting_send_message(
        meeting_id: str,
        agent_id: str,
        agent_name: str,
        content: str,
        round_number: int = 1,
        caller_agent_id: str = "",
    ) -> dict[str, Any]:
        """Send a discussion message in a meeting.

        Discussion rules:
        - Round 1: Each participant presents their views
        - Round 2+: Must read previous speakers' messages first, cite and respond to specific points
        - Final round: Summarize consensus and disagreements

        SECURITY: Set caller_agent_id to the actual agent making this call.
        If it differs from agent_id, the message is flagged as impersonation in the audit log.
        Leader sending on behalf of others should set caller_agent_id='team-lead'.

        Args:
            meeting_id: Meeting ID
            agent_id: ID of the speaking Agent
            agent_name: Name of the speaking Agent
            content: Message content
            round_number: Discussion round number, default 1
            caller_agent_id: Actual caller identity (empty = legacy, no audit)

        Returns:
            Successfully sent message info
        """
        return _api_call(
            "POST",
            f"/api/meetings/{meeting_id}/messages",
            {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "content": content,
                "round_number": round_number,
                "caller_agent_id": caller_agent_id,
            },
        )

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def meeting_read_messages(meeting_id: str, limit: int = 100) -> dict[str, Any]:
        """Read all discussion messages in a meeting.

        Args:
            meeting_id: Meeting ID
            limit: Maximum number of messages to return, default 100

        Returns:
            Message list in chronological order
        """
        return _api_call("GET", f"/api/meetings/{meeting_id}/messages?limit={limit}")

    @mcp.tool()
    def meeting_conclude(
        meeting_id: str,
        summary: str = "",
        validate_attendance: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """Conclude a meeting, marking it as completed.

        By default checks that all expected participants have spoken before concluding.
        Set force=True to override, but this will be recorded in the event log.

        Args:
            meeting_id: Meeting ID
            summary: Optional conclusion summary text (stored in team memory)
            validate_attendance: Check that all expected participants have spoken (default True)
            force: Force conclude even with missing participants (records warning event)

        Returns:
            Updated meeting info
        """
        result = _api_call(
            "PUT",
            f"/api/meetings/{meeting_id}/conclude",
            {
                "summary": summary,
                "validate_attendance": validate_attendance,
                "force": force,
            },
        )
        result["_hint"] = ("The meeting conclusion has been automatically saved to the team memory. Historical "
                          "decisions can be retrieved via memory_search or team_briefing.")
        return result

    @mcp.tool()
    def meeting_template_list() -> dict[str, Any]:
        """List available meeting templates and their round structures.

        Returns:
            templates: All available templates with round structure details
        """
        from aiteam.meeting.templates import TEMPLATE_ROUNDS

        return {"templates": TEMPLATE_ROUNDS}

    @mcp.tool()
    def meeting_list(
        team_id: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        """List meetings for a team, optionally filtered by status.

        Args:
            team_id: Team ID or name (optional, auto-uses active team if empty)
            status: Filter by meeting status: "active" or "concluded" (optional, returns all if empty)

        Returns:
            Meeting list with topic, status, participant count, etc.
        """
        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "No active team found; provide a team_id or create a team first"}
        path = f"/api/teams/{resolved}/meetings"
        if status:
            path += f"?status={urllib.parse.quote(status)}"
        return _api_call("GET", path)

    @mcp.tool()
    def debate_start(
        topic: str,
        advocate: str,
        critic: str,
        judge: str = "",
        team_id: str = "",
    ) -> dict[str, Any]:
        """Start a structured 4-round debate meeting between an Advocate and a Critic.

        Debate structure:
        - Round 1 (Advocate): Present proposal/position with evidence
        - Round 2 (Critic): Challenge risks, flaws, and propose alternatives
        - Round 3 (Advocate): Respond to challenges, revise proposal if needed
        - Round 4 (Judge): Render verdict with action items

        Args:
            topic: The subject of the debate (proposal or decision to evaluate)
            advocate: Agent name of the Advocate (proposer/defender)
            critic: Agent name of the Critic (challenger)
            judge: Agent name of the Judge (optional; defaults to team-lead if empty)
            team_id: Team ID or name (optional, auto-uses active team if empty)

        Returns:
            Meeting info with debate structure, role assignments, and round rules
        """
        from aiteam.meeting.templates import TEMPLATE_ROUNDS

        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "No active team found; provide a team_id or create a team first"}

        judge_name = judge or "team-lead"
        participants = list({advocate, critic, judge_name})

        result = _api_call(
            "POST",
            f"/api/teams/{resolved}/meetings",
            {
                "topic": f"[Debate] {topic}",
                "participants": participants,
            },
        )

        debate_template = TEMPLATE_ROUNDS["debate"]
        result["_template"] = {
            "name": "debate",
            "auto_selected": False,
            **debate_template,
        }
        result["_roles"] = {
            "advocate": advocate,
            "critic": critic,
            "judge": judge_name,
        }
        result["_guide"] = (
            f"Debate created. Role assignment:\n"
            f"  Advocate (for): {advocate} — Round 1 statement + Round 3 response\n"
            f"  Critic (against): {critic} — Round 2 challenge\n"
            f"  Judge: {judge_name} — Round 4 verdict\n"
            f"Rules summary: quote the original text → respond point by point → the verdict must attach Action Items"
        )
        return result

    @mcp.tool()
    def debate_code_review(
        file_path: str,
        change_description: str,
        team_id: str = "",
        advocate: str = "backend-architect",
        critic: str = "code-reviewer",
        judge: str = "",
    ) -> dict[str, Any]:
        """Start a debate-style code review for a specific file or change.

        Creates a structured 4-round debate where:
        - Advocate defends the current implementation
        - Critic challenges the implementation and proposes improvements
        - Judge synthesizes findings into consensus conclusions and action items

        Args:
            file_path: Path to the file being reviewed (relative or absolute)
            change_description: Brief description of what changed and why
            team_id: Team ID or name (optional, auto-uses active team if empty)
            advocate: Agent defending the implementation (default: backend-architect)
            critic: Agent challenging the implementation (default: code-reviewer)
            judge: Agent rendering the verdict (default: team-lead)

        Returns:
            Meeting info with code review debate structure and starter prompt for Round 1
        """
        from aiteam.meeting.templates import TEMPLATE_ROUNDS

        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "No active team found; provide a team_id or create a team first"}

        judge_name = judge or "team-lead"
        participants = list({advocate, critic, judge_name})
        topic = f"[Code Review Debate] {file_path}: {change_description}"

        result = _api_call(
            "POST",
            f"/api/teams/{resolved}/meetings",
            {
                "topic": topic,
                "participants": participants,
            },
        )

        debate_template = TEMPLATE_ROUNDS["debate"]
        result["_template"] = {
            "name": "debate",
            "auto_selected": False,
            **debate_template,
        }
        result["_roles"] = {
            "advocate": advocate,
            "critic": critic,
            "judge": judge_name,
        }
        result["_context"] = {
            "file_path": file_path,
            "change_description": change_description,
        }
        result["_round1_prompt"] = (
            f"{advocate} (Advocate): please present the implementation plan for `{file_path}` in Round 1."
            f"Change description: {change_description}."
            f"Format: [Plan title] + [Core design decisions] + [Supporting reasons] + [Expected benefits] + [Known "
            f"limitations]"
        )
        return result

    @mcp.tool()
    def meeting_update(
        meeting_id: str,
        topic: str = "",
        participants: list[str] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Update meeting fields (topic, participants, notes).

        Use this to add conclusions/notes to a meeting or update its topic.
        To formally conclude a meeting (mark as concluded), use meeting_conclude instead.

        Args:
            meeting_id: Meeting ID (required)
            topic: New topic text (optional)
            participants: Updated participant list (optional)
            notes: Meeting notes or conclusion summary to store (optional)

        Returns:
            Updated meeting info
        """
        payload: dict[str, Any] = {}
        if topic:
            payload["topic"] = topic
        if participants is not None:
            payload["participants"] = participants
        if notes:
            payload["notes"] = notes
        if not payload:
            return {"success": False, "error": "At least one update field must be provided (topic / participants / "
                                               "notes)"}
        return _api_call("PUT", f"/api/meetings/{meeting_id}", payload)

    @mcp.tool()
    def meeting_attendance_check(meeting_id: str) -> dict[str, Any]:
        """Check which expected participants have spoken in the current round.

        Use this after spawning all Agents via dispatch_plan to verify attendance
        before advancing to the next round or concluding the meeting.

        Args:
            meeting_id: Meeting ID

        Returns:
            {
              "round": current round number,
              "expected": all expected participants,
              "spoken": participants who have spoken in current round,
              "pending": participants who have NOT yet spoken,
              "timeout_in_seconds": seconds elapsed since round started
            }
        """
        return _api_call("GET", f"/api/meetings/{meeting_id}/attendance")
