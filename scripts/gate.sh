#!/usr/bin/env bash
# Run every check a push must pass, all at once, against a snapshot of HEAD.
#
# The checks run in a throwaway git worktree of HEAD, so editing the real
# tree while they run cannot reach them, and what is verified is exactly
# what a push would publish: commits, never uncommitted work. Commit first,
# then gate.
#
# Every check starts at once: pytest spreads over the cores, the others take
# one each. Run one after the other they left three of a Raspberry Pi's four
# cores idle. What the checks keep between runs lives in the main checkout,
# which outlives the worktree: the text read out of the fixture PDFs
# (tmp/fixture_text, see tests/__init__.py) and the two mypy caches, one per
# run since they check with different options.
#
# The interpreter is GATE_PYTHON, else .venv/bin/python in the checkout:
# any with requirements-dev.txt installed. The workflow linter runs when an
# actionlint binary sits at tmp/actionlint.
#
# Usage: scripts/gate.sh [pytest args...]
#   scripts/gate.sh                     the whole suite, as test.yml runs it
#   scripts/gate.sh tests/test_ebem.py  one file, for a quick pass
set -u

ROOT=$(git rev-parse --show-toplevel) || exit 1
cd "$ROOT" || exit 1
PYTHON=${GATE_PYTHON:-$ROOT/.venv/bin/python}
ACTIONLINT="$ROOT/tmp/actionlint"
SHA=$(git rev-parse --short HEAD)
WORKTREE="$ROOT/tmp/gate/$SHA.$$"
LOGS="$ROOT/tmp/gate/$SHA.$$.logs"

[ -x "$PYTHON" ] || { echo "no interpreter at $PYTHON; set GATE_PYTHON" >&2; exit 1; }

cleanup() {
  cd "$ROOT" || return
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1
  rm -rf "$LOGS"
}
trap cleanup EXIT INT TERM

mkdir -p "$LOGS"
git worktree add --detach --quiet "$WORKTREE" HEAD || exit 1
echo "gating $SHA in $WORKTREE"
echo

names=()
pids=()
# Start one check in the background, its output in LOGS under its position.
start() {
  local n=${#names[@]}
  names+=("$1")
  shift
  ( cd "$WORKTREE" && "$@" ) > "$LOGS/$n.log" 2>&1 &
  pids+=($!)
}

export BE_FIXTURE_TEXT_CACHE="$ROOT/tmp/fixture_text"
start "pytest" "$PYTHON" -m pytest "${@:-tests/}" -q -n auto --dist loadfile
start "ruff check" "$PYTHON" -m ruff check .
start "ruff format" "$PYTHON" -m ruff format --check .
start "mypy strict" "$PYTHON" -m mypy --cache-dir "$ROOT/.mypy_cache_strict" \
  --strict custom_components/be_gas_prices
start "mypy tests and scripts" "$PYTHON" -m mypy --cache-dir "$ROOT/.mypy_cache_all" \
  tests/ scripts/
if [ -x "$ACTIONLINT" ]; then
  # Globbed, so a workflow added later cannot slip past by not being named.
  start "actionlint" bash -c "\"$ACTIONLINT\" .github/workflows/*.yml"
else
  echo "== actionlint"
  echo "   skipped: no binary at $ACTIONLINT"
  echo
fi

failed=""
# pytest started first and is reported last, after the quick checks.
order=("${!names[@]}")
order=("${order[@]:1}" 0)
for n in "${order[@]}"; do
  wait "${pids[$n]}"
  rc=$?
  echo "== ${names[$n]}"
  cat "$LOGS/$n.log"
  if [ "$rc" -eq 0 ]; then
    echo "   ok"
  else
    echo "   FAILED"
    failed="$failed ${names[$n]// /-}"
  fi
  echo
done
if [ -n "$failed" ]; then
  echo "GATE FAILED:$failed"
  exit 1
fi
echo "gate passed on $SHA"
