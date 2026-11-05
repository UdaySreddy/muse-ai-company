# Unifying All Database Timestamps to UTC — Design Document

- Status: design finalized, code being implemented layer by layer; the existing-data shift script's dry-run is ready, `--apply` awaits the Founder's personal execution
- Decision source: Leader Briefing `90c472dc` (on 2026-07-28 the user ruled **B: unify the whole database to UTC**, no two-step compromise)
- Task wall: `94559ebc-c945-4370-908e-9b207b9c0782`
- Forensics baseline: commit `af1dd66`; a read-only copy of the production database at `/tmp/utc_probe.db` (copied 2026-07-28 17:01, 184 MB)

---

## 1. Why touch it

Before the overhaul, this database ran **two wall clocks** at the same time:

| Domain | Writing convention | Columns | Tables |
|---|---|---|---|
| Core domain (projects/teams/agents/tasks/events/…) | `datetime.now()` — host-local time, naive | 24 with defaults + several without | 21 |
| Ecosystem domain (ecosystem_*, pipeline_stage_history) | `datetime.now(tz=UTC)` — UTC, aware | 19 with defaults + several without | 15 |

SQLite has no tz-aware column type: SQLAlchemy's SQLite dialect renders a datetime as
a `'YYYY-MM-DD HH:MM:SS.ffffff'` string, and **the offset is silently discarded** — passing aware and passing naive
write byte-identical rows, and `DateTime(timezone=True)` is a no-op in that dialect. Measured and confirmed this time:

```
Writing datetime.now(UTC)  →  in the DB '2026-07-28 09:00:34.563804', read back with tzinfo=None
Writing a DateTime(timezone=True) column  →  the same, no difference whatsoever
```

So the two wall clocks are each self-consistent within their own domain and look right individually; **they only go wrong in cross-domain comparisons**, and they raise no exception — the
numbers just quietly shift by 8 hours. The D1 batch alone caught 3 places (`aggregate_model_usage`'s "most recent N days" actually counted
N days + 8 hours; the ecosystem 30-day expiry judgment fired 8 hours early; plus two places where comments said the opposite of the measurements). **Catching
three places at once shows it is continuously producing pits, not an occasional event.**

There is also a symptom the user can see right now: times on the frontend ecosystem page are **8 hours early** — the backend gives UTC strings without an
offset, and the browser's bare `new Date()` parses them as local. Meanwhile the core-domain pages "displaying correctly" is pure coincidence
(local writing + local reading happen to cancel out); change timezone or access remotely and it is immediately wrong.

**Conclusion: the dual wall clocks are not a design; they are historical drift.** This time we do not just change call sites; we make this whole class of bug
structurally inexpressible.

---

## 2. Target state

> **One rule, no exceptions: every timestamp in the system is UTC.**

It lands in three layers:

### 2.1 The single clock, `src/aiteam/clock.py`

Pure stdlib, zero dependencies, providing:

| Function | Semantics |
|---|---|
| `utc_now()` | The current instant, **aware UTC**. The whole system's only "now" |
| `naive_utc_now()` | The same but naive (only for bare SQL / migration scripts that bypass the ORM) |
| `ensure_utc(v)` | Label it: a naive value is treated as already UTC; an aware one is converted to UTC |
| `to_naive_utc(v)` | The stored form: an aware value is first converted to UTC, then the offset is removed (a **conversion**, not a truncation) |
| `from_timestamp(ts)` | POSIX timestamp → aware UTC (replacing bare `fromtimestamp`, which stamps a local offset) |
| `from_epoch_ms(ms)` | Epoch milliseconds (used by CC journals) → aware UTC |
| `parse_utc(s)` | ISO8601 → aware UTC; **a string without an offset is read as UTC** (= the storage convention) |

### 2.2 The single conversion point, `src/aiteam/storage/utc_type.py::UtcDateTime`

One `TypeDecorator`, used uniformly by all 82 datetime columns:

- **Write**: aware → `astimezone(UTC)` → remove the offset and store naive; naive → treated as already UTC and stored as-is.
- **Read**: a naive column value → stamped with `tzinfo=UTC`.

Two consequences, both intended by this design:

1. **The API contract comes for free**: everything above the storage layer (service layer, API responses, Dashboard) receives aware UTC,
   `.isoformat()` carries `+00:00` itself, **with no need to change serialization route by route**.
2. **Silence becomes loud**: a leftover bare `datetime.now()` compared against a database value raises `TypeError` on the spot, rather than
   returning an answer off by 8 hours. The old failure mode was silent; the new one cannot slip through.

**The on-disk format does not change at all** — old rows are only reinterpreted. That is exactly why the existing-data shift only needs to change "values", not "shapes".

### 2.3 The single parsing point (frontend), `dashboard/src/lib/datetime.ts`

A single entry point, `parseServerTime(s)`: a string with an offset is parsed as-is; one without an offset is parsed as UTC
(compatible with old strings from before and after the shift, and with any cached response). It is rolled out to replace the `parseAsUtc` single-point patch inside `DeepReviewSection`, which
only fixed one component.

---

## 3. The full time-column list (82 columns / 38 tables)

The classification basis is primarily the **writer's code**, corroborated by **measured production-database values** (see §3.3's conclusive evidence).

### 3.1 The local wall-clock side — needs a −8h shift (20 tables / 46 columns / 320,866 non-empty cells)

| Table | Columns | Non-empty rows |
|---|---|---|
| projects | created_at, updated_at | 4 / 4 |
| phases | created_at, updated_at | 1 / 1 |
| teams | created_at, updated_at, completed_at | 249 / 249 / 166 |
| agents | created_at, last_active_at, ctx_measured_at, tokens_measured_at | 2556 / 2556 / 1912 / 1 |
| tasks | created_at, started_at, completed_at | 154 / 124 / 132 |
| task_memos | created_at, invalid_at | 722 / 40 |
| memories | created_at, accessed_at, invalid_at | 206 / 206 / 31 |
| events | timestamp | **244,394** |
| meetings | created_at, concluded_at | 1 / 1 |
| meeting_messages | timestamp | 17 |
| agent_activities | timestamp | **51,246** |
| scheduled_tasks | created_at, last_run_at, next_run_at | 0 / 0 / 0 |
| cross_messages | created_at, read_at | 0 / 0 |
| wake_sessions | started_at, finished_at | 0 / 0 |
| leader_briefings | created_at, resolved_at | 32 / 10 |
| channel_messages | created_at | 11 |
| reports | created_at | 63 |
| workflow_runs | created_at, updated_at, started_at, completed_at, last_activity_at | 226 / 226 / 226 / 226 / 148 |
| workflow_agents | created_at, updated_at, started_at, queued_at, last_activity_at | 3176 / 3176 / 3163 / 3151 / 1566 |
| knowledge_links | created_at | 493 |

> The two columns `events.timestamp` + `agent_activities.timestamp` account for **92%** of the whole shift volume.

### 3.2 The UTC side — untouched (15 tables / 36 columns / 810 non-empty cells)

| Table | Columns |
|---|---|
| pipeline_stage_history | transitioned_at |
| ecosystem_repo_profiles | last_scanned_at, first_seen_at, last_commit_at, pushed_at, last_shallow_refreshed_at, last_status_change_at, manual_status_set_at |
| ecosystem_deep_reviews | created_at, started_at, completed_at, shallow_completed_at, architecture_completed_at, debated_at, stage3_completed_at, claimed_at, reviewed_at |
| ecosystem_tags / ecosystem_repo_tags / ecosystem_relations | created_at |
| ecosystem_scan_runs | started_at, completed_at |
| ecosystem_repo_status_snapshots | snapshot_at, pushed_at |
| ecosystem_project_settings / ecosystem_data_sources | created_at, updated_at |
| ecosystem_scan_profiles | created_at |
| ecosystem_index_diffs | generated_at |
| ecosystem_status_changes / ecosystem_repo_events | triggered_at |
| ecosystem_shallow_batches | created_at, updated_at, approved_at, completed_at |

Of these, the `pushed_at` / `last_commit_at` values come from the GitHub API (true UTC to begin with) and should not be touched in a double sense.

### 3.3 Conclusive evidence for the judgments (why we dare say which convention a column is on)

Forensics moment: local `17:01`, UTC `09:01` (Asia/Shanghai = UTC+8, **no daylight saving**, so the offset is a constant 8h).

| Observation | Value | Conclusion |
|---|---|---|
| `agents.last_active_at` maximum | `2026-07-28 17:01:24` | ≈ local now → **local wall clock** |
| `agent_activities.timestamp` maximum | `2026-07-28 17:01:24` | Same as above |
| `events.timestamp` maximum | `2026-07-28 17:01:25` | Same as above |
| `ecosystem_repo_events.triggered_at` maximum | `2026-07-21 06:23:56` | That scan batch happened at local 14:23 (D1 has proven second-level alignment with the events table) → **UTC** |
| `ecosystem_repo_profiles.last_scanned_at` range | `07-10 07:06` ~ `07-21 06:23` | Corresponds to local 15:06 / 14:23 → **UTC** |

**A live database's newest rows are necessarily only a few minutes old**, so "is max(value) closer to local now or to UTC now" is an
irrefutable criterion. The shift script hardens this criterion into a runtime guardrail (see §6.2).

### 3.4 Explicit exclusions (things not touched, with reasons)

| Object | Shape | Why it is not touched |
|---|---|---|
| `governance_lease.expires_at / updated_at` | VARCHAR, storing isoformat with `...+00:00` | **Already an offset-carrying UTC string**, deliberately not going through ORM DateTime (only lexicographic comparison). Already in the target state |
| `loop_states.updated_at` | TEXT, 198 rows | A leftover of the retired cron engine, with **zero writers** in the whole repo (already registered as a leftover by `check_schema_tables.py`). Dead data; touching it benefits no reader |
| Timestamps embedded in `events.data` | JSON, 2266 rows containing ISO strings | An event payload is a verbatim record of **what the hook reported at the time**; changing it is tampering with testimony. And its key names are unfixed, with no schema |
| Timestamps embedded in `tasks.config.memo` | JSON, 20 rows | CLAUDE.md explicitly orders it a **frozen archive** (after the memory v2 table migration, the old JSON is kept as history; do not clean it, do not write to it) |
| Body-text times in `reports.content` / `task_memos.content` | Markdown body text | Words written by a human, not data |
| `alembic_version` | No time columns | — |

---

## 4. API contract

**Change**: datetimes in all responses go from `2026-07-28T17:01:24.123456` (a bare string whose convention is guessed)
to a **self-describing** shape. Both spellings occur; both carry an offset and are semantically equivalent:

| Source | Shape | Measured example |
|---|---|---|
| FastAPI / pydantic JSON serialization | `Z` suffix | `2026-07-28T10:01:26.476961Z` |
| Hand-written `.isoformat()` in code (74 places) | `+00:00` | `2026-07-10T07:06:38.220368+00:00` |

The frontend `parseServerTime` accepts both (`hasOffset` matches `Z` and `±HH:MM` alike).

Implementation: no serialization code is written at all. `UtcDateTime` reads out aware values, after which every path carries
its offset itself.

**Backward compatibility**: this is a **breaking contract change**; any consumer parsing bare strings must change with it.
Within this repo there are only two consumers, both inside the scope of this overhaul:

1. The Dashboard (§5)
2. The wake watermark contract (§4.1)

### 4.1 The wake watermark synced at both ends

| End | Before | After |
|---|---|---|
| The `watermark` returned by `GET /api/wake/actionable` | `datetime.now().isoformat()` (local, no Z) | `utc_now().isoformat()` (with `+00:00`) |
| `wake_actionable.parse_since()` | Values with a tz are `astimezone()`-converted to local and stripped of tzinfo | Uniformly `parse_utc()` → aware UTC; a value without an offset is read as UTC |
| `scripts/os-watch.sh`'s first `SINCE` | `date +%Y-%m-%dT%H:%M:%S` (the comment hardwired "never use -u") | `date -u +...+00:00` |

The watcher itself is still a dumb poller (it hands back the watermark the API gave it verbatim); **the string carries its own convention with it**, and
the two ends no longer rely on their own conventions to align.

> **Known transition-period impact**: if, when the overhaul goes live, a watcher process is still holding an old local watermark,
> it will run 8 hours ahead of UTC and under-report increments during those 8 hours. The watcher is a session-scoped process (it dies with the session,
> and has a 1h hard timeout), so it self-heals at the next session; no extra handling is needed, but **deployment should be aware of it**.

---

## 5. Frontend parsing strategy

### 5.1 Status quo

Scattered across 28 components/pages are 73 `new Date(...)` calls, the great majority handing the backend's bare strings straight to the browser to parse as local.
The sole exception is the `parseAsUtc` in `DeepReviewSection.tsx` — a **single-point patch** applied that year for this same time difference,
which only fixed one component.

### 5.2 The plan: a shared parsing layer, not patches spread around

Create `dashboard/src/lib/datetime.ts`:

```ts
parseServerTime(s)      // with an offset → as-is; without → as UTC. Returns Date | null
serverTimeMs(s)         // → number (NaN means unparseable), for sorting/differences
formatDateTime(s, opts) // localized display (the browser's local timezone)
formatTime / formatDate / formatRelative / formatElapsed
```

**Why not simply promote `parseAsUtc` to a util and be done**: that only solves "how to parse"; it does not solve
"73 call sites each writing their own formatting". With a unified entry point, "how is a server string read" has exactly one answer, and next time
the convention changes, only one place changes.

**Why a string without an offset is still read as UTC** (rather than raising an error): before the shift completes there are still bare strings in the database,
the browser may hold cached responses, and third-party scripts may hit the API directly. Reading as UTC is the only self-consistent interpretation
that agrees with the storage convention.

---

## 6. Shifting the existing data

Script: `scripts/migrate_timestamps_utc.py`, **dry-run by default**.

### 6.1 The shift amount

`UPDATE <table> SET <column> = datetime(<column>, '-8 hours') WHERE <column> IS NOT NULL`

Applied to §3.1's 46 columns / 320,866 non-empty cells. Asia/Shanghai is a constant UTC+8 all year (no daylight saving since
1991), so −8h is a **constant conversion, not a timezone-database conversion** — no DST ambiguity exists.

### 6.2 Mixed conventions within a column: per-row verdicts and exclusion

The shift is done **per column**: a column's writer kept one convention for most of the database's life (the core domain always wrote local),
so there is no dividing line of the "rows after such-and-such a day do not move" time-point kind.

But **a column is not homogeneous inside**. The UTC new code is already on master, and `aiteam` is an editable install pointing straight at
the repo working tree, so **any process started after the merge** — an MCP server brought up by a new session, a hook subprocess,
the `aiteam` CLI, an auto-revived API — writes UTC. Measured (a read-only snapshot of the production database on 2026-07-28 18:49):
of `events.timestamp`'s 246,409 rows, 11 rows of UTC values have already mixed in, at rowids `244681` and
`245787~245792 / 245796 / 245801 / 245805 / 245810`, each dropping back by exactly 8.00 hours relative to the prefix maximum of the write order,
and alternating with local rows at millisecond level (`245786`=18:24:15 local → `245787`=10:24:19 UTC →
`245793`=18:24:21 local) — the same column has two concurrent writers. **The contamination grows monotonically with time**, so
detection must be computed fresh against the whole database at the very moment `--apply` executes; any fixed row-number list is stale.

This is also why the operational premise "stop the API and there are no writers" does not hold: the system has several built-in paths that auto-revive the
API (MCP server startup, the `ai-team-os-serve` entry point, plugin bootstrap, the `os_restart_api` tool,
`/os-up`, CLI `up`), and the `aiteam` CLI and several `scripts/` also connect directly to the production database, bypassing the API. What really backstops it
is not operational discipline, but the per-row detection below.

**The skeleton of the criterion is one hard constraint**: for a value `v` written in UTC, its real instant is `v + 8h`, and that real
instant must fall within `[T0, now]` (`T0` = the earliest moment the new code could have run). Conversely —

> Only values falling in the **risk band** **`[T0 − 8h, now − 8h]`** could possibly have been written in UTC.

Values outside the band are all **proven** to be in the local convention and are shifted as usual; values inside the band are then judged row by row:

| Column class | Oracle |
|---|---|
| Append-type columns (each table has one "birth column", written at insert time and never updated afterwards) | rowid is the physical write order: the lower bound takes the maximum of all out-of-band rows before it, the upper bound takes the value of the first out-of-band row after it, plus the upper bound given by same-row "successor columns" (`AFTER_BIRTH_COLUMNS`: columns structurally guaranteed not to be earlier than creation); fit both readings, `v` and `v + 8h`, into them, and only settle the case when exactly one is self-consistent |
| In-place update columns (`updated_at` / `last_active_at` and the like, where rowid is decoupled from the values' timing) | Take the same row's birth column as the anchor and judge by the invariant "**an update cannot be earlier than the creation**": if an update value is earlier than the creation instant by nearly one timezone, it can only be in the UTC convention |

Rows for which both readings are self-consistent, or both conflict with the evidence, **go row by row into the journal's undecidable list for human review**,
and only proceed with an explicit `--ack-undecidable` — **never silently shifted**. `T0` is inferred automatically from in-database forensics (it can also be given with
`--utc-writer-since`, at the moment the new code merged); each column's candidate count, excluded rowid set and human-review list
all go into the dry-run report and the journal.

**The tolerance is given in minutes (5 minutes), not hours**: the production database's `task_memos.created_at` contains 7.1-8.7 hours
of **legitimate** disorder (the batch backfill during the memory v2 table migration), and an hour-level tolerance would mistake that batch of backfilled rows for a timezone fingerprint and wrongly exclude
them wholesale; `memories.created_at` even has 3143 hours of disorder (rows migrated in from the Windows database).

`AFTER_BIRTH_COLUMNS` is an allowlist rather than a blacklist, because counterexamples really exist:
`workflow_runs/agents`' `started_at` / `queued_at` / `completed_at` come from CC state files'
epochs, measured as early as 2026-06-23, weeks earlier than that row's `created_at` (2026-07-06);
`scheduled_tasks.next_run_at` outright records a future instant. Treating them as "not earlier than creation" columns would have the oracle
judging against a fake invariant.

#### The whole-database guardrail

The guardrail sits at the **database level** rather than the column level — guessing column by column would only manufacture false alarms against cold data (measured: `teams.created_at`'s
newest row is 10:24, 7 hours ago, and a per-column criterion would misjudge it as "already UTC"). The criterion has two layers:

1. **Convention witness**: take **the freshest append-type column** and look at which side the **median** of its most recent 200 rows falls on.
   * Only append-type columns qualify as witnesses — their rowid is the write order, and the concept of "the most recent batch of writes" only holds for them.
   * A median rather than `max`. `max` by nature only reflects the very few newest rows: when the database has been shifted wholesale to UTC and old code
     writes one more local row, that single row suffices to make `max` hug local now — exactly the mechanism by which "an already-shifted database" gets judged
     "shiftable" (already reproduced by fault injection). The median requires **the majority** of recent writes to share one convention; a minority cannot overturn it.
   * Positive confirmation is still one-sided and unambiguous: a UTC clock **cannot write a future value**, so a witness median later than UTC now
     is necessarily written by the local wall clock.
2. **Mixed-convention confirmation**: mixed conventions are no longer a binary "let through/block" judgment — the detection-and-exclusion scheme itself handles the mixing, and the guardrail's
   job becomes confirming "the contaminated rows have been recognized and will be excluded" (reporting the excluded-row count per column), and "when undecidable is non-empty, `--ack-undecidable` is required
   to proceed". The latter cannot be overridden by `--force`.

Each column is still reported one by one as "proven local / cold data cannot self-prove", but no longer vetoes on its own.

**Fault injection already covers it** (`tests/unit/test_migrate_timestamps_utc.py`): a minority of mixed-in UTC rows is excluded row by row,
an already-shifted database + 1 new local row no longer gets a green light, legitimate out-of-order backfills are not mistaken for timezone fingerprints, a double apply is stopped by the marker,
an existing journal is refused, and excluded rows are likewise not +8h'd at rollback.

#### Only once

* **Idempotency marker**: after a successful `--apply`, `PRAGMA user_version = 20260728` is written on the database (chosen because it is the least
  intrusive: no table, no column, no business-visible object, and this repo's storage layer never reads or writes it). A second
  `--apply` hard-refuses; `--rollback` checks the marker exists before executing, and clears it afterwards.
* **Journal overwrite protection**: `--apply` / `--rollback` mandatorily require `--journal`, and if the target file already exists,
  execution is refused. The journal is the only recovery credential (including the per-row exclusion list); were it overwritten by a second apply into the "post-shift state",
  a subsequent `--rollback` would report "all re-verification passed" against the polluted baseline, while the database actually remains a whole 8 hours early.

### 6.2.1 The shift is lossless

`datetime(col, '-8 hours')` would **truncate the result to whole seconds** — that would casually erase the microsecond digits of 320,000 rows,
while the event ledger sorts by `timestamp`, and within the same second the ordering depends on those very digits. The script instead only moves the "up to the minute"
prefix and splices the seconds and sub-second digits back as-is:

```sql
substr(datetime(substr(col,1,16) || ':00', '-480 minutes'), 1, 16) || substr(col, 17)
```

The shift amount is necessarily a whole number of minutes (every existing timezone offset is a whole number of minutes, including +05:30 / +05:45), so this way of cutting
works for any timezone. Before executing there is also an `assert_uniform_width`, with two checks: a value whose width is shorter than
`YYYY-MM-DD HH:MM` would be destroyed by the prefix split; more important is the **direct criterion** — run the very expression that will be executed
once, and any row whose "original value is non-empty but whose result is NULL" would be silently emptied, so execution is refused on encountering one. Sufficient width does not imply
parseability (epoch float strings, illegal month/day, slash formats are all longer than 16 characters yet still collapse to NULL), so width is a necessary
but not sufficient condition; the latter check is the one that really works. After `--apply` there is another non-empty row count re-check: `rowcount` only counts matched
rows and cannot see "emptied into NULL"; on a mismatch it raises, at which point the transaction has not been committed and the database keeps its original state.

**Round trips have been verified byte-for-byte equal**: after `--apply`, a `--rollback`, taking 200 rows from each of four tables to compare, gives completely identical values.

### 6.3 Frozen objects are within the shift range

The `tasks` table's `created_at/started_at/completed_at` columns **are within the shift range** (the columns themselves are not
frozen objects; what is frozen is the `tasks.config.memo` JSON field). Likewise, the `task_memos` table's
DATETIME columns are shifted, while the old memo JSON in `tasks.config` is untouched — this creates a **known and
acceptable inconsistency**: the timestamps in the old JSON archive remain local, differing by 8 hours from the table-migrated `task_memos.created_at`
(UTC after the shift). Reason: that JSON is a historical archive, not live data, and CLAUDE.md explicitly orders it never be written again.
This document registers that inconsistency explicitly here, to keep later people from "fixing" it as a bug.

Before shifting, the script archives **every** affected column: row count, min, max (`--journal <path>` writes the JSON);
frozen-related tables are archived the same way.

### 6.4 Dry-run output

One line per column: table / column / non-empty row count / **excluded row count** / pre-shift min-max / post-shift min-max, plus 3 rows
of before/after samples. Then comes the **per-row convention verdicts** section: the forensic baseline `T0`, each column's candidate row count / excluded rowid set /
rows awaiting human review (giving rowid, value and the reason it cannot be judged, row by row). It ends with totals and the guardrail conclusion.

When a dry-run is given `--journal`, it writes the complete per-row list to disk (with `mode: "dry-run"`) for human review — it is likewise subject to
the "refuse if the file already exists" constraint, so a dry-run archive for human review must be named separately from the formal apply's journal.

### 6.5 Execution discipline

- `--apply` is **executed personally by the Founder**; the implementers of this overhaul never execute it (an iron rule of the task statement).
- The new code is already on master and `aiteam` is an editable install, so the order "shift first, deploy later" can **already not**
  be satisfied: any process started within the shift window is writing UTC rows. This is no longer backstopped by operational discipline, but by §6.2's
  per-row detection — UTC rows written within the window will be recognized and excluded.
- Execution order:
  1. Back up: `cp ~/.claude/data/ai-team-os/aiteam.db ~/aiteam.db.bak-utc-$(date +%Y%m%d%H%M%S)`
  2. Archive a dry-run and **human-review the undecidable list**:
     `--db <backup copy> --journal ~/utc-dryrun-<ts>.json`
  3. Minimize writers as far as possible (close extra CC sessions, do not proactively restart the API). Note this is only noise reduction, not a guarantee:
     multiple auto-revive paths + direct CLI/scripts connections all get around it.
  4. `--apply --journal ~/utc-migration-<ts>.json` (the journal must be a **new filename**; if step 2
     really had unjudgeable rows, append `--ack-undecidable`)
  5. Re-verify the journal against the script's post-shift min/max, confirming the excluded-row list agrees with the dry-run
  6. Restart the API and **confirm the old process really is dead** — both sides have the same `__version__` and `/api/health` contains no commit
     hash, and autostart will directly adopt an old process that is still alive, which will keep writing local rows into the already-shifted database (in the direction opposite to
     the shift, and without any warning). Criterion: newly written rows' timestamps should equal UTC now, not local now.

> **⚠️ Code and data must go live as a pair.** This is not a theoretical risk; it has been measured: connect the new code to an **unshifted**
> database, and for a team built locally at 10:24 the API gives `2026-07-28T10:24:46Z`, which the browser renders as
> **18:24** — a whole 8 hours late, with the page showing nothing anomalous.
>
> In other words, **in the window after the code merge and before the shift executes, core-domain times are all wrong in the interface**
> (in the direction opposite to the ecosystem earliness the user originally reported: they are late). So the merge and the `--apply` should
> be done back-to-back; do not linger long in between, and do not judge times right or wrong against the interface during that window.

### 6.6 Rollback

The shift is a **pure value transformation and reversible**: `--rollback` performs `+8 hours` on the same column set, verified with the same journal
(after rollback, row counts and min/max must equal, column by column, the pre-shift state the journal recorded). Three constraints:

- The shift amount takes the `shift_hours` recorded in the journal, not a fresh computation from the current host timezone; if the `db` path in the journal does not match,
  execution is refused — a mismatched rollback would move data by the wrong amount.
- **The exclusion list recognizes only the journal**: after the shift, the database itself no longer shows who is who, so whichever rows were not `−8h`'d back then
  are likewise not `+8h`'d at rollback.
- Rollback requires the shift marker on the database, and clears the marker after executing successfully.

Three fallback layers, by cost from low to high:

| Layer | Means | Cost |
|---|---|---|
| Data | `--rollback` (or restore the backup directly) | Seconds |
| Code | `git revert` this branch's implementation commits | Minutes |
| Combined | Restore the backup + revert + restart the API | Back to before the overhaul |

**The rollback window's constraint**: once the new code is live and starts writing UTC rows, rolling back the data alone (without rolling back the code) creates
a new-old mixture. So a rollback must be performed **with data and code as a pair**.

---

## 7. Acceptance checklist

| # | Item | Criterion |
|---|---|---|
| 1 | Unit/integration tests | `python3 -m pytest tests -q` all green, and the pass count no lower than the pre-overhaul baseline (2026 passed / 3 skipped @ af1dd66) |
| 2 | Red-line machine checks | `bash scripts/check_invariants.sh` I1–I10 all green (including hook dual copies, dual dist consistency) |
| 3 | Clock-convention machine check | New I11: within `src/`, zero bare `datetime.now()` / `datetime.fromtimestamp(` outside `clock.py` |
| 4 | Column-type full coverage | A test asserts every DateTime column in `Base.metadata` is a `UtcDateTime` |
| 5 | Round-trip conventions | Write aware `+08:00` → read back aware UTC with the **instant unchanged**; write naive → read back aware UTC with the **value unchanged** |
| 6 | API contract | Time strings in real route responses carry `+00:00` |
| 7 | Watermark contract | `parse_utc(watermark)` round-trips identically; `os-watch.sh`'s first SINCE carries an offset |
| 8 | Frontend build | `npm run build` passes; the two `dashboard-dist` copies agree (I4) |
| 9 | Frontend live check | Ecosystem page times are **no longer 8 hours early**; core page times **do not regress** |
| 10 | Shift script | The dry-run whole-database report is readable; the guardrail really aborts when the database is already UTC; round trips are byte-for-byte equal |
| 11 | Mixed-convention per-row verdicts | A dry-run against a production-database copy names and excludes rows that are already UTC; the undecidable list is reviewable row by row; double apply / journal overwrite are hard-refused |

### 7.1 Live regression record (isolated instance, 2026-07-28)

The isolated instance = a read-only copy of the production database → shifted → an independent API with `AITEAM_DB_PATH` pointing at it (:8931) +
a dashboard dev server (:8932, browser timezone fixed to Asia/Shanghai). Forensics moment: local 18:0x /
UTC 10:0x.

| Page | Observation | Verdict |
|---|---|---|
| Ecosystem profiles | A batch scan record shows `2026/07/10 16:06`; that value in the database is `08:06:01` (UTC) | ✅ 08:06 UTC + 8h = 16:06 local. **Before the overhaul this showed 08:06 here — the 8-hours-early the user reported** |
| Activity analysis · hourly buckets | The API gives `03:00/04:00/06:00/07:00/08:00/09:00 +00:00`; the page renders `11h/12h/14h/15h/16h/17h` | ✅ bucket by bucket +8, no shift, no misalignment |
| Event log | The newest row shows "6 minutes ago" (that row was written by the isolated instance itself at UTC 10:01; the screenshot was taken at 10:07) | ✅ relative time has no 8-hour deviation |
| Project details | Creation time `2026/7/6`; member cards "6 days ago / 1 hour ago / 1 day ago" | ✅ agrees with reality |
| Console | Zero time-related errors (only pre-existing Base UI / React key warnings) | ✅ |

---

## 8. Known leftovers and follow-ups

1. **JSON-embedded timestamps** (2266 rows in `events.data`) are not shifted; see §3.4. If event-payload-internal
   times are ever to be analyzed, that field's convention must be settled first.
2. **The 8-hour misalignment between `tasks.config.memo` and the `task_memos` table**; see §6.3, registered as not a bug.
3. The **`loop_states`** dead table was not shifted. If the cron engine is ever to be revived, shift it first.
4. **Frontend timezone**: currently everything is displayed in the browser's local timezone. If "always display in a fixed timezone" is ever to be supported,
   one parameter in `datetime.ts` suffices — exactly the payoff of converging on a single parsing layer.
5. **The undecidable residue of in-place update columns**: a column like `agents.last_active_at` has no upper-bound invariant available
   (no column is structurally guaranteed to be later than it), so rows falling in the risk band can only rely on human review. The risk band's width = from `T0` to the execution
   moment, so **the longer it is dragged out, the more rows need human review** — that is the mechanistic reason to urge executing `--apply` soon.
6. **`ROW_BIRTH_COLUMN` / `AFTER_BIRTH_COLUMNS` are the same fact as the ORM**: which column is set at insert, and which column
   is structurally not earlier than creation — when the ORM changes, these two tables must be synced, or the oracle will judge against fake facts. A test already asserts the birth
   column ranks first in its table (in-place update columns take it as their anchor, so it must be judged first).
7. **`SQLite CURRENT_TIMESTAMP` is a third wall clock**: `scripts/cleanup_orphan_pipeline_subtasks.py`
   uses it to write `completed_at`, and SQLite's `CURRENT_TIMESTAMP` computes UTC. §1's two-domain division did not register
   it; direct-connection scripts of this kind must either switch to `aiteam.clock`, or explicitly register their convention in their own files.
