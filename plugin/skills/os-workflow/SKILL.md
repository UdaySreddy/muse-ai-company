---
name: os-workflow
description: When using CC's built-in Workflow (ultracode) in an AI Team OS project, the standard practice for making workflow output write back to the OS. Use when the Leader is about to call the Workflow tool to orchestrate sub-agents.
---

# OS Workflow — Use CC Workflows, but Let the Output Flow Back into the OS

## Background

After you call Workflow, each internal agent is automatically registered by a hook as an OS team (`workflow-<wf_id>`; one workflow = one team). Tracking is automatic, but **the work content does not store itself** — you must do the following two things.

## 1. Put the overall task on the wall (Leader's duty, unchanged)

Before/after calling Workflow, register this work direction on the task wall with `task_create` and set it to running. The Leader handles decisions, design, and records; execution goes to the workflow — but **the accounts must be kept in the OS**. When done, `task_update` it to completed and fill in the result.

## 2. Embed a "write-back instruction" in every workflow agent's prompt

Paste the following snippet **at the end of the prompt of every `agent()` in the workflow script you write** (it is verified that workflow agents can call the OS's MCP tools + HTTP API; they are not sandboxed):

```
[Write back to the OS (mandatory at wrap-up)]
1. Load via ToolSearch: select:mcp__ai-team-os__task_memo_add,mcp__ai-team-os__report_save
2. After finishing your own work:
   - task_memo_add(task_id="<overall task id>", content="<what this step did + key conclusions>", memo_type="progress")
   - For important output, also report_save(...) it into the DB, and write the report_id into the memo
3. You are running in the project directory; the MCP automatically carries the project headers, so you do not need to worry about ports/project ids.
```

In the script, pass the `<overall task id>` obtained from step 1's `task_create` into the prompt via string interpolation.

### Script Writing Example

```js
// The Leader first task_creates to get the taskId (OS MCP), then writes the workflow:
const WRITEBACK = `\n[Write back to the OS (mandatory at wrap-up)]\n1. ToolSearch: select:mcp__ai-team-os__task_memo_add\n2. When done, task_memo_add(task_id="${taskId}", content="...", memo_type="progress")\n3. Running in the project directory; the MCP automatically carries the project headers.`

const r = await agent('Your actual task…' + WRITEBACK, { schema, label })
```

## 3. Model Tier Discipline (user ruling)

A two-tier system: **Fable = the orchestration layer** (coordination/architecture rulings/final review), **Opus = the execution layer** (all workers). Passing no model means inheriting the main session's model, so dispatching bare inside a Fable session burns the whole run at Fable rates:

- Every `agent()` **carries `model: 'opus'` explicitly by default** (a tier alias that floats to the latest Opus; never pin a model number);
- Only the **final review / adversarial ruling / hardest-fix** stages use `model: 'fable'` (usually paired with `effort: 'xhigh'`).

```js
// Typical layering: execution stages all opus; only the final-review stage is fable
const found = await parallel(ITEMS.map(x => () =>
  agent(findPrompt(x) + WRITEBACK, { model: 'opus', schema: FINDINGS })))
const verdict = await agent(judgePrompt(found) + WRITEBACK,
  { model: 'fable', effort: 'xhigh', schema: VERDICT })
```

Note: effort is chosen by the script author as needed; the governance layer sets no tier system for it. This discipline is a soft constraint only, with no hook hard block.

## 4. Structured Output Size Discipline (two production confirmations)

When giving an agent a `schema`, the prompt must state **explicit size hard constraints** (per-field character caps, item-count caps, "better concise than over the limit") — relying on the schema's `maxLength` alone cannot stop it: an agent that exceeds the limit falls into a StructuredOutput retry loop, and after exhausting the retry cap (5) the whole route dies and returns `null`.

- Give the schema's `maxLength` a safety margin; for long-text output, have the agent Write a file directly, and let the structured output return only a summary and the path.
- After a route dies, repair with resume: change only the failed route's prompt, leave the other routes' `(prompt, opts)` untouched, and replay from cache at zero cost.

## 0. First confirm ultracode is on

ultracode is not a resident mode; the user must enable it manually:

- Session not enabled → **prompt the user to enable it first**, then call Workflow; already enabled (confirmed by a system-reminder) → orchestrate directly.
- Ecosystem research output must be written back to the ecosystem tables (`ecosystem_apply_shallow_summary` / `ecosystem_apply_quality_review`), otherwise the ledger and the `/ecosystem` page cannot see it.

## Key Points

- Write-backs go through **MCP tools first** (automatic project isolation); HTTP `localhost:8000/api/*` is an equivalent fallback.
- The safety guardrails (dangerous commands/sensitive files/secret interception) apply to workflow agents as usual.
