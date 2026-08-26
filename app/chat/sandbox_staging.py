"""Provider-agnostic sandbox file staging.

Boot-time files every provider stages into the sandbox through the
provider-supplied ``stage`` callable (``SandboxProvider.stage_file``):
the restored-conversation transcript and the agnes CLI wheel. Providers
that own their workspace delivery (``syncs_workspace = True``) still use
this layer — staging is not workspace sync.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# Restored-conversation transcript for a FRESH sandbox of a chat that already
# has history (crash respawn, post-restart resume-legacy, cross-gateway
# takeover). The manager builds it from persisted chat_messages and stages
# it before spawn completes; the runner appends it to the agent's system
# prompt at boot. Outside /work so it never lands in the user's workspace
# tree. Replaces the old replay-3-raw-user-turns-over-stdin approach, which
# dropped all assistant/tool context AND burned one LLM turn per replayed
# message.
SANDBOX_CONTEXT_RESTORE = "/tmp/agnes-context.md"

# Directory the runner pip-installs the agnes CLI wheel from at boot
# (app/chat/runner.py::_install_agnes_cli). Deliberately OUTSIDE the
# workspace (/work): the workspace is the user's bind-mounted / engine-owned
# tree, so staging a ~4 MB wheel there would pollute it on every spawn.
# /tmp is ephemeral sandbox state.
SANDBOX_WHEEL_DIR = "/tmp/agnes-cli"

# Sentinel the runner waits on before installing. The runner process starts
# (inside provider.spawn) BEFORE this staging runs, so without a barrier the
# runner would glob an empty staging dir and skip the install (the race that
# left `agnes` absent). Written right after the wheel — it guarantees the
# wheel ONLY.
SANDBOX_WHEEL_READY = f"{SANDBOX_WHEEL_DIR}/.ready"


async def stage_agnes_wheel(stage) -> str | None:
    """Stage the server's pre-built agnes CLI wheel in the sandbox so the
    runner can ``pip install`` it at boot. Returns the sandbox-side wheel path.

    ``stage`` is an ``async (path, data) -> None`` callable the *provider*
    supplies (``SandboxProvider.stage_file``) — the docker provider writes
    through the apps-runner sidecar. Provider-agnostic because the wheel is
    not workspace sync: every provider with a ``stage_file`` needs it,
    including the ones that mount the workspace themselves.

    Always writes the ``.ready`` sentinel last (even when no wheel is found) so
    the runner's bounded wait terminates promptly instead of timing out.

    The wheel is the exact artifact the server already builds at image-build
    time (``uv build --wheel`` → ``/app/dist``) and serves at ``/cli/download``.
    Reusing it — rather than baking the CLI into the sandbox image or pulling
    it from git — guarantees the in-sandbox CLI version matches the running
    server's *exactly*, so the bundled hooks (``agnes admin grant/group/user``)
    and RBAC semantics stay in lockstep. The sandbox image bakes the CLI's
    runtime deps, so the runner installs ``--no-deps`` (fast spawn).

    The wheel keeps its original PEP 427 filename
    (``agnes_the_ai_analyst-<ver>-py3-none-any.whl``): ``pip install`` parses
    the filename for name/version and rejects a renamed file
    ("not a valid wheel filename"), so it cannot be flattened to ``agnes.whl``.

    Best-effort: returns ``None`` (and logs a warning) when no wheel is present
    — e.g. a dev image that skipped ``uv build``. The agent still runs; only the
    ``agnes`` verbs (``catalog``, ``query``, ``describe``, ``snapshot``) are
    unavailable.
    """
    # Imported lazily to avoid coupling the chat package to app.api at import
    # time. ``_find_wheel`` is the single source of truth for wheel discovery
    # (it honours AGNES_CLI_DIST_DIR and the /app/dist default).
    from app.api.cli_artifacts import _find_wheel

    wheel = _find_wheel()
    if wheel is None:
        logger.warning(
            "stage_agnes_wheel: no wheel found under %s — the `agnes` CLI "
            "will be absent in the sandbox (dev image without `uv build`?)",
            os.environ.get("AGNES_CLI_DIST_DIR", "/app/dist"),
        )
        # Still signal the runner so it doesn't block on the wait.
        await stage(SANDBOX_WHEEL_READY, b"")
        return None
    dest = f"{SANDBOX_WHEEL_DIR}/{wheel.name}"
    data = wheel.read_bytes()
    await stage(dest, data)
    await stage(SANDBOX_WHEEL_READY, b"")
    logger.info("staged agnes wheel %s (%d bytes) to %s", wheel.name, len(data), dest)
    return dest
