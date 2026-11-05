---
name: os-meeting
description: Quickly create and manage AI team meetings
---

# /os-meeting — Meeting Management

Help the user create or manage meetings in AI Team OS.

## Usage

The user can invoke it in the following ways:
- `/os-meeting` — Show the list of active meetings
- `/os-meeting create <topic>` — Create a new meeting
- `/os-meeting <meeting_id>` — View the details and messages of a specific meeting

## Workflow

### No arguments: list active meetings

1. Call `team_list` to get all teams
2. For each team, call `meeting_list` (status=active)
3. Present the active meetings in a table

### create mode: create a new meeting

1. Confirm with the user:
   - Meeting topic (use it directly if the user already provided one)
   - Target team (auto-select if there is only one team)
   - Participant list (call `agent_list` to show the available agents)
2. Call `meeting_create` to create the meeting
3. Show meeting usage guidance:
   - How to send messages via the API
   - Discussion rules: R1 individual views → R2+ quoted responses → final consensus summary

### View mode: view meeting details

1. Call `meeting_get` to get the meeting info
2. Call `meeting_messages` to get the message list
3. Present the messages grouped by round

## Output format

### Meeting list
```
## Active Meetings

| Meeting ID | Topic | Participants | Messages | Team |
|--------|------|--------|--------|------|
| abc123 | Architecture discussion | 3 people | 12 messages | dev-team |
```

### Meeting details
```
## Meeting: <topic>
Status: Active | Participants: A, B, C

### Round 1
**AgentA**: Statement content...
**AgentB**: Statement content...

### Round 2
**AgentA**: Statement content...
```

## Notes

- Use English for all output
- Confirm the participant list before creating a meeting
- Group messages by round_number when displaying them
