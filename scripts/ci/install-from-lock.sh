#!/usr/bin/env sh
# Install the project's dependencies from uv.lock — the SAME pinned set for the
# Docker image and every CI job — instead of re-resolving pyproject.toml's
# ranges at build time.
#
#   scripts/ci/install-from-lock.sh [--no-dev] EXTRA [EXTRA...]
#
# `uv export --frozen` turns the lock into a requirements file for the given
# extras (hashes included, so `uv pip install` verifies every artifact), then
# the project itself goes on top with `--no-deps`. `--frozen` never rewrites
# the lock: a lock that is behind pyproject.toml is caught by CI's blocking
# `lock-check` job (`uv lock --check`), never silently re-resolved here.
#
# Extras are positional and space-separated; an empty argument is skipped, so
# the Dockerfile can splice its comma-list ARG straight in via `tr ',' ' '`.
set -eu

NO_DEV=""
if [ "${1:-}" = "--no-dev" ]; then
    NO_DEV="--no-dev"
    shift
fi

EXTRA_FLAGS=""
for extra in "$@"; do
    [ -n "$extra" ] && EXTRA_FLAGS="$EXTRA_FLAGS --extra $extra"
done

REQ="$(mktemp)"
trap 'rm -f "$REQ"' EXIT

# shellcheck disable=SC2086  # NO_DEV / EXTRA_FLAGS are deliberately word-split
uv export --frozen --no-emit-project $NO_DEV $EXTRA_FLAGS --format requirements-txt --output-file "$REQ"
uv pip install --system --no-cache -r "$REQ"
uv pip install --system --no-cache --no-deps .
