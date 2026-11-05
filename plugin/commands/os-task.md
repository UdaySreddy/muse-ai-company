---
name: os-task
description: AI team task management — view the task wall, create tasks, view task details
---

# /os-task — Task Management

Help the user manage the AI Team OS task wall.

## Usage

- `/os-task` — View the current project's task wall
- `/os-task <team_id|team name>` — View only one team's task wall
- `/os-task new <description>` — Put something on the task wall
- `/os-task <task_id>` — View a single task's details and progress records

## Workflow

### No arguments: view the task wall

Call `task_list_project()` (with no arguments it uses the current active project). It returns tasks grouped into short/mid/long tiers, sorted by score within each group; present them in exactly that order — that ordering is the answer to "what should be done next", so do not re-sort it yourself.

To view only one team, pass `task_list_project(team_id="<team name or ID>")`.

### new mode: put a task on the wall

1. Confirm two things (do not ask again if the user already provided them): the task description and the target team.
   Get teams with `team_list()`; if there is only one active team, use it directly.
2. Call `task_create(...)`; to attach it to a team immediately with priority/time tier,
   use `task_run(team_id=..., description=..., priority=..., horizon=...)`.
3. **A task on the wall ≠ someone is working on it**: the OS does not execute tasks by itself. To move it forward, dispatch someone with CC's `Agent(...)`, and state the task_id in the prompt so they can write progress back with `task_memo_add`.

### View mode: task details

1. `task_status(task_id)` — status, ownership, dependencies, result.
2. `task_memo_read(task_id)` — historical progress records (must-read before taking over).
3. For the full timeline: `task_execution_trace(task_id)`; for duration/step statistics as well,
   `task_execution_trace(task_id, include_stats=True)`.

## Output format

### Task wall
```
## Project Task Wall (short / mid / long)

### short
| Priority | Task ID | Title | Status | Assigned to |
|--------|--------|------|------|--------|
| critical | 3f2a… | Fix login 500 | running | backend-dev |
```

### Task details
```
## Task 3f2a…

Status: running   Priority: critical   Owner: dev-team
Dependencies: none        Assigned to: backend-dev

### Progress Records
- [progress] Reproduced; traced to the session middleware
- [decision] Going with option A, because…
```

## Notes

- Use English for all output
- Mark statuses in English: pending=pending, running=in progress, blocked=blocked,
  completed=completed, failed=failed
- The task wall defaults to a compact view (`view: "compact"`) — that is deliberate trimming, not missing fields;
  pass `fields="all"` for all fields, and use `task_status` for a single task's full information
