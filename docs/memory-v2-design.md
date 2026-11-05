# Memory System v2 Design — Two-Layer Ledger + On-Demand Tidying

> Status: **implemented** (P0/P1/P2 in production since v1.9.0) | First draft 2026-07-12 | **v2.1 revision 2026-07-31**
> Sources: discussions 1 and 2 (tasks f5524057 / 3a1c54aa) + three lines of industry-practice research (wf_e75cf7d4, adversarially verified line by line)
> Positioning red line: **The OS is Claude Code's governance/observation ledger layer; memory is a subsystem, not a professional memory product.**
>
> What v2.1 changed (details in §3.1, all implemented): the direction layer's volume red line changed from the **two axes** of "≤40 entries × 400 characters"
> to a **single axis, "storage cap = injection budget"** (per-bucket character quotas); exceeding the cap changed from a one-line hint to a **protocol**
> (hand back the full bucket list + the usage gap + a same-turn tidying instruction); `memory_add` gained a **write-side safety scan**;
> `memory_invalidate` gained **substring location**; meeting conclusions **stopped being automatically written into direction/team memory**.

## 0. Evidence audit (a credibility statement for the design basis)

After adversarial verification, this design stands only on **confirmed-level evidence**:

| Evidence (confirmed) | Design decision it supports |
|---|---|
| ChatGPT's two-layer memory: saved memories (explicit/auditable/resident reference) + reference chat history (background synthesis/on-demand recall) — OpenAI's official help center | The two-layer model (direction layer resident + episodic layer on demand) has consumer-grade official proof |
| The Zep paper's bi-temporal model: on conflict, invalidate an edge rather than delete it (arXiv, t_valid/t_invalid + transaction ordering) | "Explicit invalidation without deletion" (discussion 1's conclusion) |
| The mem0 paper's two-stage pipeline: first retrieve the top-K similar memories as candidates, then the LLM directly chooses ADD/UPDATE/DELETE/NOOP | memory_reconcile's four-operation decision procedure (BM25 in place of vectors) |
| OpenAI Agents SDK Sessions: written text appended item by item, zero LLM, SQLite default backend | "Light writes, zero LLM" is a legitimate default for a first-tier SDK; extraction is an optional enhancement, not a forced write path |
| Google ADK: short-term raw events (no LLM) separated from the long-term store; long-term-ization is an **explicit API** (add_session_to_memory, usually at session completion) | Tidying is an on-demand explicit action within a session, not a background daemon — exactly fitting CC's non-residency |
| The dual read forced out by Zep's asynchronous ingestion: the prompt assembles the long-term context string + the most recent few raw items | The injection-time assembly paradigm of "direction-layer summary + recent records" |

Partial level (the main trunk holds; details are used with reduced weight): the CoALA four-way classification (arXiv:2309.02427 §4.1 does have working/episodic/semantic/procedural, but "tracing it to Tulving/ACT-R" was research padding — **only the terminology naming is borrowed, with no cognitive-science endorsement claimed**); Letta core memory blocks compiled resident into the system prompt + character caps; Letta sleep-time compute (an offline tidying agent exists; its default parameters were not verified verbatim); Generative Agents reflection (arXiv:2304.03442, triggered when accumulated importance crosses a threshold, producing high-level conclusions with citations); CrewAI's old LTM using SQLite with zero embeddings (the docs have since been revised; a historical precedent only).

Rejected arguments: LangMem's "data-independent vs data-dependent" as an official quote (verification judged it unsupported; the researcher's own synthesis).

## 1. Overall shape: two layers + one tool

```
┌─ Direction layer (memories table activated) ─┐
│ Preferences/corrections/design intents/constraints │
│ Low frequency · high value density · cross-task long life │
│ Write: Leader explicitly memory_adds (when the user gives a preference) │
│ Read: SessionStart + SubagentStart resident injection  │ ←—— killer: a dispatched agent inherits from birth
└──────────────────△───────────────────────┘
                   │ Distillation and promotion (with source_refs provenance)
┌─ Episodic layer (task_memos promoted to a table) ─△─┐
│ Task process records/conclusions/failure causes │
│ High frequency · written automatically by agents · large volume │
│ Write: task_memo_add (interface unchanged) │
│ Read: unified_search on-demand retrieval (BM25, three arms) │
└───────────────────────────────────────────┘
        △ Between the two layers: memory_reconcile (the on-demand tidying tool, merging items 1, 2 and 8)
```

- Relation to CC memory: MEMORY.md is the Leader's personal memory; memories are the **team-shared direction memory** (observable, governable, inheritable by agents).
- Decisions do not enter the table: events stay append-only; overturning = appending a `decision.superseded` event, with validity as a derived view (settled in discussion 1).

## 2. Episodic layer: task_memos promoted to a table (P0)

A memo currently is a `tasks.config` JSON array — no row-level ID, no index, no way to hang an invalidation axis/quality score on it. Promote it to a real table:

```sql
CREATE TABLE task_memos (
    id            TEXT PRIMARY KEY,          -- uuid, a real ID (referenceable by knowledge_links)
    task_id       TEXT NOT NULL,             -- FK tasks.id (natural provenance)
    project_id    TEXT,
    author        TEXT DEFAULT 'leader',
    memo_type     TEXT DEFAULT 'progress',   -- progress/decision/issue/summary
    content       TEXT NOT NULL,
    scope_path    TEXT DEFAULT '',           -- item 2: path scoping /project/ecosystem/research
    quality_score INTEGER,                   -- item 8: quality score (NULL=unscored, filled at tidying time)
    invalid_at    DATETIME,                  -- item 1: invalidation axis (NULL=valid)
    invalidated_by TEXT,                     -- the replacing memo id
    meta          JSON DEFAULT '{}',         -- entities/topics (filled at tidying time)
    created_at    DATETIME NOT NULL
);
CREATE INDEX idx_memos_task ON task_memos(task_id);
CREATE INDEX idx_memos_valid ON task_memos(project_id, invalid_at);
```

- **The interface is completely unchanged**: the `task_memo_add` signature stays as-is, with a new optional `supersedes=<memo_id>` (writing sets the old entry's invalid_at, zero LLM). All agents' write-back instructions change not at all.
- Migration: one-off backfill of each tasks.config.memo array into rows (historical memos have no id → generated at migration time).
- Read side: the unified_search BM25 arm changes from "unpacking all the JSON" to querying the table directly, filtering `invalid_at IS NULL` by default, with an `include_invalidated` switch.

## 3. Direction layer: the memories table activated and repositioned (P1)

Reuse the existing empty table; add columns rather than building a new table:

```sql
ALTER TABLE memories ADD COLUMN kind TEXT DEFAULT 'preference';
    -- preference / directive (instructions · working style) / constraint / design (design intent)
ALTER TABLE memories ADD COLUMN invalid_at DATETIME;
ALTER TABLE memories ADD COLUMN invalidated_by TEXT;
ALTER TABLE memories ADD COLUMN source_refs JSON DEFAULT '[]';  -- item 4: provenance, pointing back to memo/report/meeting ids
-- scope semantics narrowed: global / project / user (no more task level — that is the episodic layer's business)
```

- **Write entry (this time it really gets built)**: MCP `memory_add(content, kind, scope, supersedes?)` + `memory_invalidate(id)`.
  Behavior rules (entering the Leader rule set): **when the user gives a preference/correction/direction design, the Leader records an entry on the spot**; a changed preference = a new entry supersedes the old one (Zep invalidation semantics).
- **Volume red line** (the lesson of Letta blocks' character caps): **from v2.1, a single axis; see §3.1**. (The first draft's measure: at most 40 valid entries per project and at most 400 characters per entry, with a hint past the cap to "tidy first, then add" — kept here as the overturned original design.) **The direction layer's value lies in being small and accurate, not in being numerous.**
- **The read side = this layer's reason to exist**:
  - `session_bootstrap.py` (SessionStart): the briefing gains a "Direction memory" section (valid entries grouped by kind);
  - `inject_subagent_context.py` (SubagentStart): **every dispatched agent is injected with the direction layer from birth** — preferences like "everything in Chinese" and "report as soon as completed" no longer depend on the Leader hand-copying them into prompts;
  - Injection budget: **from v2.1, merged with the storage cap into the same axis; see §3.1** (the first draft's measure: the two places together ≤ 2000 characters).

## 3.1 v2.1 revision: a single axis + over-cap protocol + write safety (implemented 2026-07-31)

### 3.1.1 The lesion: the storage red line and the injection budget were not on the same axis

The first draft set the two things separately: on the storage side, "≤40 entries per bucket × ≤400 characters per entry" = 16,000 characters allowed, while the injection side
got 900 characters. The two numbers differ 17-fold, and the consequence is not "the extra stored is useless" but **the extra stored is the same as not stored**: measured,
of 48 valid entries, only the first 2-3 in line really entered an agent's system prompt; the rest were folded into a single sentence,
"there are 46 more; the Leader can view them with memory_list". And "a dispatched agent inherits the direction layer from birth" is precisely this layer's
only reason to exist — the red line itself hollowed out the selling point, and it was visible neither in the UI nor in tool returns:
writes succeed, injection silently truncates.

### 3.1.2 The ruling: a single axis, "storage cap = injection budget", + per-bucket quotas

Only one number is kept: **a direction-layer injection pool of 3000 characters**. Per-bucket quotas (keyed by `(scope, scope_id)`):

| Bucket | Quota | Notes |
|---|---|---|
| `global` / `system` | 1200 characters | Discipline that holds across directories and sessions |
| Each `project` | 1500 characters | Specific to this project; counted once per project |
| `user` | 300 characters | User-level preferences |

What one session actually inherits = global + this project's project bucket + user = **3000 characters**. The ≤400 characters per entry is kept
(the pointer-entry philosophy is unchanged: a trigger condition + a pointer to the authoritative file, with the body externalized). The entry-count axis (`≤40 entries`) is **abolished**
— characters are the genuinely scarce resource on the injection side; entry counts are not.

- **Verification measure**: after a write, the bucket's total characters over valid entries (`invalid_at IS NULL`) must be ≤ the quota. With
  `supersedes`, what is computed is the total **after the replacement** (the old entry's characters count as freed), or "swap a long entry for a
  short one" would be wrongly rejected — that is the inertia of entry-count-axis thinking.
- **The injection side's 900 becomes 3400** (3000 + formatting overhead), and its semantics change from "the normal truncation line" to a **fuse**:
  the write side already clamps the stored volume dead, so in normal circumstances it never triggers; it only backstops the one anomaly of "someone bypassed the API and edited the DB directly, blowing up the direction layer",
  preventing the memory section from drowning the briefing/accounting conventions/identity blocks. Both hooks are pure-stdlib processes
  (not importing the `aiteam` package), each defines its own constants, kept in sync with the byte-for-byte copies in `plugin/hooks` (I1 machine check).
- **Transitional state (important)**: on the day of the axis change, the live database was in an **over-quota state** (the project bucket's 33 entries far exceed 1500 characters).
  The red line **only blocks new writes; it does not retrospect, migrate, or delete any row** — the over-cap response truthfully reports the current usage, and the capacity
  is pressed back within quota by later distillation tasks under the user's review. The governance-layer principle of never black-box-changing data takes precedence over pretty numbers.

### 3.1.3 The over-cap protocol: making tidying happen at the moment of rejection

The old behavior was to reply "tidy first, then add"; its actual effect was that the caller retried with a shorter sentence, and the capacity pressure was handled by
nobody. Now the over-cap response hands back a complete set of material **executable in the same turn**:

1. `quota`: the bucket name / quota / used / entry count / this addition / freed by replacement / total after landing / by how much it exceeds;
2. `bucket_entries`: the id, kind, character count, creation time and **full text** of **all the bucket's currently valid entries**;
3. `error` + `next_action`: explicitly requiring that **within this turn**, `memory_invalidate` first be used
   (substring location via `content_match` is available) or `memory_reconcile_apply` free at least N characters, and only then the
   present write be retried.

The `promote` branch of `memory_reconcile_apply` goes through the same verification, but does not hand back the full bucket list
(the tidying flow's `direction_inventory` has already given the full text; restating it entry by entry would only blow up the response).

### 3.1.4 Write-side safety scan

Direction-layer content is compiled into **every** dispatched agent's system prompt, so `memory_add` is the strongest injection amplifier
in the whole system: one polluted entry affects every sub-agent thereafter. The scan sits on the write side (the author is still
on the scene and can edit), not on the injection side (both too late and a hot path). Three families:

| Family | Content | Surfaces it acts on |
|---|---|---|
| Invisible Unicode | Zero-width characters, bidirectional overrides, deprecated format characters, BOM, the Tag block (ASCII smuggling) | Direction layer + episodic layer |
| Prompt-injection phrasings | Instruction overrides (Chinese/English), system-prompt extraction, forged role/turn markers, persona takeover, jailbreak incantations | Direction layer only |
| Credential shapes | Private-key file headers, each vendor's API key shape, `key=value` secrets | Direction layer only |

- A hit is rejected, with a statement of what was hit and at which character; **a credential hit does not echo the matched content** (otherwise the rejection message
  itself becomes a second copy of the secret).
- The pattern table's single source is server-side, in `src/aiteam/memory/content_safety.py`; it is **not copied into the hooks**
  (hooks are pure stdlib, and checking at the injection end is already too late). Invisible characters in the table are written by **code-point ranges**, not as
  literal characters — otherwise the pattern table itself would be a stretch of unreviewable text, exactly what this check exists to prevent.
- `task_memo_add` **only scans invisible Unicode**: the episodic layer is high-frequency and enters no system prompt, so injection
  phrasings there are merely recorded text; but content invisible to the naked eye should not enter the database at any layer (retrieval will pull it
  back before the model's eyes).

### 3.1.5 `memory_invalidate` substring location

Tidying most often happens at "the moment of just having been pushed back by the over-cap protocol", when what the caller holds is the **entry's original text**;
querying the id once and then invalidating is a pure extra hop. So an optional `content_match` is added: within the current context's valid entries
(global + user + this project, the same source as `memory_list`/injection), a substring match is done over the body text, and it **must hit uniquely**
— with 0 hits or multiple hits, no data is touched, and with multiple hits, candidates are handed back with a request for a more precise substring. Never guess.

### 3.1.6 Meeting conclusions stop entering the layer automatically

`PUT /api/meetings/{id}/conclude` used to automatically stuff a `[Meeting decision] <topic>: <conclusion>` entry into the `team`-domain memories;
that was the memory layer's only automatic write entry: unreviewed by anyone, unmanaged by the invalidation axis,
growing linearly with the number of meetings, and duplicating the proper path for decisions. Ruling: **stop writing**. A conclusion's authoritative landing place is the decision
event + task-wall entry (Council discipline 4, the current system). The existing 126 historical rows are **frozen**: not deleted,
not invalidated, not migrated — they are true records under the system of their time, and deleting them would be fabricating history.

### 3.1.7 Truthfully recorded: unenabled extension points and undelivered phases

- `task_memos.quality_score` / `scope_path`: **the columns exist, the semantics exist, they are actually not enabled**. On the write side only
  the `score` / `merge` ops of `memory_reconcile_apply` fill them; the everyday write path
  (`task_memo_add`) does not carry these two fields, and neither the retrieval nor the injection side reads them. **Keep the columns; do not delete them**
  — the I10 freezing principle: retired tables/columns are frozen rather than deleted; dropping a column is both irreversible and would make old databases fail to upgrade.
- **P3 is entirely undelivered**: scope_path retrieval slicing, a Dashboard memory page (including direction-layer markdown export),
  mirroring of high-value CC MEMORY.md entries, and a doctor AGENTS.md interop hygiene check — none of the four has been implemented;
  the phase table's "can be deferred" still holds today.

### 3.1.8 External comparison: NousResearch/hermes-agent

The mechanism source of this overhaul. Item-by-item comparison conclusions:

| That project's practice | This design's disposition |
|---|---|
| The memory storage cap **is** the injection budget — a single number; injection never truncates; when over the cap, all current entries are handed back to the model together with a "tidy within this turn, then retry" instruction | **Copied verbatim** (§3.1.2 + §3.1.3). This is the entire mechanism source of this overhaul |
| Retrieval uses SQLite FTS5, **zero embeddings** | **Double corroboration**: independently isomorphic with this design's §5 judgment of "no vector store" (we use pure-Python BM25, because FTS5's Chinese word segmentation is unreliable). Two independent practices pointing at the same conclusion reinforce this ruling |
| A pre-write `write_approval` staging queue (memory enters a pending-approval queue first; a human confirms before it lands) | **Not implemented for now; recorded as a reference shape**. The current direction layer is only maintained by the Leader/user (§5 "do not let sub-agents edit the direction layer themselves"), so there is no second writer needing approval; if sub-agents are ever allowed to propose direction-layer entries, this staging queue is the ready-made channel shape |

## 4. On-demand tidying: memory_reconcile (P2, merging items 1, 2, 8 and 7)

CC is not resident ⇒ there is no background tidying process (the same principle as the ADK/scheduler retirement). Tidying = an **on-demand explicit action within a session**:

- **Triggers**: the user says "tidy the memory" outright; or a soft hint — when more than 150 new valid memos have appeared since the last tidying, the tool return/hook attaches a hint (an extreme simplification of Generative Agents' importance-crossing-a-threshold: counting by volume). At large volumes, the hint suggests opening ultracode and using Workflow concurrency.
- **Flow** (mem0's four-operation pipeline, the BM25 version):
  1. **Coarse screening (zero LLM)**: within the same scope_path/the same task cluster, pairwise BM25 similarity picks candidate pairs (graphiti's two-level dedup idea, MinHash swapped for BM25);
  2. **LLM fine judgment**: for each candidate group, choose one — `KEEP` (keep both) / `MERGE` (merge; the old entry is superseded) / `INVALIDATE` (contradiction; the old entry is invalidated) / `NOOP`;
  3. **Distillation** (Generative Agents reflection, only one layer): conclusions/user corrections recurring across memos → proposed as direction-layer entries, with `source_refs` pointing back to the source memos (item 4's provenance closes the loop here);
  4. **Scoring** (item 8): fill `quality_score` for summary/decision-type memos (1-10, with the reason in meta);
  5. **Produce a recommendation list → user confirmation → apply**. Governance-layer principle: **no black-box automatic changes** (ChatGPT chat-history-style implicit synthesis conflicts with the auditable positioning and is explicitly not learned from).

## 5. Explicitly not doing (over-design red lines, all from research annotations)

| Not doing | Reason |
|---|---|
| Vector store / embeddings | The corpus is at the hundred scale; BM25's three-arm RRF suffices (Anthropic's official memory approach likewise drops vectors for transparent files) |
| Resident background tidying processes / timers | CC is not resident — the same ruling as the scheduler retirement; Letta sleep-time's "idea" is kept, its "residency" is not carried over |
| Graph databases / knowledge graphs / entity communities | graphiti/Zep's heavy-asset shapes; only the invalidation semantics are carried over |
| Full bi-temporal four timestamps | A single invalidation axis (invalid_at + invalidated_by) suffices; valid_at's semantics merge into created_at |
| Automatic black-box synthesis | The governance layer must be auditable: all tidying goes "proposal → confirmation → apply" |
| LLM importance resident scoring | Scored only at on-demand tidying time |
| Letting sub-agents edit the direction layer themselves | The direction layer is maintained only by the Leader/user (Letta's self-editing toolchain is cut); sub-agents only read |

## 5.5 Direction-layer entry content standards (added 2026-07-12, from the Kun Chen global AGENTS.md case study)

A first-hand case: kunchenguid/dotfiles' `home/AGENTS.md` (formerly Meta/Microsoft/Atlassian L8; the blog post "Everyone Should Have an OPINIONS.md" + a video walkthrough). That file's 7 bullets govern all outputs of all its agents, verifying that the direction layer's "value lies in leverage, not in entry count".

**The write test (memory_add's soft gate)**: how many future tasks can this entry affect? If it only affects a single task → it goes to task_memos.

**Kind classification and exemplars** (all taken verbatim from Kun's file):
| Kind | Exemplar | Character |
|---|---|---|
| constraint (prohibitions/guardrails) | "Never use the em dash"; "NEVER auto-add agent name as co-author" | One sentence, machine-checkable, lifelong validity |
| design (value ordering) | "Technical decisions do not weigh development cost, favoring quality/simplicity/robustness/extensibility/long-term maintainability" | The basis for trade-offs when no explicit instruction exists |
| directive (methodology) | "Reproduce a bug first in an E2E scenario close to the end user"; the Boy Scout rule (fix lint/flaky on the way past) | Answers "how to do it" |
| preference (formatting preferences) | One sentence per line (semantic line breaks, good for diffs) | Optional; no default set |

**The pointer-entry shape** (the OS equivalent of the OPINIONS.md/VOICE.md pattern): direction-layer entries may take a "trigger condition + pointer" shape — what resides permanently is just one trigger instruction, while large-volume content sits in the episodic layer/reports and is pulled by retrieval on demand. This is the volume red line's pressure valve: over-cap content is not deleted, it is demoted to a pointer + externalized body.

**Two pieces of external corroboration**: 1. Kun publicly advocates turning off Claude auto-memory and instead storing in an agent-agnostic location (guarding against stale memory polluting context) — the same judgment as this design's "the Leader explicitly memory_adds; no automatic black-box writes"; 2. His repo's root-level AGENTS.md maintenance meta-rules (only put knowledge used by almost every future session / point to authoritative files rather than repeating them / prefer rewriting and streamlining over appending) are isomorphic with this design's volume red line + reconcile streamlining philosophy; his OPINIONS.md having a cron watchdog detecting stale opinions = the resident version of this design's memory_reconcile invalidation judgment (the OS changes it to on-demand per CC's non-resident reality; the direction is right).

**AGENTS.md ecosystem facts** (first-hand verified): AGENTS.md is an open standard hosted by the Linux Foundation's Agentic AI Foundation, natively read by 28+ tools; **Claude Code does not natively read it** (the official docs state it plainly; issue #6235 is open with 4300+ 👍 and no roadmap), and the official bridging method = a first line `@AGENTS.md` import in CLAUDE.md (preferred) or a symlink. → A P3 option: doctor gains an interop hygiene check (when a repo has an AGENTS.md but CLAUDE.md has not bridged it, hint at @import). Exporting the whole direction layer as AGENTS.md is judged no: a semantic-domain mismatch (direction layer = an evolving preference ledger; AGENTS.md = stable engineering conventions); only when a cross-tool user needs it, export the "stabilizable subset".

## 6. Phased delivery

| Phase | Content | Nature |
|---|---|---|
| **P0 foundation** | task_memos table promotion + migration + task_memo_add compatibility (including supersedes) + unified_search querying the table directly | Pure mechanical refactor, lowest risk, unlocks the field landing points of items 1, 4 and 8 |
| **P1 direction layer** | memories gains columns + memory_add/memory_invalidate tools + dual-hook injection (volume red line + injection budget) + **seed entries** (landing the first batch of known user preferences as entries: everything in Chinese / report on completion / the co-author ban / a pointer to the production read-only iron rule, etc., entering the database after the user's review) | New capability; the killer feature is SubagentStart injection |
| **P2 tidying** | memory_reconcile (coarse screening → four operations → distillation → scoring → proposal-confirmation flow) + volume-threshold soft hints + **staleness detection** (the on-demand version of Kun's watchdog: for each direction-layer entry ask "does it still hold" — a referenced feature retired / a version outdated / the world changed → propose invalidation) | LLM on demand; the ultracode hint connects to rule 1c |
| **P3 optional** | scope_path retrieval slicing, a Dashboard memory page (including direction-layer markdown export, for portability), mirroring of high-value CC MEMORY.md entries, doctor AGENTS.md interop hygiene check | Enhancements; can be deferred |

**reconcile's three rules** (transplanted from the maintenance meta-rules of Kun's root AGENTS.md): keep only entries useful to almost every future task; point to authoritative files/tools rather than restating their content; prefer rewriting and streamlining over appending.

Each phase is independently deliverable; P0 depends on no conclusion of discussions 3, 5 or 6.

**Delivery status (checked 2026-07-31)**: P0 / P1 / P2 are implemented and in production (since v1.9.0); P1's volume red line
and injection budget have been merged into a single axis per §3.1, with the over-cap protocol and write-side safety scan added; **all four P3 items are undelivered**.
