#!/usr/bin/env python3
"""Context Tracker - UserPromptSubmit Hook.

Reads the last assistant message's usage.input_tokens from CC transcript jsonl,
calculates context usage ratio. >=80% triggers warning, >=90% triggers critical.
Data source: Anthropic API exact usage, not estimates.
No intermediate files, no statusline dependency, project isolation built-in
(transcript_path.parent = project directory).

Usage: python -m aiteam.hooks.context_tracker
"""
import json
import os
import re
import sys
from pathlib import Path

# Computed against a 1M context window by default.
# Reason: Claude Code now defaults to a 1M window on all platforms;
# under-reporting (missed warnings) is less harmful than over-reporting (frequent false warnings interrupting work).
# To force computation against a small window (e.g. 200K), set the CLAUDE_CONTEXT_SIZE environment variable.
DEFAULT_CONTEXT_SIZE = 1_000_000
LARGE_CONTEXT_SIZE = 1_000_000
_CLAUDE_CONFIG = Path.home() / ".claude.json"
_FAMILY_RE = re.compile(r"claude-(opus|sonnet|haiku)")


def _claude_config_has_1m_variant(model: str) -> bool:
    """Detect whether the machine has 1M context mode enabled (via the historical usage entries in ~/.claude.json).

    Two detection levels:
    1. **Exact match**: `{model}[1m]` exists (most reliable)
    2. **Same-family fallback**: any `claude-{family}-*[1m]` exists → that family is considered to use 1M
       (the opus/sonnet/haiku families are each judged independently)

    Why family fallback: CC transcripts strip the `[1m]` suffix, and a new model variant's [1m]
    usage entry may not yet be written into the config (on the first run after a model upgrade). If the user has 1M
    history in the same family,
    then **the new model is most likely still in 1M mode**. This is a reasonable heuristic.
    """
    if not model:
        return False
    try:
        if not _CLAUDE_CONFIG.is_file():
            return False
        content = _CLAUDE_CONFIG.read_bytes()
    except OSError:
        return False

    # Level 1: Exact match
    exact_marker = f'{model}[1m]'.encode()
    if exact_marker in content:
        return True

    # Level 2: Same-family fallback
    family_match = _FAMILY_RE.search(model.lower())
    if not family_match:
        return False
    family = family_match.group(1)
    family_pattern = re.compile(
        rf'"claude-{family}-[a-z0-9-]+\[1m\]"'.encode()
    )
    return bool(family_pattern.search(content))


def _compute_used_pct(total_tokens: int, model: str) -> tuple[float, int]:
    """Compute the usage rate + detect the context window size.

    Priority (high to low):
    1. **ENV var `CLAUDE_CONTEXT_SIZE`** — the user's ultimate override (any positive integer token count).
       This can force a downgrade to a small window (e.g. CLAUDE_CONTEXT_SIZE=200000).
    2. **The model string contains `1m` / `1000000`** → 1M (explicit confirmation)
    3. **~/.claude.json has `{model}[1m]` or a same-family `*[1m]`** → 1M (historical-usage inference)
    4. **Default** → 1M (Claude Code now defaults to a 1M window on all platforms;
       under-reporting is less harmful than frequent false warnings interrupting work)
    """
    # 1. ENV var ultimate override (can force computation against a small window such as 200K)
    env_size = os.environ.get("CLAUDE_CONTEXT_SIZE", "").strip()
    if env_size.isdigit() and int(env_size) > 0:
        ctx_size = int(env_size)
        pct = round((total_tokens / ctx_size) * 100, 1)
        return pct, ctx_size

    # 2. The model string explicitly contains the 1m marker
    ctx_size = DEFAULT_CONTEXT_SIZE
    if model:
        ml = model.lower()
        if "1m" in ml or "1000000" in ml:
            ctx_size = LARGE_CONTEXT_SIZE

    # 3. ~/.claude.json historical-usage inference (same-family fallback)
    if ctx_size == DEFAULT_CONTEXT_SIZE and _claude_config_has_1m_variant(model):
        ctx_size = LARGE_CONTEXT_SIZE

    # 4. The default is already 1M (DEFAULT_CONTEXT_SIZE = 1_000_000); no extra judgment needed

    pct = round((total_tokens / ctx_size) * 100, 1)
    return pct, ctx_size


def _read_last_usage(transcript: Path) -> tuple[int, str] | None:
    """Scan transcript jsonl in reverse for the last assistant message usage.

    Returns (total_tokens, model_name) or None.
    total_tokens = input_tokens + cache_read + cache_creation (all count as context usage)
    """
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = entry.get("message") or entry
        if m.get("role") != "assistant":
            continue
        usage = m.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        if input_tokens is None:
            continue
        total = (
            int(input_tokens)
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
        )
        model = m.get("model", "") or entry.get("model", "")
        return total, model

    return None


def main():
    # Windows UTF-8 fix
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return  # silent failure

    transcript_path_str = payload.get("transcript_path", "")
    if not transcript_path_str:
        return
    transcript = Path(transcript_path_str)
    if not transcript.exists():
        return

    result = _read_last_usage(transcript)
    if result is None:
        return
    used_tokens, model = result

    pct, ctx_size = _compute_used_pct(used_tokens, model)

    if pct >= 90:
        print(
            f"[CONTEXT CRITICAL] Context usage: {pct}% ({used_tokens}/{ctx_size}). "
            "Stop the current work immediately, save all memories and progress to the memory files, "
            "then remind the user to run /compact. Do not start any new task."
        )
    elif pct >= 80:
        print(
            f"[CONTEXT WARNING] Context usage: {pct}% ({used_tokens}/{ctx_size}). "
            "Finish the current node task as soon as possible, then save memories and progress to the memory files, "
            "and remind the user to run /compact."
        )


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
