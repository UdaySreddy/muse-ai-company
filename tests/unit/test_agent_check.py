"""Tests for _check_agent_team_name in workflow_reminder.py.

2026-07-22 interception-retirement edition (the Creator's ruling of 'full opening + always auto-track', task 8705dac2):
The unconditional hard block is deleted — implementation-style direct dispatches without a team_name are allowed
(SubagentStart auto-enrolls them).
Retained: the explore/plan+team_name misuse reminder; the explicit team_name cross-project interception (verified at the
mock layer).
"""

from __future__ import annotations

import sys
import unittest.mock

from aiteam.hooks.workflow_reminder import _check_agent_team_name


def test_agent_with_team_name_no_warning():
    """With a team_name and the cross-project check passing, no warning is produced."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "create the auth module",
            "team_name": "my-team",
        },
    }
    with unittest.mock.patch(
        "aiteam.hooks.workflow_reminder._check_team_cross_project", return_value=None
    ):
        assert _check_agent_team_name(event) is None


def test_impl_agent_without_team_name_allowed():
    """Implementation-style, with no team_name → allowed through (the old hard block has been retired; SubagentStart
    auto-enrolls)."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "implement the login feature",
        },
    }
    assert _check_agent_team_name(event) is None


def test_impl_agent_chinese_keyword_allowed():
    """Chinese implementation keywords without a team_name → likewise allowed through."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Implement the user login module",
        },
    }
    assert _check_agent_team_name(event) is None


def test_agent_with_name_only_allowed():
    """Only a name and no team_name → allowed through (auto-enrollment covers the tracking)."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "implement the login feature",
            "name": "backend-dev",
        },
    }
    assert _check_agent_team_name(event) is None


def test_explore_agent_no_warning():
    """Explore with no team_name is allowed through normally."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "explore the codebase and find auth related files",
            "subagent_type": "explore",
        },
    }
    assert _check_agent_team_name(event) is None


def test_explore_agent_with_team_name_warns():
    """Explore + team_name → a misuse reminder (built-in read-only types do not support SendMessage)."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "explore the auth flow",
            "subagent_type": "explore",
            "team_name": "my-team",
        },
    }
    warning = _check_agent_team_name(event)
    assert warning is not None and "read-only type" in warning


def test_cross_project_team_blocked():
    """An explicit team_name hitting a cross-project case → still exit(2) (the 2026-05-08 accident defense line is
    retained)."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "scan repos",
            "team_name": "other-project-team",
        },
    }
    with unittest.mock.patch(
        "aiteam.hooks.workflow_reminder._check_team_cross_project",
        return_value="[OS BLOCK] Cross-project dispatch intercepted",
    ):
        with unittest.mock.patch.object(sys, "exit") as mock_exit:
            with unittest.mock.patch.object(sys.stderr, "write"):
                _check_agent_team_name(event)
        mock_exit.assert_called_once_with(2)


def test_non_agent_tool_no_warning():
    """Non-Agent tools are not checked."""
    event = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "npm run build",
        },
    }
    assert _check_agent_team_name(event) is None


def test_plain_agent_no_impl_keywords_allowed():
    """No implementation keywords and no team_name → allowed through (no distinction after the full opening)."""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "check the status of the deployment",
        },
    }
    assert _check_agent_team_name(event) is None
