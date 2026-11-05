#!/usr/bin/env python3
"""Usage presentation surface registry — the shared source of truth for I12 (dimension allowlist) and I13 (coverage same-screen red line).

Why a hand-written registry, rather than letting the machine check guess "which are presentation surfaces" itself:

* It cannot guess accurately. The word ``tokens`` is everywhere in ingest, parser and repository code, but those are **computation surfaces**,
  a different thing from "numbers for people to look at". Sweeping computation surfaces in too would turn the machine check into something that false-reports daily, and
  a machine check that false-reports daily is the same as no machine check.
* More importantly, **the registry itself is the gate**. The four-dimension allowlist is closed only under the precondition "there are no undeclared presentation surfaces"
  (§4.4: an allowlist only has to confirm four legal values; a new dimension must be explicitly reviewed).
  So both checks enforce bidirectional comparison: a missed declaration = red, a declaration for something that does not exist = also red.

Spec: docs/token-attribution-v1-design.md §4.4 / §1.1 / §2.5.
This module holds only data and has no side effects; it is imported by check_usage_dimensions.py and check_usage_coverage.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Dimension allowlist — a closed set; additions must be explicitly reviewed (P1)
# ---------------------------------------------------------------------------
# All presentation is expressed only in token usage, with no cross-dimension conversion (not converted to money, not converted to person-hours,
# not converted to "equivalent to how many meetings"). The legal dimensions are exhaustively listed below, four kinds in total:
ALLOWED_DIMENSIONS: dict[str, str] = {
    "token": "token count (four layers listed separately: input / output / cache_creation / cache_read)",
    "count": "counts (number of dispatches, tool calls, agents, …)",
    "duration_ms": "duration in milliseconds",
    "percent": "percentages",
}

# Numeric fields that are not usage (sequence numbers, byte cursors, trust scores, …) do not fit the four dimensions and must be **exempted by name, one by one**,
# with the reason stated — exemptions are explicit; there is no "looks unlike usage so skip it automatically" path.
NON_USAGE = ""

# ---------------------------------------------------------------------------
# Safety net for fifth-kind dimensions (a secondary mechanism, not the allowlist itself)
# ---------------------------------------------------------------------------
# The closedness of the allowlist is guaranteed by "the registry must be complete"; the word list below is only an extra net, used to
# catch conversion dimensions such as money/person-hours in a file **not yet registered**. It is a net, not a criterion — the criterion is always
# the four values above. A hit is red; to let it through, the dimension must first be added to ALLOWED_DIMENSIONS (i.e. reviewed).
#
# Matching is by **identifier word-splitting**, not substrings: ``isPending`` hides "spend" and ``statusDone`` hides
# "usd"; substring matching would produce dozens of false positives in one run, and a machine check that false-reports daily is the same as no machine check.
FORBIDDEN_UNIT_WORDS: frozenset[str] = frozenset({
    "cost", "price", "usd", "cny", "rmb", "dollar", "yuan", "fee",
    "billing", "spend", "spent", "budget", "quota", "money", "credit",
    "manhour", "workday", "manday", "fte",
})


@dataclass(frozen=True)
class FieldSpec:
    """The dimension and metric declaration of one numeric field.

    When ``dimension`` is :data:`NON_USAGE` it means "declared a non-usage field", and ``note`` is then required.
    ``metric`` is only meaningful for the token dimension and is required there — a token number detached from its metric is meaningless (§0.2).
    """

    dimension: str
    metric: str = ""
    note: str = ""


@dataclass(frozen=True)
class PySurface:
    """One presentation surface on the API schema side (one Pydantic model in ``aiteam.types``).

    ``kind``:

    * ``row``: a record surface with one fact per row. The correct form for coverage here is **no-data distinguishable from zero**
      (the column may be None).
    * ``aggregate``: a cross-row aggregate surface. Values must return at the same level as the denominator and the unattributed, otherwise it is a "part impersonating
      the whole" (§2.5 / §4.4 red line).
    """

    model: str
    kind: str
    fields: dict[str, FieldSpec]
    # Known gaps on row surfaces where "0 doubles as not collected": field -> reason (including when and how it will be closed).
    # A declaration is not an exemption — every machine check prints it out, keeping the gap visible.
    coverage_gap: dict[str, str] = field(default_factory=dict)
    # The same-level coverage fields an aggregate surface must have (numerator/denominator + unattributed classification).
    coverage_fields: tuple[str, ...] = ()


# Four token presentation surfaces: three row surfaces (existing) + one aggregate surface (TokenAttribution, stage 2).
PY_SURFACES: tuple[PySurface, ...] = (
    PySurface(
        # The only aggregate surface. Its fundamental difference from the three row surfaces: a row surface has one fact per row, while an aggregate surface kneads multiple rows
        # into a single number — and that "kneading" action is exactly where a part impersonating the whole happens. So the guards here are
        # stricter than on row surfaces: it must return at the same level as the denominator and the unattributed classification (AGGREGATE_REQUIRED_FIELDS),
        # and it **may not declare a coverage_gap** — the denominator of attribution numbers admits no exceptions.
        model="TokenAttribution",
        kind="aggregate",
        fields={
            # The four layers are always usage_sum, and this is not "temporarily so": ctx_last structurally cannot get in.
            # workflow_agents.tokens already summed the four fields into one number at ingest time; the four-layer decomposition
            # was never saved, and this structure mandates four layers listed separately and deliberately has no total field — to squeeze ctx_last
            # in you would have to either hard-stuff it into one layer or fabricate four layers out of nothing. So the ctx_last side only reports coverage
            # (UsageCoverageRow, zero token fields), while the metric field is still required to pin the metric down.
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_creation_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_read_tokens": FieldSpec("token", metric="usage_sum"),
            "dispatches_attributed": FieldSpec("count", note="numerator: the number of dispatches in this scope whose usage was measured"),
            "dispatches_total": FieldSpec("count", note="denominator: the total number of dispatches in this scope (including rows with no data)"),
            # A mapping from reason codes -> dispatch counts. Declaring it count is not a compromise: what must be recorded here is precisely "its values are in the
            # same unit as the denominator" — the numerator plus the sum of this dict's entries must equal the denominator (pinned by a contract test).
            # Tool-call-level by_design therefore cannot get in: 52,119 activities and dispatches are not the same unit.
            "unattributed_reasons": FieldSpec("count", note="unattributed dispatches counted by reason code, values in the same unit as the denominator"),
        },
        coverage_fields=("dispatches_attributed", "dispatches_total", "unattributed_reasons"),
    ),
    PySurface(
        model="Agent",
        kind="row",
        fields={
            "trust_score": FieldSpec(NON_USAGE, note="trust score 0-1, the agent governance domain, unrelated to usage"),
            "ctx_tokens": FieldSpec("token", metric="ctx_watermark"),
            "ctx_window": FieldSpec("token", metric="ctx_watermark"),
            "ctx_pct": FieldSpec("percent", metric="ctx_watermark"),
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_creation_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_read_tokens": FieldSpec("token", metric="usage_sum"),
        },
    ),
    PySurface(
        model="WorkflowRun",
        kind="row",
        fields={
            "planned_agent_count": FieldSpec("count"),
            "dynamic_nodes": FieldSpec("count"),
            "agent_count": FieldSpec("count"),
            "total_tokens": FieldSpec("token", metric="ctx_last"),
            "total_tool_calls": FieldSpec("count"),
            "duration_ms": FieldSpec("duration_ms"),
            "journal_offset": FieldSpec(NON_USAGE, note="the consumed byte watermark of journal.jsonl; an internal cursor, not a presentation dimension"),
            "live_tokens": FieldSpec("token", metric="ctx_last"),
        },
        coverage_gap={
            "total_tokens": "non-Optional, 0 doubles as 'not collected' and 'really 0' (a ctx_last-side historical legacy)."
                            "Stage 0 only rectifies names and does not change data; truthful presentation of the unattributed is borne by the stage 2 coverage structure + the stage 5 unattributed drawer.",
        },
    ),
    PySurface(
        model="WorkflowAgent",
        kind="row",
        fields={
            "phase_index": FieldSpec(NON_USAGE, note="phase sequence number, not a quantity"),
            "tokens": FieldSpec("token", metric="ctx_last"),
            "tool_calls": FieldSpec("count"),
            "duration_ms": FieldSpec("duration_ms"),
        },
        coverage_gap={
            "tokens": "non-Optional, 0 doubles as 'not collected' and 'really 0' (measured: 273 of 3,182 rows are 0)."
                      "Same as above: stage 0 does not change data; the unattributed is truthfully presented by stages 2/5.",
        },
    ),
)

# ---------------------------------------------------------------------------
# Frontend presentation surfaces
# ---------------------------------------------------------------------------
# The frontend has no type introspection available, so it can only be scanned textually. The rule is likewise bidirectional: any file under dashboard/src in which a token
# identifier appears must be in this table; a file listed in the table but no longer existing is also red (after a rename/file deletion the registry
# rots, silently shrinking the machine check's coverage).
FRONTEND_SURFACES: tuple[str, ...] = (
    "dashboard/src/api/projects.ts",
    # Stage 5: the /usage page's data-fetching layer and four presentation components. The four usage layers appear only in these few files,
    # and every place is on the same screen as the denominator (the frontend arm of I13 checks file by file).
    "dashboard/src/api/usage.ts",
    "dashboard/src/api/workflows.ts",
    "dashboard/src/components/shared/ContextWatermarkBar.tsx",
    "dashboard/src/components/usage/AttributionCard.tsx",
    "dashboard/src/components/usage/AttributionDrill.tsx",
    "dashboard/src/components/usage/SingleProbeCard.tsx",
    "dashboard/src/i18n/en.ts",
    "dashboard/src/pages/AgentLivePage.tsx",
    "dashboard/src/pages/ProjectDetailPage.tsx",
    "dashboard/src/pages/TeamDetailPage.tsx",
    "dashboard/src/pages/WorkflowsPage.tsx",
    "dashboard/src/types/index.ts",
)

# Token identifiers allowed to appear on the frontend (including display i18n keys and literals) → dimension.
# An unregistered identifier is red: adding a new token value to a page requires stating here what dimension it is.
FRONTEND_IDENTIFIERS: dict[str, str] = {
    "tokens": "token",
    "Tokens": "token",
    "ctx_tokens": "token",
    "total_tokens": "token",
    "totalTokens": "token",
    "live_tokens": "token",
    "colTokens": "token",
    "fmtTokens": "token",
    # Added in stage 5. Listing the four layers separately is a hard requirement, so each of the four layer names is registered by name — there is no
    # "total" identifier to register, because no such field exists on any presentation surface (§1.2).
    "input_tokens": "token",
    "output_tokens": "token",
    "cache_creation_tokens": "token",
    "cache_read_tokens": "token",
    "formatTokenCount": "token",
    # The dimension word itself in display copy. i18n files are presentation surfaces; when the word "token" appears in a sentence
    # meant for people to read, it refers to the token dimension — register it, and the fifth-kind dimension word list can then block
    # rewrites like "converted to money/person-hours" (rewrites of that kind appear in the copy first, precisely).
    "token": "token",
    "Token": "token",
}

# ---------------------------------------------------------------------------
# Marker words used by coverage same-screen (I13)
# ---------------------------------------------------------------------------
# "For any token value on any presentation surface, if the C_measure of its scope is < 100%, the unattributed portion must be shown
# on the same screen at the same level" (§4.4 red line). The "same-screen unattributed annotations" the machine check recognizes are the words below —
# if one of them appears, that presentation surface is taken to have spoken about the unattributed.
COVERAGE_MARKERS: tuple[str, ...] = (
    "dispatches_total", "dispatches_attributed", "unattributed", "coverage",
)

# The two things an aggregate surface must have at the same level: the denominator, and the classification counts of the unattributed (§2.5's
# TokenAttribution structure). Missing either one, a value can be rendered alone, detached from its denominator.
AGGREGATE_REQUIRED_FIELDS: tuple[str, ...] = ("dispatches_total", "unattributed_reasons")

# Stage 5 has spread metric badges everywhere: the three ctx_last places in WorkflowsPage, ContextWatermarkBar's
# ctx_watermark (shared by the AgentLive / ProjectDetail / TeamDetail pages), and the usage_sum places all over the /usage page — the frontend no longer has bare token values.
#
#
# But **one real gap remains**, registered truthfully here and printed once by every machine check, so it does not become the default state:
# the `total_tokens` / `tokens` shown by WorkflowsPage are non-Optional, with 0 doubling as "not collected" and
# "really 0" (the same-named gap on the backend row surfaces is declared separately in PY_SURFACES). The badge solved "which metric
# is this", but it cannot solve "is this 0 actually no data" — today that is only stated clearly on the /usage coverage matrix
# (the numerator and denominator of the workflow self-report row). Closing it means making those two columns nullable, a data-layer change.
FRONTEND_COVERAGE_GAP = (
    "WorkflowsPage's ctx_last values carry metric badges, but 0 still doubles as 'not collected' and 'really 0'"
    "(the columns are non-Optional); the real numerator and denominator of that path are only visible on the /usage coverage matrix."
)
