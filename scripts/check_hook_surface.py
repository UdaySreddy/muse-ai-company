#!/usr/bin/env python3
"""I8 — hook registration surface machine check.

Two entry points install AI Team OS and each one carries its own hook table:

  * plugin mode  → plugin/hooks/hooks.json          (read by CC, and by
                   auto_install._sync_main_chain when it converges the chain)
  * source mode  → install.py HOOK_SURFACE          (written into ~/.claude/settings.json)

When they disagree the project ships two different operating systems depending on
how the user installed it — the 2026-07-27 audit found the source path missing
four whole events (TaskCreated / UserPromptSubmit / PermissionDenied / PreCompact)
and registering PreToolUse hooks under the wrong matcher. This check pins them
together on (event, matcher, script, arg, timeout) and additionally pins the
bilingual README hook counts to the manifest.

README coverage is deliberately exhaustive: the first version of this check only
pinned the "Hook System (N scripts across M Lifecycle Events)" section heading,
so the same event count restated in the feature list and in the directory tree
drifted unnoticed (v1.10.3 shipped with three stale "12 lifecycle events" copies
while the heading said 11). Every numbered lifecycle-event claim in either README
is now pinned to the manifest.

Usage: python3 scripts/check_hook_surface.py    (from the repo root)
Exit code: 0 = aligned, 1 = drift.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# auto_install.py is the plugin's self-heal entry: it bootstraps the chain from
# outside, so it is registered in hooks.json but must never be in the installed
# chain. It is the only legitimate plugin-side-only hook.
PLUGIN_ONLY_SCRIPTS = {"auto_install.py"}

# `<anything> ".../hooks/<script>.py"[ <arg>]` — same shape both manifests use.
_CMD_RE = re.compile(r'/hooks/([\w-]+\.py)"?(?:\s+(\S+))?\s*$')


def _load_manifest() -> tuple[set[tuple], set[str], list[str], list[str]]:
    """Parse plugin/hooks/hooks.json.

    Returns (comparable_surface, every_registered_script, events, errors). The
    comparable surface excludes PLUGIN_ONLY_SCRIPTS; the script set keeps them
    (they are still part of "how many hooks does this OS ship").
    """
    errors: list[str] = []
    path = ROOT / "plugin" / "hooks" / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    surface: set[tuple] = set()
    scripts: set[str] = set()

    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            matcher = group.get("matcher", "")
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                # The self-heal entry ships as a cross-platform `case ... esac`
                # launcher, which no "<interpreter> <script> [arg]" regex can parse.
                plugin_only = next(
                    (name for name in PLUGIN_ONLY_SCRIPTS if name in command), None
                )
                if plugin_only:
                    scripts.add(plugin_only)
                    continue
                match = _CMD_RE.search(command)
                if not match:
                    errors.append(
                        f"hooks.json:{event}: cannot parse hook command → {command[:80]!r}"
                    )
                    continue
                script, arg = match.group(1), match.group(2) or ""
                scripts.add(script)
                surface.add((event, matcher, script, arg, hook.get("timeout")))
    return surface, scripts, list(data.get("hooks", {})), errors


def _load_installer_surface() -> set[tuple]:
    """Read HOOK_SURFACE out of the root install.py without running it."""
    spec = importlib.util.spec_from_file_location("install_surface", ROOT / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        (event, matcher, script, arg, timeout)
        for event, matcher, entries in module.HOOK_SURFACE
        for script, arg, timeout in entries
    }


def _describe(entry: tuple) -> str:
    event, matcher, script, arg, timeout = entry
    shown_matcher = matcher or "(no matcher)"
    shown_arg = f" {arg}" if arg else ""
    return f"{event} [{shown_matcher}] → {script}{shown_arg} (timeout={timeout})"


_HEADLINE_RE = {
    "README.md": re.compile(r"Hook System \((\d+) scripts? across (\d+) Lifecycle Events"),
}

# Every other place either README states a lifecycle-event count. A bare
# "lifecycle event" with no number in front (e.g. "Record sub-Agent lifecycle
# event") is prose, not a claim, and must not be matched.
_EVENT_ANCHOR_RE = (
    re.compile(r"(\d+)\s+(?:CC\s+)?[Ll]ifecycle\s+[Ee]vents?"),
)


def scan_event_anchors(text: str, event_count: int, name: str) -> list[str]:
    """Report every numbered lifecycle-event claim in ``text`` that isn't ``event_count``.

    Pure over the text so the drift behaviour is unit-testable without touching
    the real READMEs.
    """
    failures = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for regex in _EVENT_ANCHOR_RE:
            for match in regex.finditer(line):
                if int(match.group(1)) != event_count:
                    failures.append(
                        f"{name}:{lineno}: lifecycle event count declaration \"{match.group(0).strip()}\" "
                        f"≠ manifest-measured {event_count} (plugin/hooks/hooks.json)"
                    )
    return failures


def _readme_counts(script_count: int, event_count: int) -> list[str]:
    """Pin every README hook count to the manifest."""
    failures: list[str] = []
    for name, pattern in _HEADLINE_RE.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        match = pattern.search(text)
        if not match:
            failures.append(f"{name}: Hook System headline line not found — cannot verify script/event counts")
        else:
            scripts, events = int(match.group(1)), int(match.group(2))
            if scripts != script_count or events != event_count:
                failures.append(
                    f"{name}: declares {scripts} scripts / {events} events ≠ manifest-measured "
                    f"{script_count} scripts / {event_count} events (plugin/hooks/hooks.json)"
                )
        failures.extend(scan_event_anchors(text, event_count, name))
    return failures


def main() -> int:
    manifest, scripts, events, failures = _load_manifest()
    installer = _load_installer_surface()

    for entry in sorted(manifest - installer):
        failures.append(f"install.py is missing (hooks.json has): {_describe(entry)}")
    for entry in sorted(installer - manifest):
        failures.append(f"hooks.json is missing (install.py has): {_describe(entry)}")

    # Hook counts: scripts include the plugin-only self-heal entry, events are
    # whatever the manifest actually registers.
    failures.extend(_readme_counts(len(scripts), len(events)))

    # Every registered script must exist on disk in both distribution copies.
    for script in sorted(scripts):
        if not (ROOT / "plugin" / "hooks" / script).is_file():
            failures.append(f"hooks.json registers a script that does not exist: plugin/hooks/{script}")
        elif script not in PLUGIN_ONLY_SCRIPTS and not (
            ROOT / "src" / "aiteam" / "hooks" / script
        ).is_file():
            failures.append(f"registered hook is missing its in-package twin copy: src/aiteam/hooks/{script}")

    if failures:
        for failure in failures:
            print(f"❌ {failure}")
        print(f"\nConclusion: ❌ hook registration surface drift in {len(failures)} places")
        return 1

    print(
        f"✅ Hook registration surface consistent: {len(events)} events / {len(scripts)} scripts · "
        f"install.py and hooks.json aligned entry by entry (event/matcher/argument/timeout) · README numbers match"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
