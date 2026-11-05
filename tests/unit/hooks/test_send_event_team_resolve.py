"""Batch 3 (4): send_event's CC team ownership criterion took the wrong object.

The old strategy 1 took payload.agent_type (a **template name**, such as testing-bug-fixer) and matched it against CC
team
config's members[].name (**a custom member name given at dispatch time**, such as batch3-wrong-object-fixes).
The two are not from the same source and in most cases cannot match at all; occasionally colliding with a same name
would instead attribute the event to another session's
team (worse than a miss). The authoritative criterion is the leadSessionId that CC itself writes into config.json.
"""

from __future__ import annotations

import importlib
import json

send_event = importlib.import_module("aiteam.hooks.send_event")


def _write_team(home, dirname: str, config: dict) -> None:
    d = home / ".claude" / "teams" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _fake_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


class TestResolveCCTeamName:
    def test_lead_session_id_is_authoritative(self, monkeypatch, tmp_path):
        home = _fake_home(monkeypatch, tmp_path)
        # Another session's team, whose member name happens to equal the template name — the old implementation would
        # hit it (wrong object)
        _write_team(
            home,
            "workflow-wf_abc",
            {
                "name": "workflow-wf_abc",
                "leadSessionId": "other-session",
                "members": [{"name": "testing-bug-fixer"}],
            },
        )
        _write_team(
            home,
            "session-11112222",
            {
                "name": "session-11112222",
                "leadSessionId": "sess-1",
                "members": [{"name": "batch3-fixes"}],
            },
        )
        assert send_event._resolve_cc_team_name("sess-1") == "session-11112222"

    def test_no_owning_team_returns_none(self, monkeypatch, tmp_path):
        """When this session has no team, return None — do not guess at other people's teams by member name."""
        home = _fake_home(monkeypatch, tmp_path)
        _write_team(
            home,
            "workflow-wf_abc",
            {
                "name": "workflow-wf_abc",
                "leadSessionId": "other-session",
                "members": [{"name": "testing-bug-fixer"}],
            },
        )
        assert send_event._resolve_cc_team_name("sess-1") is None

    def test_empty_session_id_returns_none(self, monkeypatch, tmp_path):
        home = _fake_home(monkeypatch, tmp_path)
        _write_team(
            home,
            "session-11112222",
            {"name": "session-11112222", "leadSessionId": "sess-1", "members": []},
        )
        assert send_event._resolve_cc_team_name("") is None

    def test_missing_teams_dir_is_silent(self, monkeypatch, tmp_path):
        _fake_home(monkeypatch, tmp_path)
        assert send_event._resolve_cc_team_name("sess-1") is None

    def test_broken_config_skipped(self, monkeypatch, tmp_path):
        home = _fake_home(monkeypatch, tmp_path)
        bad = home / ".claude" / "teams" / "broken"
        bad.mkdir(parents=True)
        (bad / "config.json").write_text("{not json", encoding="utf-8")
        _write_team(
            home,
            "session-11112222",
            {"name": "session-11112222", "leadSessionId": "sess-1", "members": []},
        )
        assert send_event._resolve_cc_team_name("sess-1") == "session-11112222"

    def test_member_name_matching_is_gone(self):
        """Source-level red line: members[].name is no longer used as an ownership criterion."""
        import inspect

        src = inspect.getsource(send_event._resolve_cc_team_name)
        assert 'config.get("members"' not in src
        assert "leadSessionId" in src
