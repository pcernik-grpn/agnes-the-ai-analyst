#!/usr/bin/env bash
# Serve Agnes locally and FOLLOW a remote branch — one command, then nothing.
#
# Why this exists: when the code is being written somewhere else (a Claude Code
# web session, a teammate, a CI sandbox), that machine cannot serve a page to
# your browser — there is no inbound path to it. The workable direction is the
# other one: your machine pulls. This starts the dev server and a background
# fetcher, so a push on the followed branch appears in your browser within
# seconds without you touching a terminal.
#
# Nothing here is magic about reloading — the pieces already existed:
#   - uvicorn --reload    restarts on .py changes
#   - Jinja auto_reload   re-reads templates per request
#   - _static_url ?v=mtime  busts the browser cache for CSS/JS on pull
# All this script adds is the `git pull` that moves the files.
#
# Usage:
#   ./scripts/dev/follow-branch.sh                       # follow the current branch on :9000
#   ./scripts/dev/follow-branch.sh --branch some/branch  # follow another branch (checks it out)
#   ./scripts/dev/follow-branch.sh --chat                # also run the scripted chat engine
#   PORT=8765 ./scripts/dev/follow-branch.sh             # different port
#
# Local edits are never clobbered: the pull is `--ff-only`, so if you have your
# own commits or a conflicting change the script says so and keeps serving what
# you have.
set -euo pipefail

cd "$(dirname "$0")/../.."

PORT="${PORT:-9000}"
HOST="${HOST:-127.0.0.1}"
INTERVAL="${INTERVAL:-10}"
BRANCH=""
WITH_CHAT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="${2:-}"; shift 2 ;;
        --chat)   WITH_CHAT=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ ! -x .venv/bin/uvicorn ]; then
    echo "error: .venv/bin/uvicorn missing — run: uv pip install '.[dev,server]'" >&2
    exit 1
fi

if [ -n "$BRANCH" ]; then
    git fetch origin "$BRANCH"
    git checkout "$BRANCH"
else
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
fi
echo "following origin/${BRANCH} (checking every ${INTERVAL}s)"
echo "at $(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"

PIDS=()
cleanup() { for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

# Background follower. `--ff-only` is the safety: your own work stops the pull
# with a message rather than being rewritten under a running server.
(
    while sleep "$INTERVAL"; do
        git fetch --quiet origin "$BRANCH" 2>/dev/null || continue
        local_sha="$(git rev-parse HEAD)"
        remote_sha="$(git rev-parse "origin/${BRANCH}" 2>/dev/null || echo "$local_sha")"
        [ "$local_sha" = "$remote_sha" ] && continue
        if git merge --ff-only "origin/${BRANCH}" --quiet 2>/dev/null; then
            echo ""
            echo ">>> pulled $(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"
            echo ">>> reload your browser tab"
        else
            echo ""
            echo ">>> origin/${BRANCH} moved, but a fast-forward is not possible."
            echo ">>> You have local commits or uncommitted changes. Serving your version;"
            echo ">>> resolve with: git stash && git merge --ff-only origin/${BRANCH}"
        fi
    done
) &
PIDS+=($!)

export LOCAL_DEV_MODE=1          # auto-login as dev@localhost, no OAuth
export TESTING=1                 # skip the JWT / ANTHROPIC_API_KEY startup gates
export AGNES_CHAT_ENABLED=true
export DEBUG=0                   # LOCAL_DEV_MODE would otherwise mount the
                                 # debug toolbar, which covers the right-hand
                                 # side of every page

if [ "$WITH_CHAT" -eq 1 ]; then
    # The scripted engine (services/kai_engine_stub) speaks the real SSE
    # contract with no model and no key, so a chat turn travels the whole
    # production path. Replies are canned — type `interleaved`, `table`,
    # `markdown`, `fail`, `approval` to pick a shape.
    export KAI_HOST_JWT_SECRET=local-dev-secret
    KAI_STUB_HOST=127.0.0.1 .venv/bin/python -m services.kai_engine_stub >/tmp/agnes-kai-stub.log 2>&1 &
    PIDS+=($!)
    export AGNES_CHAT_PROVIDER=kai-agent
    export AGNES_CHAT_KAI_AGENT_URL=http://127.0.0.1:3000
    echo "scripted chat engine on :3000 (log: /tmp/agnes-kai-stub.log)"
fi

echo ""
echo "  →  http://${HOST}:${PORT}/agents?new=1"
echo ""
exec .venv/bin/uvicorn app.main:app --reload --host "${HOST}" --port "${PORT}"
