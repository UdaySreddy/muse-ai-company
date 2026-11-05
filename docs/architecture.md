# AI Team OS Architecture

> The expanded version of the one-line "Storage → API → Dashboard" summary in CLAUDE.md.
> This document records only layers and file paths that really exist, and is maintained in sync as the code evolves.

## Overview: the main data flow

```
Claude Code (MCP client)
   │  stdio
   ▼
src/aiteam/mcp/          MCP Server (fastmcp, 113 tools)
   │  server.py registers the 16 submodules in tools/; _autostart.py automatically brings up uvicorn
   │  HTTP (port discovery: ~/.claude/data/ai-team-os/api_port.txt)
   ▼
src/aiteam/api/          FastAPI service (app.py + 37 route modules in routes/)
   │  Background tasks: state_reaper.py (state reaping/governance loop), wake_manager.py, event_bus.py
   ▼
src/aiteam/storage/      Storage layer (StorageRepository + SQLAlchemy async + SQLite)
   │  connection.py (with hand-written idempotent migrations), engine_pool.py, models.py
   │  Source of truth: ~/.claude/data/ai-team-os/aiteam.db (WAL)
   ▼
dashboard/               React 19 + Vite frontend (22 pages, Zustand state management)
                         Build artifacts in two copies: dashboard/dist (local build, gitignored)
                         and plugin/dashboard-dist (committed for distribution; the I3 machine check constrains their consistency)
```

## Side components

| Directory | Responsibility |
|------|------|
| `plugin/hooks/` | The **source of truth** for CC lifecycle hook scripts (send_event / session_bootstrap / context_tracker, etc.). `src/aiteam/hooks/` is the same-named mirror copy; the I1 machine check enforces byte-for-byte identity; install.py installs them to `~/.claude/hooks/ai-team-os/` |
| `src/aiteam/services/` | The ecosystem scanning subsystem (ecosystem_scanner / tagger / summarizer / deep_reviewer, etc.) |
| `src/aiteam/meeting/` | The meeting template system |
| `src/aiteam/memory/` | Memory system v2, two layers: the episodic layer task_memos (agent work logs, BM25 on-demand retrieval) + the direction layer memories (preferences/corrections, resident injection by two hooks) + on-demand reconcile tidying (coarse screening in reconcile.py, no vectors/no resident LLM); design in docs/memory-v2-design.md |
| `src/aiteam/loop/` | The task-wall engine and governance pieces (task_wall_engine / watchdog / auto_assign / completion_verifier / failure_alchemy / replay_engine / what_if). The loop state machine itself was retired in v1.10.3; the directory name is kept |
| `src/aiteam/cli/` | The Typer CLI (the `aiteam` entry point, subcommands in commands/) |
| `src/aiteam/config/` | pydantic-settings configuration (settings.py) |
| `src/aiteam/integrations/` | External integrations (notifier.py Slack webhook, etc.) |
| `scripts/check_invariants.sh` | 11 red-line machine checks: I1 hook dual copies / I1b leftover-copy ban / I2 version lockstep in five places / I3 dual dist consistency / I4 dist freshness / I5 venv ban / I6 README numbers (`check_readme_numbers.sh`) / I7 ruff gate / I8 hook registration surface unified (`check_hook_surface.py`) / I9 MCP tool parameter descriptions (`check_tool_param_descriptions.py`) / I10 table sets consistent (`check_schema_tables.py`) |

## Legacy / retired

- `src/aiteam/orchestrator/`: the LangGraph graph execution path, reachable only from the CLI `aiteam task run`,
  depending on the optional extra `[langgraph]`.
- The built-in pipeline has been **deleted as a whole domain** (executed 2026-07, decided in the same period): the OS transformed into the
  **persistent observation and governance layer** for ultracode/CC
  Workflow, and Workflow runs are automatically tracked by hooks as
  `workflow-<wf_id>` teams. The deletion scope includes `src/aiteam/pipeline/`, `loop/pipeline.py`,
  the pipeline REST routes, the `pipeline_gate` / `autopilot_auto_stop` hooks, the autopilot
  skill and 3 MCP tools. **Kept items** (do not clean up): the `pipeline_stage_history` table and its
  ORM (append-only history; writing stopped, never dropped), the `tasks.config['pipeline']` historical field slot,
  and the `task_create(task_type=...)` parameter (soft-retired no-op).

## The shared-types iron rule

All cross-module shared types are only defined/referenced in `src/aiteam/types.py` (a CLAUDE.md core constraint).

## Runtime file locations

| File | Path |
|------|------|
| Main database | `~/.claude/data/ai-team-os/aiteam.db` |
| API port discovery | `~/.claude/data/ai-team-os/api_port.txt` |
| MCP debug log | `~/.claude/data/ai-team-os/mcp-debug.log` |
| API PID file | `<tmpdir>/aiteam-api.pid` (_autostart.py) |
| API startup lock | `<tmpdir>/aiteam-api-startup.lock` |
| Installed hooks | `~/.claude/hooks/ai-team-os/` |

## Installation and distribution

- Recommended source install: `python install.py` (copies hooks, registers settings.json, system Python with no venv — the four process types share dependencies, and venv isolation has been rejected).
- Plugin/marketplace: the source of the root `.claude-plugin/marketplace.json` points at `./plugin`.
