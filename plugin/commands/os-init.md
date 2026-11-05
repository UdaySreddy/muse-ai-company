---
name: os-init
description: Initialize the AI Team OS system — generate config files and directory structure
---

# /os-init — System Initialization

Help the user initialize the AI Team OS project configuration.

## Steps

1. Check whether an `aiteam.yaml` config file already exists in the current directory
2. If it exists, ask the user whether they want to reinitialize
3. Show the available templates and let the user choose:
   - **research** — Research team template (principal researcher + literature analyst + data analyst)
   - **development** — Development team template (technical director + backend developer + frontend developer + test engineer)
   - **analysis** — Analysis team template (principal analyst + data collector + data modeler)
   - **Default** — Blank configuration, edited manually
4. Run the initialization command with the Bash tool:
   ```bash
   python -m aiteam.cli.app init --template <template>
   ```
   or omit the template argument to generate the default configuration
5. Show the initialization result and next steps

## Post-initialization message

When initialization completes, tell the user the next steps:

```
## Initialization Complete

Config file generated: aiteam.yaml
Working directory created: .aiteam/

### Next Steps
1. Edit `aiteam.yaml` to customize the team configuration
2. Run `/os-up` to start the API service
3. Run `/os-status` to view system status
4. Run `/os-help` to see more available commands
```

## Notes

- Use English for all output
- If the config file already exists, you must get the user's consent before overwriting it
- Initialization does not start the service; it only creates config files
