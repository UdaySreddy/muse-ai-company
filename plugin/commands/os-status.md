---
name: os-status
description: Show AI Team OS system status — teams, agents, and meetings overview
---

# /os-status — System Status Overview

Query the AI Team OS API and present the current system status to the user.

## Steps

1. Call the MCP tool `team_list` to get the list of all teams
2. For each team, call `agent_list` to get its agents
3. For each team, call `meeting_list` to get active meetings (status=active)
4. Organize the results into a clear status report

## Output format

Structure the output as follows:

```
## AI Team OS System Status

### Teams
- Team name | Orchestration mode | Agent count

### Agents
- Agent name | Role | Status (IDLE/BUSY/OFFLINE) | Team

### Active Meetings
- Meeting topic | Participants | Message count | Team

### System Info
- API address: http://localhost:8000
- Dashboard: http://localhost:3000
```

## Error handling

If the API is unreachable, output:
> The AI Team OS service is not running. Run `/os-up` first to start the service.

## Notes

- Use English for all output
- Distinguish agent statuses with markers: IDLE=idle, BUSY=busy, OFFLINE=offline
- If there are no teams, prompt the user to initialize the project with `/os-init`
