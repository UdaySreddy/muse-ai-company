# Progressive Tool Loading Design — Research Verdict and Phased Plan

> Status: planning draft (awaiting user approval to implement) | 2026-07-13
> Basis: three lines of first-hand research (wf_ad494800: github-mcp toolsets / Anthropic Tool Search / progressive disclosure and subagent authorization)
> Ledger: task 7ff4ee65; complete research data in /tmp/tool_loading_research.json + the wf journal

## 0. Core verdict

**"Lazy loading" itself will not be built** — the CC client already completes it at the harness layer (ToolSearch defer: schemas are pulled on demand), and the official two big pain points (context bloat, selection accuracy) are already solved in the CC combination. The OS building its own retrieval-style loading = reinventing the wheel; embedding retrieval has counter-evidence too (ToolRet/ACL2025: strong IR models perform poorly at tool retrieval).

**But official hard numbers confirm we cross the line**: Claude's tool-selection accuracy **degrades after 30-50 tools** (official text); the OS's 166 are far above that; the ecosystem family's 47 near-synonymous names (summary_*/deep_review_*/scan_*) are exactly the "similar names mis-selection" high-risk zone the officials named. CC's defer only takes away the schemas; **the list of 166 tool names still resides in every session**.

**The correct attribution of the residual gains** (the key conclusion of research line three): trimming the tool surface by role in the CC world is a **client responsibility** (the tools/disallowedTools in subagent frontmatter, supporting whole-group patterns like `mcp__ai-team-os__*`, a structural rejection rather than a soft guardrail); the server side lacks a reliable "who am I" signal (MCP has no session-identity standard; the gap is confirmed).

## 1. Phased plan (zero new dependencies, zero vectors throughout)

### P1 — alwaysLoad core tools (the official lever, smallest change, **strict admission**)
Put `_meta: {"anthropic/alwaysLoad": true}` on a very few tools (CC v2.1.121+), exempting them from defer so they are reached directly without retrieval.

**Cost model (the basis for strictness, ruled by the user on 2026-07-13)**: a deferred tool only resides as a name (~10 tokens), while an alwaysLoad tool resides as a full schema (200-500 tokens each), and that is a **fixed expense per session + per dispatched agent**; while a ToolSearch round trip itself is very cheap — the gain is small and the cost fixed, so the threshold must be high.

**The allowlist = algorithmic dynamic rotation (user's second ruling on 2026-07-13: no stacking, no hand-tuning)**, and the implementation is deliberately kept as light as possible against over-engineering:

- **Rotation granularity = session start**: the OS's MCP server is brought up with each CC session and recomputes the list once at startup — a natural rotation point, with no runtime change (CC does not guarantee consuming listChanged, and mid-flight changes break the prompt cache).
- **The full algorithm = one SQL query + hysteresis** (measured 0.1 seconds, no resident process/no new dependencies/no in-memory residency):
  `SELECT tool_name, COUNT(*) c, COUNT(DISTINCT date(timestamp)) d FROM agent_activities WHERE timestamp > -7d AND tool_name LIKE 'mcp__ai-team-os%' GROUP BY tool_name HAVING d>=2 ORDER BY c DESC LIMIT 5`
  - The `d>=2` cross-day threshold blocks time-window bursts (measured effective: task_memo_add at 147 times/6 days was selected, ecosystem_apply_* at 12 times/1 day was blocked);
  - **Hysteresis against flapping**: a challenger must exceed the incumbent by ≥20% to rotate in (the previous list is stored in project.config, following the last_reconcile_at pattern; no new table);
  - Hard cap **≤5, starting at 3**; insufficient data (cold start) → the static seed `task_memo_add` or the empty set.
- **Over-engineering guardrail (an explicit response to the user's concern)**: complexity is capped here — **no** decay curves/real-time counting/background recomputation/ML scoring; if the statistics query fails → silently zero alwaysLoad and everything proceeds via ToolSearch as before (pure gain; if it breaks, nothing is lost); each computation's result lands as one row in the events ledger (auditable; a governance-layer principle).
- The same architectural philosophy as reconcile: **a deterministic small algorithm + data already in the ledger**, zero new residency.

Prerequisite verification: how FastMCP (Python) supports per-tool `_meta` (a gap; measure it as the first implementation step).

### P2 — AITEAM_TOOLSETS startup-time grouping switch (github-mcp style)
- Map the 29 tool modules to toolsets (task/team/meeting/memory/ecosystem/...); the environment variable `AITEAM_TOOLSETS=default|all|<group name list>` filters at the registration loop in `mcp/tools/__init__.py` — pure startup-time static gating, no runtime listChanged (whether CC consumes that notification is unproven).
- Suggested `default` ≈ the six core groups (task/team/memory/infra/reports/project, ~60 tools); `all` = the status quo (the default value, backward compatible).
- In sync, `AITEAM_READONLY=1` read-only mode: hide all write-class tools (Sentry's 25/20 read-only default, Playwright's minimal-exposure default, and github's --read-only follow the same convention).
- Benefit ordering: hard caps of non-CC clients (Cursor sends only the first 40 tools — with 166 in full, 3/4 are unreachable; a real pain point for public-repo users) > slimming the name list > relieving tool confusion.

### P3 — Agent template tool surfaces (CC side, structural least privilege)
Add frontmatter tool authorization by role to plugin/agents/*.md: research/audit-type templates get write-class and git tools `disallowedTools`ed; specialist templates use an allowlist (e.g. technical-writer does not need the ecosystem write family). Official semantics: "Every other tool call fails immediately" — a structural constraint. Note the official warning: over-restriction makes tasks fail in confusing ways; start from the obviously high-risk items.

**Trimmed template list (first batch, conservatively circled "rather too few than too many", 2026-07-13)** — `disallowedTools` (a denylist) removes clearly unneeded write tools; all read tools and meeting/memo accounting tools are kept; the ecosystem write family takes the ecosystem family (29) in `mcp/tools/toolsets.py::WRITE_TOOLS` as the authoritative measure:

| Template | Trim set | Reason |
|---|---|---|
| support-meeting-facilitator | git writes (2) + project_delete/team_delete + os_restart_api + ecosystem write family (29) = 34 | Meeting coordination; writes no code/deletes no projects or teams/restarts no services |
| debate-advocate | same 34 | Debate's affirmative side; pure text output |
| debate-critic | same 34 | Debate's negative side; pure text output |
| support-technical-writer | the above 34 + task_run = 35 | Writes documents, dispatches no work (task_run) |
| management-project-manager | git writes (2) + os_restart_api = 3 | A management role touches no code and restarts no services (task/team/project orchestration write rights kept) |

Add a `<!-- Tool trimming… -->` comment line at the head of each template's body explaining the trimming reason and the appeal route (ask the Leader to let it through). Engineering/test-class templates are untouched (they genuinely need write permissions). Verification: `yaml.safe_load` parse each frontmatter + assert the `mcp__ai-team-os__` prefix + git diff contains only the 5 templates + this document + CHANGELOG.

### Long term (unscheduled) — aggregating and slimming the ecosystem family
Aggregate the 47 fine-grained tools by job-to-be-done (the Cloudflare approach); that is a refactor, not a switch — to be discussed again once P2 data exists (which groups are really used).

## 1.5 P4 — Slim projection views for list-class return bodies (implemented 2026-07-14, user-selected)

Tool-surface governance (P1-P3) solves "which tools enter the context"; P4 solves "how much of a tool's return body enters the context" — the other half of the same architectural philosophy: for the tool surface, "names resident, schemas on demand" (ToolSearch defer); for return bodies, "summaries resident, details on demand".

**Empirical basis (benchmark task 4267426d, real load + o200k tokenization)**:
- Switching directly to TOON is a negative gain (events -11%/eco -10%) — nested heterogeneous structures defeat tabularization, and indentation is a tax; **field trimming is the main lever**: task-wall saves 84.3%, eco lists 80.7%, events 50.7% (the conservative version, including summary columns).
- A three-tier accuracy panorama: opus/sonnet score 100% on all arms (the fleet's real tiers; format makes no difference); the haiku slim version actually rises (71%→78%, fewer noise fields). TOON has a counting-question dividend only at the haiku tier (100% vs 63-90%) and does not apply to an opus fleet — **TOON is not in this batch**; to be discussed again if a weak-model consumer is introduced in the future.

**Implementation shape (mcp/tools/views.py + three tools)**:
- `task_list_project` / `event_list` / `ecosystem_search` gain a `fields="compact"|"all"` parameter, defaulting to the slim projection; **the projection lives only at the MCP tool layer (the LLM representation layer); the JSON consumed by the API routes and the Dashboard does not change by a single character**.
- **Self-identification (user ruling 2026-07-14)**: a slim response carries `view:"compact"` + a `hint` (stating plainly "this is not missing fields" and pointing to the single-item detail tool and the `fields="all"` escape hatch), preventing an agent from misjudging fields as missing.
- The projection's three iron rules: keys for subsequent calls (ids) are always complete; semantic content degrades to truncated summaries (desc 80 chars / event payload derived to one line / eco summary 120 chars) rather than being deleted; selection-action signal fields (score/assigned_to/depends_on/subtask_count) are kept or appear sparsely.
- The accounting of one extra call: already know you want the full volume → `fields="all"` costs zero extra round trips; drilling into 1-3 items after a list → the single-item get is already the recommended flow; reading the slim version and only then discovering something is missing → one added round trip (that is the upper bound of the degradation cost of a wrong trim, not a failure).

**Same-batch prompt-level adoption**: deep-scan/shallow-scan dispatch prompts are advised to use `npx -y gh-axi@0.1.27 api repos/X` (measured saving 78%, field-equivalent; the version is pinned against v0.1.x drift; it explicitly says to avoid repo view — which drops pushed_at/license) + a suspicious-outlier-data instruction (the lesson of the affaan-m/ECC search-poisoning incident; see the benchmark task's issue memo).

## 1.6 P4 supplementary drop — roster-class tools (2026-08-03, filled in after an incident)

P4's first batch only covered the task-wall / events / ecosystem families; **the roster-class tools slipped through**, and the cost was that the two most-used observation tools were effectively dead on large teams.

**Measured failures** (reproduced live by the member lifecycle audit, all real payloads):

| Tool | Payload | Before fix | After fix | Result |
|---|---|---:|---:|---|
| `workflow_get` | The full record of a 166-agent run | 268,753 | 15,909 | Before the fix it **necessarily exceeded the cap** (the harshest in the whole database) |
| `team_status` | A 173-person workflow team | 170,331 | 1,509 | Before the fix it was **rejected** |
| `team_list` | 316 teams | 148,173 | 1,790 | Before the fix it was **rejected** |
| `team_status` | A 51-person session container team | 69,660 | 2,402 | Before the fix it was **rejected** |
| `agent_list` | Same team | 67,766 | 2,171 | Before the fix it was **directly rejected by MCP** |
| `agent_activity_query` | limit=60 | 43,916 | 13,226 | Before the fix it measured **just barely not rejected** |
| `agent_template_list` | 25 templates | 32,480 | 4,871 | `grouped` duplicates `templates` verbatim, taking half |
| `team_briefing` | A 173-person team | 20,521 | 3,250 | Before the fix it was approaching the cap |

**Root-cause quantification**: offline rows make up 96.4% of the `agent_list` payload, the single `system_prompt` field takes 24.7%, each row averages 1,413 characters → the cap is hit at about 47 rows; in `workflow_get`, `prompt_preview` + `result_preview` take 53%.

**Two items still on the list after the horizontal sweep (not changed in this batch, awaiting a ruling)**:

- `task_list_project(fields="all")` = 36,288 characters — this is the **escape hatch by design**; the caller explicitly asks for the full volume, so it does not count as a failure.
- `event_list(limit≥85)` = 23,182 characters (at the default limit=20 it is 11,611) — row-level projection was already done in P4's first batch; it only crosses the line when the caller actively opens a large window. To align with the "compact views carry a window cap" of `agent_activity_query` / `workflow_get`, a cap of 60 could be added.

Everything else measures within the safety line: `decision_log` 8,265 / `memory_list` 8,005 / `task_list_project` 6,944 / `workflow_list` 5,489 / `report_list` 5,031 / `ecosystem_search` 4,771 / `context_resolve` 4,285 / `agent_reuse_recommend` 3,926 / `unified_search` 3,793 / `project_list` 1,677 / `usage_attribution` 1,457 / `briefing_list` 882 / `memory_search` 547 / `os_health_check` 240.

**Cap convention (measure first, then write assertions)**: CC 2.1.219's MCP result cap counts **tokens** — `MAX_MCP_OUTPUT_TOKENS`, with a default constant of 25,000 (overridable by the same-named environment variable); it is a separate mechanism from the per-tool `_meta: anthropic/maxResultSizeChars`, which governs the disk-persistence threshold — **raising it does not solve the token cap**. Calibrating the same OS payloads puts the real rejection threshold between **43,916 characters (passed) and 67,766 characters (rejected)**, so regression assertions take **< 20,000 characters** (less than half of what has been measured to pass).

**Window caps**: when adding a projection to a tool that had none, also give the compact view a window cap (`agent_activity_query` 40 rows / `workflow_get` 40 rows / rosters 50 rows, upstream fetching 200 rows), and state the folded amount plainly as a count or an aggregate — **truncation must expose itself, never silently drop rows**.

**A new orthogonal rule**: for roster-class tools, `fields` only governs **how wide a row is**, and `include_offline` only governs **which rows are included** — `fields="all"` does not mean "no filtering". Otherwise the escape hatch itself exceeds the cap (measured `fields="all" + include_offline=True` = 72,550 characters), which is the same as having no escape hatch. By default, offline is folded into `{count, recent[5]}`; the criterion is that offline = a terminated process (cannot be SendMessage'd, cannot be dispatched work, its current_task is already stale) and each of its bytes has the lowest decision value; **deleting agents rows is a hard-discipline ban** — folding is only a display strategy, and both `include_offline=True` and `agent_reuse_recommend` point the way in their hints.

## 2. Explicitly not doing

| Not doing | Basis |
|---|---|
| Retrieval-style tool loading (bigtool style) | CC ToolSearch already covers it; duplicate construction |
| Embedding tool retrieval | ToolRet: strong IR models doing tool retrieval have "poor performance"; the red line is zero vectors |
| enable_toolset dynamic meta-tools | github's own is beta and community feedback says LLMs often forget to load the right group; CC is not resident and does not suit an in-session state machine |
| Runtime dynamic add/remove (listChanged) | No first-hand evidence that CC consumes that notification (a gap); startup-time gating is enough |
| Server-side gating by session identity | MCP has no session-identity standard signal; role trimming belongs to CC subagent frontmatter (P3) |

## 2.5 Open-source special-research increment (wf_3acde6a3: 6 gateway items + 5 platform items + 4 academic items, 2026-07-13)

**P2 data model finalized** (convergent validation across multiple projects):
- Switch shape = `toolFilter{mode: allow|block, list}` (TBXark/mcp-proxy) + **named set aliases** (BFCL `TEST_COLLECTION_MAPPING` / github-mcp `all|default`, same style): `AITEAM_TOOLSETS=default|all|readonly|<group names>` selects a group in one line;
- Grouping keys are by **capability domain** rather than verb or physical source (Composio/JARVIS/ToolBench agree across the industry);
- Dual execution assurance = ListTools filtering + CallTool rejection (MetaMCP's `filter-tools` middleware as the blueprint; the TS→Python rewrite risk is low);
- The default group's target is **≤50 tools** (a candidate-set hard cap is a universal guardrail: AnyTool's 64 cap, JARVIS top-10→5, and the official 30-50 inflection point share one source).

**P1 industry comparison**: Composio's "important tag" — pulling tools per app returns only a curated subset by default, with the full volume explicitly requested (official text: "prevents overwhelming the LLM"), the isomorphic precedent for the alwaysLoad core set.

**A new greenfield opportunity — usage-statistics-driven (an industry-wide vacuum; the OS has unique data)**: the research confirms no project feeds call statistics back into automatic grouping/demotion/deprecation (IBM/lasso only observe; the sole precedent is Smithery driving registry ordering with useCount/score). The OS's agent_activities ledger already holds tool-call records → zero-dependency frequency statistics, used for **(a) data-driven human adjustment of the P1 core set's membership (b) delimiting the default group**. Keep it "statistics → human decision"; no automatic switches (the governance layer's auditability principle).

**Engineering warning** (the rename reverse-lookup table): if a tool's name/description is changed to improve search hits, an original_name→dispatch reverse lookup must be retained (the shared lesson of MetaMCP tool-overrides / IBM ADR-0011 dual-field storage).

**The zero-vector red line gains three more endorsements**: MCP's official registry deliberately has no categories field ("deliberately unopinionated"); Composio's semantic search is experimental and admits instability itself; academic embedding retrieval is all a product of the 16,000-API scale (the 166 scale is over-engineering). Industry consensus: **the first lever on tool-selection accuracy is name+description quality** — the OS's energy should go into polishing descriptions rather than classification mechanisms.

**Additions to the not-applicable list**: three-level category trees (no first-hand project at the O(100) scale uses 3 levels), facade compression (lasso's get_metadata+run_tool ≈ a CC ToolSearch equivalent), OCI catalog distribution, runtime mcp-find/add dynamic registration, exposing multiple endpoints per audience (with a single client, the OS degenerates to environment-variable group selection, which suffices).

## 3. Key numbers for reference (all first-hand)

- Accuracy inflection point: degradation beyond 30-50 tools (platform.claude.com tool-search-tool docs)
- Tool Search measured: Opus 4 49%→74%, Opus 4.5 79.5%→88.1% (anthropic.com/engineering/advanced-tool-use)
- Tokens: five servers' 58 tools ≈55k tokens; defer cuts >85%
- Cursor's hard cap: only the first 40 tools are sent (github-mcp issue #275)
- github-mcp: 162+ tools with only 5 groups open by default; Playwright 70+ tools with 3/7 groups open by default; Sentry 25 tools, 20 read-only by default
