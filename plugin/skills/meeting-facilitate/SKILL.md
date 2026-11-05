---
name: meeting-facilitate
description: Organize the full lifecycle of a multi-agent meeting — create the meeting, spawn real participants, advance rounds, run attendance checks, and conclude with a summary. Use when multi-party collaboration is needed for decisions, proposal reviews, debating disagreements, project retrospectives, brainstorming, or proposal evaluation.
---

# Meeting Facilitate — Meeting Facilitation Skill

This skill guides you (the Leader or an agent with facilitation duties) to organize a multi-agent meeting end to end: choose a template → create the meeting → spawn real participants → attendance check → advance rounds → verify everyone has spoken → conclude and summarize.

## Prerequisites

- Know your own `agent_id` (used to speak as the facilitator) — it is given by the "Your OS Identity" block injected by SubagentStart; if you cannot get it, look it up yourself with `GET /api/agents/whoami?name=<your name>&session_id=<session id>` (the server reverse-looks-up by cc id / session+name)
- Have clarified: the meeting's purpose, the roles needed, and the target output
- Know the key file paths involved in the meeting (for materials/context_files)

## Core Principles (read these three before going further)

1. **The OS does not spawn participants automatically** — `meeting_create` only creates the meeting record and a dispatch_plan; **getting participants to actually attend requires you to call the Agent tool yourself**. Creating without spawning = nobody attends = the meeting fails.
2. **Never speak on someone else's behalf** — when you speak as the facilitator, both `agent_id` and `caller_agent_id` must be **your own ID**. Speaking with someone else's `agent_id` gets the message flagged by the OS as `impersonation=true` and written into the audit log.
3. **Before concluding, confirm everyone has spoken** — `meeting_conclude` has `validate_attendance` on by default; participants who have not spoken will make conclude return 400. **Do not bypass it with `force=True`** — unless there is a force-majeure technical reason.

---

## Facilitation Process (7 steps)

### Step 1: Choose a meeting template

Choose a template from the table below based on the meeting's purpose. See `templates/<name>.md` for detailed template descriptions (progressive disclosure).

| Purpose | Recommended template | Rounds | Why |
|------|---------|------|------|
| Diverge creatively, generate new ideas | `brainstorm` | 4 | Independent divergence → cross-pollination → evaluation → summary |
| Choose among multiple options | `decision` | 3 | Presentations → cross-examination → convergence |
| Review code / PRs / deliverables | `review` | 3 | Presentation → independent review → response and ruling |
| Project retrospective, extract lessons | `retrospective` | 3 | 4Ls → improvement directions → commitment plan |
| Daily progress sync | `standup` | 1 | Three questions: done / planned / blockers |
| Major disagreement or risk in a decision | `debate` | 4 | Advocate's case → critic's challenge → advocate's response → verdict |
| Open agenda, free topics | `lean_coffee` | 3 | Topic collection → voting → timeboxed discussion |
| Architecture / proposal multi-perspective review | `council` | 3 | Expert perspectives → cross-examination → verdict |

Unsure? Use `template="free"` and the OS will automatically recommend one based on `topic` keywords.

### Step 2: Create the meeting (get the dispatch_plan)

**You must use structured `participants`** (a list of dicts); otherwise the `launch_call` in the dispatch_plan will be empty and not ready-to-paste.

```
meeting_create(
    topic="Review the v0.9 Prompt Registry architecture proposal",
    template="council",                           # The template chosen in Step 1
    team_id="repo-insight-arch",                  # May be omitted; the active team is used automatically
    team_name="repo-insight-arch",                # OS-side ownership only; not written into launch_call
    participants=[
        {
            "name": "arch-lead",
            "agent_template": "software-architect",
            "role": "Evaluate the architecture's overall feasibility and layering soundness",
            "context_files": ["docs/v0.9-prompt-registry.md"],
            "expected_output": "Three parts: feasibility / risks / recommendations",
        },
        {
            "name": "backend-arch",
            "agent_template": "backend-architect",
            "role": "Evaluate the storage layer and API design",
            "context_files": ["docs/v0.9-prompt-registry.md", "src/aiteam/storage/repository.py"],
            "expected_output": "Storage plan + interface contract + migration path",
        },
    ],
    rounds=[                                       # Optional; omit to use the template's default rounds
        {"topic": "Position statements", "rule": "3 paragraphs each: evaluation perspective / risk points / score 1-5"},
    ],
    materials=["docs/v0.9-prompt-registry.md"],   # Mandatory reading for everyone
)
```

Return structure (key fields):

```
{
    "data": {"id": "mtg-abc123", ...},
    "dispatch_plan": [
        {
            "participant": "arch-lead",
            "launch_call": {
                "tool": "Agent",
                "params": {
                    "subagent_type": "software-architect",
                    "name": "arch-lead",
                    "model": "opus",
                    "description": "Evaluate the architecture's overall feasibility and layering soundness",
                    "prompt": "<the complete prompt generated by the OS, including meeting_id / role / mandatory materials / speaking rules / a meeting_send_message call example / the SendMessage instruction after finishing>",
                },
            },
            "ready_to_paste": True,
        },
        ...
    ],
    "expected_participants": ["arch-lead", "backend-arch"],
    "attendance_check_command": "meeting_attendance_check(meeting_id='mtg-abc123')",
}
```

Record the `meeting_id`; every later step needs it.

### Step 3: Spawn every participant (the most critical step)

> ⚠️ **This is the key to the whole process. Skipping this step = nobody attends the meeting = every later step fails.**

Iterate over the `dispatch_plan` and, for each item with `ready_to_paste=True`, call the Agent tool, passing **`launch_call.params` wholesale as the Agent tool's arguments**:

```
for item in dispatch_plan:
    if not item["ready_to_paste"]:
        # Old string format: re-run meeting_create with structured parameters
        continue
    Agent(**item["launch_call"]["params"])
```

**Constraints:**
- **Do not modify the `prompt` field's content** — the OS has already preset the meeting_id, role, mandatory materials, speaking rules, a `meeting_send_message` call example, and the `SendMessage("spoken")` instruction after finishing. Manual edits break the closed loop.
- **Do not omit any participant** — miss one, and Step 7's conclude will be stopped by the attendance check.
- Multiple spawns can be **issued in parallel** (several Agent calls in one message) to speed up attendance.

### Step 4: Attendance — wait for everyone to speak

After each participant sends `SendMessage("finished speaking")`, call:

```
meeting_attendance_check(meeting_id="mtg-abc123")
```

Returns:

```
{
    "round": 1,
    "expected": ["arch-lead", "backend-arch"],
    "spoken": ["arch-lead"],
    "pending": ["backend-arch"],
    "timeout_in_seconds": 180
}
```

**Decide the action from `pending`:**

| pending state | timeout | Action |
|------------|---------|------|
| Empty | — | ✅ Everyone attended; proceed to Step 5 |
| Non-empty | < 5 minutes | ⏳ Keep waiting |
| Non-empty | ≥ 5 minutes | 🔁 Nudge once via SendMessage; if there is still no response, re-run Step 3's spawn for the agents in the pending list |

### Step 5: Facilitator statement (optional but recommended)

At the start or end of each round, you may speak in your facilitator capacity to guide the discussion:

```
meeting_send_message(
    meeting_id="mtg-abc123",
    agent_id="team-lead",                # Your own agent_id
    agent_name="team-lead",
    caller_agent_id="team-lead",         # ⚠️ Must match agent_id
    round_number=1,
    content="[Facilitator] Round 1: everyone has spoken. Consensus: xxx; disagreement: yyy. Next round, please focus on solutions for yyy.",
)
```

> ⚠️ **Impersonation warning:** if `caller_agent_id ≠ agent_id`, the OS flags the message as `impersonation=true` and writes it to the `meeting.impersonation` event log. **Never speak on someone else's behalf** — even if you only want to "help fill in a paragraph".

### Step 6: Advance to the next round

After everyone has spoken in Round 1, enter Round 2/3:

1. Send a facilitator summary message using Step 5's approach, clearly stating the move to the next round and the new round's speaking requirements
2. **Re-spawn the participants for the new round** (agents usually exit after finishing Round 1 and need to be re-invoked)
   - Note: the prompt generated by `meeting_create` defaults to Round 1; when entering Round 2 you need to construct the prompt manually or state this round's rules in the description
3. Return to Step 4 to wait for attendance

### Step 7: Conclude the meeting

After confirming this round's `attendance_check` has an empty `pending`:

```
meeting_conclude(
    meeting_id="mtg-abc123",
    summary="Consensus: adopt option A; to-do: backend-arch to produce the storage schema before 03-20; leftover risk: dual-write consistency during migration needs further verification.",
)
```

**Default behavior:**
- `validate_attendance=True` (default) — if not everyone has spoken, returns 400 + a missing list
- `force=False` (default) — skipping the check is not allowed

**On a 400 return:** do not immediately use `force=True`. First:
1. Call `meeting_attendance_check` to see who is absent
2. Re-spawn the absent participants or follow up with them
3. Only consider `force=True` if attendance is truly impossible (it triggers a `meeting.forced_conclude_with_missing` event and leaves an audit trail)

On success, the OS automatically stores the `summary` in team memory, retrievable via `memory_search` or `team_briefing`.

---

## Anti-Patterns (absolutely forbidden)

- ❌ **Skipping Step 3's spawn** — only calling `meeting_create` and then speaking for all participants yourself. This is historically the most serious incident pattern.
- ❌ **Speaking on others' behalf** — calling `meeting_send_message` with someone else's `agent_id`. It gets flagged as impersonation and recorded in the audit log.
- ❌ **Concluding an empty meeting** — concluding with 0 statements will be rejected with a 400.
- ❌ **Abusing `force=True`** — `force=True` is an escape hatch, not an everyday tool; every use writes an event log entry.
- ❌ **Modifying `dispatch_plan.launch_call.params.prompt`** — the OS has already tuned the closed loop; manual edits break the `meeting_send_message` call chain.
- ❌ **Using the old string format `participants=["a", "b"]`** — `launch_call` will be empty, `ready_to_paste=False`, and Step 3 cannot execute.
- ❌ **Forgetting to write a summary after the meeting** — without a summary there is no decision record in team memory, and the next retrospective relies on archaeology.

---

## End-to-End Examples

### Example 1: Architecture Council review

Scenario: reviewing the v0.9 Prompt Registry design doc, needing evaluations from arch-lead + backend-arch + ai-arch.

```
# Step 1: Choose the template — council (multi-perspective expert review)

# Step 2: Create the meeting
result = meeting_create(
    topic="Council review: v0.9 Prompt Registry architecture",
    template="council",
    team_id="repo-insight-arch",
    team_name="repo-insight-arch",
    materials=["docs/v0.9-prompt-registry.md"],
    participants=[
        {"name": "arch-lead", "agent_template": "software-architect",
         "role": "Evaluate the overall layering and evolvability",
         "context_files": ["docs/architecture.md"],
         "expected_output": "Three parts: layering evaluation / evolution risks / score 1-5"},
        {"name": "backend-arch", "agent_template": "backend-architect",
         "role": "Evaluate storage and API contracts",
         "context_files": ["src/aiteam/storage/repository.py"],
         "expected_output": "Storage plan / interface contracts / score 1-5"},
        {"name": "ai-arch", "agent_template": "ai-engineer",
         "role": "Evaluate the impact of prompt versioning on model behavior",
         "context_files": [],
         "expected_output": "Effect retention / rollback strategy / score 1-5"},
    ],
)
meeting_id = result["data"]["id"]

# Step 3: Spawn the three participants (critical!)
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])

# Step 4: Wait + attendance check
status = meeting_attendance_check(meeting_id=meeting_id)
# Continue once pending=[]

# Step 5: Facilitator guidance (optional)
meeting_send_message(
    meeting_id=meeting_id, agent_id="team-lead", agent_name="team-lead",
    caller_agent_id="team-lead", round_number=1,
    content="[Facilitator] Round 1: all three evaluations are in; entering Round 2 cross-examination.",
)

# Step 6: Advance Round 2, Round 3...

# Step 7: Conclude
meeting_conclude(
    meeting_id=meeting_id,
    summary="All three parties APPROVE, conditions: the storage dual-write migration plan backend-arch proposed needs a supplementary ADR; ai-arch requires a canary validation of the rollback strategy.",
)
```

### Example 2: Decision debate (debate template)

Scenario: deciding whether BM25 should use Tantivy or Whoosh, with disagreement.

```
result = meeting_create(
    topic="Decision debate: Tantivy or Whoosh for BM25",
    template="debate",
    participants=[
        {"name": "perf-advocate", "agent_template": "backend-architect",
         "role": "Advocate: argues for Tantivy (performance first)",
         "context_files": ["benchmarks/bm25_compare.md"],
         "expected_output": "Proposal + data + benefits + limitations"},
        {"name": "simple-critic", "agent_template": "code-reviewer",
         "role": "Critic: challenges the complexity of introducing the Rust dependency via Tantivy",
         "context_files": ["benchmarks/bm25_compare.md"],
         "expected_output": "Quote the advocate's words + risk levels + alternatives"},
        {"name": "team-lead", "agent_template": "team-lead",
         "role": "Judge",
         "context_files": [],
         "expected_output": "Points adopted / final verdict / Action Items"},
    ],
)
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])
# ... Steps 4-7 follow
```

### Example 3: Sprint retrospective (retrospective template)

Scenario: M6 phase ends; a whole-team retrospective.

```
result = meeting_create(
    topic="M6 retrospective: reporting system DB refactor and Dashboard isolation",
    template="retrospective",
    materials=["docs/m6-summary.md"],
    participants=[
        {"name": "backend-dev", "agent_template": "backend-architect",
         "role": "Backend development perspective", "context_files": [], "expected_output": "1 item for each of the 4Ls"},
        {"name": "frontend-dev", "agent_template": "frontend-developer",
         "role": "Frontend development perspective", "context_files": [], "expected_output": "1 item for each of the 4Ls"},
        {"name": "qa", "agent_template": "qa-engineer",
         "role": "Testing perspective", "context_files": [], "expected_output": "1 item for each of the 4Ls"},
    ],
)
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])
# ... Step 4 attendance → Step 6 advance to Round 2 improvement directions → Round 3 commitment plan → Step 7 conclude
```

---

## Troubleshooting

### `dispatch_plan` is empty or `ready_to_paste=False`
**Cause:** the old string participants format `participants=["arch-lead"]` was used.
**Fix:** switch to the structured dict format (see Step 2) and re-run `meeting_create`.

### `meeting_attendance_check`'s pending never decreases
**Cause:** the spawned agent exited without successfully calling `meeting_send_message` (perhaps the prompt was tampered with, or the agent drifted off task).
**Fix:**
1. `meeting_read_messages(meeting_id=...)` to see which messages actually arrived
2. Re-run Step 3's spawn for the agents in the pending list
3. If it keeps failing, check whether you manually modified the dispatch_plan

### `meeting_conclude` returns 400 + a missing list
**Cause:** Step 4 concluded before everyone had spoken.
**Fix:** do not use `force=True`. First re-spawn the agents in the missing list, wait for them to speak, then conclude.

### Seeing a `meeting.impersonation` event
**Cause:** some `meeting_send_message` call had a `caller_agent_id` inconsistent with its `agent_id`.
**Fix:** check all `meeting_send_message` calls and ensure the two IDs match. If the Leader is sending a system notification on behalf, set both to `team-lead`.

### Nobody speaks in Round 2
**Cause:** Round 1's agents exited after speaking, so nobody is present in Round 2.
**Fix:** Step 6 must re-spawn the participants for the new round and state this round's speaking rules in the prompt/description (because the OS's default prompt is for Round 1).

---

## References

- Template details: `plugin/skills/meeting-facilitate/templates/<name>.md` (each template has "When to use" and "Anti-patterns" sections)
- Meeting system design: `docs/meeting-templates-design.md`
- Related MCP tools: `meeting_create` / `meeting_send_message` / `meeting_attendance_check` / `meeting_read_messages` / `meeting_conclude` / `meeting_template_list` / `meeting_list` / `meeting_update` / `debate_start` / `debate_code_review`
