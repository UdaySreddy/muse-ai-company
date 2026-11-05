# Development Machine Migration Guide (Windows → Mac / VS Code)

Moving the AI Team OS development environment from Windows to Mac. The code itself is fully cross-platform, with **no version compatibility issues**;
what needs handling is only the three things that "do not travel with git": unpushed commits, the database, and machine-specific configuration.

## Cross-platform status (verified)

| Item | Status |
|---|---|
| Python | Requires `>=3.11` (`pyproject.toml`); installing 3.11+ on the Mac is enough |
| Plugin hooks | All use `${CLAUDE_PLUGIN_ROOT}` + `python`, fully portable |
| API autostart `_autostart.py` | Handles win32 / unix separately (port probing uses netstat/taskkill ↔ fuser/lsof/kill) |
| `.mcp.json` generation | `install.py` writes it with `sys.executable`; on a Mac it automatically writes the Mac python path |

## The three things that do not travel with git and must be handled manually

1. **Unpushed commits** — on the old machine, `git push origin master` before switching (otherwise the Mac clone lacks the newest code).
2. **The database `aiteam.db`** — at `~/.claude/data/ai-team-os/aiteam.db` (about 76MB, **not in the repo**).
   To keep the existing team / project / task data, it must be copied manually to the same path on the Mac.
3. **Machine-specific configuration** — both `.mcp.json` and `.claude/settings.json` are `.gitignore`d and do not travel with the repo:
   - `.mcp.json`: `install.py` regenerates it on the Mac (with the correct Mac python path); no manual work needed.
   - `.claude/settings.json`: the `pwsh + inject-context.ps1` hook inside it is **Windows-specific**;
     on a Mac either do not use it, or replace it with a bash/python equivalent implementation.

## Steps on the Mac

```bash
# 0) (On the old Windows machine) push first, ensuring GitHub has the newest code
git push origin master

# 1) (Mac) clone and install the Python package (Python 3.11+ required)
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company          # repo directory
pip install -e .

# 2) Install frontend dependencies and build
cd dashboard && npm install && npm run build && cd ..

# 3) Register MCP + hooks (automatically generates the Mac version of .mcp.json)
python install.py

# 4) (Optional, to keep existing data) copy the database from the old machine to the Mac
#    Old machine file: ~/.claude/data/ai-team-os/aiteam.db
mkdir -p ~/.claude/data/ai-team-os
cp /path/to/aiteam.db ~/.claude/data/ai-team-os/aiteam.db

# 5) (Optional, to use Playwright screenshots/E2E)
pip install playwright && playwright install
```

VS Code: installing the Python extension is enough; there are no other special requirements.

## Notes

- **Line endings**: the repo uses autocrlf; the Mac uses LF, with no functional impact.
- **`dashboard/dist`**: a build artifact; on the Mac it must be rebuilt with `npm run build` (`.gitignore` already ignores its JS/CSS).
- **Data isolation**: if you do not copy `aiteam.db`, the Mac is a brand-new empty database (teams/projects/tasks start from zero).
