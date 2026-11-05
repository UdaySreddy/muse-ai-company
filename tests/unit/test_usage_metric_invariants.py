"""Token usage attribution v1 phase 0 — metric rectification and two machine checks (I12 dimension whitelist / I13
same-screen coverage).

The core number quoted at project initiation, "186.2 million tokens already lying in the database, only the display
layer missing", had the wrong metric.
``workflow_agents.tokens`` takes the four-field sum of the last assistant message = an **instantaneous context
watermark snapshot**; ``token_attribution.parse_transcript_usage`` accumulates across requestIds = the real
usage. Measured on the same batch of agents, the two differ by 5–25× (117,800 vs 632,504 … 127,089 vs 3,236,088).
Adding them together, juxtaposing them, or putting them in the same "total token" is the same class of accident this
repo just had on timestamps.

So this batch promotes "metric" to a first-class dimension alongside "coverage", and makes the two red lines machine
criteria:

* **I12 dimension whitelist**: usage presentation surfaces may only show the token four layers / counts / duration
milliseconds / percentages.
  A whitelist rather than a banned-word list — a blacklist only works by enumerating every out-of-bounds phrasing; miss
  one and the defense breaks.
* **I13 same-screen coverage**: every token number must carry its metric; aggregate surfaces must return the denominator
and unattributed at the same level.
  Page annotations are soft constraints, so it is pinned at the type layer: the metric is written next to the field, not
  only in the registry.

Specification: docs/token-attribution-v1-design.md §0.2 / §1.1 / §2.5 / §2.6 / §4.4.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from aiteam.storage.connection import COLUMNS_TO_ENSURE, _sqlite_migrate
from aiteam.storage.models import AgentModel
from aiteam.types import (
    CTX_WATERMARK_METRIC,
    TOKEN_LAYERS,
    TOKEN_METRIC_LABELS,
    TOKEN_METRIC_SPECS,
    Agent,
    TokenMetric,
    TokenSource,
)

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register before executing: the dataclass decorator must be able to look its namespace back up via __module__
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


registry = _load("usage_surface")
dimensions = _load("check_usage_dimensions")
coverage = _load("check_usage_coverage")


class TestMetricVocabulary:
    """The metric vocabulary is itself a guardrail — it must be closed and must not quietly gain a member."""

    def test_two_usage_metrics_only(self):
        """TokenMetric covers only the two metrics of the single problem domain of "usage"."""
        assert {m.value for m in TokenMetric} == {"usage_sum", "ctx_last"}

    def test_context_watermark_is_deliberately_outside_the_enum(self):
        """The third metric (context watermark) is deliberately not a TokenMetric member, but must still be labelable.

        It is unrelated to usage attribution (it serves agent reuse governance), yet every token number on a page must
        carry
        its metric — including those not participating in attribution. So it is in the vocabulary, not in the enum.
        """
        assert CTX_WATERMARK_METRIC not in {m.value for m in TokenMetric}
        assert CTX_WATERMARK_METRIC in TOKEN_METRIC_LABELS
        assert TOKEN_METRIC_LABELS == frozenset({"usage_sum", "ctx_last", CTX_WATERMARK_METRIC})

    def test_every_metric_has_a_definition_and_a_producer(self):
        """A metric must state clearly who produced it — otherwise "where did this number come from" is untraceable
        afterwards."""
        assert set(TOKEN_METRIC_SPECS) == set(TOKEN_METRIC_LABELS)
        for label, (name, producer, definition) in TOKEN_METRIC_SPECS.items():
            assert name and producer and definition, f"{label}'s metric description is incomplete"

    def test_four_layers_are_the_unit_of_presentation(self):
        """Four-layer itemization is a hard requirement: measured 95.6% of the volume is cache_read; reporting only
        the total equals reporting only cache reads."""
        assert TOKEN_LAYERS == (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        )


class TestTokensSourceColumn:
    """The only newly added column — adding a column must sync four places (types / ORM / bidirectional conversion /
    migration)."""

    def test_declared_in_columns_to_ensure(self):
        """Project iron rule: ORM field additions must sync COLUMNS_TO_ENSURE, otherwise existing databases never get
        this column."""
        assert ("agents", "tokens_source") in {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}

    def test_legacy_db_gets_the_column(self, tmp_path: Path):
        """Run the migration against an old agents table without the column; the column must be added."""
        db = str(tmp_path / "legacy.db")
        con = sqlite3.connect(db)
        con.execute(
            """CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )"""
        )
        con.commit()
        con.close()

        _sqlite_migrate(db)

        con = sqlite3.connect(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(agents)")}
        con.close()
        assert "tokens_source" in cols

    def test_round_trips_through_the_orm(self):
        agent = Agent(
            team_id="t1", name="w", role="worker", tokens_source=TokenSource.TRANSCRIPT
        )
        back = AgentModel.from_pydantic(agent).to_pydantic()
        assert back.tokens_source is TokenSource.TRANSCRIPT

    def test_unmeasured_is_none_not_a_guess(self):
        """NULL = not yet collected; it is neither transcript ground truth nor an alias fallback. no-data ≠ zero."""
        agent = Agent(team_id="t1", name="w", role="worker")
        assert agent.tokens_source is None
        assert AgentModel.from_pydantic(agent).tokens_source is None
        assert AgentModel.from_pydantic(agent).to_pydantic().tokens_source is None


class TestI12Dimensions:
    def test_repo_is_green(self):
        assert dimensions.main() == 0

    def test_forbidden_words_match_by_word_not_substring(self):
        """Substring matching would read isPending as "spend" and statusDone as "usd".

        The first version was written that way and produced 64 false positives in one run. A machine check that
        false-alarms daily equals no machine check, so
        it was changed to split words by camelCase / snake_case before judging.
        """
        for innocent in ("isPending", "statusDone", "agentStatusDone", "statusData",
                         "taskStatusPending", "credential", "discount_free"):
            assert dimensions._forbidden_word(innocent) == "", f"{innocent} was falsely judged"

    def test_forbidden_words_catch_real_cross_unit_conversions(self):
        """A fifth dimension class like money/work hours must be caught however it is spelled."""
        for guilty in ("cost_usd", "costUsd", "totalCost", "priceCny", "manHour",
                       "man_hour", "estimated_workdays", "monthlyBudget", "credits_used"):
            assert dimensions._forbidden_word(guilty), f"{guilty} slipped through"

    def test_undeclared_numeric_field_is_red(self, monkeypatch):
        """A new undeclared dimension numeric field on a presentation surface = red (the whitelist is only closed
        when declarations are complete)."""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(dimensions, "PY_SURFACES", (surface,))
        problems = dimensions.check_python_schema()
        assert any("undeclared dimension" in p and "tool_calls" in p for p in problems)

    def test_dimension_outside_the_whitelist_is_red(self, monkeypatch):
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={
                "phase_index": registry.FieldSpec(registry.NON_USAGE, note="index"),
                "tokens": registry.FieldSpec("usd", metric="ctx_last"),
                "tool_calls": registry.FieldSpec("count"),
                "duration_ms": registry.FieldSpec("duration_ms"),
            },
        )
        monkeypatch.setattr(dimensions, "PY_SURFACES", (surface,))
        problems = dimensions.check_python_schema()
        assert any("not in the whitelist" in p for p in problems)

    def test_unnamed_exemption_is_red(self, monkeypatch):
        """Declaring a "non-usage field" must state a reason — exemption cannot be silent."""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={
                "phase_index": registry.FieldSpec(registry.NON_USAGE),
                "tokens": registry.FieldSpec("token", metric="ctx_last"),
                "tool_calls": registry.FieldSpec("count"),
                "duration_ms": registry.FieldSpec("duration_ms"),
            },
        )
        monkeypatch.setattr(dimensions, "PY_SURFACES", (surface,))
        assert any("exemptions must be named" in p for p in dimensions.check_python_schema())

    def test_unregistered_frontend_surface_is_red(self, monkeypatch):
        """A token number newly appearing on a page without registration = red, otherwise the whitelist has a bypass."""
        monkeypatch.setattr(
            dimensions, "FRONTEND_SURFACES", ("dashboard/src/api/workflows.ts",)
        )
        problems, _warnings = dimensions.check_frontend()
        assert any("not registered as a usage presentation surface" in p for p in problems)


class TestI13Coverage:
    def test_repo_is_green(self):
        assert coverage.main() == 0

    def test_token_field_without_metric_is_red(self, monkeypatch):
        """A token number detached from its metric is meaningless — the two metrics differ by 5–25×."""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token")},
            coverage_gap={"tokens": "historical legacy"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("metric not labeled" in p for p in problems)

    def test_metric_only_in_the_registry_is_red(self, monkeypatch):
        """The metric must be on the same screen as the field. Only in the registry = the next person editing this
        line cannot see it."""
        # The Agent definition block writes USAGE_SUM and CTX_WATERMARK_METRIC, but not ctx_last —
        # pass it off as ctx_last and the machine check must see that "this metric does not exist in the source at all".
        surface = registry.PySurface(
            model="Agent",
            kind="row",
            fields={"ctx_tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("only written in the registry" in p for p in problems)

    def test_aggregate_without_denominator_is_red(self, monkeypatch):
        """An aggregate surface missing its denominator = a part impersonating the whole. This rule is loaded but not
        fired today; it takes effect when phase 2 lands."""
        surface = registry.PySurface(
            model="WorkflowRun",
            kind="aggregate",
            fields={"total_tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("missing same-level coverage fields" in p for p in problems)

    def test_usage_sum_aggregate_cannot_declare_a_gap(self, monkeypatch):
        """The attribution numbers' denominator has no exceptions — the ctx_last-side historical gap cannot be
        analogized to usage_sum."""
        surface = registry.PySurface(
            model="Agent",
            kind="aggregate",
            fields={"input_tokens": registry.FieldSpec("token", metric="usage_sum")},
            coverage_gap={"input_tokens": "later"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("does not accept gap declarations" in p for p in problems)

    def test_row_surface_zero_as_no_data_must_be_declared(self, monkeypatch):
        """A non-Optional token column where 0 also means "not collected" — if not changed, it must be declared by
        name, not silently."""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token", metric="ctx_last")},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("'not collected' and 'really 0'" in p for p in problems)

    def test_declared_gaps_stay_visible(self, monkeypatch):
        """A declaration is not an exemption: every machine check prints the gap, so it does not become the default
        state."""
        surface = registry.PySurface(
            model="WorkflowAgent",
            kind="row",
            fields={"tokens": registry.FieldSpec("token", metric="ctx_last")},
            coverage_gap={"tokens": "ctx_last-side historical legacy"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, warnings = coverage.check_python()
        assert not problems
        assert any("declared coverage gap" in w for w in warnings)

    def test_closed_gap_must_be_cleaned_up(self, monkeypatch):
        """A field already nullable still carrying a gap declaration = registry rot; also red."""
        surface = registry.PySurface(
            model="WorkflowRun",
            kind="row",
            fields={"live_tokens": registry.FieldSpec("token", metric="ctx_last")},
            coverage_gap={"live_tokens": "closed long ago"},
        )
        monkeypatch.setattr(coverage, "PY_SURFACES", (surface,))
        problems, _ = coverage.check_python()
        assert any("gap already closed" in p for p in problems)
