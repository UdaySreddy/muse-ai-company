#!/usr/bin/env bash
# Red-line invariant checks — turn the red lines maintained by memory into executable checks.
# Each check corresponds to a real accident that has been stepped on (docs/knowledge-layer-design.md P0).
# Usage: bash scripts/check_invariants.sh   (run from the repo root; works for CI and local)
# Exit code: 0=all pass (warnings do not block), 1=violations found

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
FAIL=0
warn() { printf '⚠️  [%s] %s\n' "$1" "$2"; }
fail() { printf '❌ [%s] %s\n' "$1" "$2"; FAIL=1; }
ok()   { printf '✅ [%s] %s\n' "$1" "$2"; }

# ── I1: hook dual copies in sync (accident: 715acc8 the cross-project guard existed only in the never-distributed src copy) ──
# Bidirectional set comparison + an explicit allowlist: the old version only iterated from the plugin side and skipped missing files with `[ -f "$twin" ]`,
# so both drift classes — "the twin copy does not exist" and "a file added only on the src side" — were silently missed.
I1_OUT="$(python3 - <<'EOF'
import filecmp, os, sys

    # Files allowed to exist on one side only (each with a clear reason; additions must be registered explicitly here)
PLUGIN_ONLY = {
    "auto_install.py",  # plugin self-heal entry point: installs the chain from outside the chain; putting it in the in-package copy would recurse instead
}
SRC_ONLY = {
    "__init__.py",      # package declaration, not a CC hook
}

def pys(d):
    return {f for f in os.listdir(d) if f.endswith(".py")}

plugin, src = "plugin/hooks", "src/aiteam/hooks"
p, s = pys(plugin), pys(src)
problems = []
for name in sorted((p - s) - PLUGIN_ONLY):
    problems.append(f"{name}: exists only in plugin/hooks, missing the src/aiteam/hooks twin copy")
for name in sorted((s - p) - SRC_ONLY):
    problems.append(f"{name}: exists only in src/aiteam/hooks (never distributed, i.e. dead code)")
for name in sorted(p & s):
    if not filecmp.cmp(f"{plugin}/{name}", f"{src}/{name}", shallow=False):
        problems.append(f"{name}: dual-copy content drift")
for name in sorted(PLUGIN_ONLY & s):
    problems.append(f"{name}: allowlisted as plugin-only, yet appears in src/aiteam/hooks")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print(f"{len(p & s)} pairs of twin copies byte-for-byte identical (allowlist exemptions: {len(PLUGIN_ONLY | SRC_ONLY)})")
EOF
)" && I1_OK=1 || I1_OK=0
if [ "$I1_OK" -eq 1 ]; then
  ok I1 "hook dual copies in sync (${I1_OUT})"
else
  fail I1 "hook copy sets do not match — plugin/hooks and src/aiteam/hooks must have the same names and content:
$I1_OUT"
fi

# ── I1b: ban on leftover send_event copies (M27: the dead copies in root hooks/ and .claude/hooks/ once drifted by up to 79 lines) ──
I1B_BAD=""
[ -f hooks/send_event.py ] && I1B_BAD="$I1B_BAD hooks/send_event.py"
[ -f .claude/hooks/send_event.py ] && I1B_BAD="$I1B_BAD .claude/hooks/send_event.py"
if [ -n "$I1B_BAD" ]; then
  fail I1b "leftover send_event copies have risen from the dead:$I1B_BAD — the only source of truth is plugin/hooks/send_event.py (the src/aiteam/hooks mirror is guaranteed by I1)"
else
  ok I1b "no leftover send_event copies"
fi

# ── I2: version lockstep in five places (accident: before 7be8cd8, 9 places had diverged 0.0.0–1.6.1) ──
I2_OUT="$(python3 - <<'EOF'
import json, re, sys
vals = {}
vals['pyproject'] = re.search(r'^version = "([^"]+)"', open('pyproject.toml').read(), re.M).group(1)
vals['__init__'] = re.search(r'__version__ = "([^"]+)"', open('src/aiteam/__init__.py').read()).group(1)
vals['plugin.json'] = json.load(open('plugin/.claude-plugin/plugin.json'))['version']
for tag, p in (('marketplace(plugin)', 'plugin/.claude-plugin/marketplace.json'),
               ('marketplace(root)', '.claude-plugin/marketplace.json')):
    d = json.load(open(p))
    plugins = d.get('plugins') or []
    vals[tag] = plugins[0].get('version') if plugins else d.get('version')
uniq = set(vals.values())
if len(uniq) != 1:
    print('MISMATCH ' + ', '.join(f'{k}={v}' for k, v in vals.items()))
    sys.exit(1)
print('VERSION ' + uniq.pop())
EOF
)" || true
case "$I2_OUT" in
  VERSION*) ok I2 "version consistent in five places (${I2_OUT#VERSION })" ;;
  *)        fail I2 "version number drift: ${I2_OUT#MISMATCH }" ;;
esac

# ── I3: dual dist bundles consistent (accident: on release day plugin/dashboard-dist lagged half a day and was only caught by a human) ──
if [ -d dashboard/dist/assets ] && [ -d plugin/dashboard-dist/assets ]; then
  A="$(ls dashboard/dist/assets/*.js 2>/dev/null | xargs -n1 basename 2>/dev/null | sort)"
  B="$(ls plugin/dashboard-dist/assets/*.js 2>/dev/null | xargs -n1 basename 2>/dev/null | sort)"
  if [ "$A" != "$B" ]; then
    fail I3 "the JS bundles in dashboard/dist and plugin/dashboard-dist are inconsistent — re-run cp -R dashboard/dist plugin/dashboard-dist"
  else
    ok I3 "dual dist bundles consistent"
  fi
else
  warn I3 "dist directory missing (ignorable in an unbuilt environment)"
fi

# ── I4: dist not behind the frontend source (warning level — a src change may not affect the artifact, but lagging more than 1 day is worth a look) ──
I4_OUT="$(python3 - <<'EOF'
import os, sys
def newest(root, exts):
    latest = 0.0
    for dp, _dn, fns in os.walk(root):
        if 'node_modules' in dp or '/dist' in dp:
            continue
        for fn in fns:
            if fn.endswith(exts):
                try: latest = max(latest, os.path.getmtime(os.path.join(dp, fn)))
                except OSError: pass
    return latest
src = newest('dashboard/src', ('.ts', '.tsx', '.css'))
try:
    dist = max(os.path.getmtime(os.path.join('dashboard/dist/assets', f))
               for f in os.listdir('dashboard/dist/assets'))
except Exception:
    sys.exit(0)
lag_h = (src - dist) / 3600
print(f'{lag_h:.1f}')
EOF
)" || I4_OUT="0"
if python3 -c "import sys; sys.exit(0 if float('${I4_OUT:-0}') > 24 else 1)" 2>/dev/null; then
  warn I4 "dashboard/dist is ${I4_OUT} hours behind the frontend source — if the change involves the UI, please rebuild"
else
  ok I4 "dist freshness normal"
fi

# ── I5: venv ban (hard-luck history: ae57984..e2d0fbb, the four process types share dependencies and venv isolation has been rejected) ──
I5_HITS="$(grep -rnE '(-m venv|virtualenv|venv\.create|activate_this|\.venv/bin)' src/aiteam --include='*.py' 2>/dev/null | grep -v '^\s*#' | grep -vE '#.*(venv|virtualenv)' || true)"
if [ -n "$I5_HITS" ]; then
  fail I5 "venv creation/activation code appeared in src/ (red line):
$I5_HITS"
else
  ok I5 "no venv violations"
fi

# ── I6: README number machine check (accident: a 2026-07 audit found three rotten numbers — 18 pages / 631+ tests / 30+ ecosystem tools — all from manual maintenance) ──
I6_OUT="$(bash scripts/check_readme_numbers.sh 2>&1)"
if [ $? -eq 0 ]; then
  ok I6 "README numbers match measured values (version / MCP tools / pages / REST endpoints / tests)"
else
  fail I6 "README number drift — the README does not match the measured code values:
$I6_OUT"
fi

# ── I7: ruff lint gate (accident: on 2026-07-21/22, agent-delivered code twice failed ruff and turned the public repo CI Lint red; a human acceptance checklist is unreliable, a machine must guard it) ──
if command -v ruff >/dev/null 2>&1; then
  I7_OUT="$(ruff check --quiet . 2>&1 || true)"
  if [ -z "$I7_OUT" ]; then
    ok I7 "ruff lint all green"
  else
    fail I7 "ruff lint failed (the public repo CI will go red):
$(echo "$I7_OUT" | head -20)"
  fi
else
  ok I7 "ruff not installed, skipping (CI still guards)"
fi

# ── I8: hook registration surface unified (accident: the 2026-07-27 audit — source install and plugin install produced two different OSes,
#        the source path was missing 4 whole events and the PreToolUse matcher did not match either; README hook counts were maintained by hand) ──
I8_OUT="$(python3 scripts/check_hook_surface.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I8 "hook registration surface unified (install.py ↔ hooks.json ↔ README)"
else
  fail I8 "hook registration surface drift — the two install paths would install different OSes:
$I8_OUT"
fi

# ── I9: MCP tool parameter descriptions (accident: the 2026-07-28 audit — 10 parameters had no description in the live schema,
#        all from docstrings writing multiple parameters on one line like `limit / offset: Pagination.`; the source looked
#        complete, but the parser could not split them. In the tool-search era, a parameter without a description is one that can neither be found nor guessed right) ──
I9_OUT="$(python3 scripts/check_tool_param_descriptions.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I9 "MCP tool surface descriptions complete (${I9_OUT#✅ MCP tool surface descriptions complete: })"
else
  fail I9 "MCP tools/parameters missing descriptions — parameters without descriptions cannot be found in tool search:
$I9_OUT"
fi

# ── I10: table sets consistent (accident: 2026-07-28 D0 forensics — the Mac database was built entirely new on 07-06, the Win-side content never migrated with it,
#        and no machine check compared "the tables the code knows" with "the tables on disk"; a silently lost table is only discovered when someone goes to read it) ──
I10_OUT="$(python3 scripts/check_schema_tables.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I10 "table sets consistent (${I10_OUT##*✅ Table sets consistent: })"
  echo "$I10_OUT" | grep '^⚠️' || true
else
  fail I10 "tables declared by the ORM are missing from the database — table creation failed or a table was lost in a machine move:
$I10_OUT"
fi

# ── I11: clock convention unified (accident: 2026-07-28 — the core domain wrote local wall clock, the ecosystem domain wrote UTC,
#        SQLite storage silently strips the offset, rows of the two conventions look exactly alike, cross-domain comparisons are off by 8 hours and
#        raise no exception. It lived for several months before one audit caught three places. The dual wall clocks were decided by nobody; one module
#        after another wrote them casually — without a machine check, the same thing will happen again and again nobody will see it) ──
I11_OUT="$(python3 scripts/check_clock_convention.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I11 "clock convention unified (${I11_OUT#✅ Clock convention unified: })"
else
  fail I11 "a second clock appeared in the database — this kind of error raises no exception, it only quietly shifts by one timezone:
$I11_OUT"
fi

# ── I12: usage presentation-surface dimension allowlist (the remark at project founding, "186.2 million tokens are already in the database", had the wrong metric —
#        workflow_agents.tokens is the last-turn context watermark, measured 5-25x apart from accumulated usage. The next step after mixing metrics
#        is mixing dimensions: folding tokens into money, into person-hours, into "equivalent to how many person-days". P1 fixes it in stone:
#        express only in tokens, using an allowlist rather than a banned-word list — a blacklist that misses one spelling is breached) ──
I12_OUT="$(python3 scripts/check_usage_dimensions.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I12 "usage dimension allowlist (${I12_OUT#✅ Dimension allowlist passed: })"
else
  fail I12 "a dimension outside the allowlist appeared on a usage presentation surface — only the four token layers / counts / duration in milliseconds / percentages are allowed:
$I12_OUT"
fi

# ── I13: coverage same-screen red line (the presentation-surface form of discipline 1, no-data≠zero: a token value detached from its metric
#        and denominator is meaningless. The measured coverage of sub-agent usage is 11/2450 = 0.4%; reporting a lone
#        total at that point is a part impersonating the whole. Page annotation is a soft constraint, so this is pinned at the type layer — the metric must
#        be written next to the field, and aggregate surfaces must return at the same level as the denominator) ──
I13_OUT="$(python3 scripts/check_usage_coverage.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I13 "coverage same-screen red line (${I13_OUT##*✅ Coverage same-screen red line passed: })"
  echo "$I13_OUT" | grep '^⚠️' || true
else
  fail I13 "a token value appeared detached from metric/denominator — unattributed not presented is a part impersonating the whole:
$I13_OUT"
fi

# ── I14: the three hard constraints on historical backfill (design §6.4). The backfill script rewrites more than two thousand rows of production data in one pass, and the first of them
#        is irrecoverable if wrong: workflow_agents.tokens is the ctx_last metric, the backfill output is usage_sum,
#        measured 5-25x apart — writing it in permanently solidifies the mixed metrics into history, indistinguishable afterwards (R3). So this
#        check is behavioral: really build a temporary database, really run --apply once, really compare per-row sha256 fingerprints of the forbidden column;
#        a text scan cannot stop dynamically assembled SQL, but "did that column change after the run" can ──
I14_OUT="$(python3 scripts/check_backfill_safety.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I14 "backfill red line (${I14_OUT#✅ Backfill red line passed: })"
else
  fail I14 "historical backfill hard constraints breached — the ctx_last column was polluted / coverage windows merged / idempotency or dry-run failed:
$I14_OUT"
fi

echo
if [ "$FAIL" -eq 1 ]; then
  echo "Conclusion: ❌ red-line violations exist; committing/releasing is forbidden. Fix them and re-run bash scripts/check_invariants.sh"
  exit 1
fi
echo "Conclusion: ✅ all invariants passed"
