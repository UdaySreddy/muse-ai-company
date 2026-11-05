#!/usr/bin/env python3
"""I12 — dimension allowlist machine check for usage presentation surfaces.

Red line (token usage attribution v1 design §4.4 / P1):

    Usage-related presentation surfaces (API schema field names + frontend display units) may only use four kinds of dimensions:
    tokens (four layers, listed separately), counts, duration in milliseconds, percentages. The appearance of any dimension outside these kinds fails.

Why an allowlist rather than a banned-word list: a blacklist has to exhaustively enumerate every out-of-bounds phrasing (multiple languages, symbols, abbreviations, slang)
to work, and missing one breaks the defense; an allowlist only has to confirm four legal values, and a new dimension must be explicitly reviewed.

For the allowlist to hold, the precondition is "there are no undeclared presentation surfaces", so this check is three things rather than one:

1. **Complete declaration**: every numeric field of every model in the registry must agree **bidirectionally** with the actual fields
   introspected from Pydantic. A new field with no declaration = red; a declaration for a deleted field = also red (a rotting registry silently disables the allowlist).
2. **Legal dimensions**: every declared dimension must be in the four-kind allowlist; anything declared a "non-usage field" must state its reason.
3. **No unregistered presentation surfaces**: any model in ``aiteam.types`` carrying token fields must be in the registry;
   the same goes for any file under frontend ``dashboard/src`` in which a token identifier appears. Plus a safety net: a presentation surface
   on which a fifth-kind dimension word stem such as money/person-hours appears is red.

Usage: python3 scripts/check_usage_dimensions.py   (run from the repo root)
Exit codes: 0=all pass, 1=violations found.
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
    ALLOWED_DIMENSIONS,
    FORBIDDEN_UNIT_WORDS,
    FRONTEND_IDENTIFIERS,
    FRONTEND_SURFACES,
    NON_USAGE,
    PY_SURFACES,
)

# An identifier containing a token word stem = a numeric value of a token dimension (or its display carrier).
TOKEN_STEM = re.compile(r"token", re.IGNORECASE)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# camelCase / snake_case word splitting: HTTPServer → ["http", "server"], ctx_pct → ["ctx", "pct"]
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+")


def _words(identifier: str) -> list[str]:
    return [w.lower() for part in identifier.split("_") for w in WORD.findall(part)]


def _forbidden_word(identifier: str) -> str:
    """Return the fifth-kind dimension word this identifier hits, or an empty string if it hits none.

    Singular and plural judged the same (``costs`` = ``cost``); adjacent words are merged and judged again, catching ``manHour`` /
    ``man_hour`` style spellings that each look innocent when split apart.
    """
    words = _words(identifier)
    for w in words:
        if w in FORBIDDEN_UNIT_WORDS or w.rstrip("s") in FORBIDDEN_UNIT_WORDS:
            return w
    for a, b in zip(words, words[1:]):
        joined = a + b
        if joined in FORBIDDEN_UNIT_WORDS or joined.rstrip("s") in FORBIDDEN_UNIT_WORDS:
            return joined
    return ""


def _is_numeric(annotation: object) -> bool:
    """True for int / float (including ``X | None``); bool does not count as a numeric dimension."""
    args = typing.get_args(annotation)
    candidates = list(args) if args else [annotation]
    return any(c in (int, float) for c in candidates)


def _numeric_fields(model: object) -> set[str]:
    return {
        name
        for name, info in model.model_fields.items()  # type: ignore[attr-defined]
        if _is_numeric(info.annotation)
    }


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Return the source by line, skipping whole-line comments (explanatory text in comments is not a presentation surface)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue
        out.append((lineno, line))
    return out


def check_python_schema() -> list[str]:
    """API schema side: bidirectional comparison of the registry against actual Pydantic fields + dimension allowlist."""
    import aiteam.types as t

    problems: list[str] = []
    for surface in PY_SURFACES:
        model = getattr(t, surface.model, None)
        if model is None:
            problems.append(f"The model aiteam.types.{surface.model} declared in the registry does not exist — registry rot")
            continue
        actual = _numeric_fields(model)
        declared = set(surface.fields)
        for name in sorted(actual - declared):
            problems.append(
                f"{surface.model}.{name}: the usage presentation surface gained a numeric field with an undeclared dimension — "
                f"declare its dimension in scripts/usage_surface.py ({'/'.join(ALLOWED_DIMENSIONS)}) or exempt it by name"
            )
        for name in sorted(declared - actual):
            problems.append(f"{surface.model}.{name}: the registry declares a field that does not exist — when deleting a field, sync the registry")
        for name in sorted(declared & actual):
            spec = surface.fields[name]
            if spec.dimension == NON_USAGE:
                if not spec.note:
                    problems.append(f"{surface.model}.{name}: declared a non-usage field but gave no reason — exemptions must be named")
            elif spec.dimension not in ALLOWED_DIMENSIONS:
                problems.append(
                    f"{surface.model}.{name}: dimension '{spec.dimension}' is not in the whitelist — "
                    f"only {'/'.join(ALLOWED_DIMENSIONS)} are allowed; a new dimension must be explicitly reviewed"
                )

    # No unregistered presentation surfaces: a model with token fields must be in the registry
    registered = {s.model for s in PY_SURFACES}
    for name in dir(t):
        model = getattr(t, name)
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict) or name in registered:
            continue
        hits = sorted(f for f in fields if TOKEN_STEM.search(f))
        if hits:
            problems.append(
                f"aiteam.types.{name}: has token fields {hits} but is not registered as a usage presentation surface — "
                f"register it in PY_SURFACES in scripts/usage_surface.py"
            )
    return problems


def check_forbidden_units() -> list[str]:
    """Safety net: a fifth-kind dimension word stem (money / person-hours …) on a presentation surface is red."""
    problems: list[str] = []
    targets = [ROOT / "src" / "aiteam" / "types.py"]
    dash = ROOT / "dashboard" / "src"
    if dash.is_dir():
        targets += sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx"))
    for path in targets:
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in _code_lines(path):
            for ident in IDENTIFIER.findall(line):
                hit = _forbidden_word(ident)
                if hit:
                    problems.append(
                        f"{rel}:{lineno}: identifier '{ident}' hits fifth-kind dimension word '{hit}' — "
                        f"usage presentation may only be expressed in tokens, cross-dimension conversion is forbidden (P1)"
                    )
    return problems


def check_frontend() -> tuple[list[str], list[str]]:
    """Frontend: bidirectional registration of presentation-surface files + dimension allowlist for token identifiers."""
    problems: list[str] = []
    warnings: list[str] = []
    dash = ROOT / "dashboard" / "src"
    if not dash.is_dir():
        warnings.append("dashboard/src missing (unbuilt environment), frontend dimension check skipped")
        return problems, warnings

    declared = set(FRONTEND_SURFACES)
    found: dict[str, set[str]] = {}
    for path in sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx")):
        rel = path.relative_to(ROOT).as_posix()
        idents = {
            ident
            for _lineno, line in _code_lines(path)
            for ident in IDENTIFIER.findall(line)
            if TOKEN_STEM.search(ident)
        }
        if idents:
            found[rel] = idents

    for rel in sorted(set(found) - declared):
        problems.append(
            f"{rel}: has token identifiers {sorted(found[rel])} but is not registered as a usage presentation surface — "
            f"register it in FRONTEND_SURFACES in scripts/usage_surface.py"
        )
    for rel in sorted(declared - set(found)):
        problems.append(f"{rel}: registered in the registry as a usage presentation surface, but the file no longer contains token identifiers (or the file does not exist) — please sync the registry")
    for rel in sorted(set(found) & declared):
        for ident in sorted(found[rel]):
            dim = FRONTEND_IDENTIFIERS.get(ident)
            if dim is None:
                problems.append(
                    f"{rel}: token identifier '{ident}' has no dimension registered — "
                    f"state what dimension it is in FRONTEND_IDENTIFIERS"
                )
            elif dim not in ALLOWED_DIMENSIONS:
                problems.append(f"{rel}: identifier '{ident}''s dimension '{dim}' is not in the whitelist")
    for ident in sorted(set(FRONTEND_IDENTIFIERS) - {i for s in found.values() for i in s}):
        warnings.append(f"'{ident}' in FRONTEND_IDENTIFIERS is no longer used by anyone and can be cleaned up")
    return problems, warnings


def main() -> int:
    problems = check_python_schema() + check_forbidden_units()
    fe_problems, warnings = check_frontend()
    problems += fe_problems

    for w in warnings:
        print(f"⚠️  {w}")
    if problems:
        print("❌ Dimension allowlist violations — usage presentation surfaces may only show tokens/counts/duration in milliseconds/percentages:")
        for p in problems:
            print(f"  {p}")
        print(f"\nTotal {len(problems)} places. Spec in docs/token-attribution-v1-design.md §4.4")
        return 1

    py_fields = sum(len(s.fields) for s in PY_SURFACES)
    print(
        f"✅ Dimension allowlist passed: {len(PY_SURFACES)} API schema presentation surfaces / {py_fields} numeric fields · "
        f"{len(FRONTEND_SURFACES)} frontend presentation surfaces / {len(FRONTEND_IDENTIFIERS)} token identifiers,"
        f"zero fifth-kind dimensions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
