---
name: os-help
description: Show all available AI Team OS commands and usage help
---

# /os-help — Help

Show the user all of AI Team OS's available slash commands and usage instructions directly.

## Output content

Show the following help information:

```
## AI Team OS — Command Help

### Core Commands
| Command | Description |
|------|------|
| `/os-status` | Show system status — teams, agents, meetings overview |
| `/os-up` | Start the API server |
| `/os-init` | Initialize the project — generate config files |
| `/os-doctor` | Diagnose system health |

### Collaboration Commands
| Command | Description |
|------|------|
| `/os-meeting` | Meeting management — create and view meetings |
| `/os-meeting create <topic>` | Quickly create a meeting |
| `/os-task` | Task management — view and run tasks |
| `/os-task run <description>` | Create and run a task |

### Configuration Commands
| Command | Description |
|------|------|
| `/os-hooks` | View hooks registration status |
| `/os-hooks repair` | Rebuild the hook registration surface |
| `/os-help` | Show this help information |

### Skills (available to team members)
| Skill | Description |
|-------|------|
| `/meeting-participate` | Join a meeting as a participant |
| `/meeting-facilitate` | Manage a meeting as the facilitator |

### Quick Start
1. `/os-init` — Initialize the project configuration
2. `/os-up` — Start the services
3. `/os-status` — Confirm the system is ready
4. `/os-meeting create Architecture discussion` — Start your first team meeting

### More Information
- API docs: http://localhost:8000/docs
- Dashboard: http://localhost:3000
- Project docs: docs/architecture.md
```

## Notes

- Use English for all output
- Output the help information directly; no API calls are needed
- Keep the format concise and clear
