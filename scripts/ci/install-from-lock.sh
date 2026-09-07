#!/usr/bin/env sh
# Install the project's dependencies from uv.lock — the SAME pinned set for the
# Docker image and every CI job — instead of re-resolving pyproject.toml's
# ranges at build time.
#
#   scripts/ci/install-from-lock.sh [--no-dev] EXTRA [EXTRA...]
#
# `--no-dev` is uv's flag: it drops the `[tool.uv] dev-dependencies` GROUP
# (pytest, ruff, …) from the export — the image passes it. It says nothing
# about the `dev` optional-dependency EXTRA, which, like `server` or
# `extraction`, is requested positionally: `--no-dev dev server` would still
# install the `dev` extra. CI asks for `dev server extraction` with no flag and
# gets both the group and the extra (they mirror each other on purpose).
#
# `uv export --frozen` turns the lock into a requirements file for the given
# extras (hashes included, so `uv pip install` verifies every artifact), then
# the project itself goes on top with `--no-deps`. `--frozen` never rewrites
# the lock: a lock that is behind pyproject.toml is caught by CI's blocking
# `lock-check` job (`uv lock --check`), never silently re-resolved here.
#
# Extras are positional and space-separated; an empty argument is skipped, so
# the Dockerfile can splice its comma-list ARG straight in via `tr ',' ' '`.
#
# Target: the system interpreter by default (the image, GitHub-hosted runners).
# Set INSTALL_PYTHON=<interpreter> to install into that environment instead —
# the e2e jobs' explicit `.venv`, say — so a venv job pins the same set rather
# than falling back to `pip install -e` and re-resolving the ranges.
#
# The project's own build (the last step) runs in an isolated PEP 517 env whose
# backend is NOT in uv.lock; `[build-system] requires` pins `hatchling` exactly
# so that step is reproducible too.
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
if [ -n "${INSTALL_PYTHON:-}" ]; then
    TARGET="--python $INSTALL_PYTHON"
else
    TARGET="--system"
fi
# shellcheck disable=SC2086
uv pip install $TARGET --no-cache -r "$REQ"
# shellcheck disable=SC2086
uv pip install $TARGET --no-cache --no-deps .
