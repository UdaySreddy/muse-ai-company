#!/usr/bin/env bash
# Local preflight — before pushing, run all the gates CI will run in one go, moving "only finding it red after committing" earlier, to local.
#
# Usage:
#   bash scripts/preflight.sh          # Full (ruff + eslint + unit tests + machine checks)
#   bash scripts/preflight.sh --fast   # Skip unit tests, only run lint + machine checks (seconds)
#
# Install as a git pre-push hook (recommended):
#   ln -sf ../../scripts/preflight.sh .git/hooks/pre-push
# To skip temporarily: git push --no-verify
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

FAIL=0
run() {
  local name="$1"; shift
  printf '\033[1m▶ %s\033[0m\n' "$name"
  if "$@"; then
    printf '\033[32m  ✓ %s\033[0m\n\n' "$name"
  else
    printf '\033[31m  ✗ %s failed\033[0m\n\n' "$name"
    FAIL=1
  fi
}

# 1. ruff (aligned with CI: ruff check src/ tests/) — skipped if not installed locally (CI is the backstop),
# a missing tool ≠ a code problem and should not block a push (verified false block on 2026-07-10)
if command -v ruff >/dev/null 2>&1; then
  run "ruff (src/ tests/)" ruff check src/ tests/
elif python3 -m ruff --version >/dev/null 2>&1; then
  run "ruff (src/ tests/)" python3 -m ruff check src/ tests/
else
  printf '\033[33m⏭  ruff not installed, skipping (CI will run it)\033[0m\n\n'
fi

# 2. eslint (aligned with CI: dashboard npm run lint) — skipped if dependencies are not installed, same rule as ruff above:
# a missing tool ≠ a code problem. dashboard/node_modules is gitignored, so every newly created
# git worktree necessarily lacks it, and this repo requires parallel sessions to isolate with worktrees — without this check,
# preflight in a worktree would reliably false-red once (verified in the 2026-07-30 release preflight).
if [[ -x dashboard/node_modules/.bin/eslint ]]; then
  run "eslint (dashboard)" bash -c 'cd dashboard && npm run lint'
else
  printf '\033[33m⏭  dashboard dependencies not installed, skipping eslint (cd dashboard && npm ci; CI will run it)\033[0m\n\n'
fi

# 3. Red-line invariant machine checks (hook dual copies / version lockstep / dual dist / dist freshness / venv)
run "red-line machine checks" bash scripts/check_invariants.sh

# 4. Unit tests (aligned with CI: pytest tests/unit/) — skipped with --fast
if [[ "$FAST" == "0" ]]; then
  run "unit tests (tests/unit/)" python3 -m pytest tests/unit/ -q
else
  printf '\033[33m⏭  unit tests skipped (--fast)\033[0m\n\n'
fi

if [[ "$FAIL" == "1" ]]; then
  printf '\033[31m\033[1m✗ Preflight failed — fix before pushing (or force-push with git push --no-verify)\033[0m\n'
  exit 1
fi
printf '\033[32m\033[1m✓ Preflight all passed — OK to push\033[0m\n'
