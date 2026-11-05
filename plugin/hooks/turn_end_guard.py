#!/usr/bin/env python3
"""Turn-end guard — Stop hook for wake system v2 (docs/wake-loop-v2-design.md §8).

Prevents the Leader from "blind-stopping" in the ACTIVE state: when subagents/runs are in flight but no event watcher
is armed, if
the Leader ends the turn directly, no mechanism wakes it when the work finishes. The guard intercepts once at turn end
(decision:block, the channel confirmed in practice by batch0), forcing it to "arm the watcher or keep polling".

Two modes (argv-driven, following send_event.py):
    turn_end_guard.py               -> Stop event: decide whether to prevent a blind stop
    turn_end_guard.py user-prompt   -> UserPromptSubmit: mark manual mode (the user is present)

Stop decision (the 7 branches verified by batch0, docs/batch0-contract-tests.md test 4) priority:
    1. stop_hook_active            -> allow (recursion guard, yielding to other hooks that already blocked, e.g. /goal)
    2. manual mode within validity  -> allow (the user is present; the mark written by UserPromptSubmit)
    3. the latest user message contains a stop keyword -> allow (hard constraint: an explicit user stop must always
    pass, Chinese and English)
    4. query /api/wake/actionable:
         no work in flight (busy_agents==0 and live_runs==0) -> allow
         work in flight + watcher armed     -> allow (trust the watcher to wake)
         work in flight + watcher not armed -> block (decision:block + reason)
    5. consecutive block count over the cap -> allow (prevent a false-positive deadlock block)

Fail-open: any exception always allows (exit 0). A hook failure must never freeze the session — rather miss a block
(lose one delay, with /loop as the fallback) than wrongly block (locking the user inside a block).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# ---- Configuration constants ----
# The window (seconds) after a user message during which it counts as manual. Consistent with session_probe's
# LIVE_WINDOW caliber (15min):
# while the user keeps interacting, each message refreshes this window; after they stop beyond the window, the
# autonomous guard takes effect again.
MANUAL_TTL = 900
# Consecutive-block cap: once the accumulated blocks on the same turn chain reach this count, force an allow (prevent
# a false-positive deadlock block).
MAX_BLOCKS = 3
# The "consecutive" judgment window for the block count: if the last block is older than this many seconds, the count
# resets to zero (treated as a new chain).
BLOCK_RESET_AFTER = 300
_API_TIMEOUT = 2.0

_PORT_FILE = Path.home() / ".claude" / "data" / "ai-team-os" / "api_port.txt"
_WAKE_STATE_DIR = Path.home() / ".claude" / "data" / "ai-team-os" / "wake-state"

# Stop keywords (Chinese and English). A hit in the latest user message allows immediately — a hard constraint.
STOP_KEYWORDS = re.compile(
    r"\u505c|\u6536\u5de5|\u522b\u518d|\u6682\u505c|\u6253\u4f4f|\u6b47|\bstop\b|\bhold on\b|\bhalt\b|\bpause\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pure decision functions — all inputs passed explicitly, no I/O, directly unit-testable (same as batch0's guard_unit).
# ---------------------------------------------------------------------------
def decide(
    *,
    stop_hook_active: bool,
    manual_active: bool,
    stop_keyword_hit: bool,
    work_in_flight: bool,
    watcher_armed: bool,
    block_count: int,
    max_blocks: int = MAX_BLOCKS,
) -> tuple[str, str, str]:
    """Returns (action, branch, reason), action ∈ {'allow','block'}. See the module doc for the priority."""
    if stop_hook_active:
        return "allow", "stop_hook_active", "another Stop hook already handled this turn"
    if manual_active:
        return "allow", "manual", "user present (manual mode active)"
    if stop_keyword_hit:
        return "allow", "stop_keyword", "user explicitly asked to stop"
    if not work_in_flight:
        return "allow", "safe", "no work in flight"
    if watcher_armed:
        return "allow", "watcher_armed", "work in flight but a watcher is armed to wake"
    if block_count >= max_blocks:
        return "allow", "block_cap", f"released after {block_count} consecutive blocks"
    return (
        "block",
        "danger_zone",
        "The OS detected agents/runs in flight but no event watcher armed. Please choose one of two: "
        "(a) arm the watcher in the background (bash scripts/os-watch.sh <session_id> <team_id> &) "
        "before stopping, so it wakes you when the work finishes; (b) if you really want to wrap up, reply to the "
        "user / explicitly say stop and you will be let through.",
    )


# ---------------------------------------------------------------------------
# I/O helpers (all defensive: failures return safe defaults, never raise)
# ---------------------------------------------------------------------------
def _api_base() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(_PORT_FILE.read_text().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError, OSError):
        return "http://localhost:8000"


def _safe_sid(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")


def _state_path(session_id: str) -> Path:
    return _WAKE_STATE_DIR / f"{_safe_sid(session_id)}.json"


def _armed_path(session_id: str) -> Path:
    # The watcher (os-watch.sh) is the sole writer of this file; the guard only reads. Each occupies its own file =>
    # no read-modify-write race.
    return _WAKE_STATE_DIR / f"{_safe_sid(session_id)}.armed"


def _watcher_armed(session_id: str) -> bool:
    """Read the <sid>.armed heartbeat (epoch seconds) maintained by os-watch.sh; unexpired counts as armed."""
    try:
        armed_until = float(_armed_path(session_id).read_text(encoding="utf-8").strip())
        return armed_until > time.time()
    except Exception:  # noqa: BLE001
        return False


def _load_state(session_id: str) -> dict:
    try:
        return json.loads(_state_path(session_id).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(session_id: str, state: dict) -> None:
    try:
        _WAKE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state_path(session_id).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass  # marker persistence is best-effort


def _last_user_text(transcript_path: str) -> str:
    """Tail-read the transcript, taking the last real user message text (skipping tool_result / empty)."""
    try:
        if not transcript_path:
            return ""
        p = Path(transcript_path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > 200_000:
                f.seek(size - 200_000)
            data = f.read().decode("utf-8", errors="replace")
        last = ""
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001 — a seek-truncated first line, etc.
                continue
            if d.get("type") != "user":
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                last = content
            elif isinstance(content, list):
                parts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                if parts:
                    last = " ".join(parts)
        return last
    except Exception:  # noqa: BLE001
        return ""


def _query_actionable(session_id: str, team_id: str) -> dict:
    """Query /api/wake/actionable. Failure returns {} (→ treated as no work in flight, fail-open allow)."""
    try:
        base = _api_base()
        qs = f"session_id={session_id}"
        if team_id:
            qs += f"&team_id={team_id}"
        req = urllib.request.Request(f"{base}/api/wake/actionable?{qs}", method="GET")
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Mode: UserPromptSubmit -> write the manual mark (the user is present)
# ---------------------------------------------------------------------------
def _handle_user_prompt(payload: dict) -> None:
    session_id = payload.get("session_id", "")
    state = _load_state(session_id)
    state["manual_until"] = time.time() + MANUAL_TTL
    # The user is back: reset the block count
    state["block_count"] = 0
    _save_state(session_id, state)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Mode: Stop -> judge the stop
# ---------------------------------------------------------------------------
def _handle_stop(payload: dict) -> None:
    session_id = payload.get("session_id", "")
    stop_hook_active = bool(payload.get("stop_hook_active"))

    # The recursion guard has top priority, and this branch needs no external I/O
    if stop_hook_active:
        sys.exit(0)

    state = _load_state(session_id)
    now = time.time()

    manual_active = float(state.get("manual_until", 0) or 0) > now
    watcher_armed = _watcher_armed(session_id)

    # Consecutive block count (resets past the window)
    last_block_at = float(state.get("last_block_at", 0) or 0)
    block_count = int(state.get("block_count", 0) or 0)
    if now - last_block_at > BLOCK_RESET_AFTER:
        block_count = 0

    last_user = _last_user_text(payload.get("transcript_path", ""))
    stop_keyword_hit = bool(STOP_KEYWORDS.search(last_user)) if last_user else False

    # Only query the endpoint when none of the earlier exemptions hold (saves one HTTP call)
    work_in_flight = False
    if not (manual_active or stop_keyword_hit):
        verdict = _query_actionable(session_id, payload.get("team_id", ""))
        work_in_flight = (
            int(verdict.get("busy_agents", 0) or 0) > 0
            or int(verdict.get("live_runs", 0) or 0) > 0
        )

    action, branch, reason = decide(
        stop_hook_active=stop_hook_active,
        manual_active=manual_active,
        stop_keyword_hit=stop_keyword_hit,
        work_in_flight=work_in_flight,
        watcher_armed=watcher_armed,
        block_count=block_count,
    )

    if action == "block":
        state["block_count"] = block_count + 1
        state["last_block_at"] = now
        _save_state(session_id, state)
        print(json.dumps({"decision": "block", "reason": reason}))
        sys.exit(0)

    # allow: if a count existed, converge it to zero
    if block_count:
        state["block_count"] = 0
        _save_state(session_id, state)
    sys.exit(0)


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 — fail-open
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    try:
        if mode == "user-prompt":
            _handle_user_prompt(payload)
        else:
            _handle_stop(payload)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — fail-open: a hook fault must never block the session
        sys.exit(0)


if __name__ == "__main__":
    main()
