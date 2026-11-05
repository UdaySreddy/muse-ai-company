---
name: os-hooks
description: View the registration status of AI Team OS's Claude Code Hooks and how to repair them
---

# /os-hooks — View the Hook Registration Surface

Help the user view the registration status of AI Team OS's Claude Code Hooks.

## There is only one registration surface

All AI Team OS hooks are **registered in the global `~/.claude/settings.json`**, written by
`install.py` at the repository root; the event surface follows `plugin/hooks/hooks.json`
(use the actual counts measured from that file). Runtime copies of the scripts live in
`~/.claude/hooks/ai-team-os/`.

**Do not write AI Team OS hooks into the project-level `.claude/settings.local.json`** — coexisting with the global chain would fire every event twice.

## Usage

- `/os-hooks` — View the current registration status
- `/os-hooks repair` — Reinstall/repair the registration surface

## Workflow

### No arguments: view status

1. Read the `hooks` section of `~/.claude/settings.json` and list the events and matchers.
2. Run the three-way consistency check (does the registration surface match the repository definition):
   ```bash
   python3 scripts/check_hook_surface.py
   ```
3. Confirm the runtime script directory exists and is non-empty: `~/.claude/hooks/ai-team-os/`.
4. If the user's project has a `.claude/settings.local.json` whose `hooks` section contains
   `send_event.py`, that is a leftover that would double-fire events: show the content to the
   user first, and only delete that `hooks` section after obtaining their consent.

### repair mode

1. Explain that `~/.claude/settings.json` is about to be modified (back it up first).
2. Run:
   ```bash
   python3 install.py --update
   ```
   This command cleans up our old entries by a script-name allowlist, then rebuilds from
   `HOOK_SURFACE`; foreign hooks (such as the user's own guard scripts) are preserved as-is.
3. Re-run `python3 scripts/check_hook_surface.py` to confirm everything is green.

## Output format

### Status view
```
## Hooks Registration Status

Registration surface: ~/.claude/settings.json (global, the only one)
Registered <N> events / <M> entries (list the event names as actually read)

Runtime scripts: ~/.claude/hooks/ai-team-os/ (<K> scripts)
API target: http://localhost:<api_port.txt>/api/hooks/event
Three-way consistency check: ✅ install.py ↔ hooks.json ↔ README
```

## Notes

- Use English for all output
- Back up `~/.claude/settings.json` before modifying it, and preserve foreign hook entries
- Make sure the API service is running: `/os-up`
