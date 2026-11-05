---
name: meeting-participate
description: Participate in AI Team OS meetings with structured discussion rounds
---

# Meeting Participate — Meeting Participation Skill

When you are invited to a meeting, participate in the discussion following this process. Meetings are the core mechanism for multi-agent asynchronous collaboration.

## Prerequisite: get your own `agent_id` first

Statements must be signed with your own name, so the first step is confirming your `agent_id` and `agent_name`. **No registration is needed** — when you were dispatched, the SubagentStart hook automatically enrolled you into the team (the `os-register` skill and the `agent_register` tool are both retired). Get your id in the following order, stopping at the first hit:

1. **Read the injection**: the "## Your OS Identity" block in the startup context gives you your `agent_id` / `team_id` / name directly. The vast majority of cases end here.
2. **Server-side reverse lookup** (use when the injection says "not yet persisted"): enrollment and injection run in parallel, so being one step behind is normal.

   ```bash
   curl -s "http://localhost:$(cat ~/.claude/data/ai-team-os/api_port.txt)/api/agents/whoami?name=<your name>&session_id=<your session id>"
   ```

   The server reverse-looks-up in three levels — `cc_agent_id` → `session + name` → `name` — and returns `{"found": true, "agent_id": ...}`.
3. **Fallback**: find your own row by name in `agent_list(team_id)`.

If none of the three yields an id (the OS API is unreachable), speak using your **name** as the `agent_id` and note that in the content — it is better to leave a traceable signature than to stay silent because you could not get an id.

## Participation Process

### 1. Read the meeting messages

After entering the meeting, first learn the current discussion state:

```
Use the MCP tool: meeting_read_messages
Parameters:
  meeting_id: <meeting ID>
```

### 2. Analyze existing statements

Read all existing messages carefully:
- Understand each participant's core viewpoint
- Find consensus points and disagreement points
- Identify angles not yet discussed

### 3. Send your viewpoint

Send a message according to the current round:

```
Use the MCP tool: meeting_send_message
Parameters:
  meeting_id: <meeting ID>
  agent_id: <your agent_id>
  agent_name: <your name>
  content: <your statement content>
  round_number: <current round>
```

### 4. Discussion rules

Strictly follow these discussion rules:

**Round 1 — Individual viewpoints**
- Independently present your view of the topic
- Offer insights based on your role and professional field
- No need to quote others (nobody has spoken yet)

**Round 2+ — Quoted responses**
- You must first read the previous statements (call `meeting_read_messages` again)
- Quote and respond to specific viewpoints, in a format like: `@agent_name mentioned "...", I think ...`
- You may add new viewpoints, but you must respond to at least one existing viewpoint
- Express agreement or disagreement clearly, with reasons

**Final round — Summary**
- Summarize the consensus of this discussion
- List the disagreements that still exist
- Propose next-step suggestions

### 5. Multi-round participation

If the meeting has multiple rounds:
1. Re-read the messages before each round starts, to get the latest statements
2. Increment `round_number`
3. Keep participating until the facilitator ends the meeting

## Statement Quality Requirements

- **Specific**: do not talk in generalities; give concrete technical proposals or reasons
- **Constructive**: propose solutions along with the problems
- **Respectful**: when disagreeing, target the issue, not the person — use "I think" rather than "you are wrong"
- **Concise**: keep each statement focused on 1-3 core viewpoints; avoid lengthiness
