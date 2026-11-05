"""Tests for supervisor hook checks in workflow_reminder.py.

Tests _check_leader_doing_too_much and _check_team_has_permanent_members.
"""

from __future__ import annotations

from aiteam.hooks.workflow_reminder import (
    _LEADER_CONSECUTIVE_THRESHOLD,
    _check_leader_doing_too_much,
)


class TestLeaderDoingTooMuch:
    """Tests for _check_leader_doing_too_much."""

    def test_leader_consecutive_calls_warning(self):
        """Should produce a warning after more than the threshold of consecutive non-delegation calls."""
        state: dict = {}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}

        # The first threshold calls should not warn
        for i in range(1, _LEADER_CONSECUTIVE_THRESHOLD + 1):
            result = _check_leader_doing_too_much(event, state)
            assert result is None, f"Unexpected warning at call {i}"

        # The threshold+1-th call should warn
        result = _check_leader_doing_too_much(event, state)
        assert result is not None
        assert "B0.9" in result
        assert str(_LEADER_CONSECUTIVE_THRESHOLD + 1) in result

    def test_agent_call_resets_counter(self):
        """An Agent call should reset the counter."""
        state: dict = {}
        bash_event = {"tool_name": "Edit", "hook_event_name": "PreToolUse"}
        agent_event = {"tool_name": "Agent", "hook_event_name": "PreToolUse"}

        # Accumulate some calls
        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        # The Agent call resets the counter
        result = _check_leader_doing_too_much(agent_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

        # Re-accumulating should not warn immediately
        result = _check_leader_doing_too_much(bash_event, state)
        assert result is None

    def test_workflow_call_resets_counter(self):
        """A Workflow (ultracode delegation) call should also reset the counter — delegating via a CC workflow should
        not be nagged with 'why not delegate'."""
        state: dict = {}
        bash_event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        workflow_event = {"tool_name": "Workflow", "hook_event_name": "PreToolUse"}

        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        result = _check_leader_doing_too_much(workflow_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

    def test_workflow_resets_counter(self):
        """Workflow orchestration is also a delegation action and should reset the counter.

        (The original test_team_create_resets_counter: the TeamCreate tool no longer exists as of CC v2.1.219
        — removed from _DELEGATION_TOOLS after the 2026-07-25 tool-surface check; the test now tests Workflow, still in
        the roster;
        the Agent direct-dispatch reset is covered by test_agent_dispatch_resets_counter.)
        """
        state: dict = {}
        bash_event = {"tool_name": "Read", "hook_event_name": "PreToolUse"}
        wf_event = {"tool_name": "Workflow", "hook_event_name": "PreToolUse"}

        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        result = _check_leader_doing_too_much(wf_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

    def test_send_message_resets_counter(self):
        """A SendMessage call should also reset the counter."""
        state: dict = {}
        bash_event = {"tool_name": "Write", "hook_event_name": "PreToolUse"}
        msg_event = {"tool_name": "SendMessage", "hook_event_name": "PreToolUse"}

        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        result = _check_leader_doing_too_much(msg_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

    def test_empty_tool_name_no_warning(self):
        """An empty tool_name should not be processed."""
        state: dict = {}
        event = {"tool_name": "", "hook_event_name": "PreToolUse"}
        result = _check_leader_doing_too_much(event, state)
        assert result is None

    def test_counter_persists_in_state(self):
        """The counter should persist in the state dict."""
        state: dict = {}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}

        _check_leader_doing_too_much(event, state)
        _check_leader_doing_too_much(event, state)
        _check_leader_doing_too_much(event, state)

        assert state["leader_consecutive_calls"] == 3


class TestNormalFlowNoWarning:
    """Tests that normal (well-behaved) flows produce no warnings."""

    def test_normal_flow_no_warning(self):
        """The normal flow (mixed delegation and direct operations) should produce no warning."""
        state: dict = {}
        # The Leader does a few steps
        for tool in ["Read", "Bash", "Read"]:
            event = {"tool_name": tool, "hook_event_name": "PreToolUse"}
            r1 = _check_leader_doing_too_much(event, state)
            assert r1 is None

        # Then delegates
        event = {"tool_name": "Agent", "hook_event_name": "PreToolUse"}
        r1 = _check_leader_doing_too_much(event, state)
        assert r1 is None

        # Does a few more steps
        for tool in ["Edit", "Bash"]:
            event = {"tool_name": tool, "hook_event_name": "PreToolUse"}
            r1 = _check_leader_doing_too_much(event, state)
            assert r1 is None
