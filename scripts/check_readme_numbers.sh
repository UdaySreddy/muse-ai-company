#!/usr/bin/env bash
# README number machine check — key numbers in the README aligned with measured code values; drift turns it red.
# Background: a 2026-07 audit found three rotten numbers — 18 pages / 631+ tests / 30+ ecosystem tools — all from manual maintenance.
#
# Loose design against false positives — it only anchors four kinds of high-value claims, and ordinary wording changes cause no collateral damage:
#   ① Version: the announcement line (starting with "> ⚡") must contain v{__version__} (hard equality)
#   ② MCP tool count: "N MCP tools" claims = the measured @mcp.tool count (hard equality);
#      when the text before the number on the same line contains ecosystem, it is checked against the count measured in ecosystem.py alone
#   ③ Test count: "N+ tests" claims ≤ the count actually collected by pytest --collect-only (one-way, against exaggeration;
#      when pytest is unavailable this item is skipped and CI is the backstop)
#   ④ Page count: "N pages" claims = the route count measured in App.tsx (hard equality)
#   ⑤ REST endpoint count: "N REST endpoints" and the directory tree's "(N routes)"
#      = the operation count measured in the application's own OpenAPI route table (hard equality). The source of truth must be the route table, not grep:
#      on 2026-07-27 a static grep only counted 195 while the route table had 199 (multi-method routes and multi-line decorators slipped through).
#
# Usage: bash scripts/check_readme_numbers.sh   (run from the repo root; works for CI and local)
# Exit code: 0=all pass, 1=drift found

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── Measured sources of truth ─────────────────────────────────────
VERSION="$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('src/aiteam/__init__.py').read()).group(1))")" || {
  echo "❌ Cannot read __version__ from src/aiteam/__init__.py"; exit 1; }
MCP_TOTAL="$(grep -h '@mcp.tool' src/aiteam/mcp/tools/*.py | wc -l | tr -d ' ')"
MCP_ECO="$(grep -c '@mcp.tool' src/aiteam/mcp/tools/ecosystem.py | tr -d ' ')"
PAGES="$(grep -c '<Route .*element={<ErrorBoundary>' dashboard/src/App.tsx | tr -d ' ')"
# Actually-collected test count (accepts both the "N tests collected" and "collected N items" output forms)
TESTS="$(python3 -m pytest tests --collect-only -q 2>/dev/null | tail -5 \
  | grep -oE '[0-9]+ tests? collected|collected [0-9]+ items' \
  | grep -oE '[0-9]+' | head -1)" || TESTS=""
# REST endpoint measurement = the number of operations (path × method) in the application's own OpenAPI route table.
# Goes through the public app.openapi() interface and does not touch FastAPI's lazy include internals.
#
# Must explicitly push **this repo's** src to the very front of sys.path: aiteam is an editable install, and a bare import
# resolves to the checkout registered at install time. Run in a git worktree (this repo's multi-session parallelism discipline
# requires the second and subsequent sessions to use a worktree) and it would silently go and count the main checkout's routes, so
# this item would forever "pass" — what it checks would not be the code in front of you at all. All other items (tool counts/page counts by grep,
# I12/I13) already take their numbers by ROOT; only this one was missed.
REST="$(python3 -c '
import sys

sys.path.insert(0, "src")

from fastapi import FastAPI

from aiteam.api.routes import api_router

app = FastAPI()
app.include_router(api_router)
paths = app.openapi().get("paths", {})
verbs = ("get", "post", "put", "patch", "delete")
print(sum(1 for ops in paths.values() for m in ops if m.lower() in verbs))
' 2>/dev/null)" || REST=""

python3 - "$VERSION" "$MCP_TOTAL" "$MCP_ECO" "$PAGES" "${TESTS:-}" "${REST:-}" <<'PYEOF'
import re
import sys

version, mcp_total, mcp_eco, pages = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
tests = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
rest = int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else None

RE_MCP = re.compile(r'(\d[\d,]*)\**\s*MCP\s*tools')
RE_TEST = re.compile(r'(\d[\d,]*)\+?\**\s*(?:automated\s+)?tests\b')
RE_PAGE = re.compile(r'(\d[\d,]*)\**\s*(?:[Dd]ashboard\s*)?\s*(?:pages\b)')
RE_ECO_CTX = re.compile(r'[Ee]cosystem')
# ⑤ Both spellings are anchored: the headline's "N REST endpoints", and the directory tree's "REST endpoints (N routes)"
RE_REST = re.compile(r'(\d[\d,]*)\**\s*REST\s*(?:endpoints?\b)')
RE_REST_ROUTES = re.compile(r'REST\s*(?:endpoints?)\s*[(](\d[\d,]*)\s*(?:routes?\b)')

fails = []
for path in ('README.md',):
    lines = open(path, encoding='utf-8').read().splitlines()
    # ① Version claim
    ann = [l for l in lines if l.lstrip().startswith('> ⚡')]
    if not ann:
        fails.append(f'{path}: announcement line not found (starting with "> ⚡") — cannot verify the version claim')
    elif not any(f'v{version}' in l for l in ann):
        fails.append(f'{path}: announcement-line version claim ≠ code version v{version}'
                      f'(src/aiteam/__init__.py) — after a release, please sync the quote block at the top of the README')
    for i, line in enumerate(lines, 1):
        # ② MCP tool count (a line prefix containing ecosystem → checked against the ecosystem-only count)
        for m in RE_MCP.finditer(line):
            n = int(m.group(1).replace(',', ''))
            is_eco = bool(RE_ECO_CTX.search(line[:m.start()]))
            expect = mcp_eco if is_eco else mcp_total
            label = f'measured ecosystem tool count {mcp_eco}' if is_eco else f'measured MCP tool total {mcp_total}'
            if n != expect:
                fails.append(f'{path}:{i}: claim "{m.group(0).strip()}" ≠ {label}'
                             f'(grep @mcp.tool src/aiteam/mcp/tools/)')
        # ③ Test count (one-way: claim ≤ collected, against inflating)
        if tests is not None:
            for m in RE_TEST.finditer(line):
                n = int(m.group(1).replace(',', ''))
                if n > tests:
                    fails.append(f'{path}:{i}: test count claim "{m.group(0).strip()}" > collected {tests}'
                                 f'(pytest --collect-only) — the README number exceeds the actual test count')
        # ④ Page count
        for m in RE_PAGE.finditer(line):
            n = int(m.group(1).replace(',', ''))
            if n != pages:
                fails.append(f'{path}:{i}: page count claim "{m.group(0).strip()}" ≠ measured route count {pages}'
                             f'(dashboard/src/App.tsx)')
        # ⑤ REST endpoint count (both spellings, one source of truth)
        if rest is not None:
            for regex in (RE_REST, RE_REST_ROUTES):
                for m in regex.finditer(line):
                    n = int(m.group(1).replace(',', ''))
                    if n != rest:
                        fails.append(f'{path}:{i}: REST endpoint claim "{m.group(0).strip()}" ≠ '
                                      f'measured route-table count {rest} (app.openapi() path × method)')

if fails:
    for f in fails:
        print(f'❌ {f}')
    print(f'\nConclusion: ❌ README number drift in {len(fails)} places — please update the README with the measured values')
    sys.exit(1)

skip = '' if tests is not None else '(⚠️ pytest unavailable, test-count check skipped, CI is the backstop)'
skip_rest = '' if rest is not None else '(⚠️ application import failed, REST endpoint check skipped, CI is the backstop)'
print(f'✅ README number machine check passed: version v{version} · MCP tools {mcp_total} (ecosystem {mcp_eco})'
      f' · pages {pages} · REST endpoints {rest if rest is not None else "?"}{skip_rest}'
      f' · test claims ≤ {tests if tests is not None else "?"}{skip}')
PYEOF
