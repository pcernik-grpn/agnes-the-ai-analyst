"""In-subprocess entrypoint. Runs claude-agent-sdk inside the chat sandbox.

Stdin: JSON lines, one per frame. Inbound types: user_msg, cancel,
       approval_decision (resolves a pending ApprovalGate request),
       question_answer (resolves a pending QuestionGate request),
       ticket_push (routed to the in-sandbox relay, never enqueued).
Stdout: JSON lines. Outbound types: runner_ready, token, tool_call,
        tool_result, assistant_message, error, done,
        approval_request / approval_resolved (ApprovalGate round-trip),
        question_request / question_resolved (QuestionGate round-trip).

Env (set by ChatManager via the sandbox provider's spawn env):
- AGNES_SESSION_ID, AGNES_USER_EMAIL, AGNES_SERVER, AGNES_TOKEN
- AGNES_DAILY_BUDGET_USD, AGNES_PER_TOOL_CALL_SECONDS

Before any CLI/MCP subprocess spawn, ``_start_relay`` starts an in-sandbox
loopback relay (``app/chat/relay.py``) and rewrites ``AGNES_SERVER``/
``ANTHROPIC_BASE_URL``/``ANTHROPIC_API_KEY`` in this process's env to point at
it with a dummy key — the relay is the only thing that ever holds a real
credential, fed in-memory ``ticket_push`` frames over stdin.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import subprocess
import sys
import uuid
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # names for annotations only — no runtime import (see below)
    from app.chat.relay import Relay

# NOTE: `app.chat.relay` is intentionally NOT imported at module level. This
# file runs as a standalone script (`python3 /work/runner.py`) inside the
# sandbox, where the `app` package does not exist until `_install_agnes_cli()`
# pip-installs the uploaded wheel. A module-level `from app.chat.relay import
# Relay` crashed the runner at interpreter startup with `ModuleNotFoundError:
# No module named 'app'` — before the install ever ran — taking chat down
# end-to-end. `_start_relay()` imports it lazily, after the install. The
# forward-ref annotation below is a string (PEP 563 / `from __future__ import
# annotations`) so it needs no import at load time.

# Module-level in-sandbox relay. Populated by ``_start_relay`` in ``amain()``
# before any CLI/MCP subprocess spawn, and fed fresh tickets pushed by the
# manager over stdin (see ``_dispatch_frame``). Stays ``None`` in fake-agent
# test mode (AGNES_RUNNER_FAKE_AGENT=1), where there is no real CLI/MCP
# subprocess to broker credentials for.
_relay: "Relay | None" = None

# Directory the agnes CLI wheel is staged in by ChatManager at spawn
# (sandbox_staging.stage_agnes_wheel keeps the wheel's PEP 427 filename).
# Module-level so tests can point it at a temp dir.
_SANDBOX_WHEEL_DIR = "/tmp/agnes-cli"
# ``.ready`` sentinel the manager writes after staging the wheel. The runner
# process starts BEFORE that upload completes (provider.spawn launches it),
# so we wait for the sentinel before installing — otherwise we'd glob an empty
# dir and skip the install. Bounded; on timeout we proceed best-effort.
# Module-level so tests can zero the wait.
_WHEEL_WAIT_SECONDS = 60

# Bounded wait for the manager's workspace-upload sentinel (path arrives via
# AGNES_WORKSPACE_SYNC_SENTINEL; empty/unset → no wait, e.g. providers that
# mount the workspace themselves). The wheel sentinel above only guarantees
# the CLI wheel — the workspace tree lands separately (and slower), and the
# agent CLI reads CLAUDE.md/.claude from /work at startup, so the CLI spawn
# must gate on this one. Generous bound: a workspace near the 100 MB cap can
# take a while; on timeout we proceed best-effort (agent on a possibly
# incomplete workspace beats no agent). Module-level so tests can zero it.
_WORKSPACE_WAIT_SECONDS = 180

# Restored-conversation transcript uploaded by the manager when this runner
# is a FRESH sandbox for a chat that already has history (crash respawn,
# post-restart spawn, cross-gateway takeover). Appended to the agent's
# system prompt at boot so the conversation stays coherent — including the
# assistant's own earlier answers. Mirrors
# app/chat/sandbox_staging.SANDBOX_CONTEXT_RESTORE (this file runs
# standalone inside the sandbox, so the path is duplicated by design, like
# _SANDBOX_WHEEL_DIR above). Module-level so tests can point it elsewhere.
_CONTEXT_RESTORE_PATH = "/tmp/agnes-context.md"


def _emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


# Strong references for fire-and-forget tasks. The event loop holds tasks
# only weakly — an unreferenced create_task() can be garbage-collected while
# still pending ("Task was destroyed but it is pending!"), which killed the
# stdin reader mid-session once turns started suspending long enough for a
# GC cycle (ApprovalGate): cancel/ticket_push frames then silently stopped
# arriving. Canonical fix per asyncio docs: keep a reference, discard when
# done.
_background_tasks: set = set()


def _spawn(coro) -> "asyncio.Task":
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _hook_output(decision: str, reason: str = "") -> dict:
    """Build a PreToolUse hookSpecificOutput payload for the SDK."""
    out: dict = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason:
        out["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": out}


APPROVAL_DECISIONS = ("allow", "allow_session", "deny")

#: Manager-originated resolution for a request nobody can answer: the
#: session has no approval-capable client attached and can never gain one
#: (the agent-API one-shot path). Never sent by a user —
#: ``ChatManager.deliver_approval_decision`` hardens anything outside
#: APPROVAL_DECISIONS to "deny", so it cannot be forged over a WebSocket.
#: Denies like a timeout but carries an actionable message instead of
#: "the user denied this action".
UNATTENDED = "unattended"

#: Total budget for winding the stream down after the idle watchdog
#: interrupts a wedged turn. Bounds how long the user waits before their
#: next message is served.
_WEDGE_DRAIN_SECONDS = 5.0


class ApprovalGate:
    """In-process PreToolUse gate that makes the workspace hook's ``ask``
    verdicts real in cloud chat.

    Policy stays in the workspace's file hook
    (``.claude/hooks/pre_tool_use.py`` — operator-overridable): this gate
    re-runs it per tool call and acts on the verdict. ``deny`` passes
    through. ``ask`` becomes a genuine approval round-trip: emit an
    ``approval_request`` frame, suspend the tool call on a future, and
    resolve to allow/deny when the user's ``approval_decision`` frame
    arrives on stdin (or the timeout / a cancel fires — both deny).

    Why here and not in the CLI: under ``permission_mode=
    "bypassPermissions"`` the CLI executes a file-hook ``ask`` without
    prompting anyone (verified empirically — the bundled hook's ask rules
    were silently inert in cloud chat). An SDK-level PreToolUse hook runs
    in-process, so it CAN block the call while a human answers.

    ``allow_session`` remembers the exact approved COMMAND and auto-allows
    later asks for that identical command for this runner's lifetime (not
    the hook's reason string, which is shared across a whole command
    family and would over-approve).

    The gate is armed on EVERY surface. Whether anyone can actually answer
    a given request is not knowable here — it depends on which sinks are
    attached to the session at the moment the request lands — so that call
    belongs to the manager, which answers an unanswerable request with the
    :data:`UNATTENDED` decision (see ``ChatManager._resolve_if_unattended``).
    ``enabled=False`` (runner env ``AGNES_APPROVALS=off``) remains an
    operator kill-switch that skips the round-trip entirely.
    """

    def __init__(
        self,
        emit,
        hook_path: "Path | str",
        *,
        enabled: bool = True,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._emit = emit
        self._hook_path = Path(hook_path)
        self._enabled = enabled
        self._disabled_reason = ""
        self.timeout_seconds = timeout_seconds
        self._pending: "dict[str, asyncio.Future[str]]" = {}
        self._session_approved: set[str] = set()
        self._counter = 0

    def run_file_hook(self, payload: dict) -> dict:
        """Run the workspace PreToolUse hook; ``{}`` (no opinion) on any
        failure — a missing/broken policy hook must not take chat down."""
        if not self._hook_path.is_file():
            return {}
        try:
            proc = subprocess.run(
                [sys.executable, str(self._hook_path)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = json.loads(proc.stdout or "{}")
            if not isinstance(out, dict):
                return {}
            # Claude Code allows the verdict either at the top level or
            # nested under hookSpecificOutput. The bundled hook emits the
            # flat shape, but an operator override written against the
            # nested spec shape would otherwise read as "no opinion" and
            # have its ask/deny rules silently ignored (review on #1145).
            if "permissionDecision" not in out:
                nested = out.get("hookSpecificOutput")
                if isinstance(nested, dict) and "permissionDecision" in nested:
                    merged = {**out, **nested}
                    merged.pop("hookSpecificOutput", None)
                    return merged
                if out:
                    print(
                        "approval gate: file hook returned JSON with no permissionDecision; "
                        f"treating as no opinion: {sorted(out)[:8]}",
                        file=sys.stderr,
                        flush=True,
                    )
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"approval gate: file hook failed: {exc}", file=sys.stderr, flush=True)
            return {}

    def resolve(self, request_id: str, decision: str) -> bool:
        """Deliver a user decision to a pending request. False if unknown
        (stale decision after respawn/timeout — dropped silently)."""
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision if decision in APPROVAL_DECISIONS + (UNATTENDED,) else "deny")
        return True

    def cancel_all(self) -> None:
        """Deny every pending request (user hit Stop / turn is over)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result("deny")
        self._pending.clear()

    def disable_unsupported(self, reason: str) -> None:
        """Turn the gate off because it cannot be armed, recording WHY.

        Every path that fails to arm must come through here: leaving
        ``_enabled`` True while the hook was never registered restores the
        pre-PR behavior — an ``ask`` verdict executing unprompted — with
        nothing in the log to say so. The reason is surfaced to the agent so
        it does not relay a wrong explanation to the user.
        """
        self._enabled = False
        self._disabled_reason = reason
        print(f"approval gate: not armed — {reason}", file=sys.stderr, flush=True)

    def awaiting_approval(self) -> bool:
        """True while at least one tool call is suspended waiting for a
        human decision. The turn watchdog consults this so it doesn't
        mistake an approval wait for a wedged tool (review finding on
        #1145)."""
        return any(not fut.done() for fut in self._pending.values())

    async def check(self, input_data: dict, tool_use_id, context) -> dict:
        """SDK PreToolUse callback body. Returns hookSpecificOutput."""
        payload = {
            "tool_name": input_data.get("tool_name"),
            "tool_input": input_data.get("tool_input") or {},
        }
        verdict = await asyncio.to_thread(self.run_file_hook, payload)
        decision = (verdict or {}).get("permissionDecision")
        reason = (verdict or {}).get("permissionDecisionReason", "")
        if decision == "deny":
            return _hook_output("deny", reason or "Denied by workspace policy.")
        if decision != "ask":
            return {}
        # "Allow for session" dedupes on the exact COMMAND, not the hook's
        # reason string: the bundled hook emits one fixed reason for the whole
        # `agnes admin grant|group|user` family, so a reason-keyed cache would
        # let approving one `agnes admin grant …` silently pre-approve every
        # `agnes admin user delete …` for the runner's life. Command-keyed
        # keeps the "Allow for session" blast radius to the identical command
        # (review finding on #1145).
        command = str(payload["tool_input"].get("command", ""))
        if command and command in self._session_approved:
            return _hook_output("allow", "approved by user for this session")
        if not self._enabled:
            return _hook_output(
                "deny",
                (reason + " — " if reason else "")
                + (
                    getattr(self, "_disabled_reason", "")
                    # No longer reachable via the surface: the gate is armed
                    # everywhere and "nobody can answer this" is decided per
                    # request by the manager. What remains is the operator
                    # kill-switch.
                    or "approvals are switched off for this deployment (AGNES_APPROVALS=off)."
                )
                + " Ask the user to confirm and run the command themselves.",
            )
        self._counter += 1
        # Globally unique, not per-process: a respawned sandbox restarts the
        # counter at zero and can be handed the same pid, so the old scheme
        # could mint an id the chat window had already seen. The client dedups
        # cards by request_id, so such a prompt was silently never drawn and
        # the command hung until the approval window expired (review finding
        # on #1145). The counter stays for readable ordering within a process.
        request_id = f"appr-{self._counter}-{uuid.uuid4().hex[:12]}"
        self._emit(
            {
                "type": "approval_request",
                "request_id": request_id,
                "tool": payload["tool_name"],
                "command": command[:2000],
                "reason": reason,
                "timeout_seconds": int(self.timeout_seconds),
            }
        )
        fut: "asyncio.Future[str]" = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            outcome = await asyncio.wait_for(fut, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            outcome = "timeout"
        except asyncio.CancelledError:
            # Announce the outcome before unwinding. The emission below is
            # past this block, so a cancellation used to skip it — and the
            # manager only retires a card when it sees approval_resolved, so
            # the request stayed in pending_approvals and the card came back
            # on every reconnect with buttons that did nothing (review
            # finding on #1145).
            self._emit(
                {
                    "type": "approval_resolved",
                    "request_id": request_id,
                    "decision": "cancelled",
                }
            )
            raise
        finally:
            # Must be a finally, not just the timeout branch: cancellation
            # (Stop, turn teardown) would otherwise leave the future in
            # _pending forever, and awaiting_approval() would stay True — the
            # turn watchdog treats that as "a human is deciding" and never
            # fires again, so a genuinely stuck tool hangs the session for
            # good (review finding on #1145).
            self._pending.pop(request_id, None)
        self._emit(
            {
                "type": "approval_resolved",
                "request_id": request_id,
                "decision": outcome,
            }
        )
        if outcome in ("allow", "allow_session"):
            if outcome == "allow_session" and command:
                self._session_approved.add(command)
            return _hook_output("allow", "approved by user")
        if outcome == "timeout":
            return _hook_output(
                "deny",
                f"Approval request timed out after {int(self.timeout_seconds)}s: {reason}",
            )
        if outcome == UNATTENDED:
            return _hook_output(
                "deny",
                (reason + " — " if reason else "")
                + "This action needs a human to approve it, and this session has no interactive "
                "client that could be shown an approve/deny card (it runs through the agent API). "
                "Ask the caller to run the command themselves, or to start the task in a chat "
                "session they can answer from.",
            )
        return _hook_output("deny", f"The user denied this action: {reason}")


#: Hard caps on a ``question_answer`` payload accepted from stdin. The
#: manager hardens shapes before forwarding, but the runner revalidates:
#: answers are interpolated into the model's tool result, so an oversized
#: or non-string payload must never reach the SDK unchecked.
_MAX_QUESTION_ANSWERS = 8
_MAX_QUESTION_KEY_CHARS = 2000
_MAX_QUESTION_ANSWER_CHARS = 4000


class QuestionGate:
    """In-process ``can_use_tool`` gate that turns the agent's
    ``AskUserQuestion`` tool into a real user round-trip in cloud chat.

    The SDK surfaces AskUserQuestion through the ``can_use_tool`` control
    channel: the tool's own permission check is unconditionally "ask", even
    under ``permission_mode="bypassPermissions"`` (verified empirically —
    the bypass short-circuits ordinary tools before their permission prompt,
    but an unconditional ask still routes to the callback). Without a
    registered callback the SDK raises ``canUseTool callback is not
    provided`` at the control request and the tool call dies — which is why
    questions never reached any UI before this gate existed.

    ``ask`` emits a ``question_request`` frame carrying the tool's
    ``questions`` payload, suspends the tool call on a future, and resolves
    when the user's ``question_answer`` frame arrives on stdin (or the
    timeout / a cancel fires). An answered request is returned to the SDK as
    ``PermissionResultAllow(updated_input={**input, "answers": {question:
    label}})`` — the exact shape the CLI's own interactive permission
    component produces (its input schema's ``answers`` field is documented
    as "User answers collected by the permission component"), so the model
    sees the canonical ``Your questions have been answered: …`` tool result.
    Multi-select answers are comma-joined by the client, matching the tool's
    output contract.

    Deliberately SDK-import-free (returns plain outcome tuples; the
    ``can_use_tool`` adapter in ``_real_agent_loop`` converts them to
    PermissionResult objects) so the fake-agent test path can exercise the
    full stdin round-trip without claude-agent-sdk installed.
    """

    def __init__(self, emit, *, timeout_seconds: float = 300.0) -> None:
        self._emit = emit
        self.timeout_seconds = timeout_seconds
        self._pending: "dict[str, asyncio.Future]" = {}
        self._counter = 0

    @staticmethod
    def _clean_answers(raw) -> "dict[str, str]":
        """Coerce an inbound answers payload to a bounded str→str dict.

        Anything non-conforming is dropped rather than raising: the frame
        crossed a process boundary from a browser, and a malformed answer
        must degrade to "dismissed", not crash the turn.
        """
        if not isinstance(raw, dict):
            return {}
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            if len(cleaned) >= _MAX_QUESTION_ANSWERS:
                break
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            text = value.strip()
            if not text:
                continue
            cleaned[key[:_MAX_QUESTION_KEY_CHARS]] = text[:_MAX_QUESTION_ANSWER_CHARS]
        return cleaned

    def resolve(self, request_id: str, outcome) -> bool:
        """Deliver a user outcome to a pending request. ``outcome`` is the
        answers dict (answered), ``None`` (dismissed), or :data:`UNATTENDED`.
        False if unknown (stale answer after respawn/timeout — dropped
        silently, mirroring ``ApprovalGate.resolve``)."""
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return False
        if outcome == UNATTENDED:
            fut.set_result(UNATTENDED)
        else:
            # {} cleans to "no usable answer" → dismissed, not answered-empty:
            # an empty answers dict would make the model read "Your questions
            # have been answered" with nothing attached.
            fut.set_result(self._clean_answers(outcome) or None)
        return True

    def cancel_all(self) -> None:
        """Dismiss every pending request (user hit Stop / turn is over)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(None)
        self._pending.clear()

    def awaiting_answer(self) -> bool:
        """True while at least one tool call is suspended waiting for an
        answer. The turn watchdog consults this so it doesn't mistake a
        question wait for a wedged tool (same contract as
        ``ApprovalGate.awaiting_approval``)."""
        return any(not fut.done() for fut in self._pending.values())

    async def ask(self, tool_input: dict) -> "tuple[str, dict[str, str] | None]":
        """Run one question round-trip.

        Returns ``(outcome, answers)`` where outcome is ``"answered"``
        (answers is the question→label dict), or ``"dismissed"`` /
        ``"timeout"`` / :data:`UNATTENDED` (answers is ``None``).
        """
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            # Unreachable through the SDK (the CLI validates the tool input
            # schema before the permission round-trip), kept as a guard for
            # a direct caller: nothing to render means nothing to wait for.
            return ("dismissed", None)
        self._counter += 1
        # Globally unique for the same reason as ApprovalGate's ids: a
        # respawned sandbox restarts the counter and the client dedups cards
        # by request_id, so a reused id would silently never be drawn.
        request_id = f"ques-{self._counter}-{uuid.uuid4().hex[:12]}"
        self._emit(
            {
                "type": "question_request",
                "request_id": request_id,
                "questions": questions,
                "timeout_seconds": int(self.timeout_seconds),
            }
        )
        fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            outcome = await asyncio.wait_for(fut, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            outcome = "__timeout__"
        except asyncio.CancelledError:
            # Announce the outcome before unwinding — the manager retires the
            # card only when it sees question_resolved, so skipping this
            # would replay a dead card on every reconnect (same failure the
            # ApprovalGate cancellation path guards against).
            self._emit(
                {
                    "type": "question_resolved",
                    "request_id": request_id,
                    "decision": "cancelled",
                }
            )
            raise
        finally:
            # finally, not just the timeout branch: a cancellation would
            # otherwise leave the future in _pending forever and
            # awaiting_answer() would pin the turn watchdog open.
            self._pending.pop(request_id, None)
        if isinstance(outcome, dict):
            self._emit(
                {
                    "type": "question_resolved",
                    "request_id": request_id,
                    "decision": "answered",
                    "answers": outcome,
                }
            )
            return ("answered", outcome)
        decision = "timeout" if outcome == "__timeout__" else (UNATTENDED if outcome == UNATTENDED else "dismissed")
        self._emit(
            {
                "type": "question_resolved",
                "request_id": request_id,
                "decision": decision,
            }
        )
        return (decision, None)


def _question_outcome(frame: dict):
    """Map an inbound ``question_answer`` stdin frame to a
    ``QuestionGate.resolve`` outcome. ``unattended`` is manager-originated
    only (the WS handler never forwards it from a client, mirroring how
    ``deliver_approval_decision`` hardens decisions)."""
    if frame.get("unattended"):
        return UNATTENDED
    if frame.get("dismissed"):
        return None
    return frame.get("answers")


def _stream_event_delta_text(event: dict) -> str:
    """Extract the user-visible text delta from a raw Anthropic stream event.

    Returns ``""`` for everything that isn't assistant prose — block starts,
    tool-input ``input_json_delta``s, ``thinking_delta``s, message stops —
    so the caller can emit token frames off ``text_delta``s alone.
    """
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return ""
    return delta.get("text", "") or ""


def _install_agnes_cli() -> None:
    """Install the agnes CLI from the spawn-uploaded wheel so the agent's
    ``agnes catalog/query/describe/snapshot`` tool calls resolve on PATH.

    Without this the sandbox has the CLI's *dependencies* (baked into the
    template image) but not the ``agnes`` console script itself, so half the
    cloud-chat data-analysis rails ("Querying Agnes data" in CLAUDE.md) fail
    with "command not found".

    - ``--no-deps``: every runtime dep is already in the template image;
      reinstalling the tree would add seconds to every spawn.
    - NO ``--user``: the console script must land in ``/usr/local/bin`` (the
      sandbox base image chmods ``/usr/local`` 777, so the non-root sandbox
      ``user`` can write there). A ``--user`` install lands ``agnes`` in
      ``~/.local/bin``, which is NOT on the PATH the agent's Bash tool runs
      with — Claude Code's Bash tool resets PATH to a system default
      (``/usr/local/bin:/usr/bin:/bin:…``) and does NOT inherit the runner's
      env, so ``~/.local/bin`` would be invisible and ``agnes`` would still be
      "command not found".
    - ``--break-system-packages``: clears the PEP 668 externally-managed guard
      the Debian/Ubuntu base image sets.

    Best-effort and silent on stdout: pip's chatter is routed to stderr so it
    never corrupts the stdout JSON-frame protocol, and a failure here leaves
    ``agnes`` absent but the chat session otherwise functional — so we log to
    stderr rather than emit a user-facing error frame.
    """
    # Wait for the manager to finish staging the wheel (it writes a ``.ready``
    # sentinel last). Without this barrier we race the upload and glob an empty
    # dir. Bounded — a dev image without a wheel still writes the sentinel, so
    # the normal path returns in milliseconds; the timeout only bites if the
    # upload never happens at all.
    ready = Path(_SANDBOX_WHEEL_DIR) / ".ready"
    deadline = time.monotonic() + _WHEEL_WAIT_SECONDS
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.5)
    # The wheel keeps its PEP 427 name (pip rejects a renamed wheel), so glob
    # the staging dir rather than assuming a fixed filename.
    wheels = sorted(glob.glob(f"{_SANDBOX_WHEEL_DIR}/*.whl"))
    if not wheels:
        return
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--break-system-packages",
                wheels[-1],
            ],
            # stdin MUST be isolated from the parent's fd 0: the runner's
            # asyncio stdin reader has connect_read_pipe'd fd 0 into
            # non-blocking mode, and a child inheriting that same fd corrupts
            # the reader (user_msg frames then never arrive — the agent hangs
            # with no response). DEVNULL gives pip its own stdin.
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr.fileno(),
            stderr=sys.stderr.fileno(),
            check=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal; agent still runs
        print(f"agnes CLI install failed: {exc}", file=sys.stderr, flush=True)


async def _wait_workspace_ready() -> bool:
    """Wait for the manager's workspace-upload sentinel before the agent CLI
    spawns.

    The runner process starts while ``upload_workspace`` is still pushing the
    tree into ``/work`` — spawning ``claude`` earlier would boot it against an
    empty project (no CLAUDE.md data rails, no ``.claude`` settings/plugins).
    Sentinel path comes from ``AGNES_WORKSPACE_SYNC_SENTINEL``; empty/unset
    means the provider mounts the workspace itself and there is nothing to
    wait for. Bounded and best-effort: on timeout we log to stderr and let the
    agent start anyway.
    """
    sentinel = os.environ.get("AGNES_WORKSPACE_SYNC_SENTINEL", "").strip()
    if not sentinel:
        return True
    path = Path(sentinel)
    deadline = time.monotonic() + _WORKSPACE_WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return True
        await asyncio.sleep(0.25)
    print(
        f"workspace-ready sentinel {sentinel} never appeared after "
        f"{_WORKSPACE_WAIT_SECONDS}s; starting agent on a possibly-incomplete workspace",
        file=sys.stderr,
        flush=True,
    )
    return False


def _agnes_mcp_servers() -> dict:
    """Build the ``mcp_servers`` config that connects the sandbox agent to the
    Agnes MCP stdio server (``agnes mcp``).

    This is what makes the cloud-chat agent == local Claude Code / Cowork for
    MCP: the same ``agnes mcp`` stdio server that an analyst's local install
    spawns (cli/mcp/server.py) is spawned here as a child of the SDK's
    ``claude`` process. It exposes the built-in cowork tools (catalog, query,
    describe, …) PLUS the RBAC-filtered Universal-MCP *passthrough* tools the
    caller's groups can see (registered dynamically at run() start via
    cli/mcp/_dynamic_passthrough.py against ``/api/mcp/passthrough/tools``).
    Without this, the sandbox agent could only reach Agnes through the
    ``agnes`` CLI's Bash surface and never saw passthrough tools at all.

    The stdio server authenticates off ``AGNES_SERVER`` (+ ``AGNES_SESSION_ID``
    for the per-session re-mint path). ``AGNES_SERVER`` is the in-sandbox
    loopback relay's address by the time this runs (``_start_relay`` rewrites
    it before any subprocess spawn), not the real Agnes server — the relay is
    the only thing in the sandbox that ever holds an authenticating
    credential (a short-lived broker ticket pushed over stdin, see
    ``_dispatch_frame``), attached on the outbound leg. No ``AGNES_TOKEN`` is
    placed in this env (AC-F2b). We forward ``AGNES_SERVER`` explicitly on the
    MCP server's own ``env`` rather than relying on inheritance, because the
    SDK spawns the server through the ``claude`` CLI and env inheritance
    across that hop is not guaranteed. ``PATH`` is forwarded so the ``agnes``
    console script (installed by ``_install_agnes_cli`` into /usr/local/bin)
    resolves.

    Returns ``{}`` when ``AGNES_SERVER`` is absent (e.g. the fake-agent test
    path or a misconfigured spawn) so the agent still runs with its built-in
    tools rather than failing on a broken MCP handshake.
    """
    server = os.environ.get("AGNES_SERVER", "").strip()
    if not server:
        return {}
    # The MCP stdio server must ride the mcp-scoped broker ticket, not the
    # agent process's main-scoped one. `_start_relay` set this process's
    # AGNES_SERVER to the relay's `/agnes-api` path (main scope); rewrite it to
    # `/agnes-mcp` for the MCP subprocess so the relay attaches the mcp ticket
    # (relay._SCOPE_FOR_PREFIX). Without this, the minted+pushed mcp ticket is
    # dead and both surfaces share one scope, defeating the split (§11).
    mcp_server = server.replace("/agnes-api", "/agnes-mcp")
    env = {
        "AGNES_SERVER": mcp_server,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        # ``agnes mcp`` resolves its config dir via ``expanduser("~/.config/
        # agnes")`` (cli/config.py), which needs HOME. The ``claude`` CLI
        # spawns the stdio server and env inheritance across that hop is not
        # guaranteed, so forward HOME explicitly (default matches the sandbox
        # ``user`` home the manager seeds into the runner process).
        "HOME": os.environ.get("HOME", "/home/user"),
    }
    if session_id := os.environ.get("AGNES_SESSION_ID", "").strip():
        env["AGNES_SESSION_ID"] = session_id
    return {
        "agnes": {
            "type": "stdio",
            "command": "agnes",
            "args": ["mcp"],
            "env": env,
        }
    }


async def _call_delegation_endpoint(agent_slug: str, message: str) -> dict:
    """POST one delegation request through the in-sandbox relay's main-scope
    leg (Track C7 MVP, @delegation).

    ``AGNES_SERVER`` is already rewritten by ``_start_relay`` to the relay's
    own loopback address before any subprocess spawn — the SAME env this
    process's own outbound calls use, so this rides the identical envelope-
    replay path (``app/chat/relay.py``, ``app/api/broker.py::_replay``)
    every other Agnes MCP tool call already rides: the request runs
    server-side under THIS chat session's own identity, minted fresh by the
    broker (``_mint_identity_jwt``) — never a credential this process holds
    directly (AC-F2b).

    Never raises: a missing/broken relay, a malformed target, or an HTTP
    error all degrade to a denial-shaped dict so the model's tool call
    always gets SOMETHING to reason about, rather than the whole turn
    crashing on a delegation attempt.
    """
    if not agent_slug or not message:
        return {"status": "denied", "reason": "invalid_request", "agent_slug": agent_slug, "answer": None}
    server = os.environ.get("AGNES_SERVER", "").strip()
    if not server:
        return {
            "status": "denied",
            "reason": "delegation_unavailable",
            "agent_slug": agent_slug,
            "answer": None,
            "message": "no AGNES_SERVER in this environment — delegation is unavailable",
        }
    import urllib.parse

    import httpx

    url = f"{server.rstrip('/')}/api/v1/agents/{urllib.parse.quote(agent_slug, safe='')}/delegate"
    # Bound generously under `_DELEGATION_TIMEOUT_S` (`app/chat/manager.py`,
    # 180s) — the manager's own wait for B's turn is the tighter,
    # authoritative bound, so this client timeout only needs to comfortably
    # outlive it, not add a second race. Built lazily (not module-level):
    # httpx may not be importable yet at THIS module's own import time (see
    # the module docstring's `app.chat.relay` lazy-import note).
    timeout = httpx.Timeout(connect=15.0, read=200.0, write=30.0, pool=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={"message": message})
        resp.raise_for_status()
        result = resp.json()
        return result if isinstance(result, dict) else {"status": "denied", "reason": "bad_response"}
    except Exception as exc:  # noqa: BLE001 — a delegation failure must not crash the turn
        return {
            "status": "denied",
            "reason": "delegation_request_failed",
            "agent_slug": agent_slug,
            "answer": None,
            "message": f"delegation request failed: {exc}",
        }


def _delegation_mcp_server():
    """Build the in-process SDK MCP server exposing ``delegate_to_agent`` to
    the model (Track C7 MVP, @delegation) — ``None`` when the installed
    ``claude-agent-sdk`` predates ``create_sdk_mcp_server``/``tool`` (the
    sandbox image's SDK is outside the wheel's pin, see the HookMatcher/
    can_use_tool feature probes above for the same degrade-not-crash
    posture on an older template).

    Unlike ``ApprovalGate``/``QuestionGate`` (which intercept an EXISTING
    SDK-builtin tool's permission check), this registers a brand-new tool
    the model can choose to call — the documented ``claude_agent_sdk.tool``
    + ``create_sdk_mcp_server`` mechanism for app-defined in-process tools.
    The handler makes one HTTP round-trip (:func:`_call_delegation_endpoint`)
    and returns the server's JSON result verbatim as the tool's text
    output — all of the RBAC gate, the depth-1 guard, the one-delegation-
    per-turn guard, and the caller-bound child-session spawn live
    server-side in ``ChatManager.handle_delegation``.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool  # type: ignore[import-untyped]
    except ImportError:
        return None

    @tool(
        "delegate_to_agent",
        (
            "Delegate this request to another Agnes agent you are permitted to run, "
            "identified by its slug. The delegate answers under YOUR CALLER's own "
            "data access (never yours) and returns control to you once it answers. "
            "Depth-1 only: the delegate cannot itself delegate, and you may delegate "
            "at most once per turn."
        ),
        {"agent_slug": str, "message": str},
    )
    async def _delegate_to_agent(args: dict) -> dict:
        result = await _call_delegation_endpoint(str(args.get("agent_slug", "")), str(args.get("message", "")))
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return create_sdk_mcp_server("agnes-delegation", tools=[_delegate_to_agent])


def _register_workspace_marketplace(workdir: Path) -> None:
    """Install the workspace's shipped marketplace plugins into this project.

    The server wrote the caller's RBAC-filtered marketplace as a plain directory
    inside the workspace (``app/chat/marketplace_payload.py``); Claude Code
    registers a directory as a marketplace and installs from it with no network
    access at all:

        claude plugin marketplace add <workspace>/.claude/agnes-marketplace
        claude plugin install <name>@agnes --scope project

    Both are needed. ``marketplace add`` records the source in the CLI's HOME
    settings and ``plugin install`` records the install in its HOME registry —
    HOME is fresh in every sandbox, so this runs per spawn even though the
    workspace persists. The project-level ``enabledPlugins`` half is already in
    the workspace settings the server wrote, which is what makes the plugins
    load rather than sit installed-but-disabled.

    ``--scope user``, deliberately, even though this is conceptually a
    per-project install: ``--scope project`` writes ``enabledPlugins`` into
    ``<cwd>/.claude/settings.json``, and in a session directory that path is a
    SYMLINK to the shared workspace — which the CLI refuses outright
    (``SymlinkWriteRefusedError``), so every install failed and no plugin
    loaded. User scope has no such write, and in a sandbox it means exactly the
    same thing: HOME is created fresh for this session and thrown away with it.

    Best-effort and bounded, like every other spawn-time convergence: a failure
    costs the session its marketplace plugins, never the session. Output goes to
    stderr so it cannot corrupt the stdout frame protocol.
    """
    from shutil import which

    from app.chat.marketplace_payload import MARKETPLACE_NAME, MARKETPLACE_TREE_SUBDIR

    tree = workdir / MARKETPLACE_TREE_SUBDIR
    manifest = tree / ".claude-plugin" / "marketplace.json"
    if not manifest.is_file():
        # No stack plugins for this user, or the operator turned delivery off.
        return
    claude = which("claude")
    if claude is None:
        print("marketplace: no `claude` on PATH; skipping plugin registration", file=sys.stderr, flush=True)
        return

    def _run(args: list[str], label: str) -> bool:
        try:
            result = subprocess.run(
                [claude, *args],
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr.fileno(),
                stderr=sys.stderr.fileno(),
                check=False,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal; agent still runs
            print(f"marketplace: {label} failed: {exc}", file=sys.stderr, flush=True)
            return False
        if result.returncode != 0:
            print(f"marketplace: {label} exited {result.returncode}", file=sys.stderr, flush=True)
            return False
        return True

    if not _run(["plugin", "marketplace", "add", str(tree)], "marketplace add"):
        return
    try:
        names = [p["name"] for p in json.loads(manifest.read_text(encoding="utf-8")).get("plugins", [])]
    except (OSError, ValueError, TypeError):
        print("marketplace: shipped manifest is unreadable; skipping installs", file=sys.stderr, flush=True)
        return
    for name in names:
        _run(["plugin", "install", f"{name}@{MARKETPLACE_NAME}", "--scope", "user"], f"install {name}")


async def _dispatch_frame(frame: dict, queue: "asyncio.Queue[dict]") -> None:
    """Route one parsed inbound stdin frame.

    ``ticket_push`` frames (``{"type": "ticket_push", "main": ..., "mcp":
    ..., "data_apps": ...}``) update the module-level relay's in-memory
    tickets and are never enqueued for the agent loop — the agent must never
    see a ticket. Every other frame type (``user_msg``, ``cancel``, ``_eof``)
    is queued unchanged, exactly as it always has been.
    """
    if frame.get("type") == "ticket_push":
        if _relay is not None:
            _relay.set_tickets(frame.get("main", ""), frame.get("mcp", ""), frame.get("data_apps", ""))
        return
    await queue.put(frame)


async def _stdin_lines() -> "asyncio.Queue[dict]":
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def reader() -> None:
        loop = asyncio.get_running_loop()
        reader_obj = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader_obj)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader_obj.readline()
            if not line:
                await queue.put({"type": "_eof"})
                return
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            await _dispatch_frame(frame, queue)

    _spawn(reader())
    return queue


async def _fake_agent_loop(
    queue: "asyncio.Queue[dict]",
    *,
    per_tool_seconds: float = 90.0,
    tool_calls_per_turn: int = 50,
    gate: "ApprovalGate | None" = None,
    question_gate: "QuestionGate | None" = None,
) -> None:
    """Used by tests via AGNES_RUNNER_FAKE_AGENT=1. Echoes user_msg back.

    Special messages:
    - ``__slow_tool__`` — simulates a tool call that exceeds the per-tool
      wall-clock cap. Emits ``tool_call`` then, after timeout, emits a
      synthetic ``tool_result: {timeout: true}``.
    - ``__many_tools__:N`` — fires N tool_call frames to exercise the
      per-turn tool-call budget gate.
    - ``__approval__:<command>`` — runs the ApprovalGate against a
      synthetic Bash call so tests exercise the real stdin round-trip
      (approval_request out → approval_decision in) without the SDK.
    - ``__question__:<question>`` — runs the QuestionGate against a
      synthetic AskUserQuestion input so tests exercise the real stdin
      round-trip (question_request out → question_answer in) without the
      SDK.
    """
    while True:
        frame = await queue.get()
        if frame.get("type") == "_eof":
            return
        if frame.get("type") == "approval_decision" and gate is not None:
            gate.resolve(str(frame.get("request_id", "")), str(frame.get("decision", "")))
            continue
        if frame.get("type") == "question_answer" and question_gate is not None:
            question_gate.resolve(str(frame.get("request_id", "")), _question_outcome(frame))
            continue
        if frame.get("type") == "user_msg":
            text = frame.get("text", "")
            if text.startswith("__approval__:") and gate is not None:
                command = text.split(":", 1)[1]

                async def _run_gate(cmd: str) -> None:
                    out = await gate.check({"tool_name": "Bash", "tool_input": {"command": cmd}}, None, {})
                    decision = (out.get("hookSpecificOutput") or {}).get("permissionDecision", "allow")
                    _emit({"type": "assistant_message", "content": f"gate:{decision}"})
                    _emit({"type": "done"})

                # Concurrent task: the loop must keep reading stdin so the
                # approval_decision frame can reach gate.resolve above.
                _spawn(_run_gate(command))
                continue
            if text.startswith("__question__:") and question_gate is not None:
                question = text.split(":", 1)[1]

                async def _run_question(q: str) -> None:
                    outcome, answers = await question_gate.ask(
                        {
                            "questions": [
                                {
                                    "question": q,
                                    "header": "Test",
                                    "options": [{"label": "A"}, {"label": "B"}],
                                    "multiSelect": False,
                                }
                            ]
                        }
                    )
                    _emit(
                        {
                            "type": "assistant_message",
                            "content": f"question:{outcome}:{json.dumps(answers or {}, sort_keys=True)}",
                        }
                    )
                    _emit({"type": "done"})

                # Concurrent task for the same reason as __approval__ above.
                _spawn(_run_question(question))
                continue
            if text == "__slow_tool__":
                _emit({"type": "tool_call", "tool": "run_query", "args": {"sql": "..."}})
                try:
                    await asyncio.wait_for(
                        asyncio.sleep(per_tool_seconds + 5),
                        timeout=per_tool_seconds,
                    )
                except asyncio.TimeoutError:
                    _emit(
                        {
                            "type": "tool_result",
                            "tool": "run_query",
                            "result": {"timeout": True},
                        }
                    )
                continue
            if text.startswith("__many_tools__:"):
                try:
                    requested = int(text.split(":", 1)[1])
                except ValueError:
                    requested = 0
                # Tool-call budget gate (B.2.d): cap emitted tool_call frames
                # per turn at tool_calls_per_turn; on overflow emit a
                # confirmation_required and stop until the next user_msg.
                count = 0
                budget_hit = False
                for i in range(requested):
                    if count >= tool_calls_per_turn:
                        _emit(
                            {
                                "type": "confirmation_required",
                                "reason": "tool_call_budget",
                                "budget": tool_calls_per_turn,
                            }
                        )
                        budget_hit = True
                        break
                    _emit({"type": "tool_call", "tool": f"t{i}", "args": {}})
                    count += 1
                if not budget_hit:
                    _emit(
                        {
                            "type": "assistant_message",
                            "content": f"emitted {count} tool calls",
                            "tokens_in": 1,
                            "tokens_out": 1,
                            "model": "fake",
                        }
                    )
                continue
            _emit(
                {
                    "type": "assistant_message",
                    "content": f"echo: {text}",
                    "tokens_in": 1,
                    "tokens_out": 1,
                    "model": "fake",
                }
            )


async def _real_agent_loop(
    queue: "asyncio.Queue[dict]",
    workdir: Path,
    *,
    tool_calls_per_turn: int = 50,
    gate: "ApprovalGate | None" = None,
    question_gate: "QuestionGate | None" = None,
) -> None:
    """Real claude-agent-sdk-backed loop.

    Per-tool wall-clock cap (Phase 12.2): the fake-agent path enforces
    AGNES_PER_TOOL_CALL_SECONDS via asyncio.wait_for in _fake_agent_loop.
    For the real SDK path, tool dispatch is handled inside ClaudeSDKClient
    (agnes receives tool_call/tool_result frames, not raw coroutines), so
    per-tool wrapping is not straightforward at this boundary. A simpler
    wall-clock timeout is applied at the whole-turn level: if
    receive_response() takes longer than per_tool_seconds * max_tools_per_turn,
    the connection is interrupted. Full per-tool granularity requires either
    an SDK API that exposes individual tool dispatch coroutines, or an
    out-of-process watchdog. TODO(Phase 12.2): revisit when claude-agent-sdk
    exposes a per-tool hook or run_tool() coroutine.

    Uses ClaudeSDKClient for persistent-session bidirectional communication:
    - the ``async with`` block connects EAGERLY (``__aenter__`` → ``connect()``
      with an empty stream), so the ``claude`` CLI subprocess boots while the
      user is still typing their first message
    - query() for every user_msg (the previous connect(text)-on-first-message
      pattern spawned a SECOND CLI on top of the one ``__aenter__`` already
      started — a full CLI boot added to first-message latency)
    - each turn's receive_response() drains in a CONCURRENT task
      (_consume_turn) while this loop keeps watching the stdin queue — a
      cancel frame arriving mid-turn interrupts the live turn (with the old
      single-consumer design it sat in the queue until the turn finished on
      its own, so Stop did nothing)
    - interrupt() for cancel frames; user_msg/_eof frames arriving mid-turn
      are buffered and processed after the turn, preserving order

    Message type mapping (SDK → outbound JSON frames):
    - StreamEvent text deltas → token frames as the model produces them
      (include_partial_messages; falls back to whole-TextBlock token frames
      when the SDK predates StreamEvent or no deltas arrive)
    - AssistantMessage with TextBlock content → collected for the turn-end
      assistant_message (token frame only when no deltas streamed this turn)
    - AssistantMessage with ToolUseBlock content → tool_call frame
    - AssistantMessage with ToolResultBlock content → tool_result frame
    - ResultMessage → assistant_message frame (turn end, carries usage/model)
    """
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )

    try:  # StreamEvent ships in newer claude-agent-sdk releases only
        from claude_agent_sdk import StreamEvent  # type: ignore[attr-defined]
    except ImportError:
        StreamEvent = None

    # ``bypassPermissions`` so the agent can run its tools (Bash → ``agnes
    # catalog``/``query``/…) autonomously. The SDK's default permission mode
    # denies any tool needing approval in this headless context (no human to
    # prompt), so the agent emits a tool_call and then hangs / hallucinates
    # success without ever executing it. The sandbox is the isolation
    # boundary here (ephemeral, per-session); egress control is the workspace
    # PreToolUse hook's job and is documented as best-effort/fail-open.
    # bypassPermissions swallows the file hook's ``ask`` verdicts (executes
    # without prompting — verified empirically), so the ApprovalGate below
    # re-enforces them via an SDK in-process PreToolUse hook that CAN block
    # the call while the user answers an approval_request frame.
    # Load the workspace's filesystem config (user + project + local) — the
    # same scopes the local `claude` CLI loads by default. The SDK loads NONE
    # of them unless told to (its isolation default), which would make the
    # cloud-chat agent behave differently from a local Agnes install: it would
    # miss the workspace CLAUDE.md (the data rails that tell it to use the
    # `agnes` CLI instead of hunting for local files) and any installed
    # marketplace plugins. Loading them keeps cloud-chat == local. (The
    # marketplace registers in user scope and enables plugins at project scope,
    # so both must load for a bootstrapped plugin to resolve.)
    # ``mcp_servers`` connects the agent to the Agnes MCP stdio server so it
    # sees the RBAC-filtered passthrough tools (crm_* etc.) — the same surface
    # a local Claude Code / Cowork install gets. Empty dict when unconfigured
    # (fake-agent tests) so the agent still runs with built-in tools.
    mcp_servers = _agnes_mcp_servers()
    # Track C7 (@delegation MVP): an in-process SDK MCP server exposing
    # `delegate_to_agent` — a distinct mechanism from `_agnes_mcp_servers()`
    # above (that one spawns the `agnes mcp` STDIO subprocess; this one runs
    # in THIS process, per claude_agent_sdk.create_sdk_mcp_server). Merged
    # into the same `mcp_servers` dict under its own key so it costs no
    # extra `allowed_tools` wiring (bypassPermissions already admits every
    # registered server's tools). `None` when the installed SDK predates
    # `create_sdk_mcp_server`/`tool` — degrade to no delegation capability
    # rather than crash the runner (same posture as the HookMatcher/
    # can_use_tool feature probes above).
    delegation_server = _delegation_mcp_server()
    if delegation_server is not None:
        mcp_servers = {**mcp_servers, "agnes-delegation": delegation_server}
    options_kwargs: dict = dict(
        permission_mode="bypassPermissions",
        cwd=str(workdir),
        setting_sources=["user", "project", "local"],
        mcp_servers=mcp_servers,
    )
    # Approval gate (SDK in-process PreToolUse hook). HookMatcher AND the
    # ClaudeAgentOptions.hooks field must both exist — an older sandbox
    # image (its :latest tag is mutable, outside the wheel's pin) could
    # ship one without the other; degrade to today's behavior (no gate) rather
    # than crash the runner at ClaudeAgentOptions(**options_kwargs). Same
    # __dataclass_fields__ probe used for include_partial_messages below
    # (review finding on #1145).
    _hooks_supported = "hooks" in getattr(ClaudeAgentOptions, "__dataclass_fields__", {})
    if gate is not None:
        try:
            from claude_agent_sdk import HookMatcher  # type: ignore[attr-defined]
        except ImportError:
            HookMatcher = None

        if not _hooks_supported or HookMatcher is None:
            # Nothing can be registered, so nothing can deny either — be
            # honest about that rather than calling it fail-closed. The
            # sandbox image's SDK is outside the wheel's pin (its
            # :latest tag is mutable), so log loudly enough for an operator
            # to notice that ask-flagged commands are running unasked.
            gate.disable_unsupported(
                "the installed claude-agent-sdk cannot register a PreToolUse hook "
                f"({'ClaudeAgentOptions has no `hooks` field' if not _hooks_supported else 'no HookMatcher'}); "
                "APPROVALS ARE NOT ENFORCED in this session — upgrade the sandbox template's SDK"
            )
        else:

            async def _gate_hook(input_data, tool_use_id, context):
                return await gate.check(input_data, tool_use_id, context)

            # Matcher is Bash-only: the bundled workspace hook short-circuits
            # to allow for every non-Bash tool, so no policy is lost today,
            # and gating every Read/Write/Edit through a per-call file-hook
            # subprocess would add real latency. Consequence documented in
            # docs/cloud-chat.md: an operator override that adds `ask` rules
            # for Write/Edit/WebFetch would need this widened to see them
            # (review note on #1145).
            #
            # HookMatcher.timeout is newer than HookMatcher itself. The margin
            # over the gate's own await is what stops the CLI-side matcher
            # timeout from firing first; without it the CLI could time the
            # hook out and treat it as non-blocking, running the tool while a
            # human is still being asked.
            #
            # Two assumptions ride on that margin and cannot be checked from
            # here (they live in the CLI, not the SDK): that the value is in
            # SECONDS, and that the CLI treats a matcher timeout as
            # non-blocking rather than as a deny. If the unit were smaller the
            # margin collapses and the CLI times out first; if a timeout
            # denies, the failure is safe (a refused command) rather than an
            # unasked one. Both were consistent with an empirical ≥75s block
            # during development. The test below pins the field's existence,
            # which is what we can assert offline (review note on #1145).
            #
            # So on an SDK without it we still REGISTER the hook, with the
            # gate disabled. A disabled gate denies instantly instead of
            # waiting, so there is nothing for a CLI-side timeout to cut
            # short — whereas skipping registration would leave nothing to
            # deny at all, and ask-flagged commands would run unasked: the
            # exact behavior this change exists to remove (review finding
            # on #1145).
            try:
                _matcher = HookMatcher(matcher="Bash", hooks=[_gate_hook], timeout=gate.timeout_seconds + 30)
            except TypeError:
                gate.disable_unsupported(
                    "the installed claude-agent-sdk HookMatcher takes no `timeout`, so the gate "
                    "cannot block safely; ask-flagged commands are DENIED instead of confirmed — "
                    "upgrade the SDK to restore approvals"
                )
                _matcher = HookMatcher(matcher="Bash", hooks=[_gate_hook])
            options_kwargs["hooks"] = {"PreToolUse": [_matcher]}
    # Question gate (SDK can_use_tool callback). The AskUserQuestion tool's
    # permission check is unconditionally "ask" — bypassPermissions does NOT
    # swallow it the way it swallows ordinary tools' prompts, so every
    # AskUserQuestion call routes to this callback (and with no callback
    # registered the SDK raises "canUseTool callback is not provided" and the
    # tool call dies — the pre-gate behavior). Ordinary tools never reach the
    # callback under bypassPermissions; the passthrough allow below only
    # covers a hypothetical future unconditional-ask tool, preserving bypass
    # semantics for it. Registering can_use_tool makes the SDK pass
    # ``--permission-prompt-tool stdio`` to the CLI, which requires streaming
    # mode — ClaudeSDKClient always is. Same degrade-not-crash posture as the
    # hooks probe above for older sandbox-template SDKs.
    _can_use_tool_supported = "can_use_tool" in getattr(ClaudeAgentOptions, "__dataclass_fields__", {})
    if question_gate is not None:
        try:
            from claude_agent_sdk import (  # type: ignore[attr-defined]
                PermissionResultAllow,
                PermissionResultDeny,
            )
        except ImportError:
            PermissionResultAllow = PermissionResultDeny = None

        if not _can_use_tool_supported or PermissionResultAllow is None or PermissionResultDeny is None:
            print(
                "question gate: not armed — the installed claude-agent-sdk cannot register "
                "a can_use_tool callback; AskUserQuestion tool calls will fail in this "
                "session — upgrade the sandbox template's SDK",
                file=sys.stderr,
                flush=True,
            )
        else:

            async def _can_use_tool(tool_name, tool_input, context):
                if tool_name != "AskUserQuestion":
                    # Never reached for ordinary tools under bypassPermissions;
                    # keep bypass semantics if some other unconditional-ask
                    # tool ever lands here. No updated_input → the SDK echoes
                    # the original input.
                    return PermissionResultAllow()
                outcome, answers = await question_gate.ask(tool_input)
                if outcome == "answered":
                    # {**tool_input, "answers": ...} is the shape the CLI's own
                    # interactive permission component returns — the tool then
                    # renders the canonical "Your questions have been
                    # answered: ..." result for the model.
                    return PermissionResultAllow(updated_input={**tool_input, "answers": answers})
                if outcome == UNATTENDED:
                    return PermissionResultDeny(
                        message=(
                            "This session has no interactive client that can be shown a "
                            "question card (it runs through the agent API). Continue with "
                            "your best judgment, or state the question in your final answer."
                        )
                    )
                if outcome == "timeout":
                    return PermissionResultDeny(
                        message=(
                            f"Nobody answered the questions within {int(question_gate.timeout_seconds)}s. "
                            "Continue with your best judgment and note the open question in your answer."
                        )
                    )
                return PermissionResultDeny(
                    message=(
                        "The user dismissed the questions without answering. Continue with "
                        "your best judgment; ask in plain text if you truly need an answer."
                    )
                )

            options_kwargs["can_use_tool"] = _can_use_tool

    # Token-level streaming (include_partial_messages) when the installed SDK
    # supports it: the UI then renders text as the model produces it instead
    # of one token frame per completed content block (which for a long answer
    # means seconds of dead air followed by the whole paragraph at once).
    partial_streaming = StreamEvent is not None and "include_partial_messages" in getattr(
        ClaudeAgentOptions, "__dataclass_fields__", {}
    )
    if partial_streaming:
        options_kwargs["include_partial_messages"] = True

    # Restored-conversation transcript (fresh sandbox for a chat with
    # history): appended to the CLI's default system prompt via the
    # ``claude_code`` preset + ``append`` shape (the SDK maps it to
    # ``--append-system-prompt``, keeping the stock preset intact). Read
    # best-effort — an unreadable file degrades to a context-free session.
    try:
        _ctx_path = Path(_CONTEXT_RESTORE_PATH)
        restore_ctx = _ctx_path.read_text(encoding="utf-8", errors="replace").strip() if _ctx_path.exists() else ""
    except OSError as exc:
        print(f"context restore read failed: {exc}", file=sys.stderr, flush=True)
        restore_ctx = ""
    if restore_ctx:
        options_kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": restore_ctx,
        }

    async def _interrupt(client) -> None:
        # interrupt() is a coroutine — an un-awaited call never reaches the
        # CLI and the turn keeps running (the historical cancel-does-nothing
        # bug). Best-effort: a cancel racing the turn's natural end must not
        # kill the runner.
        try:
            await client.interrupt()
        except Exception as exc:  # noqa: BLE001
            print(f"interrupt failed: {exc}", file=sys.stderr, flush=True)

    async with ClaudeSDKClient(options=ClaudeAgentOptions(**options_kwargs)) as client:
        # ``__aenter__`` above already connected (empty-stream streaming mode)
        # — the CLI subprocess is booting from this point on, typically
        # finishing before the first user_msg arrives.

        # Frames that arrived while a turn was in flight (queued follow-up
        # user_msg, an _eof) — processed in order before the queue is read
        # again, preserving the pre-concurrency single-consumer semantics.
        pending_frames: list[dict] = []
        # Persistent queue.get() task. NEVER cancelled — cancelling a
        # Queue.get() that has already been handed an item loses the frame;
        # instead the outstanding task is carried across turns and awaited by
        # whichever loop (outer or mid-turn watcher) runs next.
        next_frame_task: asyncio.Task | None = None

        def _frame_task() -> asyncio.Task:
            nonlocal next_frame_task
            if next_frame_task is None:
                next_frame_task = asyncio.create_task(queue.get())
            return next_frame_task

        while True:
            if pending_frames:
                frame = pending_frames.pop(0)
            else:
                frame = await _frame_task()
                next_frame_task = None
            t = frame.get("type")

            if t == "_eof":
                return

            if t == "cancel":
                # Between turns: nothing is running, but the interrupt may
                # still race a just-finished turn — best-effort.
                if gate is not None:
                    gate.cancel_all()
                if question_gate is not None:
                    question_gate.cancel_all()
                await _interrupt(client)
                continue

            if t == "approval_decision":
                # Stale (turn already over / runner respawned) — resolve()
                # drops unknown ids silently.
                if gate is not None:
                    gate.resolve(str(frame.get("request_id", "")), str(frame.get("decision", "")))
                continue

            if t == "question_answer":
                # Stale (turn already over / runner respawned) — resolve()
                # drops unknown ids silently, same as approval_decision.
                if question_gate is not None:
                    question_gate.resolve(str(frame.get("request_id", "")), _question_outcome(frame))
                continue

            if t != "user_msg":
                continue

            text = frame.get("text", "")

            await client.query(text)

            # Consume the turn as a concurrent task while this loop keeps
            # watching the stdin queue. A single-consumer design (await
            # queue.get() only at the top of the loop) meant a `cancel`
            # arriving MID-TURN sat in the queue until the turn finished
            # naturally — by which point interrupt() was a no-op and the
            # Stop button did nothing (Devin Review on #975).
            turn_task = asyncio.create_task(
                _consume_turn(client, tool_calls_per_turn=tool_calls_per_turn, gate=gate, question_gate=question_gate)
            )
            interrupted_this_turn = False
            while not turn_task.done():
                ft = _frame_task()
                await asyncio.wait({turn_task, ft}, return_when=asyncio.FIRST_COMPLETED)
                if ft.done():
                    next_frame_task = None
                    mid = ft.result()
                    if mid.get("type") == "cancel":
                        # Interrupt the LIVE turn; receive_response() then
                        # winds down and turn_task completes. Pending
                        # approvals/questions must resolve too — the SDK
                        # callback awaiting them would otherwise pin the turn
                        # until its timeout despite the interrupt.
                        if gate is not None:
                            gate.cancel_all()
                        if question_gate is not None:
                            question_gate.cancel_all()
                        interrupted_this_turn = True
                        await _interrupt(client)
                    elif mid.get("type") == "approval_decision":
                        # Mid-turn is the normal case: the tool call is
                        # suspended inside the gate right now.
                        if gate is not None:
                            gate.resolve(
                                str(mid.get("request_id", "")),
                                str(mid.get("decision", "")),
                            )
                    elif mid.get("type") == "question_answer":
                        # Mid-turn is the normal case here too: the
                        # AskUserQuestion call is suspended inside the
                        # question gate right now.
                        if question_gate is not None:
                            question_gate.resolve(
                                str(mid.get("request_id", "")),
                                _question_outcome(mid),
                            )
                    else:
                        # user_msg / _eof: keep for after the turn, in order.
                        pending_frames.append(mid)
            if interrupted_this_turn and turn_task.exception() is not None:
                # Some SDK/CLI builds surface a user interrupt as an exception
                # out of receive_response() instead of a graceful
                # ResultMessage. That is the OUTCOME THE USER ASKED FOR — eat
                # it so pressing Stop never tears down the whole runner (and
                # with it the session). A turn crashing WITHOUT an interrupt
                # still propagates below: runner exits, manager respawns.
                print(
                    f"turn ended with exception after interrupt (expected): {turn_task.exception()}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                await turn_task  # propagate a crashed turn (outer handler emits error frame)
            # `done` is emitted here, not inside `_consume_turn`, so it never
            # fires ahead of a genuine crash: `await turn_task` above raises
            # before reaching this line, and the manager's `error` frame
            # handler (not `done`) is what runs — preserving the turn buffer
            # so the partial answer already streamed to the user gets saved
            # (Devin Review on #975).
            _emit({"type": "done"})


async def _consume_turn(
    client,
    *,
    tool_calls_per_turn: int = 50,
    gate: "ApprovalGate | None" = None,
    question_gate: "QuestionGate | None" = None,
) -> None:
    """Drain one turn's ``receive_response()`` stream into outbound frames.

    Runs as its own task so ``_real_agent_loop`` can keep consuming stdin
    frames (cancel!) while the turn is in flight. Does NOT emit the `done`
    frame itself — the caller does that, and only when this coroutine
    returns without raising, so a genuine crash mid-stream propagates as an
    `error` frame instead, leaving the turn buffer intact for partial-save.
    """
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    try:  # StreamEvent ships in newer claude-agent-sdk releases only
        from claude_agent_sdk import StreamEvent  # type: ignore[attr-defined]
    except ImportError:
        StreamEvent = None

    def _emit_tool_result(block) -> None:
        result = block.content
        if isinstance(result, list):
            result = " ".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in result)
        _emit(
            {
                "type": "tool_result",
                "id": block.tool_use_id,
                # Dedicated pairing key: the manager's frame envelope
                # (frame_seq.stamp_frame) OVERWRITES ``id`` with
                # ``chat_id:seq`` before fan-out, so the UI can never pair
                # tool_call↔tool_result via ``id`` — every tool block was
                # stuck on "running…" forever. ``tool_use_id`` survives the
                # stamp untouched.
                "tool_use_id": block.tool_use_id,
                "tool": block.tool_use_id,
                "result": result,
                # The SDK already knows whether the tool failed; forwarding it
                # spares the client a guess. Without this the UI fell back to
                # sniffing the payload for a leading "error"/"traceback", so a
                # real failure whose text starts anywhere else ("Catalog Error:
                # Table … does not exist") rendered with a success tick.
                "is_error": bool(getattr(block, "is_error", False)),
            }
        )

    collected_text: list[str] = []
    tokens_in = 0
    tokens_out = 0
    model = ""
    # Per-turn tool-call budget: count tool_call emissions; on
    # overflow emit a confirmation_required frame and break the loop
    # so the agent pauses until the next user_msg (which counts as
    # confirmation). Safety net against runaway tool chains.
    tool_calls_this_turn = 0
    budget_hit = False
    # Text streamed via StreamEvent deltas this turn. Non-empty ⇒ the
    # completed TextBlock repeats text the user has already seen, so its
    # whole-block token frame is suppressed (it still feeds collected_text
    # for the turn-end assistant_message). Also the fallback content source
    # should an SDK build stream deltas without a final consolidated
    # TextBlock — otherwise the live UI would show the answer but the
    # persisted assistant_message would be empty.
    streamed_pieces: list[str] = []

    # Idle watchdog: a tool call that never returns (e.g. an in-sandbox
    # `agnes pull` blocked on network) wedged the turn FOREVER — the SDK's
    # per-tool cap was never implemented for the real-agent path (Phase
    # 12.2 TODO), so the user stared at "running…" indefinitely. If the
    # agent stream produces NO message for this long, interrupt the turn
    # and surface an error frame instead. Generous default: silence is
    # normal while a model generates a long block (no partial streaming on
    # older-template SDKs), so this is a wedge-breaker, not a latency cap.
    idle_seconds = float(os.environ.get("AGNES_TURN_IDLE_SECONDS", "300") or "300")
    # The watchdog polls in slices instead of arming one long timeout, so
    # that time the user spends deciding on an approval can be excluded from
    # the idle budget rather than counted into it (review finding on #1145):
    # with a single window, a user who deliberates for most of it left the
    # approved tool only the remainder before a false "stuck tool" abort.
    idle_poll = min(idle_seconds, 5.0)
    stream = client.receive_response().__aiter__()
    # The in-flight __anext__ is kept across laps and shielded: asyncio's
    # wait_for CANCELS its argument on timeout, and a cancellation delivered
    # while an async generator is suspended at an await CLOSES it — the next
    # __anext__ then raises StopAsyncIteration and the turn would end
    # silently, which is exactly what the approval path above would have
    # triggered (review finding on #1145; verified against a plain async
    # generator).
    pending: "asyncio.Future | None" = None
    deadline = time.monotonic() + idle_seconds
    while True:
        if pending is None:
            pending = asyncio.ensure_future(stream.__anext__())
        try:
            msg = await asyncio.wait_for(asyncio.shield(pending), timeout=idle_poll)
            pending = None
            deadline = time.monotonic() + idle_seconds
        except StopAsyncIteration:
            pending = None
            break
        except asyncio.TimeoutError:
            # A tool call suspended on a human approval or question produces
            # no stream activity by design — that is NOT a wedged tool, and
            # the time spent deciding must not eat the budget the approved
            # tool then needs. Each gate's own timeout
            # (AGNES_APPROVAL_TIMEOUT_SECONDS) bounds the wait and resolves
            # to deny, after which a genuinely-idle turn trips this watchdog
            # normally.
            if (gate is not None and gate.awaiting_approval()) or (
                question_gate is not None and question_gate.awaiting_answer()
            ):
                deadline = time.monotonic() + idle_seconds
                continue
            if time.monotonic() < deadline:
                continue
            _emit(
                {
                    "type": "error",
                    "kind": "turn_idle_timeout",
                    "message": (
                        f"no agent activity for {int(idle_seconds)}s; "
                        "interrupting the turn (a tool call is likely stuck)"
                    ),
                }
            )
            # Persist whatever the turn already produced: the `done` frame
            # the outer loop emits next clears the manager's turn buffer,
            # so without this the partial text the user already saw would
            # vanish from history (parity with the crash path's
            # partial-save).
            partial = "\n\n".join(t for t in collected_text if t.strip()) or "".join(streamed_pieces)
            if partial:
                _emit(
                    {
                        "type": "assistant_message",
                        "content": partial,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "model": model,
                    }
                )
            try:
                await client.interrupt()
            except Exception as exc:  # noqa: BLE001 — watchdog must not crash the runner
                print(f"idle-watchdog interrupt failed: {exc}", file=sys.stderr, flush=True)
            # Bounded drain: the interrupt winds the stream down with a
            # tail (typically ending in a ResultMessage). Abandoning the
            # generator un-drained would leave that stale tail buffered on
            # the shared transport, and the NEXT turn's receive_response()
            # could consume it and terminate before the real answer.
            # Bounded per message so a truly wedged stream can't re-wedge
            # the watchdog itself.
            # Bound the drain as a WHOLE, not per message: the stream we are
            # draining is by definition the one that just wedged, so a
            # per-message budget is time the user waits before their next
            # message is served. Short enough not to hold the chat, long
            # enough for the interrupt tail that normally arrives at once
            # (review finding on #1145).
            drain_deadline = time.monotonic() + _WEDGE_DRAIN_SECONDS
            try:
                while True:
                    left = drain_deadline - time.monotonic()
                    if left <= 0:
                        break
                    # Reuse the in-flight __anext__ rather than starting a
                    # second one: two concurrent __anext__ calls on the same
                    # iterator are an error, and this path is reached with
                    # one still pending by construction.
                    if pending is None:
                        pending = asyncio.ensure_future(stream.__anext__())
                    await asyncio.wait_for(asyncio.shield(pending), timeout=left)
                    pending = None
            except (StopAsyncIteration, asyncio.TimeoutError):
                pass
            except Exception as exc:  # noqa: BLE001 — drain is best-effort
                print(f"idle-watchdog drain ended with: {exc}", file=sys.stderr, flush=True)
            finally:
                # Abandoning the turn here is deliberate; don't leave the
                # shielded task dangling for the loop to complain about.
                if pending is not None:
                    pending.cancel()
                    pending = None
            break
        if budget_hit:
            break
        if StreamEvent is not None and isinstance(msg, StreamEvent):
            # Token-level delta straight off the model stream. Only
            # top-level assistant text — subagent/tool-side streams
            # carry parent_tool_use_id and stay internal.
            if getattr(msg, "parent_tool_use_id", None) is None:
                piece = _stream_event_delta_text(msg.event)
                if piece:
                    _emit({"type": "token", "text": piece})
                    streamed_pieces.append(piece)
            continue
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if not streamed_pieces:
                        _emit({"type": "token", "text": block.text})
                    collected_text.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    if tool_calls_this_turn >= tool_calls_per_turn:
                        _emit(
                            {
                                "type": "confirmation_required",
                                "reason": "tool_call_budget",
                                "budget": tool_calls_per_turn,
                            }
                        )
                        budget_hit = True
                        break
                    # ``tool_use_id`` is the per-call pairing key —
                    # echoed back verbatim by ``ToolResultBlock`` so
                    # the frontend can pair a tool_call with its
                    # result even when several calls to the same tool
                    # are in flight. It rides its own key because the
                    # manager's frame envelope overwrites ``id`` with
                    # ``chat_id:seq`` (see _emit_tool_result). ``tool``
                    # carries the human-readable name for the inline
                    # block header.
                    _emit(
                        {
                            "type": "tool_call",
                            "id": block.id,
                            "tool_use_id": block.id,
                            "tool": block.name,
                            "args": block.input,
                        }
                    )
                    tool_calls_this_turn += 1
                elif isinstance(block, ToolResultBlock):
                    _emit_tool_result(block)
            model = msg.model
            if msg.usage:
                tokens_in += msg.usage.get("input_tokens", 0)
                tokens_out += msg.usage.get("output_tokens", 0)

        elif isinstance(msg, UserMessage):
            # The SDK feeds tool results back as a UserMessage carrying
            # ToolResultBlock(s) — NOT inside the AssistantMessage. Without
            # handling this branch the runner never emits a tool_result
            # frame, so the inline tool block in the UI is stuck on
            # "running…" forever even though the tool finished.
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        _emit_tool_result(block)

        elif isinstance(msg, ResultMessage):
            if msg.usage:
                tokens_in = msg.usage.get("input_tokens", tokens_in)
                tokens_out = msg.usage.get("output_tokens", tokens_out)
            # ResultMessage signals turn end; receive_response() stops after it.
            # Content prefers the consolidated TextBlocks (canonical);
            # falls back to the streamed deltas should an SDK build omit
            # the final block under partial streaming. Blocks are joined
            # with a blank line: each TextBlock is a prose segment
            # bracketing tool calls, and a bare "".join squashed segment
            # boundaries into mid-word runs ("…znovu:Z MCP pull…" in the
            # persisted history). Deltas keep "" — they are sub-block
            # fragments of one segment.
            _emit(
                {
                    "type": "assistant_message",
                    "content": "\n\n".join(t for t in collected_text if t.strip()) or "".join(streamed_pieces),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": model,
                }
            )


async def _start_relay() -> int:
    """Start the module-level loopback relay and rewrite this process's env
    so every subsequently-spawned CLI/MCP subprocess (``claude``, ``agnes``,
    ``agnes mcp``) talks to the relay with a dummy key instead of the real
    Anthropic/Agnes endpoints with a real credential.

    Must run before any such subprocess is spawned. Captures the real
    ``AGNES_SERVER`` value to construct the ``Relay`` *before* overwriting it
    below — the relay itself still needs the real server URL to forward
    brokered requests to.
    """
    global _relay
    # Lazy import: the `app` package only exists after _install_agnes_cli()
    # has pip-installed the uploaded wheel, so this MUST run after that step
    # (see amain()). Importing at module scope crashed the runner at startup.
    from app.chat.relay import Relay

    real_server = os.environ.get("AGNES_SERVER", "").strip()
    _relay = Relay(server_url=real_server)
    port = await _relay.start()
    # Keep the real address reachable by name for the things that need the
    # HOST rather than an API base: the egress allowlist in
    # `.claude/hooks/pre_tool_use.py` derives its Agnes host from the
    # environment, and it runs *after* this rewrite — so it was reading
    # `127.0.0.1` and never adding the real host, which is what made a
    # `git clone` of a data-app repo from the sandbox fail while the MCP
    # tools (which go through this very relay on loopback) kept working.
    # Written before the overwrite so the two can never disagree.
    # (Devin Review on this PR.)
    if real_server:
        os.environ["AGNES_REAL_SERVER"] = real_server
    os.environ["AGNES_SERVER"] = f"http://127.0.0.1:{port}/agnes-api"
    # The relay's ORIGIN, without the `/agnes-api` path. `data-apps.git` is
    # served off the relay root, not under the API prefix, so the data-apps
    # skill tells the agent to clone from `$AGNES_SERVER_BASE/data-apps.git/…`
    # — and that variable existed only in the skill's prose: the command as
    # printed expanded to an empty base and cloned from `/data-apps.git/<slug>`.
    # Set here rather than derived in the skill so there is one definition of
    # where the relay lives. (Devin Review on #1252.)
    os.environ["AGNES_SERVER_BASE"] = f"http://127.0.0.1:{port}"
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}/anthropic"
    os.environ["ANTHROPIC_API_KEY"] = "sk-dummy-broker"
    # Vertex mode (AGNES_LLM_PROVIDER=vertex, set by the manager from
    # chat.llm.provider): flip the CLI into Claude Code's documented
    # LLM-gateway pattern for Vertex — it then emits native Vertex request
    # shapes (…/publishers/anthropic/models/<model>:streamRawPredict) with NO
    # auth headers, still egressing through the relay's /anthropic leg; the
    # broker validates project/region server-side and attaches the Google
    # OAuth token there. The ANTHROPIC_BASE_URL/API_KEY rewrites above stay
    # in place as defense in depth for any non-vertex-aware in-sandbox tool
    # (the CLI ignores them once CLAUDE_CODE_USE_VERTEX is set).
    if os.environ.get("AGNES_LLM_PROVIDER", "").strip().lower() == "vertex":
        os.environ["CLAUDE_CODE_USE_VERTEX"] = "1"
        os.environ["ANTHROPIC_VERTEX_BASE_URL"] = f"http://127.0.0.1:{port}/anthropic"
        os.environ["CLAUDE_CODE_SKIP_VERTEX_AUTH"] = "1"
        os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] = os.environ.get("AGNES_VERTEX_PROJECT_ID", "")
        os.environ["CLOUD_ML_REGION"] = os.environ.get("AGNES_VERTEX_REGION", "")
    return port


DEFAULT_HARNESS = "claude-code"


def _harness_registry() -> dict:
    """id → session-loop registry (the runner-side half of the
    ``app/chat/harness.py`` seam — this file runs standalone in the
    sandbox, so the registry lives here, not there)."""
    return {"claude-code": _real_agent_loop}


def _select_harness(requested: "str | None"):
    """Resolve ``AGNES_HARNESS`` to a session loop.

    Unknown ids degrade to the default with a stderr warning (an
    *inherited* invalid choice must never hard-crash a version-skewed
    sandbox); the server-side boot gate is where an *explicitly
    configured* invalid id refuses (``app/main.py::_chat_harness_ok``).
    """
    registry = _harness_registry()
    harness_id = requested or DEFAULT_HARNESS
    loop_fn = registry.get(harness_id)
    if loop_fn is None:
        print(
            f"unknown AGNES_HARNESS {harness_id!r}; falling back to {DEFAULT_HARNESS!r}",
            file=sys.stderr,
            flush=True,
        )
        loop_fn = registry[DEFAULT_HARNESS]
    return loop_fn


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.parse_args()  # validates required --session-id; value read from env

    workdir = Path(os.environ.get("AGNES_WORKDIR", os.getcwd()))

    fake_agent = os.environ.get("AGNES_RUNNER_FAKE_AGENT") == "1"

    # Install the agnes CLI BEFORE handing fd 0 to asyncio (_stdin_lines calls
    # connect_read_pipe, which puts stdin in non-blocking mode). Running the
    # pip subprocess after that wedges the asyncio stdin reader — user_msg
    # frames then never arrive and the agent hangs with no response. Doing it
    # here, before the reader is attached, keeps fd 0 a plain blocking pipe for
    # the duration of the install; the client's first user_msg simply buffers
    # in the OS pipe until _stdin_lines() starts reading. Skipped in fake-agent
    # mode (tests) — there is no wheel to install.
    if not fake_agent:
        # Install the agnes CLI wheel FIRST: it ships the `app` package that
        # _start_relay() imports (`from app.chat.relay import Relay`). Ordering
        # it after _start_relay crashed the relay import with ModuleNotFoundError
        # (the `app` package doesn't exist in the sandbox until this install).
        # Safe to run before the relay: the install is an offline
        # `pip install --no-deps <uploaded wheel>` — it hits no network and
        # needs no broker. The relay only has to be up before the AGENT
        # subprocess spawns (`claude`, `agnes mcp`), which come further below.
        _install_agnes_cli()
        # Now start the in-sandbox loopback relay and repoint this process's env
        # at it, before any CLI/MCP agent subprocess spawn — the real agent
        # loop's `claude` spawn and `_agnes_mcp_servers()`'s `agnes mcp` spawn
        # must see the rewritten AGNES_SERVER / ANTHROPIC_* env.
        await _start_relay()
        # Barrier: the workspace tree must be fully in /work before anything
        # reads or writes it — the marketplace bootstrap writes project-level
        # plugin state a late-finishing workspace extraction would clobber,
        # and the agent CLI (spawned eagerly by _real_agent_loop's `async
        # with`) loads CLAUDE.md/.claude from /work at boot. The wheel install
        # above deliberately does NOT gate on this — it overlaps the upload.
        await _wait_workspace_ready()
        # Register the marketplace the server shipped in the workspace, so the
        # user's stack plugins load with everything they contain — skills,
        # agents, slash commands, hooks, MCP servers. Offline by construction
        # (a local directory), which is what the old design got wrong: it ran
        # `agnes refresh-marketplace --bootstrap` here to CLONE the marketplace,
        # impossible from inside a sandbox (the git endpoint is PAT-gated, the
        # sandbox deliberately holds no PAT, and the relay routes no marketplace
        # prefix), so it 401'd behind a `check=False` subprocess (#1552). After
        # the CLI install; before the reader attaches, for the same fd-0 reason.
        _register_workspace_marketplace(workdir)

    _emit({"type": "runner_ready"})
    queue = await _stdin_lines()

    per_tool = float(os.environ.get("AGNES_PER_TOOL_CALL_SECONDS", "90"))
    tool_calls_per_turn = int(os.environ.get("AGNES_TOOL_CALLS_PER_TURN", "50"))
    gate = ApprovalGate(
        _emit,
        workdir / ".claude" / "hooks" / "pre_tool_use.py",
        enabled=os.environ.get("AGNES_APPROVALS", "on").lower() not in ("off", "0", "false"),
        timeout_seconds=float(os.environ.get("AGNES_APPROVAL_TIMEOUT_SECONDS", "300")),
    )
    # Deliberately shares the approval timeout knob: both bound "how long a
    # tool call may wait on a human" and a second env var would just drift.
    question_gate = QuestionGate(
        _emit,
        timeout_seconds=float(os.environ.get("AGNES_APPROVAL_TIMEOUT_SECONDS", "300")),
    )
    if fake_agent:
        await _fake_agent_loop(
            queue,
            per_tool_seconds=per_tool,
            tool_calls_per_turn=tool_calls_per_turn,
            gate=gate,
            question_gate=question_gate,
        )
    else:
        loop_fn = _select_harness(os.environ.get("AGNES_HARNESS"))
        try:
            await loop_fn(
                queue,
                workdir,
                tool_calls_per_turn=tool_calls_per_turn,
                gate=gate,
                question_gate=question_gate,
            )
        except Exception as exc:
            _emit({"type": "error", "kind": "runner_exception", "message": str(exc)})
            raise


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
