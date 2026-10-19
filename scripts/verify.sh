#!/usr/bin/env bash
# Project verification recipe.
# Usage: verify.sh [quick|full]
#   quick: format, lint, types, module boundaries, fast tests, smoke run
#   full:  quick (fast and smoke tests run once, with coverage), plus end-to-end tests,
#          coverage thresholds and all pre-commit hooks
# Prints "VERIFY <mode>: PASS" or "VERIFY <mode>: FAIL (...)" as its last line.
set -u
MODE="${1:-quick}"
if [ "$MODE" != "quick" ] && [ "$MODE" != "full" ]; then
  echo "Usage: verify.sh [quick|full]"; exit 2
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

if [ ! -f pyproject.toml ]; then
  echo "No pyproject.toml yet (task M0.1 creates it). Nothing to verify."
  echo "VERIFY $MODE: PASS (nothing to verify)"
  exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed, so verification can't run. Install it from https://docs.astral.sh/uv/"
  echo "VERIFY $MODE: FAIL (uv missing)"
  exit 1
fi

failures=()
step() {
  local name="$1"; shift
  echo "== $name: $*"
  "$@"
  local rc=$?
  if [ $rc -eq 0 ]; then
    echo "   PASS: $name"
  else
    echo "   FAIL: $name (exit $rc)"
    failures+=("$name")
  fi
}
pytest_step() {
  # pytest exits 5 when no tests match the marker; that is not a failure early in the project.
  local name="$1"; shift
  echo "== $name: uv run pytest $*"
  uv run pytest "$@"
  local rc=$?
  if [ $rc -eq 0 ]; then
    echo "   PASS: $name"
  elif [ $rc -eq 5 ]; then
    echo "   SKIP: $name (no tests collected yet)"
  else
    echo "   FAIL: $name (exit $rc)"
    failures+=("$name")
  fi
}

step "format" uv run ruff format --check .
step "lint" uv run ruff check .
if grep -q "\[tool.mypy\]" pyproject.toml; then step "types" uv run mypy; fi
if grep -q "\[tool.importlinter\]" pyproject.toml; then step "module boundaries" uv run lint-imports; fi

FAST='not e2e and not smoke and not live'
COVERAGE='not e2e and not live'  # spec 4.5: the coverage run includes the smoke tests
if [ "$MODE" = "full" ]; then
  pytest_step "fast tests with coverage" -q -m "$COVERAGE" --cov=app --cov-report=term-missing --cov-fail-under=85
  if [ -f scripts/check_critical_coverage.py ]; then step "critical coverage" uv run python scripts/check_critical_coverage.py --from-progress; fi
else
  pytest_step "fast tests" -q -m "$FAST"
  pytest_step "smoke" -q -m smoke
fi

if [ "$MODE" = "full" ]; then
  pytest_step "end-to-end" -q -m e2e
  if [ -f .pre-commit-config.yaml ]; then step "pre-commit (all files)" uv run pre-commit run --all-files; fi
  if [ -f .pre-commit-config.yaml ] && grep -q "gitleaks-history" .pre-commit-config.yaml; then
    step "secrets in git history" uv run pre-commit run --hook-stage manual gitleaks-history
  fi
fi

echo
if [ ${#failures[@]} -eq 0 ]; then
  echo "VERIFY $MODE: PASS"
  exit 0
fi
echo "VERIFY $MODE: FAIL (${failures[*]})"
exit 1
