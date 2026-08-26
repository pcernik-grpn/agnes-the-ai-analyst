"""Runner JSON-line protocol tests (Task 6.1).

Uses asyncio.run() per the project convention (no pytest-asyncio required).
"""

import asyncio
import json
import os
import sys
from pathlib import Path


# Project root as seen from this worktree — needed so the subprocess can
# import app.chat.runner when the editable install points at a different
# checkout (e.g. running tests from a git worktree).
_PROJECT_ROOT = str(Path(__file__).parent.parent)


def test_runner_emits_ready_then_echoes_with_fake_agent(tmp_path: Path):
    async def _run():
        env = os.environ.copy()
        # Ensure the worktree's app package is importable inside the subprocess.
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"  # turns off real SDK call
        env["AGNES_SESSION_ID"] = "chat_test"
        env["AGNES_USER_EMAIL"] = "u@x"
        env["AGNES_API"] = "http://127.0.0.1:8000"
        env["AGNES_TOKEN"] = "fake"
        env["AGNES_DAILY_BUDGET_USD"] = "20"
        env["AGNES_PER_TOOL_CALL_SECONDS"] = "90"

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.chat.runner",
            "--session-id",
            "chat_test",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        line = await proc.stdout.readline()
        frame = json.loads(line)
        assert frame == {"type": "runner_ready"}

        proc.stdin.write((json.dumps({"type": "user_msg", "text": "hi"}) + "\n").encode())
        await proc.stdin.drain()

        # Fake-agent mode echoes back as assistant_message
        line = await proc.stdout.readline()
        frame = json.loads(line)
        assert frame["type"] == "assistant_message"
        assert "hi" in frame["content"]

        proc.stdin.close()
        rc = await proc.wait()
        assert rc == 0

    asyncio.run(_run())


def test_tool_call_budget_emits_confirmation_required(tmp_path: Path):
    """When the per-turn tool-call budget is exhausted, the runner emits a
    `confirmation_required` frame instead of continuing.

    Tested via fake-agent: feeding `__many_tools__:N` makes the fake agent
    fire N tool_call frames in a row; the budget gate stops at the configured
    cap.
    """

    async def _run():
        env = os.environ.copy()
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"
        env["AGNES_PER_TOOL_CALL_SECONDS"] = "5"
        env["AGNES_TOOL_CALLS_PER_TURN"] = "2"
        env["AGNES_SESSION_ID"] = "s"
        env["AGNES_USER_EMAIL"] = "u@x"
        env["AGNES_API"] = "http://127.0.0.1:8000"
        env["AGNES_TOKEN"] = "fake"

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.chat.runner",
            "--session-id",
            "s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        assert json.loads(line) == {"type": "runner_ready"}

        proc.stdin.write((json.dumps({"type": "user_msg", "text": "__many_tools__:5"}) + "\n").encode())
        await proc.stdin.drain()

        tool_calls_seen = 0
        saw_budget = False
        for _ in range(20):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
            frame = json.loads(line)
            if frame.get("type") == "tool_call":
                tool_calls_seen += 1
            if frame.get("type") == "confirmation_required":
                saw_budget = True
                assert frame.get("reason") == "tool_call_budget"
                break

        assert saw_budget, f"expected confirmation_required after tool budget; saw {tool_calls_seen} calls"
        # Budget capped emission at 2.
        assert tool_calls_seen == 2

        proc.stdin.close()
        await proc.wait()

    asyncio.run(_run())


def test_install_agnes_cli_noop_without_wheel(tmp_path: Path, monkeypatch):
    """Empty staging dir → install is a silent no-op (no pip invocation)."""
    from app.chat import runner

    monkeypatch.setattr(runner, "_SANDBOX_WHEEL_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(runner, "_WHEEL_WAIT_SECONDS", 0)
    called = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: called.append(a))

    runner._install_agnes_cli()
    assert called == []


def test_install_agnes_cli_invokes_pip_no_deps_system(tmp_path: Path, monkeypatch):
    """With a wheel staged, pip is invoked --no-deps --break-system-packages
    (NO --user — the console script must land in /usr/local/bin, on the agent
    Bash tool's PATH) against the PEP 427 wheel filename."""
    from app.chat import runner

    staging = tmp_path / "agnes-cli"
    staging.mkdir()
    wheel = staging / "agnes_the_ai_analyst-0.55.25-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(runner, "_SANDBOX_WHEEL_DIR", str(staging))
    monkeypatch.setattr(runner, "_WHEEL_WAIT_SECONDS", 0)

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(runner.subprocess, "run", _fake_run)

    runner._install_agnes_cli()

    argv = captured["argv"]
    assert argv[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--no-deps" in argv
    assert "--user" not in argv  # system install → /usr/local/bin (on Bash tool PATH)
    assert "--break-system-packages" in argv
    assert str(wheel) == argv[-1]
    assert argv[-1].endswith("agnes_the_ai_analyst-0.55.25-py3-none-any.whl")


def test_install_agnes_cli_swallows_pip_failure(tmp_path: Path, monkeypatch, capsys):
    """A pip failure is non-fatal — logged to stderr, no exception raised."""
    from app.chat import runner

    staging = tmp_path / "agnes-cli"
    staging.mkdir()
    (staging / "agnes_the_ai_analyst-0.55.25-py3-none-any.whl").write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(runner, "_SANDBOX_WHEEL_DIR", str(staging))
    monkeypatch.setattr(runner, "_WHEEL_WAIT_SECONDS", 0)

    def _boom(*a, **k):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(runner.subprocess, "run", _boom)

    runner._install_agnes_cli()  # must not raise
    assert "agnes CLI install failed" in capsys.readouterr().err


def test_per_tool_call_timeout_emits_synthetic_result(tmp_path: Path):
    """__slow_tool__ triggers a tool_call followed by tool_result: {timeout: true}."""

    async def _run():
        env = os.environ.copy()
        env["PYTHONPATH"] = _PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["AGNES_RUNNER_FAKE_AGENT"] = "1"
        env["AGNES_PER_TOOL_CALL_SECONDS"] = "0.5"  # very short cap for test speed
        env["AGNES_SESSION_ID"] = "s"
        env["AGNES_USER_EMAIL"] = "u@x"
        env["AGNES_API"] = "http://127.0.0.1:8000"
        env["AGNES_TOKEN"] = "fake"

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.chat.runner",
            "--session-id",
            "s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(tmp_path),
        )
        assert proc.stdin and proc.stdout

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        assert json.loads(line) == {"type": "runner_ready"}

        proc.stdin.write((json.dumps({"type": "user_msg", "text": "__slow_tool__"}) + "\n").encode())
        await proc.stdin.drain()

        saw_call = False
        saw_timeout = False
        for _ in range(10):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
            frame = json.loads(line)
            if frame.get("type") == "tool_call":
                saw_call = True
            if frame.get("type") == "tool_result" and frame.get("result", {}).get("timeout"):
                saw_timeout = True
                break

        assert saw_call, "expected tool_call frame"
        assert saw_timeout, "expected tool_result with timeout=true"

        proc.stdin.close()
        await proc.wait()

    asyncio.run(_run())


def test_agnes_mcp_servers_builds_stdio_config(monkeypatch):
    """With AGNES_SERVER set, the runner exposes the Agnes MCP stdio server
    to the sandbox agent so it sees passthrough tools. No token is placed in
    its env — the relay carries the credential (Task 8 / AC-F2b)."""
    from app.chat import runner

    monkeypatch.setenv("AGNES_SERVER", "http://localhost:8000")
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    monkeypatch.setenv("AGNES_SESSION_ID", "chat_abc")

    cfg = runner._agnes_mcp_servers()
    assert set(cfg) == {"agnes"}
    server = cfg["agnes"]
    assert server["type"] == "stdio"
    assert server["command"] == "agnes"
    assert server["args"] == ["mcp"]
    # Server + session are forwarded on the server's own env (not left to
    # inheritance across the claude-CLI hop).
    assert server["env"]["AGNES_SERVER"] == "http://localhost:8000"
    assert server["env"]["AGNES_SESSION_ID"] == "chat_abc"
    assert "PATH" in server["env"]
    # HOME is forwarded so `agnes mcp` can expanduser its config dir; env
    # inheritance across the claude-CLI spawn hop is not guaranteed.
    assert server["env"]["HOME"]


def test_agnes_mcp_servers_empty_when_unconfigured(monkeypatch):
    """No AGNES_SERVER (fake-agent path) → empty dict so the agent still runs
    on built-in tools instead of a broken MCP handshake."""
    from app.chat import runner

    monkeypatch.delenv("AGNES_SERVER", raising=False)
    assert runner._agnes_mcp_servers() == {}


def test_mcp_env_has_no_token(monkeypatch):
    """AC-F2b: whatever AGNES_TOKEN happens to be set to (or not), the MCP
    server config's env never carries it — the relay is the credential
    carrier now, not a token in a subprocess env."""
    from app.chat import runner

    monkeypatch.setenv("AGNES_SERVER", "http://127.0.0.1:9999/agnes-api")
    monkeypatch.setenv("AGNES_TOKEN", "should-never-appear")

    servers = runner._agnes_mcp_servers()
    assert servers  # sanity: config was built
    for cfg in servers.values():
        assert "AGNES_TOKEN" not in (cfg.get("env") or {})


def test_ticket_push_frame_not_enqueued(monkeypatch):
    """A ticket_push frame calls relay.set_tickets(...) and is never put on
    the agent message queue; a following user_msg IS enqueued unchanged."""
    from app.chat import runner

    class _FakeRelay:
        def __init__(self):
            self.tickets = None

        def set_tickets(self, main, mcp, data_apps=""):
            self.tickets = (main, mcp, data_apps)

    fake_relay = _FakeRelay()
    monkeypatch.setattr(runner, "_relay", fake_relay)

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        await runner._dispatch_frame({"type": "ticket_push", "main": "M", "mcp": "C", "data_apps": "D"}, queue)
        await runner._dispatch_frame({"type": "user_msg", "text": "hi"}, queue)

        assert fake_relay.tickets == ("M", "C", "D")
        assert queue.qsize() == 1
        enqueued = queue.get_nowait()
        assert enqueued["type"] == "user_msg"
        assert enqueued["text"] == "hi"

    asyncio.run(_run())


def test_mcp_server_rides_mcp_scope_url(monkeypatch):
    """The agnes-mcp stdio subprocess must target the relay's /agnes-mcp path
    (mcp-scoped ticket), not the agent process's /agnes-api (main scope) — so
    the minted mcp ticket is actually used, not dead (§11)."""
    monkeypatch.setenv("AGNES_SERVER", "http://127.0.0.1:5000/agnes-api")
    from app.chat.runner import _agnes_mcp_servers

    servers = _agnes_mcp_servers()
    assert servers["agnes"]["env"]["AGNES_SERVER"] == "http://127.0.0.1:5000/agnes-mcp"


def test_runner_has_no_module_level_app_import():
    """The runner runs as a standalone script inside the sandbox, where the
    `app` package does not exist until _install_agnes_cli() pip-installs the
    uploaded wheel. A module-level `import app.*` (e.g. the broker relay import)
    crashes the interpreter at startup with ModuleNotFoundError — before the
    install runs — taking chat down end-to-end (found by live E2E on agnes-dev).
    Guard: NO top-level `import app.*` / `from app.* import ...` in runner.py.
    All such imports must be lazy (inside functions, after the install), or
    TYPE_CHECKING-only for annotations."""
    import ast
    from pathlib import Path

    src = Path("app/chat/runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "app"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "app":
                offenders.append(node.module)
    assert not offenders, f"runner.py has module-level app.* imports (must be lazy): {offenders}"


# ---------------------------------------------------------------------------
# Spawn-latency rework: workspace-ready barrier, eager connect + query(),
# awaited interrupt, token-level streaming deltas.
# ---------------------------------------------------------------------------


def test_stream_event_delta_text_extracts_text_deltas():
    from app.chat import runner

    ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}
    assert runner._stream_event_delta_text(ev) == "Hi"
    # Everything that isn't assistant prose maps to "".
    assert runner._stream_event_delta_text({"type": "content_block_start"}) == ""
    assert (
        runner._stream_event_delta_text(
            {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}
        )
        == ""
    )
    assert (
        runner._stream_event_delta_text(
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}
        )
        == ""
    )
    assert runner._stream_event_delta_text({}) == ""
    assert runner._stream_event_delta_text(None) == ""


def test_wait_workspace_ready_skips_when_env_unset(monkeypatch):
    from app.chat import runner

    monkeypatch.delenv("AGNES_WORKSPACE_SYNC_SENTINEL", raising=False)
    assert asyncio.run(runner._wait_workspace_ready()) is True


def test_wait_workspace_ready_returns_when_sentinel_exists(tmp_path: Path, monkeypatch):
    from app.chat import runner

    sentinel = tmp_path / "ws.ready"
    sentinel.write_bytes(b"")
    monkeypatch.setenv("AGNES_WORKSPACE_SYNC_SENTINEL", str(sentinel))
    assert asyncio.run(runner._wait_workspace_ready()) is True


def test_wait_workspace_ready_times_out_best_effort(tmp_path: Path, monkeypatch, capsys):
    from app.chat import runner

    monkeypatch.setenv("AGNES_WORKSPACE_SYNC_SENTINEL", str(tmp_path / "never-written"))
    monkeypatch.setattr(runner, "_WORKSPACE_WAIT_SECONDS", 0)
    assert asyncio.run(runner._wait_workspace_ready()) is False
    assert "possibly-incomplete workspace" in capsys.readouterr().err


def _make_fake_sdk(monkeypatch, *, with_stream_event: bool):
    """Inject a fake ``claude_agent_sdk`` module into sys.modules and return
    it. ``_real_agent_loop`` imports the SDK lazily inside the function, so
    the injection takes effect without reloading the runner module."""
    import dataclasses
    import types

    mod = types.ModuleType("claude_agent_sdk")

    @dataclasses.dataclass
    class TextBlock:
        text: str

    @dataclasses.dataclass
    class ToolUseBlock:
        id: str
        name: str
        input: dict

    @dataclasses.dataclass
    class ToolResultBlock:
        tool_use_id: str
        content: object
        # The real SDK block carries this; defaulted so every existing script
        # in this file keeps meaning "the tool succeeded".
        is_error: bool = False

    @dataclasses.dataclass
    class AssistantMessage:
        content: list
        model: str = "fake-model"
        usage: dict | None = None

    @dataclasses.dataclass
    class UserMessage:
        content: object

    @dataclasses.dataclass
    class ResultMessage:
        usage: dict | None = None

    option_fields = [
        ("permission_mode", str, dataclasses.field(default="")),
        ("cwd", str, dataclasses.field(default="")),
        ("setting_sources", object, dataclasses.field(default=None)),
        ("mcp_servers", object, dataclasses.field(default=None)),
    ]
    if with_stream_event:
        option_fields.append(("include_partial_messages", bool, dataclasses.field(default=False)))
    ClaudeAgentOptions = dataclasses.make_dataclass("ClaudeAgentOptions", option_fields)

    class ClaudeSDKClient:
        instances: list = []

        def __init__(self, options):
            self.options = options
            self.calls: list = []
            self.scripts: list = []  # one list of messages per turn
            ClaudeSDKClient.instances.append(self)

        async def __aenter__(self):
            # Mirrors the real SDK: entering the context connects eagerly.
            self.calls.append(("connect", None))
            return self

        async def __aexit__(self, *a):
            return False

        async def connect(self, prompt=None):
            self.calls.append(("connect", prompt))

        async def query(self, text):
            self.calls.append(("query", text))

        async def interrupt(self):
            self.calls.append(("interrupt", None))

        def receive_response(self):
            script = self.scripts.pop(0) if self.scripts else []

            async def _gen():
                for m in script:
                    yield m

            return _gen()

    mod.TextBlock = TextBlock
    mod.ToolUseBlock = ToolUseBlock
    mod.ToolResultBlock = ToolResultBlock
    mod.AssistantMessage = AssistantMessage
    mod.UserMessage = UserMessage
    mod.ResultMessage = ResultMessage
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ClaudeSDKClient = ClaudeSDKClient

    if with_stream_event:

        @dataclasses.dataclass
        class StreamEvent:
            uuid: str
            session_id: str
            event: dict
            parent_tool_use_id: str | None = None

        mod.StreamEvent = StreamEvent

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


def _run_real_agent_turn(monkeypatch, mod, script, frames_in):
    """Drive one _real_agent_loop pass against the fake SDK; returns
    (emitted frames, client)."""
    from app.chat import runner

    emitted: list = []
    monkeypatch.setattr(runner, "_emit", emitted.append)

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        for f in frames_in:
            queue.put_nowait(f)
        queue.put_nowait({"type": "_eof"})
        await runner._real_agent_loop(queue, Path("/tmp"))

    mod.ClaudeSDKClient.instances.clear()

    # Pre-seed the turn script on construction via a subclass hook: the loop
    # constructs the client itself, so stash the script on the class.
    orig_init = mod.ClaudeSDKClient.__init__

    def _init(self, options):
        orig_init(self, options)
        self.scripts = [list(script)]

    monkeypatch.setattr(mod.ClaudeSDKClient, "__init__", _init)
    asyncio.run(_run())
    return emitted, mod.ClaudeSDKClient.instances[0]


def test_real_agent_loop_streams_deltas_without_duplicating_block_text(monkeypatch):
    """Token frames come from StreamEvent text deltas as they arrive; the
    completed TextBlock is NOT re-emitted as a token (the UI already has the
    text), but still feeds the turn-end assistant_message. Subagent deltas
    (parent_tool_use_id set) stay internal."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)

    def _delta(text):
        return mod.StreamEvent(
            uuid="u1",
            session_id="s1",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        )

    script = [
        _delta("Hel"),
        _delta("lo"),
        mod.StreamEvent(
            uuid="u2",
            session_id="s1",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "SUBAGENT"}},
            parent_tool_use_id="tu_1",
        ),
        mod.AssistantMessage(content=[mod.TextBlock(text="Hello")]),
        mod.ResultMessage(usage={"input_tokens": 3, "output_tokens": 5}),
    ]
    emitted, client = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "hi"}])

    tokens = [f["text"] for f in emitted if f["type"] == "token"]
    assert tokens == ["Hel", "lo"]  # deltas only — no duplicate "Hello" block token
    final = next(f for f in emitted if f["type"] == "assistant_message")
    assert final["content"] == "Hello"
    assert emitted[-1] == {"type": "done"}
    # Streaming was requested from the SDK.
    assert client.options.include_partial_messages is True


def test_real_agent_loop_uses_query_not_connect_for_messages(monkeypatch):
    """__aenter__ already connected (eager CLI boot); every user_msg must go
    through query() — a connect(text) here would spawn a second CLI."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)
    script = [
        mod.AssistantMessage(content=[mod.TextBlock(text="ack")]),
        mod.ResultMessage(),
    ]
    emitted, client = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "hi"}])

    assert ("query", "hi") in client.calls
    assert ("connect", "hi") not in client.calls
    # Exactly one connect — the eager one from __aenter__, with no prompt.
    assert client.calls.count(("connect", None)) == 1


def test_real_agent_loop_awaits_interrupt_on_cancel(monkeypatch):
    """cancel frames must actually reach the SDK — interrupt() is a
    coroutine, and the historical un-awaited call never ran its body."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)
    emitted, client = _run_real_agent_turn(monkeypatch, mod, [], [{"type": "cancel"}])
    assert ("interrupt", None) in client.calls


def test_real_agent_loop_falls_back_to_block_tokens_without_stream_event(monkeypatch):
    """On an SDK predating StreamEvent the loop must not request partial
    messages (the options dataclass lacks the field) and must keep emitting
    whole-block token frames so the user still sees text."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=False)
    script = [
        mod.AssistantMessage(content=[mod.TextBlock(text="Hello")]),
        mod.ResultMessage(),
    ]
    emitted, client = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "hi"}])

    tokens = [f["text"] for f in emitted if f["type"] == "token"]
    assert tokens == ["Hello"]
    assert not hasattr(client.options, "include_partial_messages")


def test_real_agent_loop_interrupts_a_live_turn(monkeypatch):
    """Devin Review on #975: a cancel arriving WHILE a turn is streaming must
    interrupt it. The old single-consumer loop only read the queue between
    turns, so the cancel sat unprocessed until the turn ended on its own.
    Here the fake turn blocks until interrupt() is called — the test only
    completes (within the timeout) if the cancel is handled mid-turn."""
    from app.chat import runner

    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)

    interrupted = asyncio.Event()

    emitted: list = []
    monkeypatch.setattr(runner, "_emit", emitted.append)

    orig_init = mod.ClaudeSDKClient.__init__

    def _init(self, options):
        orig_init(self, options)

        async def _interrupt():
            self.calls.append(("interrupt", None))
            interrupted.set()

        self.interrupt = _interrupt

        def _receive():
            async def _gen():
                yield mod.StreamEvent(
                    uuid="u1",
                    session_id="s1",
                    event={
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "thinking…"},
                    },
                )
                # Simulates a long-running turn: only ends once interrupted.
                await interrupted.wait()

            return _gen()

        self.receive_response = _receive

    monkeypatch.setattr(mod.ClaudeSDKClient, "__init__", _init)

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"type": "user_msg", "text": "hi"})
        queue.put_nowait({"type": "cancel"})
        queue.put_nowait({"type": "_eof"})
        await asyncio.wait_for(runner._real_agent_loop(queue, Path("/tmp")), timeout=5)

    asyncio.run(_run())

    client = mod.ClaudeSDKClient.instances[-1]
    assert ("interrupt", None) in client.calls
    # The turn still closed out cleanly for the UI.
    assert {"type": "done"} in emitted


def test_real_agent_loop_buffers_user_msg_arriving_mid_turn(monkeypatch):
    """A follow-up user_msg landing while a turn is in flight must not be
    dropped (nor treated as a cancel) — it runs as the next turn, in order."""
    from app.chat import runner

    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)

    emitted: list = []
    monkeypatch.setattr(runner, "_emit", emitted.append)

    def _script(reply):
        return [
            mod.AssistantMessage(content=[mod.TextBlock(text=reply)]),
            mod.ResultMessage(),
        ]

    orig_init = mod.ClaudeSDKClient.__init__

    def _init(self, options):
        orig_init(self, options)
        self.scripts = [_script("first"), _script("second")]

    monkeypatch.setattr(mod.ClaudeSDKClient, "__init__", _init)

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        # Both messages are already queued when the first turn starts — the
        # second is consumed mid-turn by the queue watcher and must be
        # buffered for after the turn.
        queue.put_nowait({"type": "user_msg", "text": "one"})
        queue.put_nowait({"type": "user_msg", "text": "two"})
        queue.put_nowait({"type": "_eof"})
        await asyncio.wait_for(runner._real_agent_loop(queue, Path("/tmp")), timeout=5)

    asyncio.run(_run())

    client = mod.ClaudeSDKClient.instances[-1]
    queries = [c for c in client.calls if c[0] == "query"]
    assert queries == [("query", "one"), ("query", "two")]
    finals = [f["content"] for f in emitted if f["type"] == "assistant_message"]
    assert finals == ["first", "second"]
    assert [f for f in emitted if f == {"type": "done"}] == [{"type": "done"}] * 2


def test_real_agent_loop_survives_interrupt_induced_turn_exception(monkeypatch):
    """Some SDK/CLI builds surface a user interrupt as an exception out of
    receive_response(). That is the outcome the user asked for — it must not
    tear down the runner (which would kill the whole session); the loop keeps
    serving subsequent messages."""
    from app.chat import runner

    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)

    interrupted = asyncio.Event()

    emitted: list = []
    monkeypatch.setattr(runner, "_emit", emitted.append)

    orig_init = mod.ClaudeSDKClient.__init__

    def _init(self, options):
        orig_init(self, options)
        self.turn = 0

        async def _interrupt():
            self.calls.append(("interrupt", None))
            interrupted.set()

        self.interrupt = _interrupt

        def _receive():
            self.turn += 1
            if self.turn == 1:

                async def _gen_interrupted():
                    yield mod.StreamEvent(
                        uuid="u1",
                        session_id="s1",
                        event={
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "long answer…"},
                        },
                    )
                    await interrupted.wait()
                    raise RuntimeError("interrupted by user")  # SDK surfaces Stop as an exception

                return _gen_interrupted()

            async def _gen_ok():
                yield mod.AssistantMessage(content=[mod.TextBlock(text="still alive")])
                yield mod.ResultMessage()

            return _gen_ok()

        self.receive_response = _receive

    monkeypatch.setattr(mod.ClaudeSDKClient, "__init__", _init)

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"type": "user_msg", "text": "one"})
        queue.put_nowait({"type": "cancel"})
        queue.put_nowait({"type": "user_msg", "text": "two"})
        queue.put_nowait({"type": "_eof"})
        # Must complete without raising — the interrupt-induced exception is
        # eaten, the runner keeps going and serves turn two.
        await asyncio.wait_for(runner._real_agent_loop(queue, Path("/tmp")), timeout=5)

    asyncio.run(_run())

    finals = [f["content"] for f in emitted if f["type"] == "assistant_message"]
    assert finals == ["still alive"]
    # Both turns closed out for the UI (done emitted from finally even on the
    # interrupted turn).
    assert len([f for f in emitted if f == {"type": "done"}]) == 2


def test_real_agent_loop_does_not_emit_done_on_genuine_crash(monkeypatch):
    """A turn that crashes WITHOUT a user interrupt must propagate the
    exception (amain's outer handler turns it into an `error` frame and the
    runner exits so the manager respawns) and must NOT emit `done` first —
    `done` clears the manager-side turn buffer needed to save the partial
    answer already streamed to the user (Devin Review on #975)."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)

    from app.chat import runner

    emitted: list = []
    monkeypatch.setattr(runner, "_emit", emitted.append)

    orig_init = mod.ClaudeSDKClient.__init__

    def _init(self, options):
        orig_init(self, options)

        def _receive():
            async def _gen_crash():
                yield mod.AssistantMessage(content=[mod.TextBlock(text="partial answer")])
                raise RuntimeError("SDK connection dropped")

            return _gen_crash()

        self.receive_response = _receive

    monkeypatch.setattr(mod.ClaudeSDKClient, "__init__", _init)

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"type": "user_msg", "text": "hi"})
        queue.put_nowait({"type": "_eof"})
        await runner._real_agent_loop(queue, Path("/tmp"))

    raised = None
    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        raised = exc

    assert raised is not None and "SDK connection dropped" in str(raised)
    # The partial token was streamed to the UI before the crash...
    assert any(f.get("type") == "token" and f.get("text") == "partial answer" for f in emitted)
    # ...but `done` must never fire, or the manager clears the buffer that
    # would otherwise save this partial answer.
    assert {"type": "done"} not in emitted


def test_assistant_message_falls_back_to_streamed_deltas(monkeypatch):
    """If an SDK build streams the text via deltas but never delivers the
    final consolidated TextBlock, the persisted assistant_message must carry
    the delta text — not be empty while the live UI showed an answer."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)

    def _delta(text):
        return mod.StreamEvent(
            uuid="u1",
            session_id="s1",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        )

    script = [
        _delta("Hel"),
        _delta("lo"),
        # No AssistantMessage with a TextBlock — straight to turn end.
        mod.ResultMessage(usage={"input_tokens": 1, "output_tokens": 2}),
    ]
    emitted, _client = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "hi"}])

    final = next(f for f in emitted if f["type"] == "assistant_message")
    assert final["content"] == "Hello"


def test_tool_frames_carry_tool_use_id_and_text_blocks_join_with_blank_line(monkeypatch):
    """The manager's frame envelope overwrites ``id`` with ``chat_id:seq``,
    so tool pairing must ride the dedicated ``tool_use_id`` key on BOTH
    tool_call and tool_result frames. And prose segments bracketing tool
    calls must not be squashed together in the persisted assistant_message
    ("…tables.35" / "…znovu:Z MCP…")."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=False)

    script = [
        mod.AssistantMessage(
            content=[
                mod.TextBlock(text="Let me check."),
                mod.ToolUseBlock(id="toolu_123", name="Bash", input={"command": "agnes catalog"}),
            ]
        ),
        mod.UserMessage(content=[mod.ToolResultBlock(tool_use_id="toolu_123", content="35")]),
        mod.AssistantMessage(content=[mod.TextBlock(text="There are 35 tables.")]),
        mod.ResultMessage(),
    ]
    emitted, _client = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "count tables"}])

    call = next(f for f in emitted if f["type"] == "tool_call")
    assert call["tool_use_id"] == "toolu_123"
    assert call["tool"] == "Bash"
    result = next(f for f in emitted if f["type"] == "tool_result")
    assert result["tool_use_id"] == "toolu_123"

    final = next(f for f in emitted if f["type"] == "assistant_message")
    assert final["content"] == "Let me check.\n\nThere are 35 tables."
    assert result["is_error"] is False, "a successful tool must say so explicitly"


def test_a_failed_tool_result_carries_the_sdk_verdict(monkeypatch):
    """The SDK block already knows the tool failed; forwarding it spares the
    client a guess. Without this the UI sniffed the payload for a leading
    "error"/"traceback", so a real failure reading "Catalog Error: Table …
    does not exist" rendered with a success tick and folded itself shut.

    This is the NATIVE (docker) path — the engine provider sets the same
    field from its own `tool-output-error` event, so both producers agree."""
    mod = _make_fake_sdk(monkeypatch, with_stream_event=False)
    script = [
        mod.AssistantMessage(
            content=[
                mod.TextBlock(text="Trying."),
                mod.ToolUseBlock(id="toolu_9", name="Bash", input={"command": "agnes query 'SELECT * FROM nope'"}),
            ]
        ),
        mod.UserMessage(
            content=[
                mod.ToolResultBlock(
                    tool_use_id="toolu_9",
                    content="Catalog Error: Table with name nope does not exist!",
                    is_error=True,
                )
            ]
        ),
        mod.AssistantMessage(content=[mod.TextBlock(text="No such table.")]),
        mod.ResultMessage(),
    ]
    emitted, _client = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "q"}])

    result = next(f for f in emitted if f["type"] == "tool_result")
    assert result["is_error"] is True
    # The failure is the TOOL's, not the turn's: the answer still completes.
    assert not result["result"].strip().lower().startswith("error"), (
        "this fixture exists because the text defeats the old leading-'error' heuristic"
    )
    assert next(f for f in emitted if f["type"] == "assistant_message")["content"].endswith("No such table.")


def test_idle_watchdog_interrupts_a_wedged_turn(monkeypatch):
    """A tool call that never returns must not wedge the turn forever: after
    AGNES_TURN_IDLE_SECONDS with no agent activity the watchdog emits an
    error frame, interrupts the turn, and the runner lives on to serve the
    next message."""
    from app.chat import runner

    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)
    monkeypatch.setenv("AGNES_TURN_IDLE_SECONDS", "0.2")

    emitted: list = []
    monkeypatch.setattr(runner, "_emit", emitted.append)

    orig_init = mod.ClaudeSDKClient.__init__

    def _init(self, options):
        orig_init(self, options)
        self.turn = 0

        def _receive():
            self.turn += 1
            if self.turn == 1:

                async def _gen_wedged():
                    yield mod.StreamEvent(
                        uuid="u1",
                        session_id="s1",
                        event={
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "querying…"},
                        },
                    )
                    await asyncio.sleep(3600)  # tool never returns

                return _gen_wedged()

            async def _gen_ok():
                yield mod.AssistantMessage(content=[mod.TextBlock(text="recovered")])
                yield mod.ResultMessage()

            return _gen_ok()

        self.receive_response = _receive

    monkeypatch.setattr(mod.ClaudeSDKClient, "__init__", _init)

    async def _run():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"type": "user_msg", "text": "one"})
        queue.put_nowait({"type": "user_msg", "text": "two"})
        queue.put_nowait({"type": "_eof"})
        await asyncio.wait_for(runner._real_agent_loop(queue, Path("/tmp")), timeout=10)

    asyncio.run(_run())

    errs = [f for f in emitted if f["type"] == "error"]
    assert errs and errs[0]["kind"] == "turn_idle_timeout"
    client = mod.ClaudeSDKClient.instances[-1]
    assert ("interrupt", None) in client.calls
    # The wedged turn's partial text is persisted (the outer loop's `done`
    # clears the manager's turn buffer, so the watchdog must save it), and
    # turn 2 ran to completion.
    finals = [f["content"] for f in emitted if f["type"] == "assistant_message"]
    assert finals == ["querying…", "recovered"]
    assert len([f for f in emitted if f == {"type": "done"}]) == 2


def test_restore_context_appended_to_system_prompt(monkeypatch, tmp_path):
    """When the manager staged a restored-conversation transcript, the runner
    appends it to the CLI's stock system prompt (claude_code preset +
    append); without the file, no system_prompt override is passed."""
    from app.chat import runner

    mod = _make_fake_sdk(monkeypatch, with_stream_event=True)
    # Give the fake options dataclass a system_prompt field so the kwarg is
    # accepted and observable.
    import dataclasses

    fields = [
        (f, object, dataclasses.field(default=None))
        for f in (
            "permission_mode",
            "cwd",
            "setting_sources",
            "mcp_servers",
            "system_prompt",
        )
    ]
    fields.append(("include_partial_messages", bool, dataclasses.field(default=False)))
    mod.ClaudeAgentOptions = dataclasses.make_dataclass("ClaudeAgentOptions", fields)

    ctx_file = tmp_path / "agnes-context.md"
    ctx_file.write_text("## Restored conversation context\n\n**User:**\nolder question\n")
    monkeypatch.setattr(runner, "_CONTEXT_RESTORE_PATH", str(ctx_file))

    script = [mod.AssistantMessage(content=[mod.TextBlock(text="ok")]), mod.ResultMessage()]
    _emitted, client = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "hi"}])

    sp = client.options.system_prompt
    assert isinstance(sp, dict) and sp.get("type") == "preset" and sp.get("preset") == "claude_code"
    assert "older question" in sp.get("append", "")

    # Without the file → stock prompt untouched.
    monkeypatch.setattr(runner, "_CONTEXT_RESTORE_PATH", str(tmp_path / "absent.md"))
    _emitted2, client2 = _run_real_agent_turn(monkeypatch, mod, script, [{"type": "user_msg", "text": "hi"}])
    assert client2.options.system_prompt is None
