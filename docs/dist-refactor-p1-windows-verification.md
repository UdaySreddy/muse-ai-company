# Distribution Refactor P1 — Windows Device Verification Checklist

> Purpose: this machine (macOS) has no Windows environment, so P1's Windows compatibility changes need to be tested on your Windows device and reported back.
> Execute the commands below in order; each step gives an **expected result**. If there is a deviation, paste back that step's complete output.
> Replace `<you>` in paths with the actual username; in Git Bash, `~` is `C:\Users\<you>`.

Change summary (what this checklist verifies):
- The auto_install entry in hooks.json was changed to a cross-platform launcher (Windows→`py -3`, macOS→`python3`), resolving the bootstrap deadlock; timeout 30s→300s.
- auto_install version self-healing + first-start progress card + main-chain registration (absolute `sys.executable` path, launchable on Windows).
- install.py/update.py/uninstall.py gained skills/commands/agents distribution (the full plugin set of 25) and symmetric cleanup.

---

## 0. Prerequisite environment survey (first confirm the interpreter and shell)

PowerShell:
```powershell
py -3.12 --version        # Expected: Python 3.12.x (standard install, stable slot)
py -3 --version           # Expected: some version 3.11+ (auto_install uses py -3)
python --version          # Record the result (may point to a volatile temporary 3.13; for reference only)
where.exe py python python3
```
Expected: both `py -3.12` and `py -3` print a 3.11+ version. If `py` does not exist, first install Python with the python.org installer (check the py launcher option).

Git Bash (search "Git Bash" in the Start menu; on Windows, CC runs hook commands with it by default):
```bash
uname -s                  # Expected: MINGW64_NT-10.0-... (proves the launcher takes the py -3 branch)
py -3 --version           # Expected: 3.11+
```
If `uname` reports not found, Git Bash is not installed — please install Git for Windows (otherwise CC falls back to PowerShell, the launcher's bash syntax will not take effect; this is a known documentation-level limitation).

---

## 1. Source installation path (your main migration path: fresh clone → py -3.12 install → uninstall the plugin)

Clone and install in a **brand-new directory** (do not share it with the old checkout):
```powershell
cd <some clean directory>
git clone https://github.com/CronusL-1141/AI-company.git ai-team-os
cd ai-team-os
py -3.12 install.py
```

### 1a. The verify report at the tail of the install
Expected at the end of the install output (all `[OK]`; the three new asset classes are now covered):
```
  [OK] ~/.claude/agents/ templates (25 present)
  [OK] ~/.claude/skills/ (6/6 present)
  [OK] ~/.claude/commands/ (8/8 present)
  [OK] ~/.claude/settings.json hooks
  [OK] Python package (aiteam)
```
If skills/commands are not 6/6 and 8/8, paste back the whole verify output.

### 1b. Asset placement (PowerShell)
```powershell
(Get-ChildItem "$HOME\.claude\agents\*.md").Count          # Expected: 25 (including debate-advocate/debate-critic/team-member)
Get-ChildItem "$HOME\.claude\skills" -Directory | Select-Object -Expand Name
#   Expected: autopilot, continuous-mode, meeting-facilitate, meeting-participate, os-register, os-workflow  # Note: autopilot/continuous-mode were removed with the pipeline retirement and the 2026-07-27 batch 6 respectively; this document is a historical record
Test-Path "$HOME\.claude\skills\meeting-facilitate\templates"   # Expected: True (nested subdirectories were copied too)
(Get-ChildItem "$HOME\.claude\commands\os-*.md").Count      # Expected: 8
```

### 1c. The main chain is an absolute path (the key to being launchable on Windows)
```powershell
Select-String -Path "$HOME\.claude\settings.json" -Pattern "ai-team-os" | Select-Object -First 3
```
Expected: the command looks like `"C:/Users/<you>/AppData/.../python.exe" "C:/Users/<you>/.claude/hooks/ai-team-os/send_event.py" ...` —
i.e. an **absolute python.exe path + forward slashes**, rather than a bare `python3`. This is the core of hooks being launchable by Git Bash on Windows.

### 1d. Functional verification after restarting CC
Restart Claude Code, and in any session:
```
/mcp                      # Expected: the ai-team-os tools are mounted (155)
/os-status                # Expected: the command exists and is executable (proves the commands distribution took effect)
```
Then confirm the hooks produce no errors: no `... hook error` should appear at the top of the CC session / in the transcript (especially no `python3 ... not found` / `exit 49`).

---

## 2. Plugin path verification (optional, for other plugin users; skip if you only use the source install)

If the plugin is installed via the marketplace (install.py never run): at the first session start, auto_install is launched by the launcher via `py -3`.

### 2a. First-start progress card
Expected at the top of the first session (a progress card):
```
[AI Team OS] Installation status:
  ✓ Dependency package installed (vX.Y.Z)   or   ✓ Dependency package upgraded → vX.Y.Z
  ✓ Main chain registered (N hooks, absolute paths)
  ✓ MCP service configured
  → Restart Claude Code to unlock all tools (one-time)
```
If dependency installation fails, the card should state the reason plainly (Python 3.11+ requirement / network failure) and give a copyable `pip install --upgrade git+...` retry command — **and the session must not be blocked**.

### 2b. After self-healing, hooks.json is rewritten to absolute paths
After auto_install runs through the first time (with _self_heal_interpreter in effect), check the plugin's hooks.json:
```bash
# <PLUGIN_ROOT> is generally the ai-team-os/plugin under ~/.claude/plugins/...
grep -c "python3 " "<PLUGIN_ROOT>/hooks/hooks.json"    # Expected: 0 or very few (all but the launcher rewritten to absolute python.exe)
grep -c "uname" "<PLUGIN_ROOT>/hooks/hooks.json"       # Expected: 1 (the auto_install launcher entry is kept, not rewritten)
```

### 2c. After a restart, a single runtime (no double runs, no noise)
Restart and open another session: the progress card should produce **zero output** (ready state adds no noise); the `/mcp` tools are present; the transcript has no `hook error`.

---

## 3. Uninstall symmetry (optional)

```powershell
py -3.12 scripts\uninstall.py --dry-run
```
Expected: the dry-run lists `[REMOVE] 25 agent template(s)` / `[REMOVE] 6 skill(s)` / `[REMOVE] 8 command(s)`,
and does **not** touch your own third-party skills/commands (such as resources like lark-* in the same directories).

---

## Report-back points (just send these results back)

1. Step 0: the `uname -s` output and `py -3 --version`.
2. Step 1a: whether verify's three asset classes are `[OK]` 6/6, 8/8, 25.
3. Step 1c: whether the ai-team-os command in settings.json is an absolute python.exe path.
4. Step 1d: after the restart, whether `/mcp` and `/os-status` are usable, and whether the transcript has any `hook error`.
5. (If the plugin was tested) Step 2a's progress card text, and whether step 2c is zero-noise after the restart.
