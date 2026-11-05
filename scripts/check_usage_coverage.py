#!/usr/bin/env python3
"""I13 — coverage same-screen red-line machine check (token values may not appear detached from their metric and
denominator).

Red line (token usage attribution v1 design §4.4 / §2.5 / P2):

    For any token value on any presentation surface, if the C_measure of its scope is < 100%, the unattributed portion
    must be shown
    on the same screen at the same level. Its absence counts as a red-line violation.

And its precondition — **any token value, detached from a metric label, is meaningless** (§0.2). This database
simultaneously
has two orthogonal metrics, measured to differ by 5-25x; putting them side by side or adding them up is the same class
of accident as the one just suffered on timestamps.

Four guards:

1. **Metric must be labeled**: every token dimension field in the registry must carry a metric, and the metric must be
in the closed set
   recognized by ``aiteam.types`` (the two TokenMetric members + the context watermark that does not participate in
   attribution).
2. **Metric on the same screen**: the metric label must **appear next to the field** (inside that model's source block
in ``types.py``),
   not only in this registry. A page comment is a soft constraint that your future self three months later will ignore;
   a metric written
   next to the field is something the next person to change that line cannot dodge.
3. **Aggregate surfaces must carry the denominator**: an aggregating presentation surface must return
``dispatches_total`` /
   ``unattributed_reasons`` at the same level, and an aggregate surface of the ``usage_sum`` metric **may not** declare
   a gap
   — the denominator of attribution numbers admits no exceptions.
4. **Row surfaces: no-data ≠ zero**: on a record surface with one fact per row, "not collected" must be distinguishable
from "really 0"
   (the column may be None); surfaces that cannot do this must declare the gap by name, and the machine check prints it
   out every time.

Guards 3 and 4 today are respectively in the "cocked but not fired" and "two gaps already declared" states — that is the
honest picture:
stage 0 only rectifies metric names, aggregate surfaces land in stage 2, and frontend badges land in stage 5.

Usage: python3 scripts/check_usage_coverage.py   (run from the repo root)
Exit codes: 0=all pass (declared gaps only warn, they do not block), 1=violations found.
"""

from __future__ import annotations

import re
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_surface import (  # noqa: E402  — must come after sys.path is in place
    AGGREGATE_REQUIRED_FIELDS,
    COVERAGE_MARKERS,
    FRONTEND_COVERAGE_GAP,
    FRONTEND_SURFACES,
    PY_SURFACES,
)

TYPES_PY = ROOT / "src" / "aiteam" / "types.py"


def _model_source(model: str) -> str:
    """Get the source block of one model in ``types.py`` (from its class line to before the next top-level class)."""
    text = TYPES_PY.read_text(encoding="utf-8")
    match = re.search(rf"^class {re.escape(model)}\(", text, re.MULTILINE)
    if not match:
        return ""
    tail = text[match.start() :]
    nxt = re.search(r"\n(?=class )", tail)
    return tail[: nxt.start()] if nxt else tail


def _is_optional(annotation: object) -> bool:
    return type(None) in typing.get_args(annotation)


def check_python() -> tuple[list[str], list[str]]:
    import aiteam.types as t

    allowed_metrics = t.TOKEN_METRIC_LABELS
    problems: list[str] = []
    warnings: list[str] = []

    for surface in PY_SURFACES:
        model = getattr(t, surface.model, None)
        if model is None:
            problems.append(f"The model aiteam.types.{surface.model} declared in the registry does not exist")
            continue
        if surface.kind not in ("row", "aggregate"):
            problems.append(f"{surface.model}: kind='{surface.kind}' is invalid, must be row / aggregate")
            continue
        source = _model_source(surface.model)
        fields = model.model_fields
        token_fields = {n: s for n, s in surface.fields.items() if s.dimension == "token"}

        for name, spec in sorted(token_fields.items()):
            # Guard 1: metric must be labeled
            if not spec.metric:
                problems.append(
                    f"{surface.model}.{name}: metric not labeled — "
                    f"a token number detached from its metric is meaningless (usage_sum and ctx_last measured 5-25x "
                    f"apart)"
                )
                continue
            if spec.metric not in allowed_metrics:
                problems.append(
                    f"{surface.model}.{name}: metric '{spec.metric}' is not in the aiteam.types."
                    f"TOKEN_METRIC_LABELS ({sorted(allowed_metrics)})"
                )
                continue
            # Guard 2: metric on the same screen (the label must sit next to the field, not live only in the registry).
            # Case-insensitive: writing the literal "ctx_last" and writing the symbol ``TokenMetric.CTX_LAST`` are
            # equally valid — what matters is that the metric is visible next to the field, not one particular spelling.
            if spec.metric.lower() not in source.lower():
                problems.append(
                    f"{surface.model}.{name}: metric '{spec.metric}' is only written in the registry,"
                    f"and is not visible inside the {surface.model} definition block in types.py — the metric label "
                    f"must be on the same screen as the field"
                )

        if surface.kind == "aggregate":
            # Guard 3: aggregate surfaces must carry the denominator and the unattributed classification
            missing = [f for f in AGGREGATE_REQUIRED_FIELDS if f not in fields]
            if missing:
                problems.append(
                    f"{surface.model}: aggregate presentation surface missing same-level coverage fields {missing} — "
                    f"values, denominator and unattributed must live and die together, otherwise a part impersonates "
                    f"the whole"
                )
            if surface.coverage_gap and any(s.metric == "usage_sum" for s in token_fields.values()):
                problems.append(
                    f"{surface.model}: a usage_sum aggregate surface does not accept gap declarations "
                    f"(declared {sorted(surface.coverage_gap)}) — the denominator of attribution numbers has no "
                    f"exceptions"
                )
            continue

        # Guard 4: row surfaces, no-data ≠ zero
        for name in sorted(token_fields):
            info = fields.get(name)
            if info is None:
                continue  # Field existence is the responsibility of I12's bidirectional comparison
            optional = _is_optional(info.annotation)
            declared = name in surface.coverage_gap
            if not optional and not declared:
                problems.append(
                    f"{surface.model}.{name}: a non-Optional token column where 0 simultaneously means"
                    f"'not collected' and 'really 0' — either make it nullable, or declare it by name in coverage_gap"
                )
            elif optional and declared:
                problems.append(
                    f"{surface.model}.{name}: the field is already nullable yet still carries a coverage_gap "
                    f"declaration — gap already closed, please clean the registry"
                )
            elif declared:
                warnings.append(f"declared coverage gap {surface.model}.{name}: {surface.coverage_gap[name]}")
    return problems, warnings


def check_frontend() -> tuple[list[str], list[str]]:
    """Frontend: once a usage_sum value reaches a page, it must carry an unattributed annotation on the same screen.

    Today this one is cocked but not fired — the four usage layers have not yet appeared on any page. The moment stage
    5's ``/usage``
    page lands, it takes effect automatically, and that is exactly the moment when it is easiest to "put the numbers up
    first and do coverage next version".
    """
    import aiteam.types as t

    problems: list[str] = []
    warnings: list[str] = []
    dash = ROOT / "dashboard" / "src"
    if not dash.is_dir():
        warnings.append("dashboard/src missing (unbuilt environment), frontend coverage check skipped")
        return problems, warnings

    layers = re.compile("|".join(re.escape(x) for x in t.TOKEN_LAYERS))
    for path in sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not layers.search(text):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if not any(m in text for m in COVERAGE_MARKERS):
            problems.append(
                f"{rel}: presents the four usage_sum layers but has no unattributed/coverage annotation — "
                f"the same-screen red line (one of these must appear: {'/'.join(COVERAGE_MARKERS)})"
            )
    warnings.append(f"Overall frontend gap: {FRONTEND_COVERAGE_GAP}")
    return problems, warnings


def main() -> int:
    py_problems, py_warnings = check_python()
    fe_problems, fe_warnings = check_frontend()
    problems = py_problems + fe_problems

    for w in py_warnings + fe_warnings:
        print(f"⚠️  {w}")
    if problems:
        print("❌ Coverage same-screen red-line violations — token values may not appear detached from metric and "
            "denominator:")
        for p in problems:
            print(f"  {p}")
        print(f"\nTotal {len(problems)} places. Spec in docs/token-attribution-v1-design.md §4.4 / §2.5")
        return 1

    token_fields = sum(
        1 for s in PY_SURFACES for spec in s.fields.values() if spec.dimension == "token"
    )
    gaps = sum(len(s.coverage_gap) for s in PY_SURFACES)
    print(
        f"✅ Coverage same-screen red line passed: {token_fields} token fields all carry a metric and the metric is on "
        f"the same screen as the field · "
        f"{len(FRONTEND_SURFACES)} frontend presentation surfaces with zero bare usage_sum · {gaps} declared gaps (see "
        f"warnings above)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
