#!/usr/bin/env bash
# Post-edit quality gate: ruff fix/format (blocking) + mypy (advisory) on the
# single Python file just edited. Invoked by Claude Code's PostToolUse hook
# (.claude/settings.json) after Edit/Write/MultiEdit. Reads the tool-input JSON
# from stdin (Claude Code hook contract).
#
# Exit 0 = continue. Non-zero = Claude sees the failure as a tool result and must
# address it before its next action. mypy is advisory and never blocks (mirrors
# CI's continue-on-error posture). Missing tools are skipped, not errors.
#
# Manual debug:
#   echo '{"tool_input":{"file_path":"src/db.py"}}' | scripts/post-edit-quality.sh
set -euo pipefail

PAYLOAD="$(cat)"

FILE_PATH="$(printf '%s' "$PAYLOAD" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' \
    2>/dev/null || true)"

[ -z "$FILE_PATH" ] && exit 0
case "$FILE_PATH" in
    *.py) ;;
    *) exit 0 ;;
esac
[ -f "$FILE_PATH" ] || exit 0

# Run from the repo root when the file is in a git repo (so pyproject.toml config
# applies); otherwise fall back to the file's directory.
REPO_ROOT="$(git -C "$(dirname "$FILE_PATH")" rev-parse --show-toplevel 2>/dev/null || true)"
cd "${REPO_ROOT:-$(dirname "$FILE_PATH")}"

# Locate ruff: prefer the venv, then PATH, then uv.
if [ -x ".venv/bin/ruff" ]; then
    RUFF=(.venv/bin/ruff)
elif command -v ruff >/dev/null 2>&1; then
    RUFF=(ruff)
elif command -v uv >/dev/null 2>&1; then
    # --frozen: run against the lock as committed, never rewrite it (a lock
    # behind pyproject.toml is CI's `lock-check` finding, not a hook side effect).
    RUFF=(uv run --frozen --quiet ruff)
else
    RUFF=()
fi

FAILED=0

if [ "${#RUFF[@]}" -gt 0 ]; then
    if ! "${RUFF[@]}" check --fix --quiet "$FILE_PATH"; then
        echo "post-edit: ruff found unresolved issues in $FILE_PATH" >&2
        FAILED=1
    fi
    "${RUFF[@]}" format --quiet "$FILE_PATH" || true
else
    echo "post-edit: ruff not found; skipping lint/format" >&2
fi

# mypy: advisory only. Never blocks. Skipped if not installed.
if [ -x ".venv/bin/mypy" ]; then
    MYPY=(.venv/bin/mypy)
elif command -v mypy >/dev/null 2>&1; then
    MYPY=(mypy)
else
    MYPY=()
fi

if [ "${#MYPY[@]}" -gt 0 ]; then
    MYPY_OUTPUT="$("${MYPY[@]}" --ignore-missing-imports --no-error-summary "$FILE_PATH" 2>&1 || true)"
    if [ -n "$MYPY_OUTPUT" ]; then
        printf '%s\n' "$MYPY_OUTPUT" | head -10 >&2
    fi
fi

# Exit 2 on a ruff failure: per the Claude Code hooks contract, a PostToolUse
# exit 2 surfaces stderr to Claude (model-visible) so it sees and fixes the
# issue. The tool already ran, so this is feedback, not a block. exit 0 = clean.
[ "$FAILED" -ne 0 ] && exit 2
exit 0
