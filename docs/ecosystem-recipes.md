# AI Team OS Ecosystem Integration Recipes

> AI Team OS positions itself as a "meta Plugin that orchestrates other Plugins/MCPs". This document provides integration recipes with mainstream development tools, helping you quickly build a working environment suited to your own team.
>
> **Principle**: integrate by combining existing MCP servers rather than writing code — AI Team OS handles orchestration, third-party MCPs handle execution.

---

## Table of Contents

- [Recipe 1: GitHub integration](#recipe-1-github-integration)
- [Recipe 2: Slack integration](#recipe-2-slack-integration)
- [Recipe 3: Linear integration](#recipe-3-linear-integration)
- [Recipe 4: Full-stack development team template](#recipe-4-full-stack-development-team-template)
- [Recipe combination matrix](#recipe-combination-matrix)
- [Troubleshooting](#troubleshooting)

---

## Recipe 1: GitHub Integration

Combine GitHub with AI Team OS to automate the orchestration of code management, PR review and Issue tracking.

### Recommended MCP

| MCP Server | Source | Notes |
|-----------|------|------|
| `github` | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers/tree/main/src/github) | The official GitHub MCP server, providing complete GitHub API capabilities |

### Installation configuration

Add the GitHub MCP server to `.mcp.json`:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<your-token>"
      }
    }
  }
}
```

**Getting a token**: GitHub Settings > Developer Settings > Personal access tokens > create a token, checking the `repo`, `issues` and `pull_requests` permissions.

### How it works with AI Team OS

#### Scenario A: Automatic commit + PR creation at the delivery stage

```
Workflow:
  1. An Agent finishes writing the code
  2. The Leader uses task_update to advance the task to the delivery state
  3. The Agent directly runs git add/commit with Bash (the agent has Bash in hand anyway)
  4. The GitHub MCP creates a Pull Request and pushes it to GitHub
```

OS toolchain:
- `task_update` — advances task state; the delivery node is visible on the task wall
- `task_list_project` — view this project's items awaiting delivery
> The git trio (`git_auto_commit` / `git_create_pr` / `git_status_check`) was retired in v1.10.3
> — a three-layer git wrapper only narrows CC's built-in Bash capability; commits and PR opening go directly to
> Bash + the GitHub MCP.

#### Scenario B: Code Review double review

```
Workflow:
  1. A development Agent submits code
  2. AI Team OS debate_code_review initiates an internal code review
  3. After the review passes, a GitHub PR is created
  4. The GitHub MCP reads the PR comments, supplementing external review feedback
  5. The final merge decision is coordinated by the Leader
```

OS toolchain:
- `debate_code_review` — AI internal code review (multi-perspective debate)
- `task_memo_add` — record the review conclusion

#### Scenario C: Bidirectional mapping between Issues and the task wall

```
Mapping rules:
  GitHub Issue (open)      ↔  AI Team OS task (todo/in_progress)
  GitHub Issue (closed)    ↔  AI Team OS task (completed)
  GitHub Issue label       ↔  AI Team OS task priority/tags
  GitHub Issue assignee    ↔  AI Team OS Agent assignee
```

Practical advice:
- At the start of each sprint, the Leader syncs tasks from GitHub Issues to the task wall
- After an Agent completes a task, the Leader closes the corresponding GitHub Issue
- Use `task_memo_add` to record the GitHub Issue link in the task

---

## Recipe 2: Slack Integration

Combine Slack with AI Team OS to automatically push team notifications, alerts and standup summaries.

### Recommended MCP

| MCP Server | Source | Notes |
|-----------|------|------|
| `slack` | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers/tree/main/src/slack) | The official Slack MCP server, supporting channel message reading and writing |

### Installation configuration

Add the Slack MCP server to `.mcp.json`:

```json
{
  "mcpServers": {
    "slack": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-slack"],
      "env": {
        "SLACK_BOT_TOKEN": "<your-bot-token>",
        "SLACK_TEAM_ID": "<your-team-id>"
      }
    }
  }
}
```

**Getting a token**: Slack App admin page > create an App > OAuth & Permissions > add the `chat:write`, `channels:read` and `channels:history` permissions > install to the workspace > copy the Bot Token.

### How it works with AI Team OS

#### Scenario A: Leader briefing push

```
Workflow:
  1. At the wrap-up of a round of work, the Leader calls team_briefing to generate a team briefing
  2. The briefing content is pushed through the Slack MCP to the designated channel
  3. Team members view the project progress in Slack
```

OS toolchain:
- `team_briefing` — generate a team work briefing
- `briefing_list` — view historical briefings (supports filtering by project/tag)
> The push itself goes to the Slack MCP: the OS's built-in `send_notification` was retired in v1.10.3; the
> webhook configuration file it depended on was never created, so calling it was guaranteed to fail.

#### Scenario B: Task failure alerts

```
Workflow:
  1. A Watchdog inspection finds a BUSY-timed-out agent / a long-PENDING task
  2. The Leader uses diagnose_task_failure to get the root cause
  3. An alert message is sent through the Slack MCP to the #ops-alerts channel
  4. Including: the failed task, the root cause, and a suggested action
```

OS toolchain:
- `diagnose_task_failure` — single-task failure diagnosis (returns a diagnosis only, writes no state)
- `failure_analysis` — extract the root cause and write the antibody into project memory
- `briefing_add` — enter the decision queue when your ruling is needed (with tags for easy filtering)
> The SRE error-budget model (`error_budget_*`) and `os_report_issue` were retired in v1.10.3:
> the former's data directory never had a single file in its whole life; the latter historically had 0 issues.

#### Scenario C: Daily standup summary

```
Workflow:
  1. The Leader calls task_list_project to get the task wall's state
  2. Summarize: completed yesterday / planned today / blockers
  3. Format as a standup summary and push to the #standup channel
  4. Can be sent together with the meeting minutes from meeting_conclude
```

OS toolchain:
- `task_list_project` — view the task wall's global view (passing team_id gives a single-team view)
- `meeting_conclude` — generate meeting minutes
- `task_status` — view a single task's current state

---

## Recipe 3: Linear Integration

Combine Linear with AI Team OS to sync tasks between project management tools.

### Recommended MCP

| MCP Server | Source | Notes |
|-----------|------|------|
| `linear` | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers/tree/main/src/linear) | The Linear MCP server, supporting Issue and Project operations |

### Installation configuration

Add the Linear MCP server to `.mcp.json`:

```json
{
  "mcpServers": {
    "linear": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-linear"],
      "env": {
        "LINEAR_API_KEY": "<your-api-key>"
      }
    }
  }
}
```

**Getting an API Key**: Linear Settings > API > Personal API keys > create a new key.

### How it works with AI Team OS

#### Scenario A: Syncing Linear Issues with AI Team OS tasks

```
Mapping rules:
  Linear Issue          ↔  AI Team OS task_create
  Linear Issue status   ↔  task_update status
  Linear assignee       ↔  Agent assignee
  Linear priority       ↔  task priority
  Linear label/project  ↔  task tags
```

Practical advice:
- At sprint start, sync Issues from Linear to the AI Team OS task wall
- After an Agent completes a task, update the Linear Issue's status
- Use task_memo to record the Linear Issue ID and establish the association

#### Scenario B: Mapping Sprints to the task wall

```
Linear Sprint stage     →  AI Team OS task state
  Backlog               →  pending
  In Progress           →  running
  In Review             →  running (in review; marked with tags)
  Done                  →  completed
```

OS toolchain:
- `task_create` / `task_update` — create tasks and advance state
- `task_list_project` — view whole-wall progress by project
> The Pipeline subsystem (`pipeline_create` / `pipeline_advance` / `pipeline_status`) was retired
> as a whole domain in v1.10.3; stage orchestration is now borne by CC's native Workflow, with the OS side using `workflow_list` /
> `workflow_get` for persistent observation.

#### Scenario C: Bidirectional state sync

```
Workflow:
  1. A new Issue is created in Linear → the Leader task_creates the corresponding task in the OS
  2. A task's state changes in the OS → the Leader updates the Issue's state through the Linear MCP
  3. A task completes in the OS → the Leader closes the Linear Issue and records a completion summary
```

---

## Recipe 4: Full-Stack Development Team Template

A complete full-stack development team configuration, combining AI Team OS + GitHub + Slack, with preset frontend, backend, test and DevOps roles.

### Recommended combination

| Component | Use |
|------|------|
| AI Team OS | Team orchestration, task management, loop driving |
| GitHub MCP | Code management, PRs, Issues |
| Slack MCP | Team notifications, alerts, standups |
| Superpowers Skill | Enforced TDD + git workflow discipline |
| VibeSec Skill | Code security review |

### MCP configuration

A complete `.mcp.json` example:

```json
{
  "mcpServers": {
    "ai-team-os": {
      "command": "python",
      "args": ["-m", "aiteam.mcp"],
      "env": {
        "AITEAM_DB_PATH": "./aiteam.db"
      }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<your-token>"
      }
    },
    "slack": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-slack"],
      "env": {
        "SLACK_BOT_TOKEN": "<your-bot-token>",
        "SLACK_TEAM_ID": "<your-team-id>"
      }
    }
  }
}
```

### Preset team configuration

Create a full-stack team using AI Team OS:

```
# Step 1: Create the project (the only step you need to do explicitly)
project_create(name="my-fullstack-app", root_path="/path/to/project")

# Step 2: Dispatch agents directly — team and member rows are built automatically by the hook chain
#   This session's container team is automatically taken in when the first agent is born; no manual team creation/registration.
Agent(subagent_type="frontend-developer", name="frontend-dev", model="opus",
      prompt="Develop the UI components…")
Agent(subagent_type="backend-architect",  name="backend-dev",  model="opus",
      prompt="Develop the API endpoints…")
Agent(subagent_type="testing-qa-engineer", name="qa-engineer", model="opus",
      prompt="Fill in E2E coverage…")
Agent(subagent_type="engineering-devops-automator", name="devops", model="opus",
      prompt="Build images and CI…")

# Step 3: See who is there (optional)
agent_list()
agent_template_recommend(task_type="fullstack")
```

> `team_create` and `agent_register` were retired in v1.10.3: a team created by the former has no CC-side directory and
> is reaped the next round after being built; the latter creates empty-shell rows with no process behind them (agents with
> `source='api'` in the whole database number 0). Take-in is completed fully automatically by the SubagentStart hook.

### A typical workflow

```
Sprint start
  │
  ├── Leader: sync tasks from GitHub Issues / Linear to the task wall
  ├── Leader: task_list_project takes the next item by priority
  │
  ├── Development stage
  │   ├── frontend-dev: develop UI components (with the Frontend-Design Skill)
  │   ├── backend-dev: develop API endpoints (with Superpowers TDD)
  │   ├── VibeSec: code security scan
  │   └── Leader: monitor progress through task_list_project
  │
  ├── Review stage
  │   ├── debate_code_review: AI internal code review
  │   ├── GitHub MCP: create a GitHub PR
  │   └── qa-engineer: run E2E tests
  │
  ├── Deployment stage
  │   ├── devops: build Docker images
  │   ├── Bash: git commit the final code
  │   └── Leader: task_update advances it to delivery complete
  │
  └── Retrospective
      ├── meeting_create: hold a retrospective meeting
      ├── team_briefing: generate a briefing → push to Slack
      └── meeting_conclude: record the meeting minutes
```

---

## Recipe Combination Matrix

Recommended combinations for different team sizes and scenarios:

| Scenario | Required | Recommended | Optional |
|------|------|------|------|
| Individual developer | AI Team OS | GitHub MCP | Superpowers |
| Small team (2-5 people) | AI Team OS + GitHub | Slack MCP | Linear, VibeSec |
| Full-stack team | AI Team OS + GitHub + Slack | Superpowers + VibeSec | Linear, Frontend-Design |
| Open-source project | AI Team OS + GitHub | code-review Skill | Slack MCP |
| Data science team | AI Team OS | Jupyter MCP | claude-mem |

---

## Troubleshooting

### Q: An MCP server fails to start

**A**: Confirm the dependencies are installed:
```bash
# The GitHub/Slack/Linear MCPs all need Node.js
node --version  # needs >= 18

# Check npx is available
npx --version
```

### Q: Insufficient GitHub Token permissions

**A**: Confirm the token contains the following scopes:
- `repo` — repository access
- `read:org` — organization information (if needed)
- `workflow` — GitHub Actions (if needed)

### Q: Slack message sending fails

**A**: Check the following points:
1. Whether the Bot Token has the `chat:write` permission
2. Whether the Bot has been invited to the target channel (`/invite @your-bot`)
3. Whether `SLACK_TEAM_ID` is correct (check it in the Slack admin panel)

### Q: Multiple MCP servers consume too much context

**A**: AI Team OS's advice is to **enable on demand**:
- Do not enable all MCP servers at the same time
- Only add the corresponding server to `.mcp.json` when needed
- If the context approaches its cap, prioritize keeping AI Team OS's core functions

### Q: How to view the available integration recipes

**A**: Use AI Team OS's built-in tools:
```
find_skill(level=2, category="integration")              # View all recipes
find_skill(level=3, skill_id="github-integration")       # View a specific recipe
find_skill(level=3, skill_id="fullstack-team")           # View the full-stack team template
```

> The recipes were merged into `find_skill`'s `integration` category on 2026-07-27 (the original `ecosystem_recipes`
> tool was retired) — "what is in the ecosystem and how to install it" is one and the same thing, and no longer occupies a tool name each.
