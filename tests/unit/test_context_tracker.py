"""Tests for context_tracker hook."""
import json
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest

from aiteam.hooks import context_tracker


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch, tmp_path):
    """Isolate ~/.claude.json and CLAUDE_CONTEXT_SIZE so the user's real environment cannot pollute the tests.

    All tests assume by default:
    - ~/.claude.json does not exist (no 1M history)
    - CLAUDE_CONTEXT_SIZE is not set
    A single test that needs the 1M scenario or an ENV override monkeypatches it inside the test body.
    """
    fake_config = tmp_path / "fake-claude.json"  # Not creating it means it does not exist
    monkeypatch.setattr(context_tracker, "_CLAUDE_CONFIG", fake_config)
    monkeypatch.delenv("CLAUDE_CONTEXT_SIZE", raising=False)


def _make_transcript(lines: list[dict]) -> Path:
    """Write jsonl file with given dict lines, return path."""
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8')
    for entry in lines:
        tmp.write(json.dumps(entry) + "\n")
    tmp.close()
    return Path(tmp.name)


def _run_hook(payload: dict) -> tuple[str, int]:
    """Run main() with given stdin payload, capture stdout and exit code."""
    raw = json.dumps(payload)
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = StringIO(raw)
    sys.stdout = StringIO()
    exit_code = 0
    try:
        context_tracker.main()
        output = sys.stdout.getvalue()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
        output = sys.stdout.getvalue()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
    return output, exit_code


class TestContextTracker:
    def test_warning_at_80_percent(self):
        # 1M context (default), 800K used -> 80% WARNING
        transcript = _make_transcript([
            {"message": {"role": "user", "content": "..."}},
            {"message": {"role": "assistant", "usage": {"input_tokens": 800_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        assert "80" in out
        transcript.unlink()

    def test_critical_at_90_percent(self):
        # 1M context (default), 900K used -> 90% CRITICAL
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 900_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT CRITICAL" in out
        transcript.unlink()

    def test_no_warning_below_80(self):
        # 1M context (default), 100K used -> 10%, no warning
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 100_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == ""
        transcript.unlink()

    def test_includes_cache_tokens(self):
        # 1M context (default), 500K input + 360K cache_read = 860K total = 86%
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {
                "input_tokens": 500_000,
                "cache_read_input_tokens": 360_000,
            }, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        assert "86" in out
        transcript.unlink()

    def test_1m_context_model(self):
        # 1M context, 800K used = 80%
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 800_000}, "model": "claude-opus-4-6[1m]"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        transcript.unlink()

    def test_1m_without_suffix_high_tokens(self):
        # 684800 tokens with model='claude-opus-4-6' (no [1m] suffix) -> auto-detect 1M -> 68.5% no warning
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 684_800}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == ""
        assert "342" not in out
        transcript.unlink()

    def test_env_200k_critical(self, monkeypatch):
        # ENV=200K forces computation against 200K, 180K -> 90% CRITICAL
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "200000")
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 180_000}, "model": "claude-sonnet-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT CRITICAL" in out
        assert "200000" in out
        transcript.unlink()

    def test_default_1m_no_warning_for_170k(self):
        # Default 1M, 170K tokens -> 17%, no warning (before the fix the 200K default falsely reported 85%)
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 170_000}, "model": "claude-opus-4-7"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"170K tokens under the default 1M should not trigger a warning, actual output: {out}"
        transcript.unlink()

    def test_missing_transcript_path(self):
        out, _ = _run_hook({})
        assert out.strip() == ""

    def test_nonexistent_transcript(self):
        out, _ = _run_hook({"transcript_path": "/nonexistent/path.jsonl"})
        assert out.strip() == ""

    def test_malformed_jsonl_line_skipped(self):
        # 1M context (default), malformed lines are skipped, 850K tokens -> 85% WARNING
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8')
        tmp.write("invalid json line\n")
        msg = {"message": {"role": "assistant", "usage": {"input_tokens": 850_000}, "model": "claude-opus-4-6"}}
        tmp.write(json.dumps(msg) + "\n")
        tmp.close()
        transcript = Path(tmp.name)
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert "CONTEXT WARNING" in out
        transcript.unlink()

    def test_empty_stdin_silent(self):
        old_stdin = sys.stdin
        sys.stdin = StringIO("")
        try:
            context_tracker.main()  # Should not raise
        finally:
            sys.stdin = old_stdin

    def test_family_level_1m_detection(self, monkeypatch, tmp_path):
        """Same-family history triggers 1M detection: claude-opus-4-7 + config containing claude-opus-4-6[1m] → 1M."""
        fake_config = tmp_path / "claude.json"
        fake_config.write_text('{"models": {"claude-opus-4-6[1m]": {"input_tokens": 1000}}}')
        monkeypatch.setattr(context_tracker, "_CLAUDE_CONFIG", fake_config)
        # 198K tokens + opus-4-7 (no [1m] history) → family fallback hits → 1M → 19.8% → no warning
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 196_877, "cache_creation_input_tokens": 1670,
            }, "model": "claude-opus-4-7"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"family-level 1M missed, warning falsely triggered: {out}"
        transcript.unlink()

    def test_no_cross_family_leak(self, monkeypatch, tmp_path):
        """No leakage across families: the config only has opus[1m], so sonnet family-level detection does not hit.
        But the DEFAULT is still 1M, so 180K tokens are computed against 1M = 18%, no warning.
        Verification point: opus history records do not wrongly affect sonnet through the family fallback."""
        fake_config = tmp_path / "claude.json"
        fake_config.write_text('{"models": {"claude-opus-4-6[1m]": {"x": 1}}}')
        monkeypatch.setattr(context_tracker, "_CLAUDE_CONFIG", fake_config)
        # 180K + sonnet + config only has opus[1m] → family fallback misses → default 1M → 18%, no warning
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 180_000}, "model": "claude-sonnet-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"sonnet should not be triggered by opus family leakage: {out}"
        transcript.unlink()

    def test_env_var_override(self, monkeypatch):
        """The CLAUDE_CONTEXT_SIZE ENV var overrides everything: 500K → 198K=39.6%, no warning."""
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "500000")
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 198_000}, "model": "claude-opus-4-6"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        assert out.strip() == "", f"ENV override did not take effect: {out}"
        transcript.unlink()

    def test_env_var_priority_beats_1m_marker(self, monkeypatch):
        """ENV has the highest priority: even if the model has a 1m suffix, ENV=200K forces computation against 200K."""
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "200000")
        transcript = _make_transcript([
            {"message": {"role": "assistant", "usage": {"input_tokens": 180_000}, "model": "claude-opus-4-6[1m]"}},
        ])
        out, _ = _run_hook({"transcript_path": str(transcript)})
        # ENV=200K + 180K → 90% CRITICAL (even if the model has a 1m suffix it is overridden)
        assert "CONTEXT CRITICAL" in out
        assert "200000" in out
        transcript.unlink()
