"""Tests for workflow_reminder.py.

Tests workflow reminder logic: TeamCreate task reminder, Agent memo reminder,
shutdown completion reminder, taskwall staleness warning, and cooldowns.
"""

from __future__ import annotations

import os
import time
from unittest import mock

import aiteam.hooks.workflow_reminder as workflow_reminder
from aiteam.hooks.workflow_reminder import _check_workflow_reminders


def _use_temp_state(tmp_path: str):
    """Patch supervisor state file to use a temp directory."""
    state_file = os.path.join(tmp_path, "supervisor-state.json")
    return (
        mock.patch.object(workflow_reminder, "_SUPERVISOR_STATE_FILE", state_file),
        mock.patch.object(workflow_reminder, "_SUPERVISOR_STATE_DIR", tmp_path),
    )


class TestTeamCreateRemindsTask:
    """After TeamCreate, should remind to put tasks on the wall."""

    def test_teamcreate_reminds_task(self):
        state = {}
        event = {"tool_name": "TeamCreate", "hook_event_name": "PostToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert len(warnings) >= 1
        assert any("task wall" in w for w in warnings)
        assert any("task_run" in w or "task_create" in w for w in warnings)


class TestAgentRemindsMemo:
    """Before creating an Agent(team_name), should remind to view the memo."""

    def test_agent_reminds_memo(self):
        state = {"last_memo_reminder": 0}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Implement the feature", "team_name": "dev-team"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        # Rule 2 now generates multiple warnings: task wall check, template reminder, memo reminder
        assert any("task_memo_read" in w for w in warnings)
        assert state["last_memo_reminder"] > 0

    def test_agent_without_team_name_no_memo_reminder(self):
        state = {"last_memo_reminder": 0}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Explore the code", "subagent_type": "explore"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        # No team_name in input, so no memo reminder
        assert not any("task_memo_read" in w for w in warnings)


class TestShutdownRemindsComplete:
    """SendMessage(shutdown) should remind to mark the task complete."""

    def test_shutdown_reminds_complete(self):
        state = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev-agent", "message": "shutdown"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        # Rule 3 shutdown reminder + possible Rule 6 parallel task reminder
        assert any("task_memo_add" in w for w in warnings)
        assert any("complete" in w or "mark" in w for w in warnings)

    def test_normal_sendmessage_no_shutdown_warning(self):
        state = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev-agent", "message": "Please continue working"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any("Close Agent" in w for w in warnings)


class TestTaskwallViewResetsTimer:
    """taskwall_view should reset the timer."""

    def test_taskwall_view_resets_timer(self):
        state = {"last_taskwall_view": 0}
        event = {"tool_name": "taskwall_view", "hook_event_name": "PostToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert state["last_taskwall_view"] > 0
        # taskwall_view itself should not produce a staleness warning
        assert not any("since the task wall was last viewed" in w for w in warnings)


class TestStaleTaskwallWarning:
    """Not viewing the task wall for over 30 minutes should remind (reminder governance: interval 900→1800s)."""

    def test_stale_taskwall_warning(self):
        # Set last_taskwall_view to 35 minutes ago (beyond the 1800s threshold)
        stale_ago = time.time() - 2100
        state = {"last_taskwall_view": stale_ago}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert any("since the task wall was last viewed" in w for w in warnings)
        # After reminding, the timer should reset
        assert state["last_taskwall_view"] > stale_ago

    def test_no_stale_warning_within_30_minutes(self):
        # Set last_taskwall_view to 20 minutes ago (within the 1800s threshold, no more nagging)
        twenty_min_ago = time.time() - 1200
        state = {"last_taskwall_view": twenty_min_ago}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("since the task wall was last viewed" in w for w in warnings)

    def test_no_stale_warning_when_never_viewed(self):
        # last_taskwall_view is 0 (never viewed); should not produce a staleness reminder
        state = {"last_taskwall_view": 0}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("since the task wall was last viewed" in w for w in warnings)


class TestTaskwallCatchupGovernance:
    """Reminder governance ①: reset events expand to all task_*/taskwall_* + a per-session reminder cap of 2, then
    silence."""

    def test_task_create_resets_timer(self):
        # task_create is a task-wall operation and should reset the timer (the old implementation only recognized the
        # two view tools)
        stale_ago = time.time() - 2100
        state = {"last_taskwall_view": stale_ago}
        event = {
            "tool_name": "mcp__ai-team-os__task_create",
            "hook_event_name": "PostToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any("since the task wall was last viewed" in w for w in warnings)
        assert state["last_taskwall_view"] > stale_ago

    def test_task_update_and_memo_add_reset_timer(self):
        for tool in (
            "mcp__ai-team-os__task_update",
            "mcp__ai-team-os__task_memo_add",
            "mcp__ai-team-os__task_status",
        ):
            stale_ago = time.time() - 2100
            state = {"last_taskwall_view": stale_ago}
            event = {"tool_name": tool, "hook_event_name": "PostToolUse"}
            warnings = _check_workflow_reminders(event, state)
            assert not any("since the task wall was last viewed" in w for w in warnings), tool
            assert state["last_taskwall_view"] > stale_ago, tool

    def test_catchup_silenced_after_two_times_same_session(self):
        # Silent after 2 reminders in the same session; a new session counts afresh
        sid = "sess-catchup-1"
        emitted = 0
        state: dict = {}
        for _ in range(4):
            state["last_taskwall_view"] = time.time() - 2100  # Manufacture a timeout each round
            event = {
                "tool_name": "Bash",
                "hook_event_name": "PreToolUse",
                "session_id": sid,
            }
            warnings = _check_workflow_reminders(event, state)
            if any("since the task wall was last viewed" in w for w in warnings):
                emitted += 1
        assert emitted == 2, f"Should remind at most 2 times, actually {emitted}"

        # Another session counts independently and can still be reminded
        state["last_taskwall_view"] = time.time() - 2100
        event2 = {
            "tool_name": "Bash",
            "hook_event_name": "PreToolUse",
            "session_id": "sess-catchup-2",
        }
        warnings2 = _check_workflow_reminders(event2, state)
        assert any("since the task wall was last viewed" in w for w in warnings2)


class TestMemoReminderCooldown:
    """Within the 5-minute cooldown, should not repeat the memo-viewing reminder."""

    def test_memo_reminder_cooldown(self):
        # First trigger
        state = {"last_memo_reminder": 0}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Implement the feature", "team_name": "dev-team"},
            "hook_event_name": "PreToolUse",
        }
        warnings1 = _check_workflow_reminders(event, state)
        assert any("task_memo_read" in w for w in warnings1)

        # Trigger again immediately (within the cooldown)
        warnings2 = _check_workflow_reminders(event, state)
        assert not any("task_memo_read" in w for w in warnings2)

    def test_memo_reminder_after_cooldown(self):
        # Set last_memo_reminder to 6 minutes ago (beyond the 5-minute cooldown)
        six_min_ago = time.time() - 360
        state = {"last_memo_reminder": six_min_ago}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Implement the feature", "team_name": "dev-team"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        assert any("task_memo_read" in w for w in warnings)


class TestNoWarningNormalFlow:
    """The normal flow should produce no extra reminders."""

    def test_no_warning_normal_flow(self):
        state = {}
        # Ordinary tool calls should not produce workflow reminders
        for tool in ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]:
            event = {"tool_name": tool, "hook_event_name": "PreToolUse"}
            warnings = _check_workflow_reminders(event, state)
            assert warnings == [], f"Unexpected warning for {tool}: {warnings}"


class TestTaskwallToolHelper:
    """_is_taskwall_tool: all task_*/taskwall_* (prefixed or bare names) count as task-wall operations."""

    def test_matches_task_and_taskwall(self):
        from aiteam.hooks.workflow_reminder import _is_taskwall_tool

        for name in (
            "mcp__ai-team-os__task_create",
            "mcp__ai-team-os__task_update",
            "mcp__ai-team-os__task_memo_add",
            "mcp__ai-team-os__task_status",
            "mcp__ai-team-os__taskwall_view",
            "task_list_project",
            "taskwall_view",
        ):
            assert _is_taskwall_tool(name), name

    def test_rejects_non_taskwall(self):
        from aiteam.hooks.workflow_reminder import _is_taskwall_tool

        for name in ("Bash", "SendMessage", "mcp__ai-team-os__team_create", "Read"):
            assert not _is_taskwall_tool(name), name


class TestSessionBucketHelper:
    """_session_bucket: session isolation + 24h TTL pruning to prevent state bloat."""

    def test_per_session_isolation(self):
        from aiteam.hooks.workflow_reminder import _session_bucket

        state: dict = {}
        b1 = _session_bucket(state, "sess-A")
        b1["flag"] = True
        b2 = _session_bucket(state, "sess-B")
        assert b2.get("flag") is None  # B cannot see A's flag
        # Fetching A again should return the same bucket
        assert _session_bucket(state, "sess-A").get("flag") is True

    def test_stale_buckets_pruned(self):
        from aiteam.hooks.workflow_reminder import _session_bucket

        state: dict = {}
        _session_bucket(state, "old-sess")
        # Manually stamp old-sess as 25h ago
        state["session_scoped"]["old-sess"]["_ts"] = time.time() - 25 * 3600
        _session_bucket(state, "new-sess")  # Triggers pruning
        assert "old-sess" not in state["session_scoped"]
        assert "new-sess" in state["session_scoped"]


class TestReportFormatDirectionGating:
    """Reminder governance ⑤: the report-format reminder fires only for subagent sessions (member reports), excluding
    Leader sessions."""

    _MSG = {
        "to": "team-lead",
        "message": (
            "The task is done, I did a lot of work, changed several files, and ran the tests once,"
            "but this message deliberately does not write those three fields in the "
                "standard format, padded past one hundred characters to trigger the check"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    }

    def test_member_session_gets_format_reminder(self):
        event = {
            "tool_name": "SendMessage",
            "tool_input": self._MSG,
            "hook_event_name": "PreToolUse",
            "session_id": "member-sess-1",
        }
        with mock.patch.object(workflow_reminder, "_is_subagent_session", return_value=True):
            warnings = _check_workflow_reminders(event, {})
        assert any("The report may be missing standard fields" in w for w in warnings)

    def test_leader_session_excluded(self):
        event = {
            "tool_name": "SendMessage",
            "tool_input": self._MSG,
            "hook_event_name": "PreToolUse",
            "session_id": "leader-sess-1",
        }
        with mock.patch.object(workflow_reminder, "_is_subagent_session", return_value=False):
            warnings = _check_workflow_reminders(event, {})
        assert not any("The report may be missing standard fields" in w for w in warnings)

    def test_member_session_throttled_once(self):
        event = {
            "tool_name": "SendMessage",
            "tool_input": self._MSG,
            "hook_event_name": "PreToolUse",
            "session_id": "member-sess-2",
        }
        state: dict = {}
        with mock.patch.object(workflow_reminder, "_is_subagent_session", return_value=True):
            first = _check_workflow_reminders(event, state)
            second = _check_workflow_reminders(event, state)
        assert any("The report may be missing standard fields" in w for w in first)
        assert not any("The report may be missing standard fields" in w for w in second)


class TestCommitBranchOwnership:
    """A1'(S5) commit-time branch assertion: who claimed which branch on which checkout.

    Ruling semantics (debate 503e07f1 topic A): all state lives in the hook's own supervisor state, no API calls;
    the first commit records (checkout, agent)→branch+ts; switching one's own branch warns loudly without blocking;
    landing on another's record within 24h
    hard-blocks with exit(2); another's expired record degrades to a warning (so a dead agent's record does not harm a
    legitimate taker-over);
    a git probe failure is fail-loud (neither passes nor hard-blocks; it explicitly declares the check was not
    performed).
    """

    _CHECKOUT = "/repo/main"

    def _event(self, session_id: str, cmd: str = "git commit -m 'x'") -> dict:
        return {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "cwd": self._CHECKOUT,
        }

    def _git(self, branch: str, checkout: str | None = None):
        """Patch the read-only git probe to report a fixed checkout + branch."""
        out = f"{checkout or self._CHECKOUT}\n{branch}"
        return mock.patch.object(
            workflow_reminder, "_run_git_readonly", return_value=(0, out)
        )

    @staticmethod
    def _own(warnings: list[str]) -> list[str]:
        return [w for w in warnings if "branch ownership" in w]

    def _records(self, state: dict) -> dict:
        return state.get("branch_ownership", {}).get(self._CHECKOUT, {})

    def test_first_commit_records_silently(self):
        state: dict = {}
        with self._git("feature-a"):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        assert self._own(warnings) == []
        rec = self._records(state)["agent-1"]
        assert rec["branch"] == "feature-a"
        assert rec["ts"] > 0

    def test_same_branch_stays_silent(self):
        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            self._records(state)["agent-1"]["ts"] = time.time() - 600
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        assert self._own(warnings) == []
        # Still working on one's own branch → the claim renews instead of expiring naturally
        assert time.time() - self._records(state)["agent-1"]["ts"] < 5

    def test_self_switch_warns_loudly_without_blocking(self):
        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
        with self._git("feature-b"):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        own = self._own(warnings)
        assert len(own) == 1
        # Shows the recorded branch and the current HEAD on the same screen
        assert "feature-a" in own[0] and "feature-b" in own[0]

    def test_other_agent_active_claim_hard_blocks(self):
        import pytest

        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            with pytest.raises(SystemExit) as exc:
                _check_workflow_reminders(self._event("agent-2"), state)
        assert exc.value.code == 2

    def test_other_agent_expired_claim_downgrades_to_warning(self):
        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            # Stamp the record as 25h ago: beyond the active TTL, short of the pruning TTL
            self._records(state)["agent-1"]["ts"] = time.time() - 25 * 3600
            warnings = _check_workflow_reminders(self._event("agent-2"), state)
        own = self._own(warnings)
        assert len(own) == 1
        assert "expired" in own[0]
        # The legitimate taker-over gets their own claim
        assert self._records(state)["agent-2"]["branch"] == "feature-a"

    def test_git_probe_failure_fails_loud(self):
        state: dict = {}
        with mock.patch.object(workflow_reminder, "_run_git_readonly", return_value=(1, "")):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        own = self._own(warnings)
        assert len(own) == 1
        assert "could not be executed" in own[0]
        # When it cannot be probed, do not record it, lest a wrong ownership be cemented
        assert state.get("branch_ownership", {}) == {}

    def test_detached_head_fails_loud(self):
        state: dict = {}
        with self._git("HEAD"):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        assert len(self._own(warnings)) == 1
        assert state.get("branch_ownership", {}) == {}

    def test_non_commit_git_command_ignored(self):
        state: dict = {}
        with self._git("feature-a"):
            for cmd in ("git status", "git log --oneline -3", "git diff HEAD"):
                warnings = _check_workflow_reminders(self._event("agent-1", cmd), state)
                assert self._own(warnings) == []
        assert state.get("branch_ownership", {}) == {}

    def test_post_tool_use_does_not_check(self):
        """Asserting after the commit is meaningless (the deed is done); it runs only at PreToolUse."""
        state: dict = {}
        event = self._event("agent-1")
        event["hook_event_name"] = "PostToolUse"
        with self._git("feature-a"):
            warnings = _check_workflow_reminders(event, state)
        assert self._own(warnings) == []
        assert state.get("branch_ownership", {}) == {}

    def test_stale_records_pruned(self):
        from aiteam.hooks.workflow_reminder import _BRANCH_OWNERSHIP_PRUNE_TTL

        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            self._records(state)["agent-1"]["ts"] = (
                time.time() - _BRANCH_OWNERSHIP_PRUNE_TTL - 60
            )
            _check_workflow_reminders(self._event("agent-9"), state)
        # Records expired to the pruning line are removed wholesale; the state file does not bloat indefinitely with the
        # agent count
        assert "agent-1" not in self._records(state)
        assert "agent-9" in self._records(state)

    def test_separate_checkouts_do_not_collide(self):
        state: dict = {}
        with self._git("feature-a", checkout="/repo/main"):
            _check_workflow_reminders(self._event("agent-1"), state)
        # A same-named branch in another worktree should not be treated as the same claim (git does not allow it anyway,
        # but the state keys must separate)
        with self._git("feature-a", checkout="/repo/wt-1"):
            warnings = _check_workflow_reminders(self._event("agent-2"), state)
        assert self._own(warnings) == []
        assert state["branch_ownership"]["/repo/wt-1"]["agent-2"]["branch"] == "feature-a"
