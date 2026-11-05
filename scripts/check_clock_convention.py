#!/usr/bin/env python3
"""Red-line machine check I11 — the whole database may have only one clock.

Incident background (docs/utc-unification-design.md): this database once ran two wall clocks at the same time: the core domain wrote
``datetime.now()`` (host-local), and the ecosystem domain wrote ``datetime.now(tz=UTC)``. When SQLite
stores an aware datetime it silently strips the offset, so rows of the two conventions look exactly alike, and cross-domain comparisons
return an answer off by 8 hours **without raising any exception**. It lived for several months before one audit caught three places.

The dual wall clocks were not decided by anyone; they were written casually, one module at a time. So fixing it is not enough — without a machine check,
the same thing will happen again, and again nobody will see it.

Three guards:

1. The Python side may not use bare wall clocks: ``datetime.now()`` / ``datetime.now(UTC)`` /
   ``datetime.fromtimestamp()`` must all go through ``aiteam.clock``.
2. The Python side may not strip offsets by hand: ``astimezone().replace(tzinfo=None)`` is the old action that moves a value back to the local
   clock; it has been replaced by ``to_naive_utc()``.
3. The frontend may not bare-parse server time strings: ``new Date(<string>)`` reads a string without an offset in the browser's local
   timezone, which is exactly why ecosystem page times were 8 hours early. Always go through ``@/lib/datetime``.

Exit codes: 0=all pass, 1=violations found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Hook scripts are standalone processes running outside the aiteam package (and must be byte-for-byte identical to plugin/hooks, I1),
# so they cannot import aiteam.clock; they only use UTC anyway.
PY_EXEMPT_DIRS = ("src/aiteam/hooks",)
PY_EXEMPT_FILES = (
    "src/aiteam/clock.py",
    "src/aiteam/storage/utc_type.py",
    "scripts/check_clock_convention.py",  # this file's pattern strings match themselves
)
SCRIPT_EXEMPT = (
    "scripts/hn_monitor.py",  # a standalone small tool unrelated to production data
    # The shift script's job is precisely to convert local wall-clock values to UTC; comparing "local now" with "UTC now" is its
    # very criterion. It also deliberately does not import aiteam — it must be able to run against a bare .db file without depending on the package.
    "scripts/migrate_timestamps_utc.py",
)

TS_EXEMPT = ("dashboard/src/lib/datetime.ts",)

PY_RULES = [
    (r"\bdatetime\.now\(", "bare wall-clock call — use aiteam.clock.utc_now() instead"),
    (
        r"\bdatetime\.fromtimestamp\(",
        "bare fromtimestamp tags the instant with the host-local offset — use aiteam.clock.from_timestamp() instead",
    ),
    (
        r"astimezone\(\)\s*\.replace\(tzinfo=None\)",
        "manually stripping the offset — use aiteam.clock.to_naive_utc() instead",
    ),
]

# new Date(<non-empty, non-pure-number, non-template-string argument>)
TS_RULE = re.compile(r"new Date\(\s*(?!\s*\))(?![\d`])")


def _iter_lines(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue
        yield lineno, line


def check_python() -> list[str]:
    problems: list[str] = []
    for base in ("src", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in PY_EXEMPT_FILES or rel in SCRIPT_EXEMPT:
                continue
            if any(rel.startswith(d) for d in PY_EXEMPT_DIRS):
                continue
            for lineno, line in _iter_lines(path):
                for pattern, why in PY_RULES:
                    if re.search(pattern, line):
                        problems.append(f"{rel}:{lineno}: {why}\n    {line.strip()}")
    return problems


def check_dashboard() -> list[str]:
    problems: list[str] = []
    src = ROOT / "dashboard" / "src"
    if not src.is_dir():
        return problems
    for path in sorted(src.rglob("*.ts")) + sorted(src.rglob("*.tsx")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in TS_EXEMPT:
            continue
        for lineno, line in _iter_lines(path):
            for m in TS_RULE.finditer(line):
                tail = line[m.end() :]
                arg = tail.split(")")[0].strip()
                # Epoch numbers (e.g. ts * 1000) are absolute instants and inherently have no timezone ambiguity
                if "* 1000" in arg or re.fullmatch(r"[\d_.]+", arg):
                    continue
                problems.append(
                    f"{rel}:{lineno}: bare parsing of a server time string — use @/lib/datetime instead\n"
                    f"    {line.strip()}"
                )
    return problems


def main() -> int:
    problems = check_python() + check_dashboard()
    if problems:
        print("❌ Clock convention violations — no second clock may appear in the database:")
        for p in problems:
            print(f"  {p}")
        print(f"\nTotal {len(problems)} places. Spec in docs/utc-unification-design.md")
        return 1
    py = sum(1 for _ in (ROOT / "src").rglob("*.py"))
    ts = sum(1 for _ in (ROOT / "dashboard" / "src").rglob("*.ts*")) if (
        ROOT / "dashboard" / "src"
    ).is_dir() else 0
    print(f"✅ Clock convention unified: {py} Python modules / {ts} frontend modules, zero bare wall clocks, zero bare parsing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
