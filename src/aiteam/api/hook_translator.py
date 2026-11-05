"""AI Team OS — Hook event translator.

Translates Claude Code hook events into OS system operations,
bridging automatic sync between CC sessions and the OS.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from aiteam.api import agent_context, leader_usage, workflow_ingest
from aiteam.api.always_load import normalize_tool_name
from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.services import token_attribution, transcript_path
from aiteam.services.agent_identity import pick_reusable_row
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType, TokenSource, WorkflowRun

# Agent standardized prompt template path
_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3] / "plugin" / "config" / "agent-prompt-template.md"
)

# CC ultracode/Workflow runtime tags every internal fan-out agent with this fixed
# agent_type. We track each Workflow *run* as its own OS team (strict 1:1) so the
# Dashboard can see "one workflow = one team + N distinct members", instead of the
# old behaviour that collapsed all of them into a single 'workflow-subagent' row.
WORKFLOW_AGENT_TYPE = "workflow-subagent"
# Workflow run id lives in the subagent transcript path: .../workflows/wf_<id>/agent-<aid>.jsonl
# Bounded to a single optional dash-suffix (not unbounded `*`): an unbounded group greedily
# swallows the worktree instance suffix CC appends for parallel branches of one run, e.g.
# ".claude/worktrees/wf_a69e7d46-a66-1" would over-match to "wf_a69e7d46-a66-1" instead of
# the true run id "wf_a69e7d46-a66", causing team lookups to miss the real wf_<id>.json
# snapshot and spawn an orphan team (2026-07 task f8207497). Matches link_extract._WF_RE's
# bounded convention.
_WF_RUN_ID_RE = re.compile(r"wf_[0-9a-z]+(?:-[0-9a-z]+)?", re.IGNORECASE)

logger = logging.getLogger(__name__)


@dataclass
class _FileEditRecord:
    """Single file edit record."""

    agent_id: str
    agent_name: str
    timestamp: datetime


@dataclass
class _FileEditTracker:
    """In-memory file edit tracker — O(1) conflict queries.

    Maintains a list of recent edit records per file, supporting:
    1. Quick check if a file was edited by another agent (conflict detection)
    2. Hotspot file statistics (files edited by multiple agents)
    3. Automatic cleanup of expired records
    """

    # file_path -> list of recent edit records
    _edits: dict[str, list[_FileEditRecord]] = field(
        default_factory=lambda: defaultdict(list),
    )
    # Record retention duration
    _window: timedelta = field(default_factory=lambda: timedelta(minutes=10))

    def record(self, file_path: str, agent_id: str, agent_name: str) -> None:
        """Record a file edit."""
        if len(self._edits) > 10000:
            self.cleanup()
        self._edits[file_path].append(
            _FileEditRecord(
                agent_id=agent_id,
                agent_name=agent_name,
                timestamp=utc_now(),
            ),
        )

    def find_conflicts(
        self,
        file_path: str,
        current_agent_id: str,
        window_minutes: int = 5,
    ) -> list[_FileEditRecord]:
        """Find edit records from other agents that conflict with the current agent.

        Returns:
            List of records from other agents who edited the same file within the time window.
        """
        cutoff = utc_now() - timedelta(minutes=window_minutes)
        records = self._edits.get(file_path, [])
        return [r for r in records if r.agent_id != current_agent_id and r.timestamp >= cutoff]

    def get_hotspots(self, window_minutes: int = 10, min_agents: int = 2) -> list[dict]:
        """Get hotspot files — files edited by multiple agents within the time window.

        Returns:
            List of hotspot files, each containing file_path, agents, edit_count.
        """
        cutoff = utc_now() - timedelta(minutes=window_minutes)
        hotspots = []
        for file_path, records in self._edits.items():
            recent = [r for r in records if r.timestamp >= cutoff]
            if not recent:
                continue
            unique_agents = {r.agent_name for r in recent}
            if len(unique_agents) >= min_agents:
                hotspots.append(
                    {
                        "file_path": file_path,
                        "agents": sorted(unique_agents),
                        "edit_count": len(recent),
                        "last_edit": max(r.timestamp for r in recent).isoformat(),
                    }
                )
        # Sort by edit count descending
        hotspots.sort(key=lambda h: h["edit_count"], reverse=True)
        return hotspots

    def get_agent_files(self, agent_id: str, window_minutes: int = 10) -> list[str]:
        """Get list of files recently being edited by a specific agent."""
        cutoff = utc_now() - timedelta(minutes=window_minutes)
        files = []
        for file_path, records in self._edits.items():
            if any(r.agent_id == agent_id and r.timestamp >= cutoff for r in records):
                files.append(file_path)
        return files

    def cleanup(self) -> int:
        """Clean up expired records, return count of removed records."""
        cutoff = utc_now() - self._window
        removed = 0
        empty_keys = []
        for file_path, records in self._edits.items():
            before = len(records)
            self._edits[file_path] = [r for r in records if r.timestamp >= cutoff]
            removed += before - len(self._edits[file_path])
            if not self._edits[file_path]:
                empty_keys.append(file_path)
        for k in empty_keys:
            del self._edits[k]
        return removed


class HookTranslator:
    """Translates Claude Code hook events into OS system operations."""

    # File edit tool names, used for conflict detection
    _FILE_EDIT_TOOLS = frozenset({"Edit", "Write"})

    # Substantive tools that trigger intent events
    _INTENT_TOOLS = frozenset({"Read", "Edit", "Write", "Bash"})

    # Intent event throttle interval (seconds)
    _INTENT_THROTTLE_SECS = 10

    def __init__(self, repo: StorageRepository, event_bus: EventBus) -> None:
        self.repo = repo
        self.event_bus = event_bus
        self._file_tracker = _FileEditTracker()
        self._prompt_template: str | None = None
        # pending_spans: key = "{agent_id}:{session_id}:{tool_name}"
        # value = (activity_id, start_time)
        self._pending_spans: dict[str, tuple[str, datetime]] = {}
        # Leader activity-touch throttling (one DB write per session per 60s; tool events can reach dozens per minute)
        self._leader_touch: dict[str, datetime] = {}
        # Intent throttle: key = agent_id, value = last_emit_time
        self._intent_last_emit: dict[str, datetime] = {}
        # Last known cwd from hook payload (for project matching)
        self._last_cwd: str = ""
        # I3a: staging area for the static plan parsed from PreToolUse(Workflow) (session_id -> plan),
        # so the PostToolUse(Workflow) receipt can fill in name/phases/planned_agent_count.
        self._workflow_plans: dict[str, dict] = {}
        # The throttler for Leader main-session usage collection (token attribution v1 stage 4). In-process state;
        # losing it does not affect correctness — persistence is overwrite-based and re-measurement is idempotent.
        self._usage_meter = leader_usage.SessionUsageMeter()

    def _load_prompt_template(self) -> str:
        """Lazy-load the Agent standardized prompt template."""
        if self._prompt_template is None:
            try:
                self._prompt_template = _TEMPLATE_PATH.read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.warning("Agent prompt template file not found: %s", _TEMPLATE_PATH)
                self._prompt_template = ""
        return self._prompt_template

    def _render_prompt(self, role: str, project_path: str = "") -> str:
        """Fill template with basic info and return system_prompt."""
        template = self._load_prompt_template()
        if not template:
            return ""
        return template.replace("{role}", role).replace("{project_path}", project_path or "Not specified")

    async def handle_event(self, payload: dict) -> dict:
        """Unified event handling entry point."""
        # Set per-request cwd for project matching (NOT persistent — safe for multi-session)
        self._current_event_cwd = payload.get("cwd", "")
        event_name = payload.get("hook_event_name", "")
        handler = {
            "SubagentStart": self._on_subagent_start,
            "SubagentStop": self._on_subagent_stop,
            "PreToolUse": self._on_pre_tool_use,
            "PostToolUse": self._on_post_tool_use,
            "SessionStart": self._on_session_start,
            "SessionEnd": self._on_session_end,
            "Stop": self._on_stop,
            "TeammateIdle": self._on_teammate_idle,
            "PostCompact": self._on_post_compact,
            "WorktreeCreate": self._on_worktree_event,
            "WorktreeRemove": self._on_worktree_event,
            "TaskCreated": self._on_cc_task_event,
            "TaskCompleted": self._on_cc_task_event,
        }.get(event_name)

        if handler:
            return await handler(payload)
        return {"status": "ignored", "reason": f"unhandled event: {event_name}"}

    def _extract_workflow_run_id(self, payload: dict) -> str | None:
        """Pull the Workflow run id (wf_<id>) from the subagent's transcript path.

        CC stores each workflow subagent transcript at
        .../subagents/workflows/wf_<id>/agent-<aid>.jsonl, so the run id is in the path.
        Returns None when no transcript field is present (e.g. trimmed from an oversized
        payload) — the caller then falls back to session-scoped grouping.
        """
        for key in ("transcript_path", "agent_transcript_path", "cwd"):
            val = payload.get(key)
            if not val:
                continue
            m = _WF_RUN_ID_RE.search(str(val).replace("\\", "/"))
            if m:
                return m.group(0)
        return None

    async def _register_workflow_subagent(
        self, payload: dict, cc_agent_id: str, session_id: str
    ) -> dict:
        """Register a CC Workflow fan-out agent as a member of its run's OS team.

        Strict one-workflow-one-team: team key = workflow-<wf_run_id>. Members are
        deduped by cc_agent_id (NOT by name — all workflow agents share the literal
        name 'workflow-subagent', so name-dedup would collapse a 16-agent run into one
        row). Unlike the normal path this does NOT require a pre-existing active team;
        the workflow team is auto-created so the run is always tracked.
        """
        # 1. Dedup by CC agent id — same internal agent re-reporting just refreshes.
        if cc_agent_id:
            existing = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if existing:
                await self.repo.update_agent(
                    existing.id,
                    status="busy",
                    session_id=session_id,
                    last_active_at=utc_now(),
                )
                return {"status": "updated", "agent_id": existing.id, "kind": "workflow"}

        # 2. Resolve the workflow run -> team key (strict 1:1; fall back to session).
        wf_id = self._extract_workflow_run_id(payload)
        session_key = f"workflow-session-{(session_id or 'unknown')[:8]}"
        team_key = f"workflow-{wf_id}" if wf_id else session_key

        # 3. Bind to the launching project strictly (cross-project fix C): cwd longest prefix →
        # same-session Leader; when unresolvable, leave None and never guess across sessions — the old _find_leader
        # global fallback
        # would bind an unregistered project's workflow teams/agents onto another project (a recorded isolation
        # violation).
        project_id = await self._resolve_project_id_by_cwd(payload.get("cwd") or "")
        if not project_id and session_id:
            try:
                _same = await self.repo.find_agents_by_session(session_id)
            except Exception:  # noqa: BLE001
                _same = []
            _leaders = [a for a in _same if a.role == "leader"]
            project_id = (_leaders[0].project_id if _leaders else None) or None

        # 4. Find-or-create the workflow team (idempotent by name across the run's agents).
        team = await self.repo.get_team_by_name(team_key)
        if team is None and wf_id:
            # Anti-fragmentation: a late wf_id is the norm (SubagentStart often arrives before the receipt); this run's
            # early-arriving
            # agents are already attached to the session fallback team — as long as the fallback team has not been
            # claimed by another run, keep using it and add the
            # workflow_run_id link, avoiding "one run, two teams" (agents in the session team while the run hangs on
            # an empty wf team, neither knowing the other; monitoring record 2026-07-06).
            fallback = await self.repo.get_team_by_name(session_key)
            fb_wf = (fallback.config or {}).get("workflow_run_id") if fallback else None
            if fallback is not None and fb_wf in (None, "", wf_id):
                team = fallback
                if fb_wf != wf_id:
                    cfg = dict(fallback.config or {})
                    cfg["workflow_run_id"] = wf_id
                    try:
                        await self.repo.update_team(fallback.id, config=cfg)
                    except Exception:  # noqa: BLE001
                        pass
        if team is None:
            team, _created = await self.repo.get_or_create_team(
                name=team_key,
                mode="coordinate",
                config={"kind": "workflow", "auto_created": True, "workflow_run_id": wf_id},
                project_id=project_id,
            )
            if _created:
                # On race-loss (concurrent same-name team creation), skip the emit — the winner has already sent
                # team.created.
                await self.event_bus.emit(
                    "team.created",
                    f"team:{team.id}",
                    {"team_id": team.id, "name": team_key, "kind": "workflow"},
                )
        else:
            await self._reactivate_workflow_team(team)

        # 5. Register this internal agent as a distinct member (unique name per cc id).
        member_name = f"wf-{(cc_agent_id or session_id or 'anon')[:10]}"
        try:
            new_agent = await self.repo.create_agent(
                team_id=team.id,
                name=member_name,
                role=WORKFLOW_AGENT_TYPE,
                source="hook",
                session_id=session_id,
                cc_tool_use_id=cc_agent_id,
                # The SubagentStart payload carries no model information — leave it empty rather than persisting the
                # repository default
                # (which once displayed an opus-4-8 run as claude-opus-4-7); the real model is backfilled by the
                # observation layer
                # from the wf_<id>.json terminal state into workflow_agents.model.
                model="",
            )
        except IntegrityError:
            # Concurrent create race (this coroutine vs the reaper live-tail corpse-collection coroutine, audit B1):
            # the cc_tool_use_id partial unique index is already occupied by the first arrival. Swallow the conflict and
            # instead
            # update the existing row, avoiding two member rows for the same cc_id. If the constraint is not this one,
            # the re-fetch misses and it propagates.
            existing = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if existing is None:
                raise
            await self.repo.update_agent(
                existing.id,
                status="busy",
                session_id=session_id,
                project_id=project_id,
                last_active_at=utc_now(),
            )
            return {"status": "updated", "agent_id": existing.id, "kind": "workflow"}
        await self.repo.update_agent(
            new_agent.id,
            status="busy",
            project_id=project_id,
            last_active_at=utc_now(),
        )
        await self.event_bus.emit(
            "decision.agent_created",
            f"agent:{new_agent.id}",
            {"agent_id": new_agent.id, "name": member_name, "team_id": team.id, "kind": "workflow"},
        )
        return {
            "status": "created",
            "agent_id": new_agent.id,
            "team_id": team.id,
            "kind": "workflow",
        }

    async def _reactivate_workflow_team(self, team: object) -> None:
        """Re-open a closed workflow team that a new subagent just joined.

        A ``workflow-session-<sid8>`` shell is reachable by name forever, so a long
        lived session can register into one the reaper already closed (its previous
        runs having settled). Members landing in a ``completed`` team are invisible to
        every active-only view; the reaper's contradiction self-heal catches it within
        a tick, but joining is the exact moment we know the team is in use again.
        Best-effort: a failure here just falls back to that self-heal.
        """
        if team is None or not str(getattr(team, "status", "")).endswith("completed"):
            return
        try:
            await self.repo.update_team(team.id, status="active")
        except Exception:  # noqa: BLE001 — a resurrection failure is covered by the reaper's contradiction-state self-healing
            logger.debug("workflow team reactivation failed team=%s", getattr(team, "id", "?"))

    async def _promote_workflow_team(self, agent: object, payload: dict) -> None:
        """Re-key a workflow subagent from the session-fallback team to its per-run team.

        Strict one-workflow-one-team: wf_id is ABSENT at SubagentStart (payload only
        carries the Leader's main transcript), but PRESENT later in agent_transcript_path
        (SubagentStop) and the subagent's own tool-call payloads. Once a wf_id is visible,
        migrate the agent to workflow-<wf_id>. Idempotent: no-op if already in that team
        or if no wf_id is resolvable from this payload.
        """
        if agent is None or getattr(agent, "role", None) != WORKFLOW_AGENT_TYPE:
            return
        wf_id = self._extract_workflow_run_id(payload)
        if not wf_id:
            return
        team_key = f"workflow-{wf_id}"
        cur_team = await self.repo.get_team(agent.team_id) if getattr(agent, "team_id", None) else None
        if cur_team is not None and cur_team.name == team_key:
            return  # already correctly grouped
        project_id = getattr(agent, "project_id", None)
        team = await self.repo.get_team_by_name(team_key)
        if team is None:
            # race-safe find-or-create (concurrent regroups to the same per-run team name would hit the UNIQUE
            # constraint)
            team, _ = await self.repo.get_or_create_team(
                name=team_key,
                mode="coordinate",
                config={"kind": "workflow", "auto_created": True, "workflow_run_id": wf_id},
                project_id=project_id,
            )
        await self.repo.update_agent(agent.id, team_id=team.id)
        await self.event_bus.emit(
            "agent.updated",
            f"agent:{agent.id}",
            {"agent_id": agent.id, "changes": {"team_id": team.id, "reason": "workflow_run_regroup"}},
        )

    def _parse_workflow_plan(self, script: str) -> dict:
        """Statically extract a Workflow run's planned roster from its script (Step 4).

        Knowable up-front (before any agent runs): meta.phases (declarative skeleton)
        and literal agent() calls. NOT knowable: dynamic fan-out (.map / while / pipeline
        over runtime arrays) — reported as a count of dynamic nodes whose size is runtime.
        """
        phases = re.findall(r"title:\s*['\"]([^'\"]+)['\"]", script)
        literal_agents = len(re.findall(r"\bagent\(", script))
        dynamic_nodes = len(re.findall(r"\.map\(|while\s*\(|\bpipeline\(", script))
        m = re.search(r"name:\s*['\"]([^'\"]+)['\"]", script)
        return {
            "name": m.group(1) if m else "",
            "phases": phases,
            "literal_agent_count": literal_agents,
            "dynamic_nodes": dynamic_nodes,
        }

    async def _on_subagent_start(self, payload: dict) -> dict:
        """Handle sub-agent start event.

        CC SubagentStart payload structure:
        - agent_type: Agent name (from Agent tool's name parameter)
        - agent_id: CC internal agent ID (for correlating subsequent tool calls)
        - session_id: Parent session ID
        - cc_team_name: (optional) CC team name, injected by send_event.py

        Deduplication strategy (4-level lookup chain):
        1. Exact match by cc_tool_use_id (fastest, covers duplicate SubagentStart)
        2. Match by session_id + name
        3. Match by same-name agent within team (covers MCP pre-registration)
        4. None found -> find/create OS team by cc_team_name -> register

        Levels 2 and 3 match on **name**, which is not an identity: two dispatches
        of the same agent name are two dispatches, each with its own transcript and
        therefore its own token ledger. Welding the second one onto the first row
        overwrites a ledger that cannot be rebuilt (A-06). So every same-name
        candidate goes through ``pick_reusable_row``, which only ever reuses a row
        that is the same dispatch or has never been a dispatch at all - anything
        else gets a new row.
        """
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "unnamed-agent")
        session_id = payload.get("session_id", "")
        cc_team_name = payload.get("cc_team_name", "")

        # Workflow (ultracode) fan-out agents take a dedicated path: each Workflow
        # run becomes its own OS team, every internal agent a distinct member.
        if agent_name == WORKFLOW_AGENT_TYPE:
            return await self._register_workflow_subagent(payload, cc_agent_id, session_id)

        existing = None
        leader = None
        team = None

        # 1. Exact match by cc_tool_use_id (fastest, covers duplicate SubagentStart events)
        if cc_agent_id:
            existing = await self.repo.find_agent_by_cc_id(cc_agent_id)

        # 2. Determine target team, then deduplicate within team
        if not existing:
            if cc_team_name:
                # Has cc_team_name -> resolve target team, deduplicate by name within that team only
                team = await self._resolve_cc_team(cc_team_name, session_id)
                if team:
                    team_agents = await self.repo.list_agents(team.id)
                    matches = [a for a in team_agents if a.name == agent_name]
                    existing = pick_reusable_row(matches, cc_agent_id)
            else:
                # No cc_team_name -> legacy compat: global lookup by session_id+name
                existing = pick_reusable_row(
                    await self.repo.find_agents_by_session_and_name(session_id, agent_name),
                    cc_agent_id,
                )

        # 3. Still no match -> find team via Leader, deduplicate by name within team
        if not existing and not team:
            leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)
            if team:
                team_agents = await self.repo.list_agents(team.id)
                matches = [a for a in team_agents if a.name == agent_name]
                existing = pick_reusable_row(matches, cc_agent_id)

        if existing:
            # Already registered -> update status, bind session and CC agent ID
            update_fields: dict = {
                "status": "busy",
                "session_id": session_id,
                "last_active_at": utc_now(),
            }
            # Never blank out an existing binding: cc_tool_use_id is this row's
            # dispatch identity, and an empty payload agent_id is "unknown", not
            # "none" - erasing it would strand the row's transcript and ledger.
            if cc_agent_id:
                update_fields["cc_tool_use_id"] = cc_agent_id
            # If existing role contains " — ", auto-split into role + current_task
            if existing.role and " — " in existing.role:
                parts = existing.role.split(" — ", 1)
                update_fields["role"] = parts[0].strip()
                update_fields["current_task"] = parts[1].strip()
            await self.repo.update_agent(existing.id, **update_fields)
            await self.event_bus.emit(
                "agent.status_changed",
                f"agent:{existing.id}",
                {
                    "agent_id": existing.id,
                    "name": agent_name,
                    "status": "busy",
                    "trigger": "hook",
                },
            )
            return {"status": "updated", "agent_id": existing.id}

        # 4. Not registered -> find/create OS team by cc_team_name, then register agent
        if not team and cc_team_name:
            team = await self._resolve_cc_team(cc_team_name, session_id)

        if not team:
            if not leader:
                leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)

        if not team and session_id:
            # Automatic enrollment (Creator ruling 2026-07-22: "fully open + always track automatically", task
            # 8705dac2):
            # a directly-dispatched agent with no team to belong to is no longer skipped and left unmanaged —
            # find-or-create this session's session-<sid8>
            # container team and enroll it. Confirmed in practice: in the Wenge session, a directly-dispatched agent was
            # actually running while the frontend showed nothing (memo 8e8b162b).
            team = await self._find_or_create_session_team(session_id, payload)
            if team:
                # Leader homing (fleet-layer §3: the Leader's home = the session container team): when adopted by a
                # workflow
                # team and not returned, or when its home team is already completed, home it together with the
                # enrollment — otherwise
                # find_active_team_by_leader keeps resolving to someone else's / an already-closed team.
                if not leader:
                    leader = await self._find_leader(session_id)
                if leader and leader.team_id != team.id:
                    home = (
                        await self.repo.get_team(leader.team_id)
                        if leader.team_id else None
                    )
                    if (
                        home is None
                        or home.status == "completed"
                        or str(home.name or "").startswith("workflow-")
                    ):
                        await self.repo.update_agent(leader.id, team_id=team.id)
                        logger.info(
                            "SubagentStart: leader '%s' re-homed to session team %s",
                            leader.name, team.name,
                        )

        if not team:
            logger.info(
                "SubagentStart: agent '%s' not registered and no active team found, skipping",
                agent_name,
            )
            return {"status": "skipped", "reason": "no active team"}

        # Final name dedup before creation (race condition: MCP may have completed registration during lookup)
        team_agents = await self.repo.list_agents(team.id)
        late_match = [a for a in team_agents if a.name == agent_name]
        existing = pick_reusable_row(late_match, cc_agent_id)
        if existing:
            late_fields: dict = {
                "status": "busy",
                "session_id": session_id,
                "last_active_at": utc_now(),
            }
            if cc_agent_id:
                late_fields["cc_tool_use_id"] = cc_agent_id
            await self.repo.update_agent(existing.id, **late_fields)
            logger.info(
                "SubagentStart: concurrent dedup hit for agent '%s' (id=%s)",
                agent_name,
                existing.id,
            )
            return {"status": "updated", "agent_id": existing.id}

        # Extract role and current_task from agent_name (if contains " — " separator)
        if " — " in agent_name:
            parts = agent_name.split(" — ", 1)
            auto_role = parts[0].strip()
            auto_task = parts[1].strip()
        else:
            auto_role = agent_name
            auto_task = None

        # Ownership resolution (2026-07-27 incident fix): the Leader's authority takes precedence over team.project_id.
        # Record: the session-80d0cc5e container team was bound to Wenge by the startup backfill, while the same team's
        # Leader row was
        # AI Team OS — the old code directly used `project_id=team.project_id`, so agent rows were misattributed
        # and the system_prompt's {project_path} rendered another project's path (one root cause, not two
        # bugs). When it cannot be resolved, leave it empty; never fall back on the cross-session-shared cwd global
        # state.
        agent_project_id = await self._resolve_agent_project_id(team, session_id)

        # Auto-fill standardized prompt template
        project_path = ""
        if agent_project_id:
            project = await self.repo.get_project(agent_project_id)
            if project:
                project_path = project.root_path or ""
        auto_system_prompt = self._render_prompt(auto_role, project_path)

        new_agent = await self.repo.create_agent(
            team_id=team.id,
            name=agent_name,
            role=auto_role,
            source="hook",
            session_id=session_id,
            cc_tool_use_id=cc_agent_id,
            system_prompt=auto_system_prompt,
        )
        # create_agent defaults to status=waiting, immediately set to busy
        update_kwargs: dict = {
            "status": "busy",
            "project_id": agent_project_id,
            "last_active_at": utc_now(),
        }
        if auto_task:
            update_kwargs["current_task"] = auto_task
        await self.repo.update_agent(new_agent.id, **update_kwargs)

        await self.event_bus.emit(
            "agent.status_changed",
            f"agent:{new_agent.id}",
            {
                "agent_id": new_agent.id,
                "name": agent_name,
                "status": "busy",
                "trigger": "hook_auto_register",
            },
        )
        # Decision event: Agent created (cockpit Phase 1)
        await self.event_bus.emit(
            "decision.agent_created",
            f"team:{team.id}",
            {
                "agent_id": new_agent.id,
                "agent_name": agent_name,
                "role": auto_role,
                "team_id": team.id,
                "team_name": team.name,
                "rationale": "auto_registered_via_hook",
                "alternatives": [],
                "outcome": "pending",
            },
        )
        logger.info(
            "SubagentStart: auto-registered agent '%s' -> team '%s' (cc_id=%s)",
            agent_name,
            team.name,
            cc_agent_id[:8] if cc_agent_id else "?",
        )
        return {"status": "created", "agent_id": new_agent.id}

    async def _on_subagent_stop(self, payload: dict) -> dict:
        """Handle sub-agent stop event.

        CC SubagentStop payload contains agent_id for exact matching.
        """
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "")
        session_id = payload.get("session_id", "")

        updated: list[str] = []
        if cc_agent_id:
            # Find via _resolve_agent (supports late binding fallback)
            agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)
            if agent:
                # Only update last_active_at, don't change status or current_task
                # CC's SubagentStop only means "one turn ended", agent may still be working
                # State changes are handled by StateReaper: 5min inactive->waiting, 30min->offline
                updates: dict = {"last_active_at": utc_now()}
                # P1 context watermark ledger (batch 1B): the SubagentStop payload
                # carries agent_transcript_path (batch0 contract test section 5), so
                # tail-read the sub-agent's last assistant usage and record its exact
                # context watermark. Best-effort: never block the stop path.
                try:
                    tpath = payload.get("agent_transcript_path") or ""
                    if tpath:
                        measured = agent_context.measure(tpath)
                        if measured is not None:
                            updates.update(measured)
                            updates["transcript_path"] = tpath
                except Exception:  # noqa: BLE001 — watermark capture must not break stop
                    pass
                # The session hop of the attribution chain (token attribution v1 stage 1): session_id should have been
                # written at
                # SubagentStart, but that column has historically been wiped (old SessionEnd code +
                # startup self-healing), so at the stop moment it is determined once more by the **file truth** — the
                # path
                # .../projects/<slug>/<session_id>/subagents/... itself encodes it.
                # Only when it cannot be derived do we fall back to the payload's session_id; when neither exists, leave
                # it empty and do not guess.
                _sid = transcript_path.derive_session_id(
                    payload.get("agent_transcript_path") or ""
                ) or session_id
                if _sid:
                    updates["session_id"] = _sid
                # Billing-caliber token attribution (a different matter from the context watermark above). The same
                # transcript is parsed along the way: group by requestId, take each group's last snapshot, then
                # accumulate;
                # naively adding row by row would severely inflate (streaming output_tokens are increasing snapshots,
                # not increments).
                # The model is captured along with it — the transcript holds the full model name, which is exactly
                # "backfilled by observation".
                try:
                    tpath = payload.get("agent_transcript_path") or ""
                    if tpath:
                        usage = token_attribution.parse_transcript_usage(tpath)
                        if usage:
                            updates["input_tokens"] = usage["input_tokens"]
                            updates["output_tokens"] = usage["output_tokens"]
                            updates["cache_creation_tokens"] = usage["cache_creation_tokens"]
                            updates["cache_read_tokens"] = usage["cache_read_tokens"]
                            updates["tokens_measured_at"] = utc_now()
                            # How this row's numbers came to be must be persisted with the row (stage 0 §2.6):
                            # the four-layer numbers that reach this point are all settled as truth by line-by-line
                            # transcript parsing,
                            # which is a different thing from numbers derived by read-side alias fallbacks; they must be
                            # distinguishable afterwards.
                            updates["tokens_source"] = TokenSource.TRANSCRIPT.value
                            if usage.get("model"):
                                updates["model"] = usage["model"]
                except Exception:  # noqa: BLE001 — bookkeeping must never block the stop path
                    logger.debug("token attribution failed", exc_info=True)
                await self.repo.update_agent(agent.id, **updates)
                # Strict 1:1 — SubagentStop carries agent_transcript_path with the wf_id;
                # promote workflow subagents out of the session-fallback team into their run team.
                await self._promote_workflow_team(agent, payload)
                updated.append(agent.id)
        else:
            # Fallback: find BUSY agents in this session, only update last_active_at without changing status
            agents = await self.repo.find_agents_by_session(session_id)
            for agent in agents:
                if agent.status == "busy":
                    await self.repo.update_agent(
                        agent.id,
                        last_active_at=utc_now(),
                    )
                    updated.append(agent.id)
        return {"status": "updated", "agents_waiting": updated}

    async def _resolve_cc_team(self, cc_team_name: str, session_id: str) -> object | None:
        """Find or create the corresponding OS team for a CC team name.

        1. Exact name match on existing OS teams (prefer active status)
        2. Not found -> auto-create a same-name OS team
        """
        if not cc_team_name:
            return None

        # 1. Find existing team by name
        existing_team = await self.repo.get_team_by_name(cc_team_name)
        if existing_team:
            logger.info(
                "CC team mapping: '%s' -> existing OS team (id=%s, status=%s)",
                cc_team_name,
                existing_team.id,
                existing_team.status,
            )
            # Auto-revive completed team if a new agent is joining — keeps
            # team.status consistent with reality (busy member exists).
            # Without this, agents end up "in a historical team" on the
            # dashboard, which confused the user when ecosystem-indexer
            # registered into a closed phase1-impl.
            # Project re-binding (confirmed in practice 2026-07-25: the wenge team's project_id was empty, the frontend
            # project view could not filter to it, and
            # 4 running agents were completely invisible). The old logic only bound the project at team-creation time —
            # if at that moment the Leader had not yet
            # registered (or the owner session id has a different origin from CC's internal team name) it could not
            # bind, and there was never a re-binding
            # opportunity afterwards. Re-bind on every resolution: fill it when empty, leave it when already bound.
            if not getattr(existing_team, "project_id", None):
                # Re-binding only recognizes the authoritative path (session→Leader→project); **cwd fallback is
                # forbidden**:
                # _current_event_cwd is an instance attribute; multi-session events sharing one translator overwrite
                # each other —
                # re-binding happens whenever any event arrives, and using cwd would very easily bind project A's team
                # to project B
                # (measured 2026-07-26: the wenge team was bound to AI Team OS). A cwd fallback is only allowed at
                # team-creation time
                # (at that moment the cwd and the event share an origin, and the 2026-05-08 cross-project leak
                # incident's line of defense is still in place).
                pid = await self._infer_project_id(session_id, allow_cwd_fallback=False)
                if pid:
                    await self.repo.update_team(existing_team.id, project_id=pid)
                    existing_team.project_id = pid
                    logger.info(
                        "CC team mapping: back-filled project %s onto team '%s'",
                        pid, cc_team_name,
                    )
            if existing_team.status == "completed":
                await self.repo.update_team(existing_team.id, status="active")
                logger.warning(
                    "Team '%s' was completed; auto-revived to active because "
                    "a new agent (cc_team_name=%s) is registering.",
                    cc_team_name, cc_team_name,
                )
                await self.event_bus.emit(
                    "team.auto_revived",
                    f"team:{existing_team.id}",
                    {"team_id": existing_team.id, "team_name": cc_team_name,
                     "reason": "new agent registration on completed team"},
                )
                existing_team.status = "active"
            return existing_team

        # 2. Auto-create same-name OS team. Stamp owner_session_id so SessionEnd
        # only closes teams owned by the ending session (fleet-layer design §5,
        # fixes 7ae3b7cd: a bystander session's SessionEnd must not clobber it).
        new_team, _created = await self.repo.get_or_create_team(
            name=cc_team_name,
            mode="coordinate",
            config={"owner_session_id": session_id} if session_id else {},
        )
        if not _created:
            # Concurrent find-or-create race: another event has already created a same-named team (teams.name UNIQUE);
            # get_or_create_team already swallowed the IntegrityError and re-fetched the existing row. The winner
            # completes
            # the project link and the team.created event itself — the loser returns the existing row directly, avoiding
            # an ASGI 500.
            return new_team
        logger.info(
            "CC team mapping: auto-created OS team '%s' (id=%s)",
            cc_team_name,
            new_team.id,
        )

        project_id = await self._infer_project_id(session_id)
        if project_id:
            await self.repo.update_team(new_team.id, project_id=project_id)
            logger.info(
                "CC team mapping: team '%s' linked to project %s",
                cc_team_name,
                project_id,
            )

        await self.event_bus.emit(
            "team.created",
            f"team:{new_team.id}",
            {
                "team_id": new_team.id,
                "team_name": cc_team_name,
                "source": "cc_team_mapping",
                "session_id": session_id,
            },
        )
        return new_team

    async def _touch_session_leader(self, session_id: str) -> None:
        """Tool-event-driven Leader activity: busy while the conversation is in progress, with last_active following
        along.

        A 5-minute heartbeat once knocked a Leader mid-conversation offline during a long turn (user-measured:
        "we were talking to it, yet it showed as closed"). The tool event stream is the activity source of truth:
        throttled to one DB write
        per 60s; when the status is not busy it revives immediately. After the turn ends there are no events → the
        heartbeat times out and decays naturally,
        which is semantically correct (busy = events flowing, offline = silence timeout).
        """
        if not session_id:
            return
        now = utc_now()
        prev = self._leader_touch.get(session_id)
        if prev is not None and (now - prev).total_seconds() < 60:
            return
        self._leader_touch[session_id] = now
        try:
            for a in await self.repo.find_agents_by_session(session_id):
                if a.role == "leader":
                    kwargs: dict = {"last_active_at": now}
                    if str(getattr(a, "status", "")).lower() != "busy":
                        kwargs["status"] = "busy"
                    await self.repo.update_agent(a.id, **kwargs)
        except Exception:  # noqa: BLE001 — an activity touch must never affect the main event flow
            pass

    @staticmethod
    def _read_session_model(path: str) -> str:
        """Tail-read the main session transcript's real model — the implementation is unified in session_probe.

        The parameter is deliberately not named ``transcript_path``: that is the name of the deriver module imported by
        this module, and
        a same-named parameter would shadow it inside the function body — a pitfall that only bites when someone adds a
        line of code here.
        """
        from aiteam.api import session_probe

        return session_probe.read_session_model(path)

    async def _find_leader(self, session_id: str) -> object | None:
        """Find the leader agent for the current session — strictly by session_id.

        session_id is the hard identity boundary (fleet-layer design §3): a leader
        row belongs to exactly one CC session and is never reused or rebound across
        sessions. The former cross-session fallback (global role="leader" lookup +
        self-heal session rebind) was the churn source behind 03fe7cae (leader rows
        shared across sessions, session_id/project_id rewritten back and forth,
        leader parasitizing a workflow team). It is removed: no match -> return None.
        Callers resolve project by cwd or skip; they never guess a leader from
        another session (aligns with the workflow side's "strict ownership, leave
        empty rather than guess").
        """
        if not session_id:
            return None
        agents = await self.repo.find_agents_by_session(session_id)
        if not agents:
            return None
        # Prefer leader role agent
        leaders = [a for a in agents if a.role == "leader"]
        if leaders:
            return leaders[0]
        # Then prefer api-source agent
        api_matches = [a for a in agents if a.source == "api"]
        if api_matches:
            return api_matches[0]
        # Finally return any matching agent (BUSY first)
        agents.sort(key=lambda a: 0 if a.status == "busy" else 1)
        return agents[0]

    async def _self_heal_agent(self, agent, trigger: str = "self_heal") -> None:
        """Self-heal: WAITING agent receives tool event -> correct to BUSY."""
        if agent.status != "waiting":
            return
        await self.repo.update_agent(agent.id, status="busy")
        await self.event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "waiting",
                "status": "busy",
                "trigger": trigger,
            },
        )
        logger.info("Self-heal: %s WAITING->BUSY (trigger=%s)", agent.name, trigger)

    @staticmethod
    def _extract_file_path(tool_input: dict | str) -> str:
        """Extract file path from tool input."""
        if isinstance(tool_input, dict):
            return tool_input.get("file_path", "") or tool_input.get("path", "")
        return ""

    # Tools whose interesting field is neither description nor command, so the
    # generic fallback stringifies the whole input and truncates at 200 chars —
    # which for these is a wall of JSON that says nothing. Each entry names the
    # field that actually identifies the call.
    _SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
        "Skill": ("skill", "args"),
        "SendMessage": ("summary", "to"),
        "TaskCreate": ("subject", "description"),
        "TaskUpdate": ("subject", "status", "owner"),
        "TaskGet": ("taskId",),
        "TaskStop": ("taskId", "reason"),
        "AskUserQuestion": ("question",),
        "ExitPlanMode": ("plan",),
        "EnterWorktree": ("name", "path"),
        "ExitWorktree": ("name", "path"),
        "WebFetch": ("url", "prompt"),
        "WebSearch": ("query",),
        "ToolSearch": ("query",),
    }

    def _extract_input_summary(self, tool_name: str, tool_input: dict | str) -> str:
        """Extract summary from tool input — file edit tools prioritize storing file_path."""
        if isinstance(tool_input, dict):
            if tool_name in self._FILE_EDIT_TOOLS:
                return (
                    tool_input.get("file_path", "")
                    or tool_input.get("path", "")
                    or tool_input.get("description", "")
                    or str(tool_input)[:200]
                )
            for field in self._SUMMARY_FIELDS.get(tool_name, ()):
                value = tool_input.get(field)
                if value:
                    return str(value)[:200]
            return (
                tool_input.get("description", "")
                or tool_input.get("command", "")
                or tool_input.get("file_path", "")
                or tool_input.get("pattern", "")
                or str(tool_input)[:200]
            )
        if isinstance(tool_input, str):
            return tool_input[:200]
        return ""

    async def _check_file_edit_conflict(
        self,
        tool_name: str,
        tool_input: dict | str,
        target_agent_id: str,
        target_agent_name: str,
        session_id: str,
    ) -> None:
        """Detect file edit conflicts — O(1) query via in-memory tracker + DB fallback.

        Enhancements:
        1. In-memory tracker first: O(1) query, no DB scan needed
        2. Exact file_path matching: no longer relies on input_summary substring matching
        3. Conflict severity grading: 2 agents editing same file vs 3+ agents
        4. Records to tracker for hotspot statistics
        """
        if tool_name not in self._FILE_EDIT_TOOLS:
            return

        file_path = self._extract_file_path(tool_input)
        if not file_path:
            return

        # Periodically clean up expired records (piggyback on each detection, negligible overhead)
        self._file_tracker.cleanup()

        # Record this edit
        self._file_tracker.record(file_path, target_agent_id, target_agent_name)

        # Use in-memory tracker to find conflicts (O(1) lookup)
        conflicts = self._file_tracker.find_conflicts(
            file_path,
            target_agent_id,
            window_minutes=5,
        )

        if not conflicts:
            # In-memory tracker has no conflicts -> DB fallback (covers cold start after tracker restart)
            conflicts = await self._db_fallback_conflict_check(
                file_path,
                target_agent_id,
                session_id,
            )

        if not conflicts:
            return

        # Dedup: report each agent only once
        seen_agents: dict[str, _FileEditRecord] = {}
        for c in conflicts:
            if c.agent_id not in seen_agents:
                seen_agents[c.agent_id] = c

        # Conflict severity
        conflict_count = len(seen_agents)
        severity = "high" if conflict_count >= 2 else "medium"

        conflicting_agents = [
            {"name": r.agent_name, "id": r.agent_id, "last_edit": r.timestamp.isoformat()}
            for r in seen_agents.values()
        ]

        await self.event_bus.emit(
            "file.edit_conflict",
            f"file:{file_path}",
            {
                "file_path": file_path,
                "current_agent_name": target_agent_name,
                "current_agent_id": target_agent_id,
                "conflicting_agents": conflicting_agents,
                "severity": severity,
                "session_id": session_id,
            },
        )
        agent_names = ", ".join(r.agent_name for r in seen_agents.values())
        logger.warning(
            "File edit conflict[%s]: %s — %s (prior) vs %s (current)",
            severity,
            file_path,
            agent_names,
            target_agent_name,
        )

    async def _db_fallback_conflict_check(
        self,
        file_path: str,
        current_agent_id: str,
        session_id: str,
    ) -> list[_FileEditRecord]:
        """DB fallback conflict detection — when in-memory tracker has no data (cold start).

        Improved: directly matches file_path instead of substring matching input_summary.
        """
        session_agents = await self.repo.find_agents_by_session(session_id)
        other_busy = [a for a in session_agents if a.id != current_agent_id and a.status == "busy"]
        if not other_busy:
            return []

        cutoff = utc_now() - timedelta(minutes=5)
        conflicts: list[_FileEditRecord] = []
        for other in other_busy:
            activities = await self.repo.list_activities(other.id, limit=20)
            for act in activities:
                if act.timestamp and act.timestamp < cutoff:
                    break
                if act.tool_name not in self._FILE_EDIT_TOOLS:
                    continue
                # Improved: exact file_path matching (normalized path separators)
                act_summary = (act.input_summary or "").replace("\\", "/")
                normalized_path = file_path.replace("\\", "/")
                if normalized_path == act_summary or normalized_path in act_summary:
                    record = _FileEditRecord(
                        agent_id=other.id,
                        agent_name=other.name,
                        timestamp=act.timestamp,
                    )
                    conflicts.append(record)
                    # Also populate in-memory tracker
                    self._file_tracker.record(
                        file_path,
                        other.id,
                        other.name,
                    )
                    break  # Only take most recent per agent
        return conflicts

    def get_file_hotspots(self, window_minutes: int = 10) -> list[dict]:
        """Get hotspot file info — used by team_briefing.

        Returns:
            List of files edited by multiple agents, with agents and edit_count.
        """
        self._file_tracker.cleanup()
        return self._file_tracker.get_hotspots(window_minutes=window_minutes)

    def get_agent_editing_files(self, agent_id: str) -> list[str]:
        """Get files recently being edited by an agent — used when registering agents."""
        return self._file_tracker.get_agent_files(agent_id)

    async def _resolve_agent(
        self,
        cc_agent_id: str,
        agent_name: str,
        session_id: str,
    ) -> object | None:
        """Resolve which agent a tool call belongs to — supports cc_id exact match + name fallback.

        CC team agents have a race condition: SubagentStart may fire before MCP registration,
        leaving cc_tool_use_id unbound. This method falls back to name matching within team
        when cc_id lookup fails, and binds cc_tool_use_id (late binding) to fix all subsequent lookups.

        The name fallback is gated by the same dispatch-identity rule as registration
        (A-06): late binding **writes** cc_tool_use_id onto the row it picks, and the
        SubagentStop path then overwrites that row's token ledger - so binding onto a
        row that already carries an account, or that already belongs to another
        dispatch, destroys a ledger that cannot be rebuilt. When no candidate is
        eligible the call stays unresolved: losing one observation of an unregistered
        agent beats corrupting an existing one (same stance as TeammateIdle).
        """
        # 1. Priority: exact match via cc_tool_use_id
        if cc_agent_id:
            agent = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if agent:
                return agent

        # 2. Fallback: cc_agent_id exists but unbound (race condition), find by name within team
        if cc_agent_id and agent_name:
            leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)
                if team:
                    team_agents = await self.repo.list_agents(team.id)
                    matches = [a for a in team_agents if a.name == agent_name and a.id != leader.id]
                    agent = pick_reusable_row(matches, cc_agent_id)
                    if agent is not None:
                        # Late binding: bind cc_tool_use_id to fix all subsequent lookups
                        await self.repo.update_agent(
                            agent.id,
                            cc_tool_use_id=cc_agent_id,
                            session_id=session_id,
                        )
                        logger.info(
                            "Late binding: agent '%s' bound cc_id=%s",
                            agent_name,
                            cc_agent_id[:8],
                        )
                        return agent

        # 3. No agent_id -> main session tool call (Leader)
        if not cc_agent_id:
            return await self._find_leader(session_id)

        return None

    async def _on_pre_tool_use(self, payload: dict) -> dict:
        """Record tool use event.

        CC PreToolUse payload:
        - agent_id/agent_type: present when from a sub-agent
        - tool_name, tool_input: tool information
        - tool_input.description: tool call description
        """
        tool_name = payload.get("tool_name", "unknown")
        session_id = payload.get("session_id", "")
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "")
        tool_input = payload.get("tool_input", {})

        input_summary = self._extract_input_summary(tool_name, tool_input)

        # Step 4 — pre-register the planned roster when a Workflow is launched.
        # The script is in tool_input.script (confirmed); parse meta.phases (declarative,
        # 100% knowable) + literal agent() calls so the run's skeleton is visible BEFORE
        # any agent starts working (esp. serial later phases). Dynamic fan-out size is runtime.
        if tool_name == "Workflow" and isinstance(tool_input, dict):
            try:
                plan = self._parse_workflow_plan(str(tool_input.get("script", "")))
                # I3a: stash the plan so the PostToolUse receipt can fill it in (the wf_id is not visible yet at this
                # point).
                if session_id:
                    self._workflow_plans[session_id] = plan
                await self.event_bus.emit(
                    "workflow.planned",
                    f"session:{session_id}",
                    {
                        "session_id": session_id,
                        "name": plan["name"],
                        "phases": plan["phases"],
                        "literal_agent_count": plan["literal_agent_count"],
                        "dynamic_nodes": plan["dynamic_nodes"],
                    },
                )
            except Exception:  # noqa: BLE001 — pre-registration must never block the call
                pass

        # Resolve which agent this tool call belongs to (supports cc_id exact match + name fallback)
        target_agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)

        if target_agent:
            # Self-heal: IDLE agent receives tool event -> correct to BUSY
            await self._self_heal_agent(target_agent)

            # Update last active time + heal missing project binding.
            # Project binding used to happen only at SessionStart; a Leader created
            # before its project was registered (or before cwd resolved) stayed
            # project_id=None forever, so project liveness ("working") never saw it
            # despite constant activity. Heal here so any tool call repairs it.
            update_fields: dict = {"last_active_at": utc_now()}
            if (
                getattr(target_agent, "role", None) == "leader"
                and not getattr(target_agent, "project_id", None)
            ):
                healed_pid = await self._resolve_project_id_by_cwd(payload.get("cwd", ""))
                if healed_pid:
                    update_fields["project_id"] = healed_pid
            await self.repo.update_agent(target_agent.id, **update_fields)

            # Strict 1:1 — a workflow subagent's own tool call may carry agent_transcript_path
            # with the wf_id; promote it out of the session-fallback team as early as possible.
            await self._promote_workflow_team(target_agent, payload)

            start_time = utc_now()
            activity = await self.repo.create_activity(
                agent_id=target_agent.id,
                session_id=session_id,
                tool_name=tool_name,
                input_summary=input_summary,
                status="running",
            )
            # Record pending span for PostToolUse correlation
            span_key = f"{target_agent.id}:{session_id}:{tool_name}"
            self._pending_spans[span_key] = (activity.id, start_time)
            # current_task is set by Leader via API, hook does not auto-override

            # Intent event: only emit for substantive tools and when throttle threshold exceeded
            if tool_name in self._INTENT_TOOLS:
                last_emit = self._intent_last_emit.get(target_agent.id)
                elapsed = (start_time - last_emit).total_seconds() if last_emit else float("inf")
                if elapsed >= self._INTENT_THROTTLE_SECS:
                    self._intent_last_emit[target_agent.id] = start_time
                    await self.event_bus.emit(
                        "intent.agent_working",
                        f"agent:{target_agent.id}",
                        {
                            "agent_id": target_agent.id,
                            "agent_name": target_agent.name,
                            "tool_name": tool_name,
                            "intent_summary": f"Using {tool_name}",
                            "input_preview": input_summary[:100],
                        },
                    )

            # File edit conflict detection (only records events, does not block operations)
            try:
                await self._check_file_edit_conflict(
                    tool_name,
                    tool_input,
                    target_agent.id,
                    target_agent.name,
                    session_id,
                )
            except Exception as exc:
                logger.warning("Conflict detection error (does not affect tool use): %s", exc)

        # Decision events below match on the BARE tool name: CC sends the client-side
        # full name (mcp__ai-team-os__meeting_create), so the old bare literals could
        # never match — and the old `meeting_start` literal is not even a real tool
        # name (the tool is meeting_create), so that branch was doubly dead.
        # (2026-07-27 batch 3.)
        bare_tool = normalize_tool_name(tool_name)

        # Abort observation: CC provides no hook on the abort side (there is no TaskStop/TaskAborted event,
        # and an Esc interruption is silent too); the only place that can see "a task was stopped" is the TaskStop
        # **tool call**.
        # Measured in practice, aborting is the main path — TaskStop was used 40 times and is still in use, TaskCreate
        # 14 times and dropped to zero after 7/8
        # (in newer CC, a background subagent is a task, and stopping an agent goes through it). Pulling it out of the
        # fifty thousand
        # cc.tool_use entries into a first-class event is the only way the abort signal can be queried.
        if bare_tool == "TaskStop" and isinstance(tool_input, dict):
            await self.event_bus.emit(
                EventType.CC_TASK_STOPPED.value,
                f"session:{session_id}",
                {
                    "session_id": session_id,
                    "cc_task_id": str(tool_input.get("taskId") or ""),
                    "reason": str(tool_input.get("reason") or ""),
                    "agent_name": payload.get("agent_type", ""),
                },
            )

        # Decision event: meeting created (meeting_create tool call)
        if bare_tool == "meeting_create" and isinstance(tool_input, dict):
            await self.event_bus.emit(
                "decision.meeting_started",
                f"session:{session_id}",
                {
                    "agent_name": payload.get("agent_type", ""),
                    "topic": tool_input.get("topic", ""),
                    "participants": tool_input.get("participants", []),
                    "rationale": tool_input.get("purpose", "")[:200],
                    "alternatives": [],
                    "outcome": "pending",
                    "session_id": session_id,
                },
            )

        # Decision event: task assigned (task_run tool call)
        if bare_tool == "task_run" and isinstance(tool_input, dict):
            await self.event_bus.emit(
                "decision.task_assigned",
                f"session:{session_id}",
                {
                    "agent_name": payload.get("agent_type", ""),
                    "task_title": tool_input.get("title", tool_input.get("task", "")),
                    "assigned_to": tool_input.get("agent_name", tool_input.get("assigned_to", "")),
                    "rationale": tool_input.get("description", "")[:200],
                    "alternatives": [],
                    "outcome": "pending",
                    "session_id": session_id,
                },
            )

        await self.event_bus.emit(
            "cc.tool_use",
            f"session:{session_id}",
            {
                "tool_name": tool_name,
                "tool_input_summary": input_summary[:200],
                "session_id": session_id,
                "agent_name": payload.get("agent_type", ""),
            },
        )
        return {"decision": "allow"}

    async def _on_post_tool_use(self, payload: dict) -> dict:
        """Record tool completion event, including output summary.

        CC PostToolUse payload additionally contains:
        - tool_response: {stdout, stderr} or other tool output
        """
        tool_name = payload.get("tool_name", "unknown")
        session_id = payload.get("session_id", "")
        cc_agent_id = payload.get("agent_id", "")
        tool_input = payload.get("tool_input", {})
        tool_response = payload.get("tool_response", {})

        # Leader activity: tool events flowing = conversation in progress (60s throttling, see _touch_session_leader)
        await self._touch_session_leader(session_id)

        input_summary = self._extract_input_summary(tool_name, tool_input)

        # Extract output summary
        output_summary = ""
        if isinstance(tool_response, dict):
            output_summary = (
                tool_response.get("stdout", "")
                or tool_response.get("stderr", "")
                or str(tool_response)[:500]
            )
            output_summary = output_summary[:500]
        elif isinstance(tool_response, str):
            output_summary = tool_response[:500]

        # I3a: Workflow start receipt → run skeleton (running) + workflow.started (a correlation anchor, not a completed
        # state).
        if tool_name == "Workflow":
            try:
                await self._ingest_workflow_receipt(payload, session_id, tool_response)
            except Exception:  # noqa: BLE001 — observation ingestion must never block the hook's return
                logger.warning("workflow receipt ingest failed", exc_info=True)

        # Resolve which agent this tool call belongs to (supports cc_id exact match + name fallback)
        agent_name = payload.get("agent_type", "")
        target_agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)

        if target_agent:
            # Self-heal: IDLE agent receives tool completion event -> correct to BUSY
            await self._self_heal_agent(target_agent, trigger="self_heal_post")

            # Update last active time
            now = utc_now()
            await self.repo.update_agent(target_agent.id, last_active_at=now)

            # Try to correlate with the running activity created by PreToolUse
            span_key = f"{target_agent.id}:{session_id}:{tool_name}"
            pending = self._pending_spans.pop(span_key, None)

            if pending:
                activity_id, start_time = pending
                duration_ms = int((now - start_time).total_seconds() * 1000)
                await self.repo.update_activity(
                    activity_id,
                    status="completed",
                    output_summary=output_summary,
                    duration_ms=duration_ms,
                )
            else:
                # Backward compat: no pending span found, create new completed record
                await self.repo.create_activity(
                    agent_id=target_agent.id,
                    session_id=session_id,
                    tool_name=tool_name,
                    input_summary=input_summary,
                    output_summary=output_summary,
                    status="completed",
                )

        await self.event_bus.emit(
            "cc.tool_complete",
            f"session:{session_id}",
            {
                "tool_name": tool_name,
                "session_id": session_id,
                "agent_name": payload.get("agent_type", ""),
            },
        )
        await self._record_notable_call(payload, session_id, tool_name, tool_response)
        return {"status": "recorded"}

    # The body cap for SendMessage. Messages between teammates can be long (dispatch letters routinely run to thousands
    # of characters); storing them in full
    # has value but cannot be unbounded — the excess is truncated, and the true length is recorded separately in
    # message_chars.
    _MESSAGE_BODY_CAP = 4000

    async def _record_notable_call(
        self, payload: dict, session_id: str, tool_name: str, tool_response: object
    ) -> None:
        """Three kinds of tool calls are worth pulling out of the cc.tool_complete flood and archiving in full.

        - **ExitPlanMode**: ``tool_input.plan`` is the full plan the Leader has thought through and is about to present
          to
          the user. Previously it existed in the activity stream only as a 200-character-truncated input_summary,
          and the plan body was lost on the spot.
        - **AskUserQuestion**: the original scene of a human-review decision — what was asked and which option the user
          chose.
          27 times in 30 days; every one is the basis for all subsequent work, and previously likewise only a truncated
          one-line summary remained.
        - **SendMessage**: CC-native teammate direct messages, 279 times in 30 days. The mailbox is managed by CC
          itself,
          and the OS's positioning is a **read-only mirror** — so it mirrors into the event stream and **does not write
          into
          channel_messages**: that table is the OS's own broadcast channel (planned but not enabled),
          whose semantics differ from teammate direct messages; merging them would blur the boundary between the two
          channels irreversibly.
          The recipient is recorded only here; cc.tool_use stores only the tool name and a one-line summary.

        All three are low-frequency, and their bodies are worth keeping in full (contrast: heartbeats run to tens of
        thousands a day, and this batch just turned those off).
        Failures are uniformly silent — bookkeeping must not, in turn, block tool returns.
        """
        if tool_name not in ("ExitPlanMode", "AskUserQuestion", "SendMessage"):
            return
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return
        try:
            if tool_name == "ExitPlanMode":
                plan = str(tool_input.get("plan") or "")
                if not plan:
                    return
                await self.event_bus.emit(
                    "decision.plan_presented",
                    f"session:{session_id}",
                    {
                        "session_id": session_id,
                        "agent_name": payload.get("agent_type", ""),
                        "plan": plan,
                        "plan_chars": len(plan),
                    },
                )
                return
            if tool_name == "SendMessage":
                body = tool_input.get("message")
                body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                await self.event_bus.emit(
                    "cc.message_sent",
                    f"session:{session_id}",
                    {
                        "session_id": session_id,
                        "sender": payload.get("agent_type", ""),
                        "recipient": str(tool_input.get("to") or ""),
                        "summary": str(tool_input.get("summary") or "")[:200],
                        "message": body[: self._MESSAGE_BODY_CAP],
                        "message_chars": len(body),
                    },
                )
                return
            # AskUserQuestion's real payload is a **plural questions array** (each item has question /
            # header / options); previously only the singular tool_input["question"] was read, so 6 production
            # human-review decision records had empty question/options with all real content stuffed into the answer
            # JSON.
            # The singular form is kept as a fallback.
            questions = tool_input.get("questions")
            headers: list[str] = []
            if isinstance(questions, list) and questions:
                texts: list[str] = []
                options: list = []
                for item in questions:
                    if not isinstance(item, dict):
                        continue
                    texts.append(str(item.get("question") or ""))
                    if item.get("header"):
                        headers.append(str(item["header"]))
                    item_options = item.get("options")
                    if isinstance(item_options, list):
                        options.extend(item_options)
                question = "\n".join(t for t in texts if t)
                question_count = len(texts)
            else:
                question = str(tool_input.get("question") or "")
                options = tool_input.get("options") or []
                question_count = 1 if question else 0
            await self.event_bus.emit(
                "decision.user_asked",
                f"session:{session_id}",
                {
                    "session_id": session_id,
                    "agent_name": payload.get("agent_type", ""),
                    "question": question,
                    "question_count": question_count,
                    "headers": headers,
                    "options": options,
                    "answer": tool_response if isinstance(tool_response, (str, dict, list)) else "",
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("decision moment capture failed for %s", tool_name, exc_info=True)

    async def _on_worktree_event(self, payload: dict) -> dict:
        """WorktreeCreate / WorktreeRemove — the birth and disappearance of isolated workspaces.

        This repo's multi-session parallel discipline requires the second and later sessions to be isolated with git
        worktrees, while the OS
        previously had no awareness of worktrees appearing or disappearing at all. The two payload fields are
        **asymmetric** (verified against the CC
        binary): WorktreeCreate only gives ``name``, and WorktreeRemove only gives
        ``worktree_path``, so each side records its own and does not pretend they can be paired.
        """
        event_name = payload.get("hook_event_name", "")
        created = event_name == "WorktreeCreate"
        session_id = payload.get("session_id", "")
        await self.event_bus.emit(
            "cc.worktree_created" if created else "cc.worktree_removed",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "cwd": payload.get("cwd", ""),
                "name": payload.get("name", ""),
                "worktree_path": payload.get("worktree_path", ""),
            },
        )
        return {"status": "recorded"}

    async def _ingest_workflow_receipt(
        self, payload: dict, session_id: str, tool_response: object
    ) -> None:
        """PostToolUse(Workflow) receipt → run skeleton (running) + workflow.started + best-effort file ingest.

        The receipt counts only as a "start receipt / correlation anchor", not a completed state: measured in practice,
        the receipt returns in ~7s while a run takes ≈56min,
        so at this moment wf_<id>.json has mostly not landed yet; build only the running skeleton and let the
        reaper/reconciliation fill in the completed state.
        Obtain wf_id+cc_task_id+script_path+name all at once, root-fixing the wf_id being invisible at SubagentStart.
        """
        text = tool_response if isinstance(tool_response, str) else ""
        if not text and isinstance(tool_response, dict):
            text = (
                tool_response.get("stdout")
                or tool_response.get("content")
                or str(tool_response)
            )
        receipt = workflow_ingest.parse_workflow_receipt(text or "")
        wf_id = receipt.get("wf_id")
        if not wf_id:
            return

        plan = self._workflow_plans.get(session_id, {})

        # Associate the existing workflow-<wf_id> team; if there is none, adopt this session's fallback team —
        # this run's early-arriving agents all hang there, and the receipt is the first moment a wf_id is available, so
        # patch the link in place;
        # otherwise run.team_id and team.config.workflow_run_id stay empty in both directions for the whole live run
        #(monitoring record 2026-07-06: team=NULL for the entire run).
        team = await self.repo.get_team_by_name(f"workflow-{wf_id}")
        fallback = None
        if team is None and session_id:
            fallback = await self.repo.get_team_by_name(
                f"workflow-session-{session_id[:8]}"
            )
            fb_wf = (fallback.config or {}).get("workflow_run_id") if fallback else None
            if fallback is not None and fb_wf in (None, "", wf_id):
                team = fallback
                fallback = None  # The whole team has been adopted; no need to migrate members again
                await self._reactivate_workflow_team(team)
        team_id = team.id if team else None
        project_id = (getattr(team, "project_id", None) or "") if team else ""
        if not project_id:
            # Cross-project fix C (strict ownership): resolve only by the launching session's cwd → longest prefix among
            # registered projects,
            # with the same-session Leader as second choice; when unresolvable, leave it empty and never guess. The old
            # _find_leader cross-session fallback
            # once attributed an unregistered project's run to another project's Leader (a recorded isolation
            # violation).
            project_id = (
                await self._resolve_project_id_by_cwd(payload.get("cwd") or "") or ""
            )
            if not project_id and session_id:
                try:
                    same = await self.repo.find_agents_by_session(session_id)
                except Exception:  # noqa: BLE001
                    same = []
                leaders = [a for a in same if a.role == "leader"]
                project_id = (leaders[0].project_id or "") if leaders else ""

        # Receipt-time team creation (2026-07-07 D1 record): per-run teams were originally created lazily only at
        # SubagentStop's
        # promote moment — a run killed mid-turn / whose long turn never ends would forever
        # have no team and stay invisible on the project page throughout. The receipt is the earliest moment each run
        # reliably obtains a wf_id (~7s),
        # so create the team in place and migrate this session's still-active workflow-subagents from the fallback team
        # into it; subsequent
        # SubagentStop _promote_workflow_team calls then only need to migrate and no longer bear the team-creation duty.
        if team is None:
            team, _created = await self.repo.get_or_create_team(
                name=f"workflow-{wf_id}",
                mode="coordinate",
                config={"kind": "workflow", "auto_created": True, "workflow_run_id": wf_id},
                project_id=project_id or None,
            )
            team_id = team.id
            # On race-loss (a same-named per-run team was concurrently created), skip migrating members — the winner
            # already migrated them; any stragglers that slipped through
            # are migrated as a fallback by later SubagentStop _promote_workflow_team calls.
            if _created and fallback is not None and session_id:
                try:
                    same = await self.repo.find_agents_by_session(session_id)
                except Exception:  # noqa: BLE001
                    same = []
                for a in same:
                    if (
                        getattr(a, "team_id", None) == fallback.id
                        and getattr(a, "role", "") == WORKFLOW_AGENT_TYPE
                        and str(getattr(a, "status", "")).endswith("busy")
                    ):
                        # Negative exclusion (mis-migration confirmed 2026-07-08, wf-a6a1a875a3): when multiple runs run
                        # serially in the same session,
                        # the fallback team may still hold a busy agent from the previous run that has not stopped yet —
                        # when the observation row knows its true wf_id, skip those not belonging to this run;
                        # when no observation row can be found (a new agent in this run's first few seconds), let it
                        # pass; if wrong,
                        # SubagentStop's promote will correct it precisely.
                        try:
                            cc_id = getattr(a, "cc_tool_use_id", "") or ""
                            if cc_id:
                                known = await self.repo.find_workflow_agents_by_cc_id(cc_id)
                                if known and all(w.wf_id != wf_id for w in known):
                                    continue
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            await self.repo.update_agent(a.id, team_id=team.id)
                        except Exception:  # noqa: BLE001
                            pass

        # phases: in the plan they are a list of title strings; normalized to [{index,title}].
        phases = [
            {"index": i, "title": t}
            for i, t in enumerate(plan.get("phases", []) or [], start=1)
        ]

        run = WorkflowRun(
            wf_id=wf_id,
            project_id=project_id,
            team_id=team_id,
            session_id=session_id or None,
            cc_task_id=receipt.get("cc_task_id") or None,
            name=receipt.get("name") or plan.get("name", ""),
            status="running",
            source="hook",
            phases=phases,
            planned_agent_count=int(plan.get("literal_agent_count", 0) or 0),
            dynamic_nodes=int(plan.get("dynamic_nodes", 0) or 0),
            summary=receipt.get("summary") or "",
            script_path=receipt.get("script_path") or "",
            # Cross-project fix A: persist the receipt's Transcript dir — from then on, live/terminal states address it
            # directly,
            # no longer depending on project registration (runs of unregistered projects were once misjudged as
            # interrupted, fully blind while live).
            transcript_dir=receipt.get("transcript_dir") or "",
        )
        await self.repo.upsert_workflow_run(run)

        # link team.config.workflow_run_id → team_id (already written when the existing chain creates the team; this is
        # the fallback).
        if team is not None and (team.config or {}).get("workflow_run_id") != wf_id:
            cfg = dict(team.config or {})
            cfg["workflow_run_id"] = wf_id
            try:
                await self.repo.update_team(team.id, config=cfg)
            except Exception:  # noqa: BLE001
                pass

        await self.event_bus.emit(
            "workflow.started",
            f"workflow:{wf_id}",
            {
                "wf_id": wf_id,
                "name": run.name,
                "status": "running",
                "cc_task_id": run.cc_task_id,
                "script_path": run.script_path,
                "team_id": team_id,
                "project_id": project_id,
                "planned_agent_count": run.planned_agent_count,
                "dynamic_nodes": run.dynamic_nodes,
                "phases": run.phases,
                "source": "hook",
                "session_id": session_id,
            },
            entity_id=wf_id,
            entity_type="workflow",
        )

        # best-effort: when the receipt returns, the file has mostly not landed → no-op; if it has landed, complete the
        # ingestion directly.
        jp = workflow_ingest.run_json_path_from_transcript_dir(
            receipt.get("transcript_dir", ""), wf_id
        )
        if jp is not None and jp.exists():
            try:
                await workflow_ingest.ingest_run_from_file(self.repo, self.event_bus, jp)
            except Exception:  # noqa: BLE001
                logger.warning("workflow receipt best-effort ingest failed", exc_info=True)

    async def _resolve_project_id_by_cwd(self, cwd: str) -> str | None:
        """Resolve project_id from a cwd via longest root_path prefix match.

        Several projects can prefix-match the same cwd (e.g. C:/Users/TUF vs
        C:/Users/TUF/Desktop/<proj>); the most specific (longest) wins. Returns
        None when cwd is empty or matches no registered project.
        """
        cwd_norm = (cwd or "").replace("\\", "/").rstrip("/").lower()
        if not cwd_norm:
            return None
        best_id: str | None = None
        best_len = -1
        for proj in await self.repo.list_projects():
            rp = (proj.root_path or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd_norm == rp or cwd_norm.startswith(rp + "/")) and len(rp) > best_len:
                best_id = proj.id
                best_len = len(rp)
        return best_id

    async def _leader_usage_outcome(
        self,
        payload: dict,
        *,
        force: bool,
        leader: object | None = None,
    ) -> tuple[dict | None, str | None]:
        """Snapshot this session's main-transcript token usage onto its leader row.

        The sole write entry point of token attribution v1 stage 4 (design §3.3). Four mount points share it:
        ``SessionStart`` / ``SessionEnd`` / ``PostCompact`` pass ``force=True`` to force a
        freeze-frame; ``Stop`` passes ``force=False`` and goes through throttling.

        Semantic essentials (details in the aiteam.api.leader_usage module docs):
        * **Overwrite**: what is written is the cumulative snapshot "from session start to now", not this round's
          increment;
        * **Caliber**: what lands here is ``usage_sum`` (accumulated across requestIds), which, compared with
          ``workflow_agents.tokens``' ``ctx_last``, **is not the same quantity and cannot be added to it**;
        * **Separate presentation**: the Leader's magnitude far exceeds the sum of all sub-agents, so the presentation
          layer must split by role
          (that belongs to stage 5; here it is only noted as a constraint);
        * Best-effort throughout: collection never blocks any main hook flow.

        Returns ``(summary, skip_reason)`` — both are always present and exactly one is non-empty. The reason strings
        were added during the 2026-08-03 incident investigation: before that, five outcomes (no session_id / no Leader
        row / transcript not locatable / throttled / exception raised) all collapsed into the same ``None``,
        and exceptions landed only in ``logger.debug``, so "it ran but produced no data" and "it blew up" were
        indistinguishable in the database, the logs, and the receipt alike —
        the whole chain was unfalsifiable. The grading of the criteria is deliberately as follows:
        * **Throttling** is the most common normal outcome in steady state — stay quiet: no shouting and no event
          recorded;
        * a **forced freeze-frame miss** (``force=True`` and not throttling) is definitely an anomaly — shout a WARNING,
          because that moment's watermark can never be recovered;
        * **an exception** additionally records one event — logs get truncated on process restart; events do not.
        """
        session_id = payload.get("session_id", "")
        if not session_id:
            return None, "no-session-id"
        try:
            if leader is None:
                leader = await self._find_leader(session_id)
            # _find_leader falls back to this session's other agents when there is no leader row, while a usage snapshot
            # is only valid for the main session — landing it on a sub-agent row would be a case of mistaken identity.
            if leader is None or getattr(leader, "role", "") != "leader":
                return None, "no-leader-row"
            transcript = leader_usage.locate_main_transcript(
                payload.get("transcript_path") or "",
                cwd=payload.get("cwd") or "",
                session_id=session_id,
            )
            if transcript is None:
                return None, "no-transcript"
            snapshot, reason = self._usage_meter.capture_or_reason(
                session_id, transcript, force=force
            )
            if snapshot is None:
                return None, reason
            await self.repo.update_agent(leader.id, **snapshot.as_agent_updates())
            return snapshot.as_summary(), None
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never block the main hook flow
            reason = f"error:{type(exc).__name__}"
            logger.warning(
                "leader usage capture failed (session=%s force=%s): %s",
                session_id[:8],
                force,
                exc,
                exc_info=True,
            )
            await self._emit_leader_usage_failure(session_id, force, f"{reason}: {exc}")
            return None, reason

    async def _emit_leader_usage_failure(self, session_id: str, forced: bool, reason: str) -> None:
        """Leave a durable, queryable trace of a capture that threw.

        Only recorded on **genuine anomalies** — this batch just cut the events table's writes by forty percent, and
        routine throttling skips must never
        enter this table. The event itself is also best-effort: when even leaving a trace fails, it must not blow up the
        main flow in return;
        but that hand must also be a WARNING — if "the record of the failure failed" only lands at DEBUG, it would
        exactly
        replicate the disease this fix addresses (when ``EventType`` lacks a member, ``create_event`` raises ValueError;
        this fix stepped right into that trap at the start, as measured in practice).
        """
        try:
            await self.event_bus.emit(
                EventType.LEADER_USAGE_CAPTURE_FAILED.value,
                f"session:{session_id}",
                {"session_id": session_id, "forced": forced, "reason": reason},
            )
        except Exception:  # noqa: BLE001
            logger.warning("leader usage failure event not recorded", exc_info=True)

    async def _capture_leader_usage(
        self,
        payload: dict,
        *,
        force: bool,
        leader: object | None = None,
    ) -> tuple[dict | None, str | None]:
        """Collection + "shout when a forced freeze-frame misses" — the single entry point all four mount points should
        call.

        Split into two layers instead of stuffed into one function: the inner :meth:`_leader_usage_outcome` only judges
        the
        outcome, while this layer only speaks up graded by outcome. All mount points go through here, so "a forced
        freeze-frame failing silently"
        has no second path to take in the code.
        """
        summary, reason = await self._leader_usage_outcome(payload, force=force, leader=leader)
        if (
            force
            and reason is not None
            and reason != leader_usage.SKIP_THROTTLED
            and not reason.startswith("error:")  # the exception branch has already issued its WARNING
        ):
            logger.warning(
                "leader usage forced capture missed (session=%s): %s",
                str(payload.get("session_id", ""))[:8],
                reason,
            )
        return summary, reason

    async def _on_session_start(self, payload: dict) -> dict:
        """Record CC session start.

        Leader = the CC session opened by the user. Each session corresponds to one Leader.
        Flow:
        1. Find project by cwd
        2. Look for existing Leader in project (role=leader + project_id match)
        3. Found -> reuse, update session_id + status=busy
        4. Not found -> create new Leader
        No longer creates session-xxx ghost agents each time.
        """
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")
        leader = None

        # 1. Find project by cwd — longest root_path match (see helper).
        project = None
        matched_pid = await self._resolve_project_id_by_cwd(cwd)
        if matched_pid:
            project = await self.repo.get_project(matched_pid)

        # 2. Check if this session already has a Leader
        existing = await self.repo.find_agents_by_session(session_id)
        leaders_in_session = [a for a in existing if a.role == "leader"]

        if leaders_in_session:
            # Reuse THIS session's leader (session_id match — never another session's).
            leader = leaders_in_session[0]
            update_kwargs: dict = {
                "status": "busy",
                "last_active_at": utc_now(),
            }
            # Heal project binding — project liveness (summary "working") keys off
            # leader.project_id. This is now safe from the 03fe7cae rebind churn:
            # a leader row is bound to exactly one session (one cwd), so the resolved
            # project is stable across the session's life (heals only an unbound row,
            # never ping-pongs between projects like the old cross-session reuse did).
            if project and leader.project_id != project.id:
                update_kwargs["project_id"] = project.id
            await self.repo.update_agent(leader.id, **update_kwargs)
            # Session-resumption team repair (2026-07-22, 8705dac2 lesion 2): after SessionEnd closes the container
            # team,
            # when a session with the same id resumes (restart / compact continuation), this code only reused the Leader
            # — the container team stayed
            # completed, and subsequent direct dispatches hit completed in find_active_team_by_leader and all fell out
            # of management.
            # Ensure the container team is in place and active (the helper includes ownership verification and
            # auto-revive).
            await self._find_or_create_session_team(session_id, payload)
        elif project:
            # No leader for THIS session yet -> always create a fresh per-session
            # leader (fleet-layer design §3: one leader row per session, born bound,
            # never reused across sessions). The old path here reused another
            # session's leader via find_leader_by_project and rebound its session_id
            # -> the exact churn 03fe7cae is about. Removed.
            team = await self._find_or_create_session_team(session_id, payload)
            if team:
                leader = await self.repo.create_agent(
                    team_id=team.id,
                    name="Leader",
                    role="leader",
                    backstory="Project Leader",
                    source="hook",
                    session_id=session_id,
                    # The main-session model is not carried by hook events — leave it empty, to be backfilled by the
                    # transcript tail-read below,
                    # rather than persisting the repository default (which once constantly displayed claude-opus-4-7 and
                    # misled the display).
                    model="",
                )
                # Bind project_id at birth (fleet-layer §3 "bind at birth"). create_agent
                # drops project_id from kwargs, so it must be set via update_agent —
                # the historical omission is exactly why session leaders were observed
                # unbound (the "7 orphan rows"). One cwd per session makes this stable.
                await self.repo.update_agent(
                    leader.id,
                    status="busy",
                    project_id=project.id,
                    last_active_at=utc_now(),
                )
                # Link the container team to its leader so find_active_team_by_leader
                # resolves (used by _on_subagent_start's no-cc_team_name fallback and
                # by SessionEnd ownership inference).
                await self.repo.update_team(team.id, leader_agent_id=leader.id)
                logger.info("SessionStart: created project Leader -> team %s", team.name)
        else:
            # No project match -> do NOT auto-create. Log for user prompt.
            logger.info(
                "SessionStart: no project match for cwd=%s. "
                "User can register via project_create MCP tool or Dashboard.",
                cwd,
            )

        # Main-session real-model backfill: hook events do not carry the main-session model, but SessionStart carries
        # transcript_path; tail-read the last assistant message's message.model and write it into
        # Leader.model (the same idea as workflow_agents' terminal-state model backfill).
        try:
            _model = self._read_session_model(payload.get("transcript_path") or "")
            if _model and leader is not None and getattr(leader, "model", "") != _model:
                await self.repo.update_agent(leader.id, model=_model)
        except Exception:  # noqa: BLE001 — an undecided leader branch / a file-read failure are both silent
            pass

        # Main-session usage first measurement (one of the stage 4 mount points): once at each session start/resumption,
        # to establish the baseline for this session's
        # Leader row. A resumed session's transcript already has stock, so this one measurement fills in the history;
        # a brand-new session's file has no assistant rows yet, so collection returns None and writes no zeros.
        usage, usage_skip = await self._capture_leader_usage(payload, force=True, leader=leader)

        # I3a: durable fallback — at session start, reconciliation scans the whole session's workflows/ and fills in
        # runs missing/unfinished in the DB.
        # Fully try/except-wrapped so it never blocks session start (runs that happened while the OS was offline can be
        # fully recovered once it comes back online).
        try:
            await workflow_ingest.reconcile(
                self.repo, self.event_bus, project_dir=cwd or None
            )
        except Exception:  # noqa: BLE001
            logger.warning("SessionStart workflow reconcile failed", exc_info=True)

        await self.event_bus.emit(
            "cc.session_start",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "cwd": cwd,
                "leader": leader.name if leader else None,
            },
        )
        return {
            "status": "recorded",
            "leader": leader.name if leader else None,
            "leader_usage": usage,
            # When nothing was captured, it can say why — the None in the receipt used to be the only clue, and it
            # proved nothing.
            "leader_usage_skip": usage_skip,
        }

    @staticmethod
    def _team_owned_by_session(team: object, session_id: str) -> bool:
        """Whether a team is owned by the given CC session (fleet-layer design §5).

        Ownership signals, in order:
        1. team.config.owner_session_id == session_id (stamped at creation — the
           authoritative, immutable key for all teams created after this change).
        2. Legacy fallback: the session-container team name encodes the session id8
           (``session-<sid8>``), covering rows created before owner stamping.

        Deliberately conservative: an unknown-ownership team returns False so
        SessionEnd never closes a team it cannot prove belongs to the ending
        session. Slightly delayed cleanup of legacy teams (reaper handles them) is
        the correct trade against cross-session clobbering (7ae3b7cd).
        """
        if not session_id:
            return False
        owner = (getattr(team, "config", None) or {}).get("owner_session_id")
        if owner:
            return owner == session_id
        return getattr(team, "name", "") == f"session-{session_id[:8]}"

    async def _on_session_end(self, payload: dict) -> dict:
        """Handle CC session end — reconcile and clean up state.

        Sets this session's agents offline but **keeps their session_id**:
        session_id is identity, status is state, and only the latter ends with
        the session. The old code cleared it here, which detached every row from
        the session that owned it and broke ``_on_session_start``'s reuse lookup
        (``find_agents_by_session``) — so each resume of the same session minted
        another leader. Production carried 120 leader rows, all session_id NULL,
        11 of them from a single day and three of those inside one team.
        """
        session_id = payload.get("session_id", "")
        agents = await self.repo.find_agents_by_session(session_id)
        # Main-session usage final measurement (one of the stage 4 mount points, force): the session ends here, and this
        # entry is that
        # Leader row's final value. **Measure before setting the agent offline** — the order does not affect correctness
        # (overwriting is idempotent), but measuring first guarantees the freeze-frame even if the cleanup below throws.
        end_leader = next((a for a in agents if a.role == "leader"), None)
        end_usage, end_usage_skip = (
            await self._capture_leader_usage(payload, force=True, leader=end_leader)
            if end_leader is not None
            else (None, "no-leader-row")
        )
        for agent in agents:
            # Workflow sub-agent exemption (symmetric with the kind=workflow team exemption below): a CC Workflow run
            # can far outlive the launching session; its fan-out members register with a session_id, but their lifecycle
            # belongs to ingest, which collects them by
            # run terminal state (WP9 member wrap-up). Setting them offline unconditionally would falsely injure members
            # of runs still in progress, causing
            # offline flickering (audit WP6). Members' offline state is governed solely by the run-terminal-state ingest
            # path.
            if getattr(agent, "role", "") == WORKFLOW_AGENT_TYPE:
                continue
            await self.repo.update_agent(agent.id, status="offline", current_task=None)

        # Reconciliation stats
        hook_count = await self.repo.count_agents_by_source(
            source="hook",
            session_id=session_id,
        )
        api_count = await self.repo.count_agents_by_source(
            source="api",
            session_id=session_id,
        )

        # Close ONLY the teams owned by THIS session (fleet-layer design §5, fixes
        # 7ae3b7cd). The old code closed every active non-workflow team in the whole
        # DB regardless of ownership, so a bystander session's SessionEnd clobbered
        # teams other live sessions were still using (record: c4fab878's SessionEnd closed
        # abff40af's team). Ownership key = team.config.owner_session_id (stamped at
        # creation); legacy teams without it fall back to the session-container name.
        # Teams owned by other sessions (or unknown ownership) are left for the reaper
        # to reap by their own liveness — never cross-session clobbered here.
        # Workflow teams remain fully exempt (their lifecycle is maintained by ingest according to run state).
        closed_teams = []
        all_teams = await self.repo.list_teams()
        for team in all_teams:
            if (team.config or {}).get("kind") == "workflow":
                continue
            if team.status != "active":
                continue
            if not self._team_owned_by_session(team, session_id):
                continue
            await self.repo.update_team(team.id, status="completed")
            closed_teams.append(team.name)
            logger.info("SessionEnd: closed owned team '%s'", team.name)
            # Offline any stragglers left in this owned team (the first loop already
            # handled agents still carrying this session_id; this catches members
            # whose session_id was cleared/never set but who live in the closed team).
            for agent in await self.repo.list_agents(team.id):
                if agent.status != "offline":
                    await self.repo.update_agent(
                        agent.id, status="offline", current_task=None
                    )

        await self.event_bus.emit(
            "cc.session_end",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "agents_hook": hook_count,
                "agents_api": api_count,
                "sync_warning": hook_count > api_count,
                "closed_teams": closed_teams,
            },
        )
        return {
            "status": "reconciled",
            "hook_agents": hook_count,
            "api_agents": api_count,
            "closed_teams": closed_teams,
            "leader_usage": end_usage,
            "leader_usage_skip": end_usage_skip,
        }

    async def _on_stop(self, payload: dict) -> dict:
        """Handle CC Stop event — a turn ended in *this* session.

        Touches ``last_active_at`` on the stopping session's own busy agents and
        refreshes the leader's model. Status transitions are not this hook's job:
        StateReaper owns liveness, SessionEnd owns session teardown.

        There used to be a "mode 2 global fallback" here — when the stopping
        session owned no busy agents it walked *every team in the database* and
        offlined anything busy and recently active, on the theory that a Stop
        with no session match must mean a session ended. It is the same
        cross-session clobber 7ae3b7cd fixed for SessionEnd, and it fired in
        production on 2026-07-27: a live sub-agent of session 80d0cc5e was
        flipped offline (``trigger=stop_global``) mid tool-call because an
        unrelated session in another project ended a turn. "No busy agent in
        this session" is the *normal* state of a plain Leader turn end, so the
        premise never held. Removed rather than narrowed: with session_id no
        longer wiped at SessionEnd, the session-scoped path above is reliable,
        and stragglers are already reaped by liveness.
        """
        session_id = payload.get("session_id", "")
        updated: list[str] = []

        # Mode 1: find by session_id -> only update last_active_at, don't change status
        # State changes are handled by StateReaper's config_liveness detection
        recent_cutoff = utc_now() - timedelta(seconds=30)
        agents = await self.repo.find_agents_by_session(session_id)
        for agent in agents:
            if agent.status == "busy" and agent.source == "hook":
                if agent.created_at and agent.created_at > recent_cutoff:
                    continue  # Recently created agent, skip to prevent old Stop from overriding
                await self.repo.update_agent(
                    agent.id,
                    last_active_at=utc_now(),
                )
                updated.append(agent.id)

        # Real-time Leader model tracking: the user can switch with /model at any time; after a switch, the next
        # assistant message
        # is the new model — refreshing once per Stop round by tail-reading the transcript gives real-time semantics (a
        # one-time SessionStart
        # backfill is not enough; user requirement 2026-07-07). Only write to the DB on change; zero write amplification
        # in steady state.
        try:
            _model = self._read_session_model(payload.get("transcript_path") or "")
            if _model:
                for agent in agents:
                    if agent.role == "leader" and getattr(agent, "model", "") != _model:
                        await self.repo.update_agent(agent.id, model=_model)
        except Exception:  # noqa: BLE001 — a file-read failure is silent and does not affect the main Stop flow
            pass

        # Main-session usage (one of the stage 4 mount points) — **this one must be throttled**. Stop fires on every
        # round,
        # while a full parse grows linearly more expensive with the session (measured 45.1 MB / 0.18 s, and only larger
        # from here). Within the throttle window it
        # returns None directly, without even reading the file a second time (the criterion uses only one stat).
        leader_row = next((a for a in agents if a.role == "leader"), None)
        usage, usage_skip = (
            await self._capture_leader_usage(payload, force=False, leader=leader_row)
            if leader_row is not None
            # This session has no Leader row (a session of an unregistered project) — no need to look it up again
            else (None, "no-leader-row")
        )

        logger.info("Stop event: %d heartbeat updates (session %s)", len(updated), session_id[:8])
        return {
            "status": "ok",
            "heartbeat_updates": updated,
            "agents_offline": [],
            "leader_usage": usage,
            "leader_usage_skip": usage_skip,
        }

    async def _on_teammate_idle(self, payload: dict) -> dict:
        """CC says a teammate went idle — recorded, deliberately not acted on.

        TeammateIdle is CC's own first-party answer to "is this teammate still
        working", i.e. exactly the signal the OS has been approximating with
        transcript mtime and tool-event touches. It is wired up now so the two
        readings accumulate side by side; the switch to using it as the liveness
        authority is a separate, evidence-gated decision (C13, batch 9).

        Deliberately does **not** write ``status``. CC's idle means "finished a
        turn, awaiting input" — the OS's ``offline`` means "gone". Conflating
        them is precisely the "team dead while the agent is alive" misread this
        batch is unwinding, so the event records both views and changes nothing.
        Payload (CC v2.1.219): ``{teammate_name, team_name(@deprecated)}`` plus
        the common base — no idle reason, no idle duration.
        """
        session_id = payload.get("session_id", "")
        name = payload.get("teammate_name", "")
        # Resolved by *exact name inside this session*, never through
        # _resolve_agent: that helper's last resort is "no cc agent id means the
        # main session, so return the Leader", which is right for tool calls and
        # wrong here — live check attributed worker-b9's idle signal to the
        # Leader row. Misattributed signals would poison the very comparison this
        # event exists to collect, so an unmatched name stays unmatched.
        agent = None
        if name and session_id:
            agent = await self.repo.find_agent_by_session(session_id, name)
        await self.event_bus.emit(
            "cc.teammate_idle",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "teammate_name": name,
                "cc_team_name": payload.get("team_name", ""),
                "agent_id": getattr(agent, "id", ""),
                "os_status": getattr(getattr(agent, "status", None), "value", ""),
                "observed_only": True,
            },
        )
        return {"status": "observed", "agent_id": getattr(agent, "id", "")}

    async def _on_cc_task_event(self, payload: dict) -> dict:
        """TaskCreated / TaskCompleted — the **observation** surface for CC-native tasks.

        Deliberately only records events and never touches the task wall: putting tasks on the wall still belongs to
        cc_task_bridge, which at TaskCompleted filters by
        "has an owner or a dependency chain" before bookkeeping (the Q1 completion-time bookkeeping ruling stands). What
        is added here is telemetry —
        the bridge previously hung on an event that did not also have send_event attached, so how many times it fired
        and how many entries it filtered went completely unrecorded,
        and even "is the bridge alive" could not be determined.
        """
        session_id = payload.get("session_id", "")
        created = payload.get("hook_event_name") == "TaskCreated"
        event_type = (
            EventType.CC_TASK_CREATED.value if created else EventType.CC_TASK_COMPLETED.value
        )
        await self.event_bus.emit(
            event_type,
            f"session:{session_id}",
            {
                "session_id": session_id,
                "cc_task_id": str(payload.get("task_id") or ""),
                "subject": str(payload.get("task_subject") or ""),
                "owner": str(payload.get("teammate_name") or ""),
                "cc_team_name": str(payload.get("team_name") or ""),
                "observed_only": True,
            },
        )
        return {"status": "observed", "cc_task_id": str(payload.get("task_id") or "")}

    async def _on_post_compact(self, payload: dict) -> dict:
        """Compaction really completed — closing out the checkpoint PreCompact stored.

        A PreCompact trigger does not equal compaction happening (the user can interrupt it), so the checkpoint needs a
        "really
        compacted" receipt; otherwise it is impossible to tell afterwards whether a checkpoint corresponds to a real
        compaction or a cancellation.

        **Does not record the compact_summary body**: that summary is already in the model's context after compaction,
        so storing another copy would both duplicate it and pour large amounts of conversation content into the events
        table — this batch just cut that table's
        writes by forty percent and should not add them back from the other end. Record only the length; that suffices
        to judge whether the summary was produced normally.
        """
        session_id = payload.get("session_id", "")
        # The hook side has already replaced the body with its length (send_event._trim_payload) — in large-summary
        # scenarios the body
        # never reaches here at all, so only recognizing the length keeps it from being recorded as 0. The old form
        # (body still present) continues as a fallback.
        precomputed = payload.get("compact_summary_chars")
        if isinstance(precomputed, int):
            summary_chars = precomputed
        else:
            summary_chars = len(payload.get("compact_summary", "") or "")
        # Main-session usage is force-frozen at the compaction point (one of the stage 4 mount points). Measured in
        # practice, compact **appends boundary rows in place**
        # without truncating or replacing the file, so this measurement is not rescuing data about to disappear;
        # it fixes "at which watermark compaction happened" and covers the handover point where, after compact/resume,
        # the session starts a new file
        # (the pre-compact rows replayed in the new file have all-zero usage, so parsing the two files separately does
        # not double-count).
        # PostCompact's payload is the most easily trimmed by the hook side; when the path is missing, a reverse lookup
        # by slug provides the fallback.
        usage, usage_skip = await self._capture_leader_usage(payload, force=True)
        await self.event_bus.emit(
            "session.compact_completed",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "trigger": payload.get("trigger", ""),
                "summary_chars": summary_chars,
            },
        )
        return {
            "status": "recorded",
            "summary_chars": summary_chars,
            "leader_usage": usage,
            "leader_usage_skip": usage_skip,
        }

    async def _find_or_create_session_team(
        self,
        session_id: str,
        payload: dict,
    ):
        """Find or create THIS session's own container team (fleet-layer design §3).

        Each CC session gets its own container team that anchors its leader row.
        Never reuse another session's team: the old fallbacks (cwd -> an existing
        project team, or "most recently created team") let a session's leader
        parasitize another session's / a workflow team — the 03fe7cae churn. Removed.

        1. If this session already owns a team (its leader/agents point at one), reuse it.
        2. Otherwise create a fresh ``session-<sid8>`` container, tagged kind="session"
           + owner_session_id so its lifecycle is governed by the owning session's file
           mtime (reaper) and closed session-scoped at SessionEnd, and so the reaper's
           CC-config-based liveness check exempts it (it never has a ~/.claude/teams dir).
        """
        # 1. This session's own team (contains its leader / agents)
        container_name = f"session-{session_id[:8]}" if session_id else "session-unknown"
        if session_id:
            agents = await self.repo.find_agents_by_session(session_id)
            if agents:
                leader_agents = [a for a in agents if a.role == "leader"]
                target = leader_agents[0] if leader_agents else agents[0]
                team = await self.repo.get_team(target.team_id)
                # Ownership verification (2026-07-22, 8705dac2 lesion 3): a team found by following leader/agents
                # must truly be this session's container — when the Leader has been adopted by a workflow team and not
                # returned, following
                # the drifted team_id here would parasitize someone else's / an already-closed team. If it is not this
                # session's container, fall to step 2
                # and find-or-create by name.
                if team is not None and (
                    (getattr(team, "config", None) or {}).get("owner_session_id")
                    == session_id
                    or team.name == container_name
                ):
                    return await self._revive_if_completed(team)

        # 2. Create this session's own container team (never reuse another session's).
        # race-safe: two concurrent hook events of the same session both creating the session-<sid8> container team
        # would hit the teams.name
        # UNIQUE constraint (confirmed 2026-07-21: session-e713a6cb → an uncaught ASGI 500 lost the event).
        # get_or_create_team swallows the conflict and re-fetches the existing row; the loser reuses the container team
        # the winner created.
        config: dict = {"kind": "session", "owner_session_id": session_id} if session_id else {}
        # Also record the CC process that owns this team. It must be recorded **at this team-creation moment**: CC keeps
        # one session registration per process,
        # and when the process switches sessions it rewrites sessionId in place, leaving no trace of the old value;
        # asking later finds nothing.
        # The criterion is exact session_id equality, with no approximate kinship matching by cwd / start-time window
        # mixed in (measured locally:
        # two processes can start 1 second apart under the same cwd, so approximate matching is ambiguous). If it cannot
        # be found,
        # do not stamp it — the display layer can recognize "unknown", while a wrong guess would merge two processes
        # into one.
        if session_id:
            from aiteam.api import session_registry

            try:
                pid = session_registry.pid_for_session(session_id)
            except Exception:  # noqa: BLE001 — a probe failure must never block team creation
                pid = None
            if pid:
                config["cc_pid"] = pid

        team, _created = await self.repo.get_or_create_team(
            name=container_name,
            mode="coordinate",
            config=config,
        )
        if _created:
            logger.info(
                "Auto-created session container team: %s (cwd=%s)",
                team.name,
                payload.get("cwd", ""),
            )
            return team
        # An existing container team may already have been closed by SessionEnd/the reaper (the lifecycle black hole of
        # same-id session resumption,
        # 8705dac2 lesion 2) — revive it, following the auto-revive precedent of _resolve_cc_team.
        return await self._revive_if_completed(team)

    async def _infer_project_id(
        self, session_id: str, *, allow_cwd_fallback: bool = True
    ) -> str | None:
        """Resolve the project a team should belong to: the session Leader takes priority, with cwd longest-prefix
        matching as the fallback.

        The Leader's project_id is locked when the session opens and is more reliable than cwd (multi-window cwds may
        overlap in prefixes). The cwd branch takes the longest matching root_path — taking the first match early on
        often mis-selected a broader parent project.
        Extracted from the team-creation branch on 2026-07-25 into a helper, for reuse by "re-binding a project for an
        existing team".
        """
        # 1) Authoritative: session_id -> Leader -> project_id
        if session_id:
            leader = await self._find_leader(session_id)
            if leader and leader.project_id:
                logger.info(
                    "CC team mapping: session %s -> leader '%s' -> project %s",
                    session_id[:8], leader.name, leader.project_id,
                )
                return leader.project_id

        # 2) Fallback: cwd longest-prefix match (only when no Leader yet)
        if not allow_cwd_fallback:
            # Re-binding path: rather not bind at all than guess the wrong project (the cost of
            # cross-project pollution is far greater than not binding)
            return None
        cwd = ""
        if getattr(self, "_current_event_cwd", ""):
            cwd = self._current_event_cwd.replace("\\", "/").rstrip("/").lower()
        if not cwd:
            import os as _os
            cwd = _os.getcwd().replace("\\", "/").rstrip("/").lower()
        if not cwd:
            return None
        projects = await self.repo.list_projects()
        best_match = None
        best_len = -1
        for p in projects:
            rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
                best_match = p
                best_len = len(rp)
        if best_match is None:
            return None
        logger.info(
            "CC team mapping: cwd '%s' -> project %s (root_path=%s)",
            cwd, best_match.name, best_match.root_path,
        )
        return best_match.id

    async def _resolve_agent_project_id(self, team, session_id: str) -> str | None:
        """Resolve the project a newly registered agent should belong to — only the authoritative chain counts; leave it
        empty when unavailable.

        Tiers (the earlier, the more authoritative):
        1. This session Leader's project_id (locked at session start by that session's cwd; the ownership iron rule
           "the session's startup directory is authoritative");
        2. The **same-team Leader row**'s project_id — agents.session_id has historically been NULL on a large scale
           (measured 2026-07-27: 120/120 leader rows all empty), so relying on tier 1 alone would constantly fail;
           this tier was added so the in-team Leader also counts as authoritative;
        3. team.project_id — the team's own binding, as the last fallback. It may be polluted by historical dirty data
           (the startup backfill's bulk cwd claiming, already fixed in deps._auto_create_projects),
           so it ranks after the Leader.
        4. None of them available → None. Never guess from the process cwd / cross-session-shared global state.
        """
        # 1) This session's Leader
        leader = await self._find_leader(session_id) if session_id else None
        if leader is not None and getattr(leader, "project_id", None):
            return str(leader.project_id)

        # 2) Same-team Leader row
        if team is not None:
            try:
                members = await self.repo.list_agents(team.id)
            except Exception:  # noqa: BLE001 — ownership resolution must never affect the main registration flow
                members = []
            for a in members:
                if getattr(a, "role", "") == "leader" and getattr(a, "project_id", None):
                    return str(a.project_id)

        # 3) The team's own binding
        return getattr(team, "project_id", None) or None

    async def _revive_if_completed(self, team):
        """Revive a completed session container team to active (a new member / a session resumption is de facto using
        it)."""
        if team is not None and team.status == "completed":
            await self.repo.update_team(team.id, status="active")
            logger.warning(
                "Session container team '%s' was completed; auto-revived to active.",
                team.name,
            )
            await self.event_bus.emit(
                "team.auto_revived",
                f"team:{team.id}",
                {"team_id": team.id, "team_name": team.name,
                 "reason": "session container back in use"},
            )
            team.status = "active"
        return team
