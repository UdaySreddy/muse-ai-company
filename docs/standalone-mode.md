# Standalone Mode — AI Team OS without Claude Code

AI Team OS was built as a Claude Code layer, but its core does not need
Claude Code:

- The **API server, dashboard, task wall, meetings and memory** are plain
  FastAPI + SQLite. They run anywhere Python 3.11+ runs.
- **Task execution** (the LangGraph coordinate/broadcast graphs) calls an LLM
  through LangChain. That was always a *direct API* call — Claude Code was
  never in that loop. What was missing was a supported way to choose the
  model/provider, and a company team that exists without CC sessions.

Standalone mode adds exactly those two pieces, plus the Muse company org.

## The company

| Role | Who | Job |
|---|---|---|
| Chairman | You (human) | Mission, strategy, approvals for external/irreversible actions |
| CEO | The graph's Leader nodes | Plan → delegate → synthesize the final result |
| COO | `coo` agent (also a CC template: `management-chief-operating-officer`) | Operations: sequencing, follow-ups, delivery tracking, blocker escalation |
| CTO | `cto` agent | Architecture, technical decisions, quality bar |
| Backend / Frontend / QA | engineer agents | Build and verify |

The team is created on first run as `muse-company` and reused afterwards
(idempotent — see `src/aiteam/standalone.py`).

## Quick start

```bash
pip install 'ai-team-os[langgraph]'

# Offline smoke run — no API key, no network, no Claude Code:
aiteam standalone org
aiteam standalone run --task "Draft a launch plan for our app" --provider fake

# Real run via direct Anthropic API:
export ANTHROPIC_API_KEY=sk-ant-...
aiteam standalone run --task "Draft a launch plan for our app" --provider anthropic

# Or OpenAI (needs: pip install langchain-openai):
export OPENAI_API_KEY=sk-...
aiteam standalone run --task "..." --provider openai
```

Provider/model can also be set by environment: `AITEAM_LLM_PROVIDER`,
`AITEAM_LLM_MODEL`. The dashboard/API run as before (`aiteam up`) and show
the same database the standalone runner writes to.

Programmatic use:

```python
from aiteam.standalone import run_company_task

result = await run_company_task("Draft a launch plan", provider="anthropic")
print(result.status, result.result, result.agent_outputs)
```

## How it works

- `src/aiteam/orchestrator/llm_factory.py` builds the chat model
  (`anthropic` / `openai` / `fake`) and resolves an explicitly injected
  model first.
- Orchestrator nodes (`leader_node`, `agent_node`, `reducer_node`) now call
  `resolve_llm(configurable, model)` instead of hard-coding
  `ChatAnthropic(...)`. With nothing configured, behaviour is unchanged.
- `TeamManager.run_task(..., llm=...)` or `llm_provider=...` passes the
  model through the graph's runtime config.

## What still needs Claude Code (by design)

These features *observe or drive Claude Code itself*, so they have no
standalone meaning: plugin hooks, CC session fleet/wake (`claude -p
--resume`), CC workflow ingest from `~/.claude` transcripts, and the
`plugin/agents` templates as CC sub-agents. In standalone mode they are
simply unused — nothing in the standalone path reads `~/.claude` or
requires a `claude` binary.
