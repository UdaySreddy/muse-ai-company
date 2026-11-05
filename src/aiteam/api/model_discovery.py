"""Model discovery and default model configuration — model governance (docs/model-governance-design.md).

Available models = automatically pulled from the file source of truth: scanning all local CC transcripts for the
message.model values that actually appeared
(the models you have genuinely used are the models available to you), with zero API dependency and zero hardcoded
lists (a hardcoded list cannot capture third-party integrations like deepreasoning-coding-max-4.7 — verified to exist in
practice).

The default startup model is written to the top-level "model" key of ~/.claude/settings.json (officially supported by
CC; takes effect in new
sessions). settings.json is the user's global configuration, protected by three layers of write protection: touch only
the model key /
back up before writing / atomic tmp+rename write.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]{2,80})"')
_TAIL_BYTES = 100_000
_SYNTHETIC = "<synthetic>"
# Pure tier-level aliases (not full model IDs) — marked as aliases and kept out of the default dropdown's main list
_ALIASES = {"opus", "sonnet", "haiku", "fable", "default"}

_cache: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 60.0


def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def scan_available_models(force: bool = False) -> list[dict]:
    """Scan all main-session transcripts and aggregate the models that appeared.

    Returns [{model, file_count, last_seen_ts, alias}], in descending last_seen order.
    Measured at ~1s over 109 files; 60s in-process cache.
    """
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["data"]

    stats: dict[str, dict] = {}
    root = _projects_dir()
    try:
        slug_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []
    for slug in slug_dirs:
        try:
            files = [f for f in slug.glob("*.jsonl") if f.stat().st_size > 1024]
        except OSError:
            continue
        for f in files:
            try:
                st = f.stat()
                with open(f, "rb") as fh:
                    if st.st_size > _TAIL_BYTES:
                        fh.seek(st.st_size - _TAIL_BYTES)
                    data = fh.read()
            except OSError:
                continue
            seen_here: set[str] = set()
            for m in _MODEL_RE.finditer(data):
                try:
                    model = m.group(1).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                if model == _SYNTHETIC or model in seen_here:
                    continue
                seen_here.add(model)
                entry = stats.setdefault(
                    model, {"model": model, "file_count": 0, "last_seen_ts": 0.0}
                )
                entry["file_count"] += 1
                entry["last_seen_ts"] = max(entry["last_seen_ts"], st.st_mtime)

    result = sorted(stats.values(), key=lambda x: x["last_seen_ts"], reverse=True)
    for r in result:
        r["alias"] = r["model"] in _ALIASES
    _cache["ts"] = now
    _cache["data"] = result
    return result


def read_default_model() -> str:
    """Read the top-level model key of settings.json (returns an empty string when unset)."""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return str(json.load(f).get("model") or "")
    except Exception:  # noqa: BLE001
        return ""


def set_default_model(model: str) -> dict:
    """Write the top-level model key of settings.json (empty string = remove it, restoring CC's own default).

    Three layers of write protection: touch only the model key / back up to .bak-aiteam before writing / atomic
    tmp+rename write.
    """
    model = (model or "").strip()
    sp = _settings_path()
    try:
        with open(sp, encoding="utf-8") as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"settings.json failed to parse; Write refused: {exc}"}

    # Back up (best-effort)
    try:
        if sp.exists():
            backup = sp.with_name("settings.json.bak-aiteam")
            backup.write_bytes(sp.read_bytes())
    except OSError:
        pass

    prev = settings.get("model")
    if model:
        settings["model"] = model
    else:
        settings.pop("model", None)

    tmp = sp.with_name("settings.json.tmp-aiteam")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, sp)
    except OSError as exc:
        return {"ok": False, "error": f"Write failed: {exc}"}
    return {"ok": True, "previous": prev or "", "current": model}
