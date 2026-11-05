"""Data-fetching unit tests for the single-measurement card — attribution v1 §5.2 ④.

This card is **explicitly exempt from the coverage gate**, so it is the only place on the whole page with "a number
without a denominator". The exemption holds
only because it paid the price: the data can only come from parsing a transcript live. Every test below tests whether
that price
is still being paid — once the price lapses, the exemption becomes a back door around the gate.

The most important is :class:`TestNoBareTotal`: the parser internally computes a ``total_tokens`` (it needs it
itself), while the presentation surface must show no total. Between the two there is only one "convenient passthrough".
"""

from __future__ import annotations

import json

import pytest

from aiteam.api import usage_probe
from aiteam.types import TOKEN_LAYERS


def _write_transcript(path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _assistant(request_id: str, usage: dict, text: str = "", ts: str = "2026-07-29T00:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": request_id,
        "message": {
            "model": "claude-opus-5",
            "usage": usage,
            "content": [{"type": "text", "text": text}] if text else [],
        },
    }


def _user(text: str, ts: str = "2026-07-29T00:00:00Z") -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


USAGE = {
    "input_tokens": 10,
    "output_tokens": 20,
    "cache_creation_input_tokens": 30,
    "cache_read_input_tokens": 40,
}


class TestNoBareTotal:
    """No total may appear on the presentation surface — the parser's internal total_tokens must be discarded."""

    def test_payload_has_four_layers_and_no_total(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_user("do it"), _assistant("r1", USAGE, "done")])

        payload = usage_probe.probe_dispatch(str(p))

        assert payload is not None
        for layer in TOKEN_LAYERS:
            assert layer in payload
        # Measured cache_read is 95.6%; any "total" is a synonym for "cache read volume" (§1.2)
        assert "total_tokens" not in payload
        assert not any("total" in key for key in payload), f"A total field appeared: {sorted(payload)}"

    def test_metric_is_always_stamped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_assistant("r1", USAGE)])
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["metric"] == "usage_sum", "A token number without a metric is meaningless"


class TestNoDataIsNotZero:
    """File absent ≠ used 0 — the former must make the whole card absent, not display a row of zeros."""

    def test_missing_file_returns_none(self, tmp_path):
        assert usage_probe.probe_dispatch(str(tmp_path / "nope.jsonl")) is None

    def test_transcript_without_usage_rows_returns_none(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_user("hi"), {"type": "system", "timestamp": "2026-07-29T00:00:00Z"}])
        assert usage_probe.probe_dispatch(str(p)) is None


class TestSameArithmeticAsTheLedger:
    """There is only one copy of the numeric algorithm — the card and the ledger must use the exact same accumulation
    metric."""

    def test_same_request_id_takes_last_snapshot_not_sum(self, tmp_path):
        # One API call produces multiple assistant rows with output increasing as it streams; bare per-row addition
        # inflates several-fold
        p = tmp_path / "t.jsonl"
        _write_transcript(
            p,
            [
                _assistant("r1", {**USAGE, "output_tokens": 3}),
                _assistant("r1", {**USAGE, "output_tokens": 300}),
                _assistant("r2", USAGE),
            ],
        )
        payload = usage_probe.probe_dispatch(str(p))

        assert payload["output_tokens"] == 320  # 300 + 20, not 3 + 300 + 20
        assert payload["input_tokens"] == 20  # 10 per requestId, two of them
        assert payload["api_calls"] == 2


class TestSampleExcerpts:
    def test_first_user_and_last_assistant_text_are_excerpted(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(
            p,
            [
                _user("First dispatch prompt"),
                _assistant("r1", USAGE, "Interim conclusion"),
                _user("Tool result fed back"),
                _assistant("r2", USAGE, "Final output"),
            ],
        )
        payload = usage_probe.probe_dispatch(str(p))

        assert payload["prompt_excerpt"] == "First dispatch prompt"
        assert payload["result_excerpt"] == "Final output"

    def test_excerpts_are_clipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_user("x" * 5000), _assistant("r1", USAGE, "y" * 5000)])
        payload = usage_probe.probe_dispatch(str(p))

        assert len(payload["prompt_excerpt"]) == usage_probe.EXCERPT_LIMIT + 1  # +1 = the ellipsis
        assert payload["result_excerpt"].endswith("…")

    def test_duration_comes_from_transcript_timestamps(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(
            p,
            [
                _user("go", ts="2026-07-29T00:00:00Z"),
                _assistant("r1", USAGE, "done", ts="2026-07-29T00:02:30Z"),
            ],
        )
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["duration_ms"] == 150_000

    def test_broken_line_does_not_kill_the_whole_sample(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            "\n".join([json.dumps(_user("go")), "{not json", json.dumps(_assistant("r1", USAGE, "ok"))]),
            encoding="utf-8",
        )
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["result_excerpt"] == "ok"


class TestModelIsObserved:
    def test_model_read_from_transcript_not_guessed(self, tmp_path):
        p = tmp_path / "t.jsonl"
        _write_transcript(p, [_assistant("r1", USAGE)])
        payload = usage_probe.probe_dispatch(str(p))
        # Transcripts always carry the full model name; the `model: opus` in templates is a tier alias — the two are not
        # interchangeable
        assert payload["model"] == "claude-opus-5"
        assert payload["model_source"] == "transcript"

    def test_synthetic_model_is_not_reported_as_a_real_model(self, tmp_path):
        p = tmp_path / "t.jsonl"
        rows = [_assistant("r1", USAGE)]
        rows[0]["message"]["model"] = "<synthetic>"
        _write_transcript(p, rows)
        payload = usage_probe.probe_dispatch(str(p))
        assert payload["model"] == ""
        assert payload["model_source"] == "unknown"


@pytest.mark.parametrize("field", ["prompt_excerpt", "result_excerpt", "duration_ms", "transcript_path"])
def test_card_fields_present(tmp_path, field):
    p = tmp_path / "t.jsonl"
    _write_transcript(p, [_user("go"), _assistant("r1", USAGE, "done")])
    assert field in usage_probe.probe_dispatch(str(p))
