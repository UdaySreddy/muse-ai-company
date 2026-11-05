# AI Team OS — Hooks Migration Guide

## Phase 1 (current): Coexistence mode

The plugin `hooks.json` coexists with the hooks configuration in the global `~/.claude/settings.json`.

- **Global hooks** (`~/.claude/hooks/ai-team-os/send_event.py`, installed by install.py): existing users are unaffected
- **Plugin hooks** (`plugin/hooks/send_event.py`): take effect automatically once the plugin is installed

The two configurations point to their own scripts; the logic is identical and they do not conflict.

## Phase 2 (future): Plugin takes over

Once the plugin hooks fully take over, you can remove the OS-related hooks from the global settings.json:
1. Edit `~/.claude/settings.json`
2. Delete the `send_event.py` entries from hooks (keep other third-party hooks)
3. The plugin hooks.json automatically provides coverage for all events

## Comparison

| Item | Global settings.json | Plugin hooks.json |
|------|-------------------|-------------------|
| Path | `~/.claude/hooks/ai-team-os/send_event.py` | `plugin/hooks/send_event.py` |
| timeout | None (default 5s) | 2000ms |
| PreToolUse matcher | `*` | `Agent\|Bash\|Edit\|Write` |
| session_bootstrap | No | Yes (API health check) |
| Payload size protection | Field-level truncation only | Field-level truncation + overall 32KB cap |

## Coexistence with third-party hooks

The global `settings.json` may contain third-party hooks (e.g. `observe.ps1`); they do not interfere with the plugin hooks:
- Third-party hooks are managed by the global settings.json; the plugin never touches them
- Plugin hooks use a separate `hooks.json` containing only AI Team OS events
- CC executes all hooks for the same event in order; there is no override conflict

## Behavior when the API is unavailable

- `send_event.py`: HTTP request timeout 1.5s; on failure it writes to stderr and does not block CC
- `session_bootstrap.py`: API check timeout 1s; when unreachable it prints a stderr hint to run `/os-up`
- While the API is restarting: hook events are lost (fire-and-forget) and resume automatically after the restart
