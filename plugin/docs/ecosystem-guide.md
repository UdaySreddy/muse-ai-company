# AI Team OS Ecosystem Recommendation Guide

> AI Team OS's design philosophy is to serve as an ecosystem guidance system — it does not have to build everything in, but instead recommends and bundles excellent third-party Plugin combinations, letting users build the AI team work environment that suits them best, on demand.
>
> **Progressive loading**: this guide supports 3 layers of progressive discovery and works best together with the `find_skill` MCP tool.

---

## How to Use

```
# Layer 1 — Quick recommendation: describe the task, get the top matches
find_skill(task_description="build secure REST API", level=1)

# Layer 2 — Category browsing: view all skills by category
find_skill(level=2, category="security")

# Layer 3 — Full detail: view a single skill's complete documentation
find_skill(level=3, skill_id="vibesec")
```

---

<!-- ============================================================ -->
<!-- LAYER 1: Quick Reference — one-line description + install command -->
<!-- ============================================================ -->

## Layer 1: Quick Reference Table

> Choose quickly by task type. `find_skill(level=1, task_description="...")` returns this layer's data.

| ID | Name | One-line description | Install command |
|----|------|-----------|---------|
| claude-mem | claude-mem | Automatic session capture + 95% AI compression + cross-session recovery | `/plugin marketplace add thedotmack/claude-mem` |
| continuous-learning-v2 | continuous-learning-v2 | Observe behavior → extract instincts → evolve into reusable skills | `/plugin marketplace add continuous-learning-v2` |
| code-review | code-review | Automatic PR review, analyzing code changes and giving suggestions | `/plugin marketplace add code-review` |
| pr-review-toolkit | pr-review-toolkit | A multi-angle PR review expert team | `/plugin marketplace add pr-review-toolkit` |
| superpowers | Superpowers | A complete development workflow framework: design → plan → TDD → Git | `/install obra/superpowers` |
| frontend-design | Frontend-Design | The official frontend Skill, generating production-grade UI with a distinctive aesthetic | `/install anthropics/claude-code#plugins/frontend-design` |
| vibesec | VibeSec | Security guardian: automatically detects IDOR/XSS/SQLi/SSRF and embeds protection | `/install BehiSecc/VibeSec-Skill` |
| skill-creator | Skill-Creator | Turn any workflow into a reusable Skill, fitting 14+ platforms | `/install FrancyJGLisboa/agent-skill-creator` |
| jupyter-notebook | Jupyter Integration | Use the Claude Code AI Agent inside JupyterLab/Notebook | `pip install jupyter-cc` |

### Task-Type → Skill Quick Mapping

| Task type | Recommended Skill | Notes |
|---------|-----------|------|
| Production-grade feature development | Superpowers | Enforced engineering discipline: design → plan → TDD → Git |
| Frontend UI prototypes | Frontend-Design | Avoid the generic AI look; generate interfaces with an aesthetic |
| Web security hardening | VibeSec | Embed security review at the code generation stage |
| VibeCoding rapid builds | Frontend-Design + VibeSec | Fast and good-looking + basic security assurance |
| Data analysis / ML experiments | jupyter-cc / Notebook Intelligence | Use Claude capabilities directly inside a Notebook |
| Solidifying team workflows | Skill-Creator / Skill-Factory | Turn internal standards into shareable Skills |
| Knowledge-base Q&A | NotebookLM Skill | Citation-style answers based on uploaded documents |
| Memory persistence | claude-mem | Preserve Agent operation history across sessions |
| Team knowledge accumulation | continuous-learning-v2 | Automatically extract repeated patterns into reusable Skills |
| PR code review | code-review / pr-review-toolkit | Multi-dimensional code quality gatekeeping |

---

<!-- ============================================================ -->
<!-- LAYER 2: Category Browse — features/tags/scenarios by category  -->
<!-- ============================================================ -->

## Layer 2: Category Browsing

> Browse grouped by task type. `find_skill(level=2, category="...")` returns this layer's data.

### Memory Enhancement (memory)

#### claude-mem
- **Tags**: `memory` `context` `session` `compression`
- **GitHub**: [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | Stars: 21.5k+
- **Features**:
  - Automatically capture session operations
  - 95% AI compression
  - Cross-session context recovery
- **Scenarios**: cross-session memory persistence, single-Agent context continuation

### Continuous Learning (learning)

#### continuous-learning-v2
- **Tags**: `learning` `instinct` `skill-evolution` `knowledge`
- **Features**:
  - Observe session behavior patterns
  - Abstract repeated patterns into atomic-level instincts
  - Aggregate multiple instincts into reusable skills
- **Scenarios**: team knowledge distillation, automatically extracting patterns from repeated tasks

### Code Quality (code-quality)

#### code-review
- **Tags**: `code-review` `pr` `quality`
- **Features**: automatic PR analysis, code change suggestions
- **Scenarios**: automatic code review during solo development

#### pr-review-toolkit
- **Tags**: `code-review` `pr` `quality` `team`
- **Features**: multi-dimensional code review, expert team perspectives
- **Scenarios**: team collaboration scenarios needing multi-angle review

### Development Workflow (dev-workflow)

#### Superpowers
- **Tags**: `workflow` `tdd` `git` `planning` `engineering`
- **GitHub**: [obra/superpowers](https://github.com/obra/superpowers)
- **Features**:
  - `brainstorming` — refine requirements through questions before coding
  - `using-git-worktrees` — automatically create isolated branch workspaces
  - `writing-plans` — split work into precise 2-5 minute steps (including file paths and verification methods)
  - `test-driven-development` — enforce the RED-GREEN-REFACTOR loop
- **Scenarios**: production-grade feature development, enforced TDD, multi-person collaboration with strict quality requirements

### Frontend Design (frontend)

#### Frontend-Design
- **Tags**: `frontend` `ui` `design` `aesthetic` `vibe-coding`
- **GitHub**: [anthropics/claude-code — plugins/frontend-design](https://github.com/anthropics/claude-code/tree/main/plugins/frontend-design)
- **Features**:
  - Guide Claude to create a distinctive visual style
  - Support extreme aesthetic directions: minimalism, retro-futurism, industrial style, editorial magazine style
  - Avoid the "AI samey" generic look
- **Scenarios**: full-stack UI prototypes, VibeCoding rapid web apps, breaking the "default Tailwind blue"
- **Community variant**: [Koomook/claude-frontend-skills](https://github.com/Koomook/claude-frontend-skills)

### Security Detection (security)

#### VibeSec
- **Tags**: `security` `xss` `sqli` `ssrf` `idor` `audit`
- **GitHub**: [BehiSecc/VibeSec-Skill](https://github.com/BehiSecc/VibeSec-Skill) | Official site: [vibesec.sh](https://vibesec.sh/)
- **Features**:
  - Review code from a "vulnerability hunter" perspective
  - Automatically implement access control, security headers, input validation and sanitization
  - Proactively block IDOR, XSS, SQL injection, SSRF, and weak authentication
- **Scenarios**: web application development, security hardening after VibeCoding, security embedded in code generation
- **Compatibility**: Claude Code, Cursor, GitHub Copilot, and all AI tools that support custom instructions
- **Variant**: [raroque/vibe-security-skill](https://github.com/raroque/vibe-security-skill)

### Developer Tools (dev-tools)

#### Skill-Creator / Skill-Factory
- **Tags**: `skill-builder` `workflow` `template` `meta`
- **GitHub**: [alirezarezvani/claude-code-skill-factory](https://github.com/alirezarezvani/claude-code-skill-factory), [FrancyJGLisboa/agent-skill-creator](https://github.com/FrancyJGLisboa/agent-skill-creator)
- **Features**:
  - Skill-Factory: a production-grade Skill building toolkit with structured template generation
  - Agent-Skill-Creator: turn workflows into Skills; one SKILL.md fits 14+ platforms
- **Scenarios**: solidify team workflows into shareable Skills, embed internal standards into Agent behavior
- **Tip**: the official docs at [code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills) provide the Skill structure spec

### Data Science (data-science)

#### Jupyter / NotebookLM Integration
- **Tags**: `jupyter` `notebook` `data-science` `ml` `visualization`
- **GitHub**: [notebook-intelligence](https://github.com/notebook-intelligence/notebook-intelligence), [jupyter-cc](https://github.com/vinceyyy/jupyter-cc), [jupyter-notebook-mcp](https://github.com/jjsantos01/jupyter-notebook-mcp)
- **Features**:
  - Notebook Intelligence: Claude Code Agent mode inside JupyterLab
  - jupyter-cc: invoke Claude via IPython magic commands
  - jupyter-notebook MCP: control Jupyter Notebooks through the MCP protocol
  - NotebookLM Skill: Claude communicates with Google NotebookLM
- **Scenarios**: data science and ML workflows, AI Agents in Notebook environments, rapid research prototypes

---

<!-- ============================================================ -->
<!-- LAYER 3: Full Detail — complete docs incl. OS complementarity   -->
<!-- ============================================================ -->

## Layer 3: Full Detail

> A single skill's complete documentation. `find_skill(level=3, skill_id="...")` returns this layer's data.

### claude-mem — Full Documentation

- **Stars**: 21.5k+
- **Installation**:
  ```bash
  /plugin marketplace add thedotmack/claude-mem
  ```
- **Complementarity with the OS**:
  - The OS handles **team-level coordination** — task assignment, inter-Agent communication, workflow orchestration
  - claude-mem handles **individual-level memory** — a single Agent's session history, operation habits, context continuation
  - Combined, they deliver "team coordination + individual memory" dual-layer coverage without conflict

```
┌─────────────────────────────────────────┐
│            AI Team OS (team layer)       │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │ Agent A  │  │ Agent B  │  │ Agent C  │ │
│  │┌───────┐│  │┌───────┐│  │┌───────┐│ │
│  ││claude ││  ││claude ││  ││claude ││ │
│  ││ -mem  ││  ││ -mem  ││  ││ -mem  ││ │
│  │└───────┘│  │└───────┘│  │└───────┘│ │
│  └─────────┘  └─────────┘  └─────────┘ │
│         Team coordination + individual memory │
└─────────────────────────────────────────┘
```

### continuous-learning-v2 — Full Documentation

- **Learning process**:
  1. **Observation phase**: monitor the Agent's operation patterns within a session
  2. **Extraction phase**: abstract repeated patterns into atomic-level instincts
  3. **Evolution phase**: aggregate multiple instincts into a reusable skill
- **Complementarity with the OS**:
  - The OS manages team structure and task flow
  - continuous-learning handles the automatic accumulation of the team's gathered knowledge
  - Forms a "practice → extract → share → evolve" positive loop

### Superpowers — Full Documentation

- **GitHub**: [obra/superpowers](https://github.com/obra/superpowers) | Community Skills: [obra/superpowers-skills](https://github.com/obra/superpowers-skills)
- **Installation**:
  ```bash
  /install obra/superpowers
  ```
- **Core Skills included**:
  - `brainstorming` — refine requirements through questions before coding, exploring alternatives
  - `using-git-worktrees` — automatically create an isolated branch workspace after the design passes
  - `writing-plans` — split work into precise 2-5 minute steps (including file paths and verification methods)
  - `test-driven-development` — enforce the RED-GREEN-REFACTOR loop
- **Complementarity with the OS**: the OS orchestrates the team; Superpowers enforces engineering discipline inside each Agent. Especially valuable for production-grade code and strict-quality teams.

### Frontend-Design — Full Documentation

- **GitHub**: [anthropics/claude-code — plugins/frontend-design](https://github.com/anthropics/claude-code/tree/main/plugins/frontend-design)
- **Installation**:
  ```bash
  /install anthropics/claude-code#plugins/frontend-design
  ```
- **Community variant**: [Koomook/claude-frontend-skills](https://github.com/Koomook/claude-frontend-skills) — provides more extended design styles
- **Complementarity with the OS**: the OS handles project management and team collaboration; Frontend-Design focuses on giving every frontend component a unique visual style and aesthetic depth.

### VibeSec — Full Documentation

- **GitHub**: [BehiSecc/VibeSec-Skill](https://github.com/BehiSecc/VibeSec-Skill) | Official site: [vibesec.sh](https://vibesec.sh/)
- **Installation**:
  ```bash
  /install BehiSecc/VibeSec-Skill
  ```
- **Compatibility**: supports Claude Code, Cursor, GitHub Copilot, and all AI tools that support custom instructions
- **Related variant**: [raroque/vibe-security-skill](https://github.com/raroque/vibe-security-skill) — focused on auditing security vulnerabilities in AI-generated code
- **Complementarity with the OS**: the OS manages workflows and team coordination; VibeSec embeds security awareness at the code generation stage, so vulnerabilities are intercepted before reaching production.

### Skill-Creator / Skill-Factory — Full Documentation

- **GitHub (Skill Factory)**: [alirezarezvani/claude-code-skill-factory](https://github.com/alirezarezvani/claude-code-skill-factory)
- **GitHub (Agent Skill Creator)**: [FrancyJGLisboa/agent-skill-creator](https://github.com/FrancyJGLisboa/agent-skill-creator)
- **Installation**:
  ```bash
  # Skill Factory
  git clone https://github.com/alirezarezvani/claude-code-skill-factory
  # Agent Skill Creator
  /install FrancyJGLisboa/agent-skill-creator
  ```
- **Tip**: the official docs at [code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills) provide the Skill structure spec; a SKILL.md only needs YAML frontmatter + instructions to take effect
- **Complementarity with the OS**: the OS defines team roles and workflows; Skill-Creator solidifies team best practices into distributable skills, so new members automatically inherit team standards.

### Jupyter / NotebookLM Integration — Full Documentation

- **Notebook Intelligence**: [notebook-intelligence/notebook-intelligence](https://github.com/notebook-intelligence/notebook-intelligence) — provides Claude Code AI Agent mode for JupyterLab
- **jupyter-cc**: [vinceyyy/jupyter-cc](https://github.com/vinceyyy/jupyter-cc) — IPython magic command extension
- **jupyter-notebook MCP**: [jjsantos01/jupyter-notebook-mcp](https://github.com/jjsantos01/jupyter-notebook-mcp) — control Jupyter through the MCP protocol
- **NotebookLM Skill**: [PleasePrompto/notebooklm-skill](https://github.com/PleasePrompto/notebooklm-skill) — Claude communicates with Google NotebookLM
- **Installation (jupyter-cc)**:
  ```bash
  pip install jupyter-cc
  # Use inside a Notebook
  %load_ext jupyter_cc
  %%claude Analyze this data and generate a visualization
  ```
- **Complementarity with the OS**: the OS manages projects and teams; the Jupyter integration lets data science Agents use AI capabilities natively in a notebook environment, without leaving the experiment environment.

---

## External Tool Integration Recipes

> For detailed integration recipes, see **[docs/ecosystem-recipes.md](../../docs/ecosystem-recipes.md)**.

As a "meta-Plugin that orchestrates other MCPs", AI Team OS can be combined with external development tools' MCP servers. Verified integration solutions:

| Integration target | MCP Server | Typical scenario | Query command |
|---------|-----------|---------|---------|
| GitHub | `@modelcontextprotocol/server-github` | PR management, Issue sync, code commits | `find_skill(level=3, skill_id="github-integration")` |
| Slack | `@modelcontextprotocol/server-slack` | Team notifications, alert pushes, standup summaries | `find_skill(level=3, skill_id="slack-integration")` |
| Linear | `@modelcontextprotocol/server-linear` | Task sync, Sprint↔Workflow mapping | `find_skill(level=3, skill_id="linear-integration")` |
| Full-stack team | GitHub + Slack + AI Team OS | One-click startup of a preset multi-role team | `find_skill(level=3, skill_id="fullstack-team")` |

### Quick Queries

```
# List all integration recipes
find_skill(level=2, category="integration")

# View a specific recipe's details (including recommended MCPs, scenarios, OS toolchain)
find_skill(level=3, skill_id="github-integration")
```

---

## Best Practices for Combined Use

### Recommended Installation Order

1. **Install AI Team OS first** — establish the team infrastructure
2. **Create teams and Agents** — clarify role division
3. **Then install auxiliary Plugins** — enable enhanced capabilities for specific Agents on demand

```bash
# Step 1: Ensure the OS is installed and running
# Step 2: Create teams
# Step 3: Add auxiliary plugins on demand
/plugin marketplace add thedotmack/claude-mem        # Memory enhancement
/plugin marketplace add continuous-learning-v2         # Continuous learning
```

### OS + claude-mem: dual-layer coverage of team-level + individual-level memory

- The OS maintains team-level shared context (task status, Agent roles, collaboration history)
- claude-mem maintains each Agent's individual memory (operation preferences, session continuation)
- When a new Agent joins the team, the OS provides the team background and claude-mem provides the individual accumulation

### OS + continuous-learning: automatic team knowledge accumulation

- When the team repeatedly performs similar tasks across projects, the learning system automatically extracts patterns
- Extracted skills can be distributed to other Agents in the team through the OS's knowledge sharing mechanism
- Forms a "practice → extract → share → evolve" positive loop

---

## Notes

### Hook Conflict Risk

- Multiple Plugins may register hooks such as `PostToolUse`
- When multiple hooks trigger simultaneously, execution order may be uncertain or they may interfere with each other
- **Recommendation**: after installing a new plugin, check `.claude/hooks.json` and confirm the hooks do not conflict

### Context Consumption Control

- Every Plugin's system prompt and tool definitions consume context window
- **Enable on demand; do not install everything** — only enable the Plugins needed at the current stage
- If the context nears its cap, prioritize keeping the OS's core functions and temporarily disable auxiliary Plugins

### Compatibility Notes

- The Plugin recommendations in this guide are based on current ecosystem research; versions and features may change over time
- Before installing, confirm the Plugin's compatibility with the current Claude Code version
- When you hit problems, consult each Plugin's official documentation and issue list first
