#!/bin/bash
# Build the chat sandbox image an instance running `chat.provider: docker`
# spawns its sessions from.
#
# The image is operator-built by design — the release pipeline does not publish
# it (see app/initial_workspace_default/docker-sandbox/README.md). Its build
# context ships INSIDE the app image, so extracting it from the very image this
# VM runs keeps the sandbox and the server on one release rather than on
# whatever a curl from a moving branch would hand over.
#
# Idempotent. The built image carries the context's own hash as a label, so a
# second call is a no-op and an app upgrade that actually changed the sandbox
# Dockerfile rebuilds exactly once. Callers run under `set -e`: invoke as
# `agnes-chat-sandbox-image.sh "$IMAGE" || echo "WARN: ..."` — a failed build
# must not abort a boot, the app's own chat gate already refuses (loudly) when
# the image is missing.
#
# Usage: agnes-chat-sandbox-image.sh <app-image-ref> [sandbox-tag]
set -uo pipefail

APP_IMAGE="${1:-}"
SANDBOX_TAG="${2:-agnes-chat-sandbox:latest}"
# Path of the build context inside the app image (Dockerfile WORKDIR /app).
CTX_PATH="/app/app/initial_workspace_default/docker-sandbox"
# Not the image's own `agnes.chat-sandbox.contract` label: that one is bumped
# by hand when the filesystem contract changes, so it cannot see an edit that
# only adds a dependency. The context hash sees every edit.
LABEL="agnes.chat-sandbox.source"

if [ -z "$APP_IMAGE" ]; then
    echo "usage: agnes-chat-sandbox-image.sh <app-image-ref> [sandbox-tag]" >&2
    exit 2
fi

TMP_CTX=$(mktemp -d)
EXTRACT_CID=""
cleanup() {
    rm -rf "$TMP_CTX"
    [ -n "$EXTRACT_CID" ] && docker rm -f "$EXTRACT_CID" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

EXTRACT_CID=$(docker create "$APP_IMAGE" true 2>/dev/null) || {
    echo "ERROR: cannot create a container from $APP_IMAGE to extract the chat sandbox context" >&2
    exit 1
}
if ! docker cp "$EXTRACT_CID:$CTX_PATH/." "$TMP_CTX/" 2>/dev/null || [ ! -f "$TMP_CTX/Dockerfile" ]; then
    echo "ERROR: $APP_IMAGE carries no chat-sandbox build context at $CTX_PATH" >&2
    echo "       (AGNES_TAG predating the docker chat provider?) — chat will refuse to start" >&2
    exit 1
fi

WANT=$(sha256sum "$TMP_CTX/Dockerfile" | awk '{print $1}')
HAVE=$(docker image inspect -f "{{ index .Config.Labels \"$LABEL\" }}" "$SANDBOX_TAG" 2>/dev/null) || HAVE=""
if [ "$WANT" = "$HAVE" ]; then
    echo "chat sandbox image $SANDBOX_TAG is current ($WANT)"
    exit 0
fi

echo "building chat sandbox image $SANDBOX_TAG from $APP_IMAGE ($CTX_PATH)"
if ! docker build --label "$LABEL=$WANT" -t "$SANDBOX_TAG" "$TMP_CTX"; then
    echo "ERROR: chat sandbox image build failed — chat.provider=docker will refuse to start" >&2
    exit 1
fi
echo "chat sandbox image $SANDBOX_TAG built ($WANT)"
