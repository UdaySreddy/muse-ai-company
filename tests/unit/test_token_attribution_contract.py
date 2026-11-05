"""TokenAttribution contract tests — the three rules of attribution v1 §2.5; they are acceptance conditions, not options.

The design deliberately pins these three rules **at the type layer rather than the view layer**, for a reason confirmed in the meeting: page annotations are soft
constraints that oneself three months later will ignore. So what is tested here is not "does the page show the coverage", but
"can the page physically obtain an isolated total" — the answer must be no.

The three rules verbatim (§2.5):

1. ``TokenAttribution`` **has no** ``total`` / ``total_tokens`` field or property;
2. In any API response body, token numeric fields must appear at the same level as ``dispatches_total`` and ``metric``,
   missing any one fails the test;
3. ``metric`` has no default — without labeling the metric, the object cannot be constructed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aiteam.types import (
    TOKEN_LAYERS,
    AttributionMethod,
    AttributionScope,
    TokenAttribution,
    TokenMetric,
)


def _sample(**overrides) -> TokenAttribution:
    kwargs = {
        "scope": AttributionScope.PROJECT,
        "scope_id": "p1",
        "metric": TokenMetric.USAGE_SUM,
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_tokens": 3,
        "cache_read_tokens": 4,
        "dispatches_attributed": 1,
        "dispatches_total": 2,
        "unattributed_reasons": {"not_yet_measured": 1},
        "measured_window": None,
        "method": AttributionMethod.TRANSCRIPT_PARSE,
    }
    kwargs.update(overrides)
    return TokenAttribution(**kwargs)


class TestNoBareTotal:
    """Contract ①: no isolated total is obtainable — not "the page does not show it", but structurally absent."""

    @pytest.mark.parametrize("banned", ["total", "total_tokens"])
    def test_no_total_field(self, banned):
        assert banned not in TokenAttribution.model_fields

    @pytest.mark.parametrize("banned", ["total", "total_tokens"])
    def test_no_total_attribute_or_property(self, banned):
        # Fields being absent is not enough: adding an @property bypasses the first assertion, and that is exactly the most natural way to write
        # "add a total for the frontend's convenience".
        assert not hasattr(TokenAttribution, banned)
        assert not hasattr(_sample(), banned)

    def test_no_attribute_sums_the_four_layers(self):
        """No accessible member may exactly equal the sum of the four layers — renaming it sum/aggregate does not help."""
        att = _sample()
        four_sum = sum(getattr(att, layer) for layer in TOKEN_LAYERS)
        for name in dir(att):
            # model_* are Pydantic's own introspection interfaces; taking them from an instance warns and is irrelevant here
            if name.startswith(("_", "model_")) or name in TOKEN_LAYERS:
                continue
            value = getattr(att, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                assert value != four_sum, (
                    f"{name} exactly equals the sum of the four layers — that is just a renamed total."
                    f"Measured: 95.6% of the four layers is cache_read, so reporting only the total equals reporting only the cache read volume (§1.2)"
                )

    def test_dump_carries_no_total(self):
        dumped = _sample().model_dump()
        assert "total" not in dumped
        assert "total_tokens" not in dumped
        assert set(TOKEN_LAYERS) <= set(dumped)


class TestMetricHasNoDefault:
    """Contract ③: without labeling the metric, the object cannot be constructed."""

    def test_metric_is_required(self):
        assert TokenAttribution.model_fields["metric"].is_required()

    def test_construction_without_metric_fails(self):
        with pytest.raises(ValidationError) as exc:
            TokenAttribution(
                scope=AttributionScope.PROJECT,
                scope_id="p1",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                dispatches_attributed=0,
                dispatches_total=0,
                unattributed_reasons={},
                measured_window=None,
                method=AttributionMethod.TRANSCRIPT_PARSE,
            )
        assert "metric" in str(exc.value)

    def test_denominator_is_required_too(self):
        """Denominator and metric are peers: if either is missing, the number can be rendered alone."""
        for field in ("dispatches_total", "dispatches_attributed", "unattributed_reasons"):
            assert TokenAttribution.model_fields[field].is_required()

    def test_frozen(self):
        """Immutable after construction — otherwise one could construct legally first and then change the denominator to 0."""
        att = _sample()
        with pytest.raises(ValidationError):
            att.dispatches_total = 0


class TestNumeratorPlusUnattributedEqualsDenominator:
    """Numerator + unattributed == denominator. This is the machine-checkable form of R2.

    The way to "optimize" coverage to 100% is to move rows without data out of the denominator; this invariant makes that approach
    immediately unbalanced — the moved rows are in neither the numerator nor the unattributed, and the equation fails on the spot.
    """

    def test_holds_for_a_well_formed_value(self):
        att = _sample(
            dispatches_attributed=3,
            dispatches_total=10,
            unattributed_reasons={"no_transcript_path": 5, "not_yet_measured": 2},
        )
        assert att.dispatches_attributed + sum(att.unattributed_reasons.values()) == (
            att.dispatches_total
        )

    def test_by_design_never_enters_the_reason_dict(self):
        """``by_design`` speaks of the tool-call level (52,119 activities), not the same unit as dispatches.

        It is the fourth code of §3.4, a formal label on the presentation surface, but mixing it into this dict would break
        the equation above, and would mix dimensions — the thing this design most opposes.
        """
        from aiteam.types import UnattributedReason

        assert UnattributedReason.BY_DESIGN.value == "by_design"
        att = _sample(
            dispatches_attributed=1,
            dispatches_total=2,
            unattributed_reasons={"not_yet_measured": 1},
        )
        assert UnattributedReason.BY_DESIGN.value not in att.unattributed_reasons


class TestRegisteredAsAggregateSurface:
    """A phase-0 handover item: not registered as an aggregate surface, I13 should go red."""

    def test_registered_in_usage_surface(self):
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "scripts"))
        from usage_surface import AGGREGATE_REQUIRED_FIELDS, PY_SURFACES

        surface = next((s for s in PY_SURFACES if s.model == "TokenAttribution"), None)
        assert surface is not None, "TokenAttribution is not registered — I13 will go red (phase 0 handover item)"
        assert surface.kind == "aggregate"
        for field in AGGREGATE_REQUIRED_FIELDS:
            assert field in TokenAttribution.model_fields
        assert not surface.coverage_gap, "The usage_sum aggregate surface does not accept coverage_gap declarations"
