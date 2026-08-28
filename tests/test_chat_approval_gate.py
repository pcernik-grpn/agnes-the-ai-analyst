"""ApprovalGate — the runner's in-process PreToolUse gate that makes the
workspace hook's ``ask`` verdicts real in cloud chat.

Unit tests drive ``ApprovalGate.check`` directly (asyncio.run per project
convention); the final test drives the full stdin round-trip through a
fake-agent runner subprocess (``__approval__:`` trigger).
"""

import asyncio
import json
import os
import pathlib
import sys

import pytest
from pathlib import Path

from app.chat.runner import ApprovalGate

_PROJECT_ROOT = str(Path(__file__).parent.parent)

# Verdict fixture hook: deny for "denyme", ask for "askme", allow otherwise.
_HOOK_SRC = """\
import json, sys
p = json.loads(sys.stdin.read() or "{}")
cmd = (p.get("tool_input") or {}).get("command", "")
if "denyme" in cmd:
    print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": "nope"}))
elif "askme" in cmd:
    print(json.dumps({"permissionDecision": "ask", "permissionDecisionReason": "needs approval"}))
else:
    print(json.dumps({"permissionDecision": "allow"}))
"""


def _write_hook(tmp_path: Path, src: str = _HOOK_SRC) -> Path:
    hook = tmp_path / ".claude" / "hooks" / "pre_tool_use.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(src)
    return hook


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def _decision_of(out: dict) -> str | None:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def test_file_hook_deny_passes_through(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path))
        out = await gate.check(_bash("denyme"), None, {})
        assert _decision_of(out) == "deny"
        assert "nope" in out["hookSpecificOutput"]["permissionDecisionReason"]
        assert emitted == []  # no approval round-trip for a deny

    asyncio.run(_run())


def test_file_hook_allow_yields_no_opinion(tmp_path):
    async def _run():
        gate = ApprovalGate(lambda f: None, _write_hook(tmp_path))
        assert await gate.check(_bash("ls"), None, {}) == {}

    asyncio.run(_run())


def test_missing_and_broken_hooks_yield_no_opinion(tmp_path):
    async def _run():
        gate = ApprovalGate(lambda f: None, tmp_path / "nope.py")
        assert await gate.check(_bash("askme"), None, {}) == {}
        broken = _write_hook(tmp_path, "print('this is not json')")
        gate2 = ApprovalGate(lambda f: None, broken)
        assert await gate2.check(_bash("askme"), None, {}) == {}

    asyncio.run(_run())


def test_ask_allow_roundtrip(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme now"), None, {}))
        # wait for the request frame
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        req = emitted[0]
        assert req["type"] == "approval_request"
        assert req["reason"] == "needs approval"
        assert req["command"] == "askme now"
        assert gate.resolve(req["request_id"], "allow") is True
        out = await task
        assert _decision_of(out) == "allow"
        resolved = [f for f in emitted if f["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "allow"

    asyncio.run(_run())


def test_allow_session_dedupes_same_command_only(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme now"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "allow_session")
        assert _decision_of(await task) == "allow"
        # IDENTICAL command: allowed immediately, no second request frame
        out2 = await gate.check(_bash("askme now"), None, {})
        assert _decision_of(out2) == "allow"
        assert len([f for f in emitted if f["type"] == "approval_request"]) == 1

    asyncio.run(_run())


def test_allow_session_does_not_leak_across_commands(tmp_path):
    """Dedup is command-keyed, not reason-keyed: approving one command of a
    family must NOT pre-approve a different command sharing the hook's
    reason string (review finding on #1145)."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme grant"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "allow_session")
        assert _decision_of(await task) == "allow"
        # DIFFERENT command, SAME hook reason ("needs approval"): a fresh
        # request must be raised, not silently pre-approved
        task2 = asyncio.create_task(gate.check(_bash("askme delete"), None, {}))
        for _ in range(100):
            if len([f for f in emitted if f["type"] == "approval_request"]) == 2:
                break
            await asyncio.sleep(0.01)
        reqs = [f for f in emitted if f["type"] == "approval_request"]
        assert len(reqs) == 2
        assert gate.awaiting_approval() is True
        gate.resolve(reqs[1]["request_id"], "deny")
        assert _decision_of(await task2) == "deny"

    asyncio.run(_run())


def test_awaiting_approval_reflects_pending(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        assert gate.awaiting_approval() is False
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        assert gate.awaiting_approval() is True  # suspended on the human
        gate.resolve(emitted[0]["request_id"], "allow")
        await task
        assert gate.awaiting_approval() is False

    asyncio.run(_run())


def test_user_deny_denies_tool(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "deny")
        out = await task
        assert _decision_of(out) == "deny"
        assert "denied" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()

    asyncio.run(_run())


def test_timeout_denies(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=0.05)
        out = await gate.check(_bash("askme"), None, {})
        assert _decision_of(out) == "deny"
        assert "timed out" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()
        resolved = [f for f in emitted if f["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "timeout"

    asyncio.run(_run())


def test_unattended_denies_with_an_actionable_message(tmp_path):
    """The manager resolves a request nobody can answer with `unattended`
    (agent-API one-shot). It denies like a timeout but says WHY and what to
    do instead — the runner's message is the only thing the calling agent
    sees."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=30)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        assert gate.resolve(emitted[0]["request_id"], "unattended") is True
        out = await task
        assert _decision_of(out) == "deny"
        why = out["hookSpecificOutput"]["permissionDecisionReason"].lower()
        assert "agent api" in why and "run the command themselves" in why
        resolved = [f for f in emitted if f["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "unattended"

    asyncio.run(_run())


def test_disabled_gate_denies_ask_without_prompting(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), enabled=False)
        out = await gate.check(_bash("askme"), None, {})
        assert _decision_of(out) == "deny"
        assert emitted == []

    asyncio.run(_run())


def test_cancel_all_denies_pending(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.cancel_all()
        out = await task
        assert _decision_of(out) == "deny"

    asyncio.run(_run())


def test_invalid_decision_hardens_to_deny(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_bash("askme"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(emitted[0]["request_id"], "totally-bogus")
        assert _decision_of(await task) == "deny"

    asyncio.run(_run())


def test_runner_subprocess_roundtrip(tmp_path):
    """Full stdin round-trip through the fake-agent runner: __approval__:
    trigger → approval_request frame out → approval_decision in →
    gate:allow assistant message. Also proves a stale decision id is
    dropped without crashing the frame loop."""

    async def _run():
        _write_hook(tmp_path)
        env = os.environ.copy()
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"
        env["AGNES_SESSION_ID"] = "chat_appr"
        env["AGNES_APPROVAL_TIMEOUT_SECONDS"] = "10"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.chat.runner",
            "--session-id",
            "chat_appr",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        try:
            line = await asyncio.wait_for(proc.stdout.readline(), 10)
        except TimeoutError:
            proc.kill()
            err = (await proc.stderr.read())[:2000].decode(errors="replace")
            raise AssertionError(f"no runner_ready frame; runner stderr: {err}") from None
        assert json.loads(line) == {"type": "runner_ready"}

        # stale decision for an unknown request: must be silently dropped
        proc.stdin.write(
            (json.dumps({"type": "approval_decision", "request_id": "ghost", "decision": "allow"}) + "\n").encode()
        )
        proc.stdin.write((json.dumps({"type": "user_msg", "text": "__approval__:askme now"}) + "\n").encode())
        await proc.stdin.drain()

        try:
            req = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
        except TimeoutError:
            proc.kill()
            err = (await proc.stderr.read())[:2000].decode(errors="replace")
            raise AssertionError(f"no approval_request frame; runner stderr: {err}") from None
        assert req["type"] == "approval_request"
        assert req["reason"] == "needs approval"

        proc.stdin.write(
            (
                json.dumps({"type": "approval_decision", "request_id": req["request_id"], "decision": "allow"}) + "\n"
            ).encode()
        )
        await proc.stdin.drain()

        frames = []
        for i in range(3):
            try:
                frames.append(json.loads(await asyncio.wait_for(proc.stdout.readline(), 10)))
            except TimeoutError:
                proc.kill()
                raise AssertionError(f"missing frame {i + 1}/3; got so far: {frames}") from None
        types = [f["type"] for f in frames]
        assert "approval_resolved" in types
        msg = next(f for f in frames if f["type"] == "assistant_message")
        assert msg["content"] == "gate:allow"

        proc.stdin.close()
        try:
            rc = await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            proc.kill()
            raise AssertionError("runner did not exit after stdin EOF") from None
        assert rc == 0

    asyncio.run(_run())


def test_cancelled_anext_closes_a_plain_async_generator():
    """Why the turn loop keeps a shielded in-flight ``__anext__``.

    ``asyncio.wait_for`` cancels its argument on timeout, and a cancellation
    delivered while an async generator is suspended at an ``await`` closes
    it — the next ``__anext__`` then raises ``StopAsyncIteration``. The turn
    loop would read that as "stream finished" and end silently, which is
    exactly the path an approval longer than one poll slice takes. This
    pins the language behavior the shield exists for, so the mitigation is
    not quietly removed later.
    """
    import asyncio

    async def gen():
        yield "a"
        await asyncio.sleep(5)
        yield "b"

    async def drive():
        it = gen().__aiter__()
        assert await asyncio.wait_for(it.__anext__(), timeout=1) == "a"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(it.__anext__(), timeout=0.05)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(it.__anext__(), timeout=1)

    asyncio.run(drive())


def test_shielded_pending_anext_survives_repeated_timeouts():
    """The pattern the turn loop uses: the generator stays alive across polls."""
    import asyncio

    async def gen():
        yield "a"
        await asyncio.sleep(0.4)
        yield "b"

    async def drive():
        it = gen().__aiter__()
        pending = None
        got = []
        for _ in range(40):
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            try:
                got.append(await asyncio.wait_for(asyncio.shield(pending), timeout=0.05))
                pending = None
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                continue
        assert got == ["a", "b"], got

    asyncio.run(drive())


def test_file_hook_accepts_the_nested_verdict_shape(tmp_path, monkeypatch):
    """Claude Code allows the verdict nested under hookSpecificOutput.

    The bundled hook emits the flat shape, but an operator override written
    against the nested spec shape must not read as "no opinion" — its
    ask/deny rules would be silently inert.
    """
    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text(
        "import json,sys\n"
        'print(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",'
        ' "permissionDecisionReason": "nope"}}))\n'
    )
    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._hook_path = hook
    out = gate.run_file_hook({"tool_name": "Bash", "tool_input": {"command": "x"}})
    assert out.get("permissionDecision") == "deny"
    assert out.get("permissionDecisionReason") == "nope"


def test_file_hook_flat_shape_still_wins(tmp_path):
    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'allow'}))\n")
    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._hook_path = hook
    assert gate.run_file_hook({"tool_name": "Bash"}).get("permissionDecision") == "allow"


def test_gate_disables_itself_when_hookmatcher_takes_no_timeout():
    """Without a matcher timeout the gate cannot guarantee it blocks.

    The gate's own `asyncio.wait_for` bounds only the gate's wait, not the
    CLI's. If the CLI timed the PreToolUse hook out and treated it as
    non-blocking, the tool would run while a human was still being asked and
    their decision would land after the fact — the barrier silently becoming
    a delay. Denying is the only honest option, so the fallback must fail
    closed rather than arm an unenforceable gate.
    """
    import app.chat.runner as runner

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._enabled = True
    gate._disabled_reason = ""
    gate._session_approved = set()
    gate.disable_unsupported("HookMatcher takes no timeout")
    assert gate._enabled is False
    # The recorded reason must reach the agent, so it cannot relay the
    # wrong explanation (e.g. "this session was not started in web chat").
    assert "timeout" in gate._disabled_reason


def test_installed_sdk_hookmatcher_supports_timeout():
    """The fallback above must stay unreachable on a supported SDK.

    If a future pin lands on a build without `timeout`, approvals would
    silently switch to deny-everything — better to fail here.
    """
    import dataclasses

    from claude_agent_sdk import HookMatcher

    assert "timeout" in [f.name for f in dataclasses.fields(HookMatcher)], (
        "installed claude-agent-sdk HookMatcher has no timeout field; the approval gate "
        "will refuse to arm (see ApprovalGate.disable_unsupported)"
    )


def test_disabled_gate_is_still_registered_so_it_can_deny(tmp_path):
    """A disabled gate must still be wired into the SDK, or it denies nothing.

    Setting `_enabled = False` only produces a deny if the hook is actually
    called. Skipping registration on an SDK whose HookMatcher takes no
    timeout left nothing to deny — ask-flagged commands ran unasked, the
    exact behavior this feature removes. A disabled gate answers instantly,
    so there is no wait for a CLI-side hook timeout to cut short.
    """
    import app.chat.runner as runner

    import asyncio

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'ask', 'permissionDecisionReason': 'risky'}))\n")

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._enabled = False
    gate._disabled_reason = "SDK too old"
    gate._session_approved = set()
    gate._hook_path = hook

    res = asyncio.run(gate.check({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, None, None))
    hso = res.get("hookSpecificOutput", res)
    assert hso.get("permissionDecision") == "deny", res
    assert "SDK too old" in (hso.get("permissionDecisionReason") or ""), res


def test_cancelled_approval_does_not_leak_into_pending(tmp_path):
    """A cancelled wait must not pin awaiting_approval() to True forever.

    The turn watchdog reads awaiting_approval() as "a human is deciding" and
    skips the stuck-tool abort. A future left in _pending by a cancellation
    (Stop, turn teardown) therefore disables the watchdog for the rest of
    the session, and a genuinely stuck tool hangs it for good.
    """
    import asyncio
    import contextlib

    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'ask', 'permissionDecisionReason': 'r'}))\n")

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate._enabled = True
    gate._disabled_reason = ""
    gate._session_approved = set()
    gate._pending = {}
    gate._counter = 0
    gate._hook_path = hook
    gate.timeout_seconds = 30
    emitted: list[dict] = []
    gate._emit = emitted.append

    async def drive():
        task = asyncio.create_task(
            gate.check({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, None, None)
        )
        # let it register the pending future
        for _ in range(50):
            await asyncio.sleep(0.01)
            if gate._pending:
                break
        assert gate._pending, "the gate never registered a pending approval"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert gate._pending == {}, "a cancelled approval leaked its future"
        assert gate.awaiting_approval() is False
        # …and the card must be retired, or the manager keeps replaying it.
        resolved = [f for f in emitted if f.get("type") == "approval_resolved"]
        assert resolved and resolved[-1]["decision"] == "cancelled", emitted

    asyncio.run(drive())


def test_request_ids_are_unique_across_gate_instances(tmp_path):
    """A respawned sandbox must not mint ids the chat window already knows.

    The old scheme was pid+counter; a fresh sandbox restarts the counter at
    zero and can be handed the same pid, so a new prompt could reuse an id.
    The client dedups cards by request_id, so that prompt was never drawn
    and the command hung until the approval window expired.
    """
    import asyncio

    import app.chat.runner as runner

    hook = tmp_path / "hook.py"
    hook.write_text("import json\nprint(json.dumps({'permissionDecision': 'ask', 'permissionDecisionReason': 'r'}))\n")

    seen: list[str] = []

    def make_gate():
        g = runner.ApprovalGate.__new__(runner.ApprovalGate)
        g._enabled = True
        g._disabled_reason = ""
        g._session_approved = set()
        g._pending = {}
        g._counter = 0          # a fresh sandbox restarts it at zero
        g._hook_path = hook
        g.timeout_seconds = 0.05  # resolve fast; we only want the id
        g._emit = lambda frame: (
            seen.append(frame["request_id"]) if frame.get("type") == "approval_request" else None
        )
        return g

    async def drive():
        for _ in range(3):      # three "sandbox lifetimes"
            gate = make_gate()
            for _ in range(2):
                await gate.check({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}, None, None)

    asyncio.run(drive())
    assert len(seen) == 6, seen
    assert len(set(seen)) == 6, f"request ids collided across gate instances: {seen}"


# ── MCP tools: approval routed by the tool's own annotations ──────────────
#
# The gate used to match `Bash` only, so every mutating MCP tool
# (`data_app_delete_draft`, `data_app_deploy`, `pull`, …) executed without
# the confirmation round-trip its own contract asks for. Routing is by
# `readOnlyHint`, never by tool name: a tool nobody has classified counts as
# mutating (security finding llm-agency-mcp-approval-2).


def _mcp(tool: str, tool_input: dict | None = None) -> dict:
    return {"tool_name": tool, "tool_input": tool_input or {}}


def _await_request(emitted: list) -> dict:
    return [f for f in emitted if f["type"] == "approval_request"][0]


def test_read_only_mcp_tool_runs_without_approval(tmp_path):
    """A `readOnlyHint=True` tool must not cost the user a click."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        for tool in ("mcp__agnes__catalog", "mcp__agnes__query", "mcp__agnes__schema"):
            assert await gate.check(_mcp(tool, {"sql": "SELECT 1"}), None, {}) == {}
        assert emitted == []

    asyncio.run(_run())


def test_mutating_mcp_tool_requires_approval(tmp_path):
    """A non-read-only MCP tool takes the same round-trip as an ask-flagged
    Bash command — emit, suspend, resolve on the user's decision."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(
            gate.check(_mcp("mcp__agnes__data_app_delete_draft", {"slug": "demo"}), None, {})
        )
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        req = _await_request(emitted)
        assert req["tool"] == "mcp__agnes__data_app_delete_draft"
        # The card renders `command` and nothing else names the call, so it
        # must carry both the tool and the arguments the user is judging.
        assert "data_app_delete_draft" in req["command"] and "demo" in req["command"]
        assert "data_app_delete_draft" in req["reason"]
        assert gate.awaiting_approval() is True
        gate.resolve(req["request_id"], "allow")
        assert _decision_of(await task) == "allow"

    asyncio.run(_run())


def test_user_deny_denies_a_mutating_mcp_tool(tmp_path):
    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "d"}), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(_await_request(emitted)["request_id"], "deny")
        out = await task
        assert _decision_of(out) == "deny"
        assert "denied" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()

    asyncio.run(_run())


def test_unknown_mcp_tool_is_treated_as_mutating(tmp_path):
    """No annotation found = mutating. A tool added after this runner was
    built (or a per-caller passthrough tool, which carries no annotation at
    all) must fail closed, never silently pass."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_mcp("mcp__agnes__tool_from_the_future"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        assert _await_request(emitted)["tool"] == "mcp__agnes__tool_from_the_future"
        gate.resolve(_await_request(emitted)["request_id"], "deny")
        assert _decision_of(await task) == "deny"

    asyncio.run(_run())


def test_a_foreign_mcp_server_cannot_borrow_an_agnes_read_only_name(tmp_path):
    """The allowlist is keyed on (server, tool), not the bare tool name.

    A workspace-configured MCP server is outside Agnes' control — and the
    agent can write the workspace's own `.mcp.json` — so a server that names
    its write tool `catalog` must not inherit Agnes' read-only verdict.
    """

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_mcp("mcp__notagnes__catalog"), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        req = _await_request(emitted)
        assert req["tool"] == "mcp__notagnes__catalog"
        # …and the card says which server, so `catalog` cannot read as Agnes'.
        assert "notagnes.catalog" in req["command"]
        gate.resolve(_await_request(emitted)["request_id"], "deny")
        assert _decision_of(await task) == "deny"

    asyncio.run(_run())


def test_disabled_gate_denies_a_mutating_mcp_tool_without_prompting(tmp_path):
    """The SDK-fallback path (HookMatcher without `timeout`) disables the
    gate. A disabled gate cannot block safely, so mutating MCP tools are
    DENIED — never silently allowed — exactly as ask-flagged Bash is."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), enabled=False)
        gate.disable_unsupported("SDK too old")
        out = await gate.check(_mcp("mcp__agnes__data_app_delete_draft", {"slug": "d"}), None, {})
        assert _decision_of(out) == "deny"
        assert "SDK too old" in out["hookSpecificOutput"]["permissionDecisionReason"]
        assert emitted == []
        # …and a read-only tool still runs: a disabled gate is not a kill
        # switch for reads.
        assert await gate.check(_mcp("mcp__agnes__catalog"), None, {}) == {}

    asyncio.run(_run())


def test_allow_session_for_an_mcp_tool_does_not_leak_across_arguments(tmp_path):
    """`allow_session` keys on tool + arguments, mirroring the Bash path's
    exact-command key: approving one delete must not pre-approve the next
    one with a different slug."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "a"}), None, {}))
        for _ in range(100):
            if emitted:
                break
            await asyncio.sleep(0.01)
        gate.resolve(_await_request(emitted)["request_id"], "allow_session")
        assert _decision_of(await task) == "allow"
        # identical call → no second card
        out2 = await gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "a"}), None, {})
        assert _decision_of(out2) == "allow"
        assert len([f for f in emitted if f["type"] == "approval_request"]) == 1
        # different arguments → a fresh card
        task3 = asyncio.create_task(gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "b"}), None, {}))
        for _ in range(100):
            if len([f for f in emitted if f["type"] == "approval_request"]) == 2:
                break
            await asyncio.sleep(0.01)
        reqs = [f for f in emitted if f["type"] == "approval_request"]
        assert len(reqs) == 2
        gate.resolve(reqs[1]["request_id"], "deny")
        assert _decision_of(await task3) == "deny"

    asyncio.run(_run())


def test_non_bash_builtin_tools_stay_out_of_the_file_hook(tmp_path):
    """Widening the matcher must not put a per-call subprocess in front of
    every Read/Grep: the file hook allows every non-Bash tool anyway."""

    async def _run():
        hook = tmp_path / "always_ask.py"
        hook.write_text(
            "import json\nprint(json.dumps({'permissionDecision': 'ask', 'permissionDecisionReason': 'r'}))\n"
        )
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, hook, timeout_seconds=5)
        assert await gate.check({"tool_name": "Read", "tool_input": {"file_path": "/x"}}, None, {}) == {}
        assert emitted == []

    asyncio.run(_run())


def test_read_only_allowlist_matches_the_stdio_mcp_server():
    """The allowlist is a copy of `readOnlyHint=True` in the stdio MCP
    server; drift in either direction is a bug.

    A new read-only tool missing here only costs a needless approval card,
    but a tool that FLIPS to mutating and stays listed would keep passing
    unasked — so pin equality, not containment.
    """
    pytest.importorskip("mcp", reason="mcp package not installed")

    import app.chat.runner as runner
    from cli.mcp import server as stdio_server

    tools = asyncio.run(stdio_server.mcp.list_tools())
    read_only = {t.name for t in tools if getattr(t.annotations, "readOnlyHint", None) is True}
    assert read_only == set(runner._READ_ONLY_AGNES_MCP_TOOLS), (
        "app/chat/runner.py::_READ_ONLY_AGNES_MCP_TOOLS drifted from cli/mcp/server.py's "
        "readOnlyHint annotations — the sandbox's approval gate reads the copy"
    )


def test_the_allowlist_is_keyed_on_the_server_name_the_runner_registers():
    """`mcp__<server>__<tool>` — the `<server>` half must be the name
    `_agnes_mcp_servers()` registers, or every Agnes tool reads as foreign
    and the gate asks for approval on every catalog call."""
    import app.chat.runner as runner

    monkey = os.environ.get("AGNES_SERVER")
    os.environ["AGNES_SERVER"] = "http://127.0.0.1:1/agnes-api"
    try:
        servers = runner._agnes_mcp_servers()
    finally:
        if monkey is None:
            os.environ.pop("AGNES_SERVER", None)
        else:
            os.environ["AGNES_SERVER"] = monkey
    assert list(servers) == [runner._AGNES_MCP_SERVER_NAME]


def test_pretool_matchers_cover_both_bash_and_mcp_tools():
    """The gate is wired to see MCP tool calls at all.

    Two disjoint matchers on purpose: `Bash` stays the exact, proven
    matcher, and MCP tools ride their own. One combined alternation would
    put Bash's coverage at the mercy of the MCP pattern.
    """
    import app.chat.runner as runner
    from claude_agent_sdk import HookMatcher

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate.timeout_seconds = 300.0
    matchers = runner._build_pretool_matchers(gate, HookMatcher)
    patterns = [m.matcher for m in matchers]
    assert "Bash" in patterns
    assert any(p and p.startswith("mcp__") for p in patterns), patterns
    for m in matchers:
        assert m.hooks, "a matcher with no callback gates nothing"
        assert m.timeout and m.timeout > gate.timeout_seconds


def test_mcp_matcher_is_still_registered_when_hookmatcher_takes_no_timeout():
    """The fail-closed fallback covers MCP too: the gate is disabled, but
    both matchers stay registered so a mutating MCP tool is DENIED rather
    than run unasked."""
    import app.chat.runner as runner

    class _NoTimeoutHookMatcher:
        def __init__(self, matcher=None, hooks=None):
            self.matcher = matcher
            self.hooks = hooks or []
            self.timeout = None

    gate = runner.ApprovalGate.__new__(runner.ApprovalGate)
    gate.timeout_seconds = 300.0
    gate._enabled = True
    gate._disabled_reason = ""
    matchers = runner._build_pretool_matchers(gate, _NoTimeoutHookMatcher)
    assert gate._enabled is False, "the gate must fail closed when it cannot block safely"
    patterns = [m.matcher for m in matchers]
    assert "Bash" in patterns and any(p and p.startswith("mcp__") for p in patterns), patterns


# ── Review fixes on the MCP routing ───────────────────────────────────────


def test_file_hook_deny_is_honoured_for_an_mcp_tool(tmp_path):
    """An operator's `deny` must be ENFORCED for MCP tools, not downgraded.

    Routing MCP calls straight to the annotation check skipped the workspace
    hook entirely, so an operator who denied an MCP tool got an approval card
    the user could click past — a silent downgrade of a deny to an ask.
    """

    async def _run():
        hook = tmp_path / "deny_mcp.py"
        hook.write_text(
            "import json, sys\n"
            "p = json.loads(sys.stdin.read() or '{}')\n"
            "if str(p.get('tool_name', '')).startswith('mcp__'):\n"
            "    print(json.dumps({'permissionDecision': 'deny', "
            "'permissionDecisionReason': 'blocked by policy'}))\n"
            "else:\n"
            "    print(json.dumps({'permissionDecision': 'allow'}))\n"
        )
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, hook, timeout_seconds=5)
        out = await gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "d"}), None, {})
        assert _decision_of(out) == "deny"
        assert "blocked by policy" in out["hookSpecificOutput"]["permissionDecisionReason"]
        assert emitted == [], "a deny must not cost the user a card"
        # A read-only tool the operator denied is denied too: the annotation
        # decides whether to ASK, never whether to override a policy deny.
        assert _decision_of(await gate.check(_mcp("mcp__agnes__catalog"), None, {})) == "deny"

    asyncio.run(_run())


def test_file_hook_allow_does_not_bypass_the_mcp_annotation_route(tmp_path):
    """The bundled hook answers `allow` for every non-Bash tool, so `allow`
    is "no opinion" here — it must not switch the annotation gate off."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "d"}), None, {}))
        for _ in range(200):
            if emitted:
                break
            await asyncio.sleep(0.01)
        req = _await_request(emitted)
        assert req["tool"] == "mcp__agnes__data_app_deploy"
        gate.resolve(req["request_id"], "deny")
        assert _decision_of(await task) == "deny"

    asyncio.run(_run())


def test_preview_render_directives_run_without_approval(tmp_path):
    """`agnes_data_app_refresh` / `agnes_data_app_close` are pure render
    directives — no server round-trip, nothing changes — and the authoring
    skill calls them many times per turn. An approval card on each one
    breaks the preview loop outright in an UNATTENDED agent-API session,
    where every card resolves to a deny."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        for tool in ("mcp__agnes__agnes_data_app_refresh", "mcp__agnes__agnes_data_app_close"):
            assert await gate.check(_mcp(tool, {"slug": "demo"}), None, {}) == {}
        assert emitted == []

    asyncio.run(_run())


def test_preview_itself_still_asks(tmp_path):
    """`agnes_data_app_preview` mints a scoped preview grant server-side, so
    it stays a write and keeps its card."""

    async def _run():
        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        task = asyncio.create_task(
            gate.check(_mcp("mcp__agnes__agnes_data_app_preview", {"slug": "demo"}), None, {})
        )
        for _ in range(200):
            if emitted:
                break
            await asyncio.sleep(0.01)
        req = _await_request(emitted)
        assert req["tool"] == "mcp__agnes__agnes_data_app_preview"
        gate.resolve(req["request_id"], "deny")
        assert _decision_of(await task) == "deny"

    asyncio.run(_run())


def test_an_internal_gate_error_fails_closed_for_a_mutating_tool(tmp_path):
    """A raise inside the gate reaches the SDK boundary, where an errored
    hook is effectively fail-OPEN — the tool runs. Catch it here and deny
    anything the gate is responsible for gating."""

    async def _run():
        def _boom(payload):
            raise RuntimeError("gate is broken")

        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        gate.run_file_hook = _boom  # type: ignore[method-assign]

        out = await gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "d"}), None, {})
        assert _decision_of(out) == "deny"
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "approval gate" in reason.lower() and "gate is broken" in reason
        # Bash rides the same guard.
        assert _decision_of(await gate.check(_bash("anything"), None, {})) == "deny"
        # …and a read-only MCP tool is NOT punished for the gate's own bug.
        assert await gate.check(_mcp("mcp__agnes__catalog"), None, {}) == {}

    asyncio.run(_run())


def test_an_internal_gate_error_in_the_round_trip_denies(tmp_path):
    """The guard covers the round-trip too, not just the file-hook call."""

    async def _run():
        async def _boom(**_kwargs):
            raise RuntimeError("emit failed")

        emitted: list[dict] = []
        gate = ApprovalGate(emitted.append, _write_hook(tmp_path), timeout_seconds=5)
        gate._round_trip = _boom  # type: ignore[method-assign]
        out = await gate.check(_mcp("mcp__agnes__data_app_deploy", {"slug": "d"}), None, {})
        assert _decision_of(out) == "deny"
        assert "emit failed" in out["hookSpecificOutput"]["permissionDecisionReason"]

    asyncio.run(_run())
