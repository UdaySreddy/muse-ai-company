"""Unit tests for turn_end_guard Stop hook (wake system v2 §8).

decide() is precision-tested over 7 branches as a pure function; main() uses mock stdin + a tmp state directory + mock
endpoint queries
to test the Stop/user-prompt two modes and fail-open. Aligned with test (4) in batch0-contract-tests.md.
"""

from __future__ import annotations

import io
import json
import time

import pytest

import aiteam.hooks.turn_end_guard as g


# ---- decide() 7 branches (pure function) --------------------------------------------
def test_decide_stop_hook_active_allows():
    a, b, _ = g.decide(stop_hook_active=True, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "stop_hook_active"


def test_decide_manual_allows_even_in_danger():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=True,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "manual"


def test_decide_stop_keyword_allows_even_in_danger():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=True, work_in_flight=True,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "stop_keyword"


def test_decide_no_work_allows():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=False,
                        watcher_armed=False, block_count=0)
    assert a == "allow" and b == "safe"


def test_decide_watcher_armed_allows():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=True, block_count=0)
    assert a == "allow" and b == "watcher_armed"


def test_decide_block_cap_allows():
    a, b, _ = g.decide(stop_hook_active=False, manual_active=False,
                        stop_keyword_hit=False, work_in_flight=True,
                        watcher_armed=False, block_count=g.MAX_BLOCKS)
    assert a == "allow" and b == "block_cap"


def test_decide_danger_zone_blocks():
    a, b, reason = g.decide(stop_hook_active=False, manual_active=False,
                            stop_keyword_hit=False, work_in_flight=True,
                            watcher_armed=False, block_count=0)
    assert a == "block" and b == "danger_zone"
    assert "watcher" in reason


# ---- Stop-keyword regex --------------------------------------------------------
@pytest.mark.parametrize("text", ["先停一下", "收工吧", "ok please stop", "hold on", "暂停"])
def test_stop_keywords_match(text):
    assert g.STOP_KEYWORDS.search(text)


@pytest.mark.parametrize("text", ["继续推进", "keep going", "下一步做什么"])
def test_stop_keywords_no_false_positive(text):
    assert not g.STOP_KEYWORDS.search(text)


# ---- main() integration ----------------------------------------------------------
def _run_main(payload, monkeypatch, argv=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("sys.argv", argv or ["turn_end_guard.py"])
    code = 0
    try:
        g.main()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    return code


@pytest.fixture()
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_WAKE_STATE_DIR", tmp_path)
    return tmp_path


def test_main_blocks_in_danger_zone(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 2, "live_runs": 0})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续推进")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    # The count has been persisted
    state = json.loads((tmp_state / "s1.json").read_text())
    assert state["block_count"] == 1


def test_main_allows_when_no_work(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 0, "live_runs": 0})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "")
    code = _run_main({"session_id": "s1", "stop_hook_active": False}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""  # No block output


def test_main_stop_hook_active_short_circuits(tmp_state, monkeypatch, capsys):
    # If stop_hook_active, never query the endpoint and never block
    def _boom(sid, tid):
        raise AssertionError("endpoint must not be queried")
    monkeypatch.setattr(g, "_query_actionable", _boom)
    code = _run_main({"session_id": "s1", "stop_hook_active": True}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_stop_keyword_allows_without_endpoint(tmp_state, monkeypatch, capsys):
    def _boom(sid, tid):
        raise AssertionError("keyword exemption must short-circuit before endpoint")
    monkeypatch.setattr(g, "_query_actionable", _boom)
    monkeypatch.setattr(g, "_last_user_text", lambda p: "好了先停")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": "x"}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_watcher_armed_allows(tmp_state, monkeypatch, capsys):
    # The arming file is within its validity period → even with work pending, let it through
    (tmp_state / "s1.armed").write_text(str(time.time() + 60))
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 5, "live_runs": 1})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_block_cap_releases(tmp_state, monkeypatch, capsys):
    (tmp_state / "s1.json").write_text(json.dumps({"block_count": g.MAX_BLOCKS, "last_block_at": time.time()}))
    monkeypatch.setattr(g, "_query_actionable", lambda sid, tid: {"busy_agents": 1, "live_runs": 0})
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""  # The cap has been reached; let it through and block no more


def test_main_user_prompt_writes_manual_marker(tmp_state, monkeypatch):
    code = _run_main({"session_id": "s1"}, monkeypatch, argv=["turn_end_guard.py", "user-prompt"])
    assert code == 0
    state = json.loads((tmp_state / "s1.json").read_text())
    assert state["manual_until"] > time.time()
    assert state["block_count"] == 0


def test_main_manual_marker_allows_stop(tmp_state, monkeypatch, capsys):
    (tmp_state / "s1.json").write_text(json.dumps({"manual_until": time.time() + 300}))

    def _boom(sid, tid):
        raise AssertionError("manual exemption must short-circuit before endpoint")
    monkeypatch.setattr(g, "_query_actionable", _boom)
    monkeypatch.setattr(g, "_last_user_text", lambda p: "继续推进")
    code = _run_main({"session_id": "s1", "stop_hook_active": False, "transcript_path": ""}, monkeypatch)
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_main_fail_open_on_bad_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{{{"))
    monkeypatch.setattr("sys.argv", ["turn_end_guard.py"])
    code = 0
    try:
        g.main()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    assert code == 0
