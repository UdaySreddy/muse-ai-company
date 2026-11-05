"""Model governance unit tests — file source-of-truth scanning + settings.json write protection."""

from __future__ import annotations

import json

from aiteam.api import model_discovery as md


def test_scan_extracts_models_and_filters_synthetic(tmp_path, monkeypatch):
    slug = tmp_path / "projects" / "-Users-x-proj"
    slug.mkdir(parents=True)
    f = slug / "sess-1.jsonl"
    f.write_text(
        ('{"type":"assistant","message":{"model":"claude-test-a"}}\n' * 30)
        + '{"type":"assistant","message":{"model":"<synthetic>"}}\n'
        + '{"type":"assistant","message":{"model":"opus"}}\n'
    )
    monkeypatch.setattr(md, "_projects_dir", lambda: tmp_path / "projects")
    result = md.scan_available_models(force=True)
    models = {m["model"]: m for m in result}
    assert "claude-test-a" in models
    assert "<synthetic>" not in models
    assert models["opus"]["alias"] is True  # Alias annotation; does not enter the main dropdown
    assert models["claude-test-a"]["alias"] is False
    assert models["claude-test-a"]["file_count"] == 1  # Deduplicated within the same file


def test_set_default_model_only_touches_model_key(tmp_path, monkeypatch):
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps({"env": {"KEY": "1"}, "permissions": {"deny": ["x"]}}))
    monkeypatch.setattr(md, "_settings_path", lambda: sp)

    r = md.set_default_model("claude-test-b")
    assert r["ok"] is True
    data = json.loads(sp.read_text())
    assert data["model"] == "claude-test-b"
    assert data["env"] == {"KEY": "1"}  # Other keys untouched to the letter
    assert data["permissions"] == {"deny": ["x"]}
    assert sp.with_name("settings.json.bak-aiteam").exists()  # Backup

    # An empty string = remove the key
    r2 = md.set_default_model("")
    assert r2["ok"] is True
    assert "model" not in json.loads(sp.read_text())
    assert md.read_default_model() == ""


def test_set_default_refuses_on_corrupt_settings(tmp_path, monkeypatch):
    sp = tmp_path / "settings.json"
    sp.write_text("{corrupt json")
    monkeypatch.setattr(md, "_settings_path", lambda: sp)
    r = md.set_default_model("claude-x")
    assert r["ok"] is False and "Write refused" in r["error"]
    assert sp.read_text() == "{corrupt json"  # The original file was not corrupted
