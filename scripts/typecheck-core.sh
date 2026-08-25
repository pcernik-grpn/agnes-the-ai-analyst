#!/usr/bin/env bash
# Blocking type check over the "typed core" — the modules that must stay
# mypy-clean so `typing.assert_never` actually enforces something.
#
# Why a curated list rather than the whole tree: the repo-wide mypy step in
# ci.yml is advisory (`|| true` + continue-on-error) because the tree has
# never been type-clean, and making it blocking would mean fixing years of
# backlog first. But an advisory type checker cannot enforce exhaustiveness:
# a dispatch that loses an arm produces an error nobody reads. This list is
# the subset where the check DOES block, so exhaustive dispatch over a
# declared enum is a real guarantee instead of a convention.
#
# The gate rides the test suite (tests/test_exhaustiveness_gate.py) so it
# lands on the already-required `test` check without a branch-protection
# change. Run it directly while working:
#
#     scripts/typecheck-core.sh
#
# Adding a module here is the ratchet: it must be clean when you add it, and
# it stays clean afterwards.

set -euo pipefail

cd "$(dirname "$0")/.."

# --- the typed core --------------------------------------------------------
# Keep this list the single source of truth; the test reads it from here.
CORE_MODULES=(
    connectors/keboola/storage_api.py
)

# Locate mypy: prefer the venv, then PATH, then uv (mirrors
# scripts/post-edit-quality.sh so both agree on which mypy runs).
if [ -x ".venv/bin/mypy" ]; then
    MYPY=(.venv/bin/mypy)
elif command -v mypy >/dev/null 2>&1; then
    MYPY=(mypy)
elif command -v uv >/dev/null 2>&1; then
    MYPY=(uv run --quiet mypy)
else
    echo "typecheck-core: mypy not found. Install the dev extra:" >&2
    echo "    uv pip install '.[dev]'" >&2
    exit 127
fi

if [ "${1:-}" = "--list" ]; then
    printf '%s\n' "${CORE_MODULES[@]}"
    exit 0
fi

echo "typecheck-core: checking ${#CORE_MODULES[@]} module(s)"
"${MYPY[@]}" "${CORE_MODULES[@]}" --ignore-missing-imports
