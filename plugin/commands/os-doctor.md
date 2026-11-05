---
name: os-doctor
description: Diagnose AI Team OS system health — check services, configuration, and dependencies
---

# /os-doctor — System Diagnostics

Perform a comprehensive health check of AI Team OS and help the user troubleshoot problems.

## Checklist

Run the following checks in order, marking each as pass/fail/warning:

### 1. Python environment
- Check the Python version is >= 3.12
- Check that the `aiteam` package is installed: `python -c "import aiteam; print(aiteam.__version__)"`

### 2. Config files
- Check that `aiteam.yaml` exists
- Check that the `.aiteam/` directory exists

### 3. API service
- Check that http://localhost:8000/api/teams is reachable (timeout 2 seconds)
- If reachable, show the response status

### 4. Database
- Check that the `aiteam.db` file exists (SQLite mode)
- Or check the PostgreSQL connection (if configured)

### 5. Hooks configuration
- Check whether hooks are configured in `.claude/settings.local.json`
- Check that the hook script files exist

### 6. Dashboard
- Check that the `dashboard/` directory exists
- Check whether dependencies are installed in `dashboard/node_modules/`

## Output format

```
## AI Team OS System Diagnostics

| Check | Status | Details |
|--------|------|------|
| Python environment | PASS | Python 3.12.x |
| aiteam package | PASS | v0.3.0 |
| Config files | PASS | aiteam.yaml exists |
| API service | FAIL | http://localhost:8000 unreachable |
| Database | PASS | aiteam.db (SQLite) |
| Hooks configuration | WARN | 14/17 entries registered |
| Dashboard | PASS | Dependencies installed |

### Recommendations
- The API service is not running; run `/os-up` to start it
- The hook registration surface is incomplete; run `python3 install.py --update` to rebuild it
```

## Notes

- Use English for all output
- Status markers: PASS=pass, FAIL=fail, WARN=warning
- Give a concrete fix suggestion for each failing item
- Use the Bash tool to run the check commands
