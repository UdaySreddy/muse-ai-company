# Token Usage Attribution v1 — Design

**Status**: Proposed (design first; implementation starts separately after approval)
**Task**: b1a1df19-36ff-44ef-bb71-6f92abd3ea06
**Code baseline**: master@2b651f8
**Forensics date**: 2026-07-29 (the production database was opened read-only throughout with `mode=ro`; no writes of any kind)

---

## 0. Summary and three non-negotiable premises

### 0.1 In one sentence

Compute "how many tokens a given piece of work burned" from the **file truth** (the transcripts), attribute it level by level along project / session / workflow / agent; **attribution coverage and metric labels are part of the data itself**, not a footnote on a page.

### 0.2 The biggest difference between this design and existing beliefs (read this section first)

The headline number cited when this project was chartered — "186 million tokens already sit in the database; only the display layer is missing" — **uses the wrong metric**.

`workflow_agents.tokens` is not a billing metric. The truth source is `_last_assistant_ctx_tokens` at `src/aiteam/api/workflow_ingest.py:220`, whose comment says it very clearly:

> The D1-ruled metric: the four-field sum of input + cache_creation_input + cache_read_input + output (reconciled directly against the wf_\<id\>.json terminal per-agent tokens: error agents match exactly, done agents are only 3~12% higher). **Cross-round accumulation is rejected (cache_read counted repeatedly inflates ~445x)**.

That is, it takes the **four-field sum of the last assistant message = a snapshot of the instantaneous context level**, not how much that agent consumed in total. Whereas `src/aiteam/services/token_attribution.py`'s `parse_transcript_usage` does **accumulation across requestIds**, which is the real usage metric. The two metrics are semantically orthogonal.

A measured comparison on the same batch of agents (6 rows that already had `workflow_agents.tokens` and were also collected by D1 into the five columns):

| agent | `workflow_agents.tokens` (level snapshot) | Four-layer accumulation (usage metric) | Multiple |
|---|---:|---:|---:|
| wf-abc2a662bb | 117,800 | 632,504 | 5.4x |
| wf-a707a925bc | 133,177 | 1,471,825 | 11.1x |
| wf-ac51f07a6c | 119,007 | 1,551,237 | 13.0x |
| wf-a4729e6909 | 111,311 | 2,195,791 | 19.7x |
| wf-af2d08f9a2 | 152,050 | 3,445,696 | 22.7x |
| wf-ad1bd33148 | 127,089 | 3,236,088 | 25.5x |

**Conclusion**: the two metrics differ by 5~25x (at the level of the 11-row total, 42x). Adding them together, placing them side by side, or putting them into a single "total tokens" is the same class of incident this repo just had with timestamps — mixing metrics. This design therefore elevates "metric" to a first-class dimension alongside "coverage": **any token number, stripped of its metric label, is meaningless**.

### 0.3 Three non-negotiable premises

| # | Premise | How it lands | Source |
|---|---|---|---|
| P1 | **Naming discipline**: everything is presented only as token usage, with no cross-dimension conversion of any kind | A **dimension allowlist machine check** (§4.4), not human vigilance | Meeting resolution |
| P2 | **Coverage as a first-class citizen**: the unattributed part must be presented honestly; a part must never pose as the whole | Lands at the **type layer** (§2.5); a page physically cannot obtain an isolated total | Meeting resolution + discipline ① no-data≠zero |
| P3 | **No timers / background daemons** | Everything triggered on demand: hook event points + explicit tool calls + lazy evaluation | Deliberate decision |

Two more premises grown out of the forensics:

| # | Premise | Reason |
|---|---|---|
| P4 | **Create no new tables** | The I10 machine check "declared in the ORM but missing in the DB = FAIL", and `create_all` only runs at startup while the production instance cannot be restarted just to create a table. `request_ledger.py` has already established this precedent: carry aggregates in events, carry facts in extra columns |
| P5 | **Never hard-code model names; observe and backfill** | A deliberate decision from 2026-07-07. Alias resolution is only a read-side fallback and must never write back to the `model` column (`MODEL_ALIAS_LEDGER` is already implemented this way) |

---

## 1. Terminology and metric definitions (the foundation of this design)

### 1.1 Two orthogonal metrics

| Metric id | Name | Definition | Existing producer | Purpose |
|---|---|---|---|---|
| `usage_sum` | Usage accumulation | Within one transcript, grouped by `requestId`, taking the **last** usage snapshot per group, then summed across groups, over four layers of tokens | `token_attribution.parse_transcript_usage` | Answers "how much did this piece of work use in total" |
| `ctx_last` | Last-round context level | The last assistant message's `input + cache_creation + cache_read + output` | `workflow_ingest._last_assistant_ctx_tokens` → `workflow_agents.tokens` | Answers "how full was this agent's context when it ended" |

There is a third existing metric that does not participate in this design: `agents.ctx_tokens / ctx_pct` (context level percentage, produced by `agent_context.measure`), used for agent reuse governance and unrelated to usage attribution. **None of the three is ever added to another.**

### 1.2 Four-layer separation is a hard requirement, not an option

The four layers of `usage_sum` are extremely unbalanced in measurement (11 already-collected sample rows):

| Layer | Amount | Share |
|---|---:|---:|
| `cache_read_tokens` | 78,866,965 | **95.6%** |
| `cache_creation_tokens` | 3,019,782 | 3.7% |
| `output_tokens` | 605,669 | 0.7% |
| `input_tokens` | 1,717 | 0.0% |

The non-cache part (input + output) is only **0.74%**.

Implication: any "total" number is in effect a synonym for "cache read volume". If only the total is presented, comparisons across models, dispatch paths and stages are all dominated by cache reads, and conclusions are systematically distorted. **Therefore the four layers must always be separable for display, and "total" must never be the only presentation, nor the default sort key.**

### 1.3 A defect to fix

`parse_transcript_usage`, when taking the model, does not filter out compact synthetic rows. Measured against a 35.1 MB main-session transcript, the returned `model` was `<synthetic>`. Whereas `session_probe.read_session_model` is correct — it explicitly skips `SYNTHETIC_MODEL`.

Subagent transcripts generally contain no synthetic rows, so D1's existing path was never bitten; but as soon as main-session collection (§3.3) goes live it will be. **Fix**: `parse_transcript_usage` reuses `session_probe`'s same constant and skipping logic instead of each writing its own copy.

---

## 2. Data model and the attribution chain

### 2.1 The real shape of the attribution chain

```
                    ┌──────────────────────────────────────────┐
                    │  transcript file (sole authoritative usage source) │
                    │  ~/.claude/projects/<slug>/<session_id>/  │
                    │       subagents/[workflows/<wf_id>/]      │
                    │            agent-<cc_id>.jsonl            │
                    └────────────────────┬─────────────────────┘
                                         │ parse (path + content)
             ┌───────────────┬───────────┼───────────┬──────────────┐
             ▼               ▼           ▼           ▼              ▼
        project_id      session_id     wf_id     agent row      four-layer tokens
             │               │           │           │              │
             │               │           │           │              └─ lands in the agents five columns
             │               │           │           └─ agents.id (cc_tool_use_id match)
             │               │           └─ workflow_runs.wf_id → workflow_agents
             │               └─ session container teams.config.owner_session_id
             └─ projects.root_path (slug reverse lookup only as verification)

                                    ┆ weak edge (currently almost no data, see §2.4)
                                    ▼
                                  task
```

**Key insight: the first four segments of the chain need no new collection at all; they are all encoded in `transcript_path`.**

Measured path samples:

```
~/.claude/projects/<project-slug>/80d0cc5e-.../subagents/agent-a672b51d.jsonl
~/.claude/projects/<project-slug>/80d0cc5e-.../subagents/workflows/wf_8cd4fced-95a/agent-af2d08f9.jsonl
```

Running regex parsing over the 1,922 registered `transcript_path` values in the production database gives a **100% success rate** (of which 94.5% carry a `wf_id`, covering 12 sessions and 6 project slugs). This is the same trick `hook_translator._extract_workflow_run_id` already uses — that code only extracted `wf_id`; this design uses the same information in full.

### 2.2 Existing entity edges and measured coverage (the basis for prioritization)

| Edge | Carrying field | Measured coverage | Verdict |
|---|---|---|---|
| agent → team | `agents.team_id` | 2,450 / 2,450 = **100%** | Usable |
| agent → project | `agents.project_id` | 2,404 / 2,450 = **98.1%** | Usable |
| agent → session | `agents.session_id` | **1 / 2,567** | **Effectively nonexistent**; derive from paths instead |
| agent → workflow | `workflow_agents.os_agent_id` | 2,331 / 2,450 = 95.1% | Usable |
| agent → transcript | `agents.transcript_path` | 1,922 / 2,450 = **78.4%** | Usable (and 150/150 sampled files still on disk) |
| team → task | `tasks.team_id` | **1 / 161** | **Unusable** |
| agent → task | `agents.current_task` | **0 / 2,567** | Dead field |
| agent ↔ task (evidence) | `task_memos.author` × `task_id` | 306 pairs; but only 19 agent rows match by name, 12 uniquely | Weak |

Two facts that must be stated plainly:

* **`agents.session_id` is empty.** Of the table's 2,567 rows only 1 is non-empty, and all 117 Leader rows are NULL. The comment at `hook_translator.py:1904` also acknowledges the existence of rows where "session_id was cleared/never set". So the `session → agent` hop written in the resolution **does not currently exist in the DB**.
* **The `task` edge basically does not exist.** Of 248 teams only 1 has a task attached; the number of subagent rows landing on "teams with a task" is **0**. `tasks.assigned_to` stores an agent **name string** (`'tagger-fix'` / `'leader'`) rather than an id, and only 28/161 rows have a value.

### 2.3 At dispatch time the task binding is unavailable (why "add a column" cannot fix it)

The `SubagentStart` payload contains only `agent_id` / `agent_type` / `session_id` / `cc_team_name` (see `hook_translator._on_subagent_start`) — **no prompt, no task identifier of any kind**. That is:

> At the moment an agent is born, the OS has no way of knowing which task it is working for.

Therefore the intuitive fix of "add a `task_id` column to `agents`" is wrong: **that column could never collect a value and would stay NULL forever, and in an attribution context NULL is extremely liable to be read as "this work spent no tokens"** — exactly the form that the no-data≠zero discipline names as the thing to guard against. Adding a column that cannot be filled is worse than not adding one.

### 2.4 The correct solution for the task edge: piggyback on existing bookkeeping behavior

The constraint is hard: internal data shows that in 22 days `task_memo_add` was called 711 times, `task_memo_read` 240 times, `task_update` 179 times, and "any design that requires a human to proactively call one extra tool struggles to survive" has been verified repeatedly. So **we cannot add a new "bind agent to task" action**.

Plan: **write the edge down incidentally inside bookkeeping behavior that is bound to happen anyway.**

* Trigger points: `task_memo_add` / `task_update` / `report_save` — the call parameters of these three tools **naturally carry both `task_id` and `author` (the agent name)**.
* Action: when the MCP/API server handles these three calls, if it can resolve the author to an agent row, it writes an `agent --worked_on--> task` edge.
* Storage: reuse the **already existing** `knowledge_links` table (`from_kind/from_id/to_kind/to_id/link_type` + a five-tuple UNIQUE dedupe + append-only), adding `from_kind='agent'`, `link_type='worked_on'`. **No new table** (satisfies P4), and no schema changes.
* Honest semantics: this edge means "this agent left bookkeeping on this task", not "all of this agent's tokens belong to this task". When one agent spans multiple tasks, split intervals by the edges' time order; the parts that cannot be split are **honestly counted as "task-level unattributed"**, with no averaging or apportionment — apportionment would manufacture unfalsifiable numbers.

**v1's promise for task-level attribution must be narrowed to**: providing the **data structure and derivers** for task-level attribution, and honestly reporting its coverage (the current baseline is close to 0, see §4.3). This agrees with the meeting ruling that "the differentiated wording is downgraded to 'the data structure is in place'".

### 2.5 Type-layer guardrail: making "obtaining an isolated total" physically impossible

This is the one constraint in this design that **must land at the type layer and is not accepted at the view layer**. The reason was confirmed at the meeting: a page annotation is a soft constraint, and one's future self three months from now will ignore it.

The storage layer exposes only one return structure, and **provides no interface that returns a bare total**:

```python
@dataclass(frozen=True)
class TokenAttribution:
    """The complete answer to one attribution query — the value and its denominator and metric live and die together.

    There is no `total` field, and no method returns one. A caller wanting to render a value
    must simultaneously obtain dispatches_total and metric — the denominator and the metric are
    part of the data, not optional decoration.
    """
    scope: AttributionScope        # project / session / workflow_run / agent / task
    scope_id: str
    metric: TokenMetric            # usage_sum | ctx_last — mandatory label, no default
    input_tokens: int              # the four layers in separate columns; no combined field is provided
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    dispatches_attributed: int     # numerator: dispatches in this scope whose usage has been measured
    dispatches_total: int          # denominator: total dispatches in this scope
    unattributed_reasons: dict[str, int]   # unattributed dispatches counted by reason (§3.4)
    measured_window: tuple[datetime, datetime] | None
    method: AttributionMethod      # transcript_parse | self_report | alias_fallback
```

Companion contract tests (part of the acceptance conditions, not optional):

1. `TokenAttribution` has **no** `total` / `total_tokens` field or property;
2. In any API response body, token value fields must appear at the same level as `dispatches_total` and `metric`; missing any one fails the test;
3. `metric` has no default — without labeling the metric, the object cannot be constructed.

### 2.6 Schema change list (minimal)

| Change | Table | Notes | Iron rule |
|---|---|---|---|
| Backfill (not a new column) | `agents.session_id` | Derived and filled from `transcript_path`; historical rows backfilled once + new rows written incidentally at SubagentStop | — |
| Backfill (not a new column) | `agents.transcript_path` (Leader rows) | Currently 0/117; filled by reverse lookup via `session_probe` on slug + session_id | — |
| **Add 1 column** | `agents.tokens_source` `VARCHAR(20)` | Values `transcript` / `alias_fallback`; used to audit "where this row's numbers came from". NULL = not collected | **Must sync `COLUMNS_TO_ENSURE`** (any ORM field addition must sync the migration — a project iron rule) |
| Reuse (not a new table) | `knowledge_links` | Add `from_kind='agent'` + `link_type='worked_on'` edges | No schema change |
| **Do not touch** | `workflow_agents.tokens` | It is the `ctx_last` metric; keep it as-is. A back-collected `usage_sum` **must never be written into this column** | See §6 |
| **Do not add** | `agents.task_id` | Cannot be collected, see §2.3 | — |
| **Do not create** | Any new table | P4 | — |

`tokens_source` is the only new column in this design. The reason for adding it: `method` must be auditable ("was this number settled from a transcript, or inferred by alias fallback"), and that fact must persist with the row rather than be recomputed on every query. The cost of adding a column is one `COLUMNS_TO_ENSURE` entry + ORM + types + bidirectional conversions, four places to sync — D1's five columns already demonstrated the full routine; copy it.

---

## 3. Collection gap analysis

### 3.1 Panorama of the current collection surface

| Collection surface | Carrier | Metric | Measured coverage | Nature of the gap |
|---|---|---|---|---|
| Subagents (SubagentStop) | `agents` five columns | `usage_sum` | **11 / 2,450 = 0.4%** | D1 went live 2026-07-28 16:26; only covers dispatches after that |
| Workflow agents (wf JSON / journal) | `workflow_agents.tokens` | `ctx_last` | 2,909 / 3,182 = 91.4% | Different metric; cannot be added to the row above |
| Workflow runs | `workflow_runs.total_tokens / live_tokens` | `ctx_last` aggregate | 226 / 228 | Same as above |
| Leader main sessions | **None** | — | **0 / 117** | Completely blank, see §3.3 |
| Agent tool activity | `agent_activities` | **No token field** | 0 / 52,119 | Structural absence, see §3.2 |

### 3.2 `agent_activities` has no tokens: **recommendation: do not add them**

52,119 activity records without a single token looks like the biggest hole. But architecturally, **this hole should not be plugged here**:

* `agent_activities` is at **per-tool-call** granularity, while token billing is at **per-API-request** granularity; the two are not in one-to-one correspondence (one tool call can span several requests, and a request may contain no tool call);
* Hook payloads carry no tokens; attributing per activity would mean reverse-looking-up the transcript for every activity — enormous write amplification and inevitably introducing estimation;
* Agent-level usage is already covered by the `agents` five columns, and the question "which tool did this agent spend time on" can be answered 80% by `duration_ms`.

**Conclusion**: `agent_activities` keeps no token field, and the coverage presentation explicitly labels it "tool-call-level usage: not collected by design". This is a **deliberate choice**, not an omission — written into the doc so that half a year from now nobody "fixes" it as a bug.

### 3.3 Leader main sessions: the biggest real gap, and solvable

**Status quo**: 117 Leader rows, all five token columns empty, `transcript_path` also empty.

**Feasibility, measured** (run directly this time):

* The existing `parse_transcript_usage` can parse a main-session transcript **with no modification**;
* One 35.1 MB main-session transcript took **158 ms** for a full parse, yielding 2,012 API calls and a cumulative 852,524,309 tokens (of which `cache_read` 811,684,198);
* Main-session transcripts are located by `session_probe` at `<project-slug>/<session_id>.jsonl`; the mechanism already exists (`session_last_active` works exactly this way); both `SessionStart` and `Stop` payloads carry `transcript_path`.

**Three traps that must be written into the implementation**:

1. **Must overwrite, never accumulate.** A main-session transcript is a cumulative file; each parse yields the total "from session start to now". The persistence semantics must be **snapshot overwrite**. If one copies the subagent "write once at stop" mindset and makes it an accumulation, a session is overcounted by as many multiples as it has turns.
2. **Must throttle.** `Stop` fires on every conversation turn. 158 ms × every turn is perceptible overhead, growing linearly with the session. Recommendation: at `Stop`, parse only when (a) more than N minutes have passed since the last measurement, or (b) the file's mtime has changed beyond a threshold; `SessionEnd` and `PostCompact` each force one measurement (the latter especially important — after a compact the transcript is rewritten, and without freezing a value at the compact point that segment is lost).
3. **Must filter synthetic rows** (§1.3), otherwise the Leader's `model` will be written as `<synthetic>`.

**A mandatory presentation requirement**: the magnitude of Leader main sessions (850 million in a single session) far exceeds the sum of all subagents. If the two are mixed in one ranking, subagent attribution results would be completely submerged. Therefore **main sessions and subagents must be presented in separate columns and not merged by default**.

### 3.4 Unattributed reason classification (for coverage presentation)

"Unattributed" is not one state but four, each handled differently:

| Reason code | Meaning | Current magnitude | Remediable? |
|---|---|---|---|
| `no_transcript_path` | The agent row never registered a transcript path | 528 / 2,450 | No (historical rows); new rows are covered |
| `transcript_gone` | The path exists but the file is gone | Sampled 0 / 150 | No, and it **only grows over time** |
| `not_yet_measured` | The path exists, the file exists, collection just has not run yet | ~1,911 | **Yes**, see §6 |
| `by_design` | Not collected by design (e.g. tool-call level) | 52,119 activity rows | Not applicable |

This table is the contents of the "unattributed" drawer. Any presentation surface that reports coverage must be able to drill down to this table — otherwise "22% unattributed" is a sentence nobody can act on.

---

## 4. Coverage: definition, measurement and gates

### 4.1 Coverage is a vector, not a scalar

The chain has multiple hops, each hop has its own coverage, and end-to-end coverage is the product of the hops. Expressing it as one scalar would mask the real bottleneck. Definitions:

```
C_measure(scope)  = dispatches with measured usage / total dispatches in that scope
C_hop(edge)       = rows where the edge resolves / rows needing that edge
C_e2e(scope)      = measured usage attributable to that scope / all measured usage
```

**The denominator definitions must be pinned down in code and covered by unit tests**, because the denominator is the only place that can be quietly faked:

* The denominator of `C_measure` = rows in the `agents` table with `role != 'leader'` (= the number of dispatches), **including** rows with no transcript. They must not be excluded on the grounds of "no path, so not counted" — that is exactly letting a part pose as the whole.
* Leader main sessions are counted separately, with denominator = rows with `role = 'leader'`.
* Denominators are always computed by `created_at` falling in the query window, **never by `tokens_measured_at`** (otherwise unmeasured rows vanish from the denominator and coverage is permanently 100%).


### 4.2 Measurement method

Triggered on demand, with no periodic task of any kind (P3):

* **Entry points**: one read-only aggregate query + one MCP tool + one API endpoint, all three sharing the same storage method and returning `TokenAttribution`;
* **Trigger moments**: when the Dashboard opens the page, when the MCP tool is called, and `os_health_check` incidentally outputs a one-line summary;
* **Consistency self-check**: parsing the same transcript twice must give field-for-field identical results (the parser is a pure function, so this can be a unit test rather than a runtime check).

### 4.3 Baseline (today's real numbers)

| Metric | Metric id | Measured |
|---|---|---|
| `C_measure` (subagents) | `usage_sum` | **11 / 2,450 = 0.4%** |
| `C_measure` (subagents, estimated after back-collection) | `usage_sum` | 1,922 / 2,450 = **78.4%** |
| `C_measure` (Leader main sessions) | `usage_sum` | **0 / 117 = 0%** |
| `C_measure` (workflow self-report) | `ctx_last` | 2,909 / 3,182 = 91.4% |
| `C_hop` (agent→session, path-derived) | — | 1,922 / 1,922 = **100%** |
| `C_hop` (agent→project) | — | 2,404 / 2,450 = 98.1% |
| `C_hop` (agent→workflow) | — | 2,331 / 2,450 = 95.1% |
| `C_hop` (agent→task) | — | 19 / 2,450 = **0.8%** |
| `C_e2e` (task-level attribution) | `usage_sum` | ≈ **0.8%** (limited by the task edge) |

Visible at a glance: **the bottleneck is not token collection; it is the task edge.** Even if every transcript is back-collected, task-level attribution remains close to zero. That is the reason the §2.4 "piggyback on bookkeeping behavior" plan exists, and why v1 must not speak of "attribution by task" as a capability it already has.

### 4.4 Gate recommendations (including one correction to the existing formulation)

The meeting-ruled three-gate structure is kept, but **gate 2's threshold as literally stated is unsatisfiable and must be corrected**.

The original formulation was "self-reported vs transcript-measured deviation <10%". Per §0.2's measurements, the self-reported value is `ctx_last` and the transcript measurement is `usage_sum`; the two naturally differ by 5~25x — this gate is not hard to pass, it is **impossible to pass**, because it compares two different quantities. Corrected:

| Gate | Purpose | Threshold (corrected) |
|---|---|---|
| **Gate 1** | Internally visible | No numeric threshold; but must satisfy: ① every number carries a `metric` label; ② numerator and denominator returned in the same structure; ③ coverage is in the first block of the first screen; ④ the unattributed part can drill down to §3.4's four categories |
| **Gate 2** | May enter external materials | ① **Same-metric** reconciliation: sample ≥30 agents, `usage_sum` computed via two independent paths (parser / manual transcript check), field-by-field deviation **= 0** (a pure function; zero tolerance rather than 10%); ② on the `ctx_last` side, reconcile separately: the deviation between `workflow_agents.tokens` and a recomputed `ctx_last` is **<10%** (this is where the original threshold belongs); ③ `C_measure` (subagents) ≥ 95%; ④ every `C_hop` edge ≥ 95%, **except the task edge, which must be separately labeled with its true value** |
| **Gate 3** | May be used for pre-dispatch judgments | Gate 2 all green, and `C_measure` ≥ 95% sustained across two versions. A judgment may only be a one-time check at dispatch time (`PreToolUse`); **resident monitoring is prohibited** (P3) |

**One additional red line (recommended for machine checking)**:

> Any token number on any presentation surface whose scope's `C_measure < 100%` must display the unattributed part at the same level on the same screen. Its absence counts as a red-line violation.

And P1's machine-check form — a **dimension allowlist** rather than a banned-word list, because an allowlist is a closed, enumerable set that cannot leak:

> Usage-related presentation surfaces (API schema field names + frontend display units) allow exactly four dimensions: **tokens (four layers in separate columns), counts, duration in milliseconds, percentages**. The appearance of any dimension outside these four fails the check.

Why an allowlist beats a blocklist: a blocklist must enumerate every out-of-bounds spelling (multiple languages, symbols, abbreviations, slang) to take effect, and one leak breaks it; an allowlist only needs to confirm four legal values, and adding a new dimension must explicitly pass review.

---

## 5. Dashboard presentation draft

### 5.1 Landing point: a new `/usage` page

* **Not** crammed into `/analytics`: that page is about team output efficiency, and usage attribution is a different reader mindset; mixing them would dilute the coverage labeling.
* **Not** given a prominent sidebar position: this is an internal diagnostics page, not first-screen narrative material.
* Adding a route changes the route count in `App.tsx`, and **the page counts in the bilingual README must be synced in the same batch** (the I6 machine check is a hard equality). This is the concrete form of "adding a page is more expensive than it looks"; count it in the estimates.

### 5.2 Page structure (top to bottom; order is priority)

**① Coverage matrix (first block of the first screen, cannot be folded or closed)**

This is the page's main body, not its header. The table looks like:

| Dispatch path | Dispatches | Measured | Coverage | Metric |
|---|---:|---:|---:|---|
| Subagents (direct dispatch) | 119 | n | n% | `usage_sum` |
| Subagents (workflow) | 2,331 | n | n% | `usage_sum` |
| Leader main sessions | 117 | n | n% | `usage_sum` |
| Workflow self-report (historical) | 3,182 | 2,909 | 91.4% | `ctx_last` |
| Tool-call level | 52,119 | — | Not collected by design | — |

Design points:
* Every row carries a metric column — `ctx_last` rows and `usage_sum` rows must be visually distinguishable (different background + metric badge), **and this table offers no "total" row**;
* "Not collected by design" is a formal value, not a blank — a blank would be read as a bug.

**② Unattributed drawer (directly beneath the matrix, expanded by default)**

Counts split by §3.4's four reason codes, each expandable to sample rows. This block exists to turn "78% coverage" into an actionable sentence — after reading it, the user should know whether the remaining 22% can be rescued and how.

**③ Attributed details (drill-down)**

Hierarchy: `project → session → workflow_run → agent`. Each level is a `TokenAttribution` card, the four token layers shown in separate columns, **with no combined total shown**. The default sort key is `output_tokens` (the only layer strongly correlated with "how much work was done"), not the sum of the four layers — see §1.2 for the reason.

Task level is presented as a **side branch**, a standalone card whose title explicitly reads "Task-level attribution (coverage n%; the rest is task-level unattributed)".

**④ Single-measurement card (a dedicated channel for external examples)**

A copyable card: one real dispatch's input summary, output summary, four-layer tokens, duration, and model (obtained by observation).

This card is **explicitly exempt from the coverage gates** — it is a single measurement settled as truth, not a full ledger, and the two chains are decoupled. A fixed line must be printed on the card: **"Single measurement, not a full ledger"**. This exemption was explicitly ruled at the meeting, but its **cost** must be honored: card data may only come from parsing a single transcript, never from an aggregate view (otherwise the exemption becomes a backdoor around the gates).

**⑤ Metric explanation (permanent page footer)**

A fixed paragraph explaining the difference between `usage_sum` and `ctx_last` and why the two are not added together. Written into the page rather than a doc, because people looking at the numbers will not go read the doc.

### 5.3 Other touchpoints

* **MCP tool**: add one read-only tool returning `TokenAttribution`. Note that adding a tool simultaneously triggers I6 (the README tool-count hard equality) and I9 (the parameter-description machine check); the bilingual README must change in the same batch.
* **`os_health_check`**: incidentally outputs a one-line coverage summary — on-demand triggered, zero new daemons.
* **Existing pages untouched**: the tokens `WorkflowsPage` / `AgentLivePage` currently display are the `ctx_last` metric; v1 does only one thing to them — **adds a metric badge**, changing neither the data nor the algorithm.

---

## 6. Whether historical back-collection joins v1

### 6.1 Fact baseline

The back-collection target is much larger than "170 opus-alias rows":

| Group | Rows | With `transcript_path` | Back-collectable rate |
|---|---:|---:|---:|
| `workflow_agents` with `model='opus'` and tokens=0 | 170 | 138 | 81.2% |
| `workflow_agents` with `model='claude-fable-5'` and tokens=0 | 85 | 44 | 51.8% |
| `workflow_agents` remaining zero-token rows | 18 | 0 | 0% |
| **Zero-token subtotal** | **273** | **182** | **66.7%** |
| All subagent rows in the `agents` table | 2,450 | 1,922 | 78.4% |

Sampling 150 registered paths, **all 150 files are still on disk**. Checking `os.path.exists` one by one for the 138 paths of `model='opus'`, **138/138 survive**.

### 6.2 Pros

1. **This is the only step that lifts coverage from 0.4% to 78.4%.** Without back-collection, the v1 page at launch is a sea of "unattributed", gate 2 is never reachable, and the whole direction is judged as spinning in place.
2. **The window is closing.** CC by default cleans up older local session history, and the `transcript_gone` category only grows. Today it is 150/150 surviving; three months from now it may not be. **The cost of back-collection rises monotonically with time; its benefit falls monotonically.**
3. **Extremely low technical risk.** It only writes "columns that were never filled" (the `agents` five columns + `tokens_source`): naturally idempotent, dry-runnable, re-runnable, discardable on failure. Subagent transcripts have a median of only 97 KB and a maximum of 10.9 MB; a full parse of all 1,922 is a minutes-scale job.
4. **It also solves the alias problem, without violating the deliberate decision.** The true model names of the 138 opus-alias rows can be read directly from the transcript's `message.model` (transcripts always carry the full model name) — this is **observational backfill**, exactly what the "leave the model default empty, backfill by observation" decision wanted, and it does not conflict with "never hard-code model names". The remaining 32 rows without transcripts go through the `MODEL_ALIAS_LEDGER` read-side fallback, returning None outside the window rather than guessing.

### 6.3 Cons

1. **Metric contamination risk (the biggest one).** Back-collection produces `usage_sum`, while `workflow_agents.tokens` is `ctx_last`. If for convenience the back-collected value were written into that column, the 5~25x metric mixing would be **permanently solidified into historical data**, and afterwards it would be impossible to tell which rows are which metric. That is much worse than not back-collecting.
2. **Coverage will "inflate", masking the real health metric.** After back-collection, total coverage jumps to 78%, but "the collection rate of new dispatches" is a different matter — the latter is the metric that judges whether the collection chain is healthy. One number masks the other.
3. **A historically uneven window.** Back-collection can only cover rows whose transcripts still exist; early rows are missing forever, so **any time-series chart crossing the back-collection boundary is distorted**.
4. **It incidentally modifies 2,000 rows of production data.** Even though idempotent, it needs backup and dry-run gates.

### 6.4 Recommendation: **join v1, but with three hard constraints**

**Recommendation: do it, because 6.2-2 (the window is closing) overrides everything** — this is the only item in this design where "doing it late means never being able to do it"; everything else done late is just done late.

Three hard constraints:

1. **Write only the `agents` table's five columns + `tokens_source`; never touch `workflow_agents.tokens`.** That column keeps the `ctx_last` metric unchanged. When the read side needs to display a workflow agent's `usage_sum`, it joins via `os_agent_id` to the `agents` row to fetch it — **solved by joining, not by overwriting**.
2. **Coverage must be presented windowed by `tokens_measured_at`**, splitting "historical backfill" and "incremental collection" into two numbers. Back-collection must not make the incremental collection rate invisible (countermeasure to 6.3-2).
3. **The backfill script is on-demand triggered, dry-run first, re-runnable with zero changes.** Implementation follows the UTC shift script's precedent: dry-run produces a report first, a human reviews it, then `--apply`; with an idempotency marker, re-running produces no changes.

**Parts not to do**: do not change agent templates' frontmatter for the backfill (`model: opus` is a tier-level alias frozen by the 2026-07-10 ruling), and do not write alias-resolution results back into any `model` column.

---

## 7. Implementation split (each stage independently acceptable)

The order has dependencies, but each stage is separately deliverable, rollback-able, and acceptable.

### Stage 0 — Metric naming and machine checks (S)

* `parse_transcript_usage` reuses `session_probe`'s synthetic-row filtering (§1.3);
* Define the `TokenMetric` enum and the two metrics' constants; label the read side of `workflow_agents.tokens` as `ctx_last`;
* Land two machine checks: the dimension allowlist (§4.4) and the coverage same-screen red line; deliberately break one place to verify the machine checks really go red.

**Acceptance**: the machine checks are reproducibly red, and green after fixing; unit tests cover synthetic-row filtering.

### Stage 1 — Chain deriver (S–M)

* Implement the `transcript_path → (project_slug, session_id, wf_id, cc_agent_id)` parser (regex + unit tests, including three sample classes: no wf, worktree paths, non-ASCII slugs);
* Backfill `agents.session_id` (the historical 1,922 rows + new rows written incidentally at SubagentStop);
* Backfill Leader rows' `transcript_path` (via `session_probe` reverse lookup).

**Acceptance**: 100% parse success over the 1,922 rows; a random sample of 30 rows manually verified for correct session attribution; non-ASCII slugs do not misjudge the project (with `agents.project_id` authoritative, the slug only a cross-check).

### Stage 2 — Coverage and the type layer (M)

* Definitions of `TokenAttribution` / `AttributionScope` / `AttributionMethod`;
* A storage-layer aggregation method + a read-only API + an MCP tool;
* Contract tests: no bare total exists, `metric` has no default, numerator and denominator at the same level.

**Acceptance**: contract tests all green; the API cannot return an isolated total under any parameter combination; the coverage baseline numbers agree with §4.3.

### Stage 3 — Historical back-collection (M)

* An idempotent backfill script, dry-run first, human review, then `--apply`;
* Writes only the `agents` five columns + `tokens_source`.

**Acceptance**: the dry-run report agrees row by row with the actual writes; re-running after `--apply` produces zero changes; `workflow_agents.tokens` unchanged row by row (checksum comparison); coverage visible split by window.

### Stage 4 — Leader main-session collection (M)

* Throttled measurement at `Stop` + forced measurement at `SessionEnd` / `PostCompact`;
* Snapshot-overwrite semantics + targeted unit tests for the three traps.

**Acceptance**: after one real session ends, the Leader row has values; **triggering 10 times repeatedly leaves the values unchanged** (guards against accumulation); one freeze each before and after a compact; the extra overhead of a single `Stop` is measurable and zero within the throttle window.

### Stage 5 — The `/usage` page (M)

* The five-block structure (§5.2) + metric badges + unattributed drill-down;
* Sync the bilingual README page count (I6) and tool count in the same batch (if stage 2 added an MCP tool).

**Acceptance**: coverage is in the first block of the first screen; the four unattributed categories are expandable; `ctx_last` and `usage_sum` are visually distinguishable with no total row; the I6 machine check is green.

### Stage 6 — Not in v1

The pre-dispatch usage judgment gate (the original A4). Precondition: gate 3 all green and sustained across two versions. Its form can only be a one-time `PreToolUse` judgment; **resident monitoring is prohibited**.

### Dependencies and parallelism

```
Stage 0 ──┬─→ Stage 1 ──→ Stage 2 ──┬─→ Stage 3
          │                         └─→ Stage 5
          └─→ Stage 4 (can run in parallel with 1/2; only depends on stage 0's synthetic-row fix)
```

Stages 3 and 5 can run in parallel. Stage 4 depends only on stage 0 and can start earliest, but its **presentation** depends on stage 5.

---

## 8. Risks and open questions

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Metric mixing recurs — a later arrival adds the two token columns together | Numbers off by an order of magnitude | The type layer's `metric` has no default + the page-footer metric explanation + this file's §0.2 |
| R2 | Coverage gets "optimized" to 100% (rows without data moved out of the denominator) | A part posing as the whole; discipline ① breached | Denominator definitions pinned down + unit tests + the same-screen red-line machine check |
| R3 | Back-collection writes `usage_sum` into the `ctx_last` column | Historical data permanently contaminated, indistinguishable | Stage 3 acceptance includes a row-by-row checksum comparison that `workflow_agents.tokens` is unchanged |
| R4 | Leader collection implemented as accumulation | Values inflate linearly with turns | Stage 4 acceptance includes "triggering 10 times repeatedly leaves the values unchanged" |
| R5 | Task-level attribution spoken of externally as "a capability we already have" | Credibility risk (claiming solved while the table is empty) | v1 only promises "the data structure and derivers are in place", and mandates same-screen disclosure of the true 0.8% coverage |
| R6 | `transcript_gone` grows over time | The back-collection window closes | Stage 3 is prioritized early, not scheduled at the end of v1 |
| R7 | Main-session transcripts growing makes parsing slower | Perceptible latency on the `Stop` path | Throttling + the measured baseline (35.1 MB / 158 ms) as a regression threshold |

**Open questions (to confirm before implementation, but not blocking this design's approval)**:

1. **Why is `agents.session_id` empty across the whole table** — was it never written, or cleared somewhere? The comment at `hook_translator.py:1904` implies both situations exist. Stage 1's backfill can bypass the question, but the root cause is worth investigating in passing, or newly written values might be cleared again by the same mechanism.
2. **The transcript rewrite behavior after a compact** — after `PostCompact`, is the original transcript truncated, replaced, or archived? This directly decides whether stage 4's "forced freeze" really preserves the pre-compact segment's usage. Needs one measurement.
3. **The relationship between `workflow_agents.tokens` and the wf JSON self-reported values** — D1's comment says the two are "exactly equal for error agents, 3~12% higher for done agents". Whether that 3~12% deviation itself needs deducting in gate 2's `ctx_last` reconciliation needs one sampling confirmation.

---

## Appendix A: Forensics method and raw numbers

**Method**: the production database was opened via a `sqlite3` read-only URI (`mode=ro`); no write statement of any kind was executed and no files other than snapshot copies were created; transcript files were only read. Code-side comparison against `master@2b651f8`.

**Key raw numbers** (2026-07-29):

```
events                     247,469 rows   2026-07-06 02:05 → 2026-07-29 02:14 (DB age = observation window = 23 days)
agents                       2,567 rows   non-leader 2,450 / leader 117
  transcript_path non-empty    1,922 rows   sampled 150/150 files still exist (median 97 KB, max 10.9 MB)
  tokens_measured_at non-empty    11 rows   D1 went live 2026-07-28 16:26
  session_id non-empty               1 row
  project_id non-empty (non-leader) 2,404 rows
workflow_agents              3,182 rows   tokens>0: 2,909   total 186,995,224 (ctx_last metric)
  model='opus' and tokens=0     170 rows   of which 138 have transcripts and all files survive
  model='claude-fable-5' and 0   85 rows   of which 44 have transcripts
agent_activities            52,119 rows   no token field
tasks                          161 rows   team_id non-empty 1 row; assigned_to non-empty 28 rows (stores agent names)
teams                          248 rows   teams with a task attached: 1
knowledge_links                        existing from_kind ∈ {task_memo, report}, to_kind ∈ {commit, run, task, memory}
Main-session transcripts               7   largest 42.8 MB / second largest 35.1 MB / total 82.5 MB
  35.1 MB parse measurement        158 ms   2,012 API calls, four-layer total 852,524,309
```

**The raw two-metric comparison rows** are in §0.2's table.

**Path parsing validation**: the regex `/projects/(?P<slug>[^/]+)/(?P<sid>[0-9a-f-]{36})/subagents/(?:workflows/(?P<wf>wf_[^/]+)/)?` matched all 1,922 paths, of which 1,817 carry a `wf_id`.

## Appendix B: Code touchpoint index

| File | Existing responsibility | Involved in this design |
|---|---|---|
| `src/aiteam/services/token_attribution.py` | `usage_sum` parsing + alias ledger | Stage 0 fixes synthetic-row filtering; stage 1 adds the path parser (or a separate module) |
| `src/aiteam/api/workflow_ingest.py:220` | `ctx_last` computation | Unchanged; only label the metric on the read side |
| `src/aiteam/api/hook_translator.py:661` | `SubagentStop` backfills the five columns | Stage 1 writes `session_id` incidentally; stage 0's `tokens_source` |
| `src/aiteam/api/hook_translator.py:1710/1929` | `SessionStart` / `Stop` | Stage 4's main-session collection mount points |
| `src/aiteam/api/session_probe.py` | Main-session location + synthetic-row filtering | Stage 0 reuses it; stage 1 reverse-looks-up Leader transcripts |
| `src/aiteam/api/request_ledger.py` | Request-level ledger | Unchanged; a paradigm reference for "on-demand triggering + event aggregation + no new tables" |
| `src/aiteam/storage/models.py` | ORM | Stage 0 adds the `tokens_source` column |
| `src/aiteam/storage/connection.py` | `COLUMNS_TO_ENSURE` | **Adding a column must sync here** (project iron rule) |
| `scripts/check_invariants.sh` | Red-line machine checks | Stage 0 adds two |
| `dashboard/src/App.tsx` | Routing | Stage 5 adds `/usage`, syncing the I6 page count |
