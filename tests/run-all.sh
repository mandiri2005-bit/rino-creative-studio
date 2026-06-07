#!/usr/bin/env bash
# run-all.sh — Run all test suites
# Usage: bash tests/run-all.sh [node|python|frontend|all]

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUITE="${1:-all}"
PASS=0; FAIL=0

green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
header() { echo -e "\n\033[1;36m══════ $* ══════\033[0m"; }

run() {
  local name="$1"; shift
  header "$name"
  if "$@"; then
    green "✅ $name PASSED"
    PASS=$((PASS + 1))
  else
    red "❌ $name FAILED"
    FAIL=$((FAIL + 1))
  fi
}

# ─── Node.js helpers (node:test, no install needed) ──────────────────────────
run_node() {
  header "Node.js — helper unit tests"
  cd "$ROOT"
  node --test tests/node/helpers.test.mjs && \
  node --test tests/node/routes.test.mjs
}

# ─── Python (pytest) ─────────────────────────────────────────────────────────
run_python() {
  header "Python — FastAPI unit tests"
  cd "$ROOT"
  # Install test deps if needed
  pip install --break-system-packages -q \
    pytest pytest-asyncio httpx \
    fastapi uvicorn openai requests pydantic 2>/dev/null || true
  python -m pytest tests/python/ -v \
    --tb=short \
    --ignore=tests/python/conftest.py \
    -p no:warnings
}

# ─── Frontend (Jest) ─────────────────────────────────────────────────────────
run_frontend() {
  header "Frontend — Jest unit tests"
  cd "$ROOT/tests/frontend"
  if ! command -v npx &>/dev/null; then
    yellow "⚠️  npx not found — skipping frontend tests"
    return 0
  fi
  npm install --silent 2>/dev/null || true
  npx jest --no-coverage 2>&1 | tail -30
}

# ─── Main ─────────────────────────────────────────────────────────────────────
case "$SUITE" in
  node)     run "Node.js helpers"  run_node ;;
  python)   run "Python FastAPI"   run_python ;;
  frontend) run "Frontend Jest"    run_frontend ;;
  all)
    run "Node.js helpers"  run_node
    run "Python FastAPI"   run_python
    run "Frontend Jest"    run_frontend
    ;;
  *)
    echo "Usage: $0 [node|python|frontend|all]"
    exit 1
    ;;
esac

header "RESULTS"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
[ "$FAIL" -eq 0 ] && green "All tests passed ✅" || red "Some tests failed ❌"
exit "$FAIL"
