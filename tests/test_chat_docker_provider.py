"""DockerSandboxProvider unit tests — mock the sidecar client at the import
boundary.

There is no mock provider
class. These tests patch `app.chat.docker_provider.SandboxRunnerClient` so the
real provider code runs against a fake sidecar. Daemon-backed coverage is
`@pytest.mark.docker`-gated in `tests/test_chat_docker_provider_daemon.py`
(excluded from the default run); real end-to-end lives in
`tests/e2e/test_docker_sandbox_smoke.py` (opt-in via `AGNES_E2E_DOCKER=1`).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chat.docker_provider import (
    OWNER_LABEL,
    SESSION_LABEL,
    DockerSandboxHandle,
    DockerSandboxProvider,
    container_name,
)
from app.chat.provider import SandboxCapacityError


class FakeStream:
    """Stand-in for SandboxAttachStream: yields frames, then optionally holds
    the attach open (a real one only ends when the container exits)."""

    def __init__(self, frames=(), *, hold=False):
        self._frames = list(frames)
        self._hold = hold
        self._release = None
        self.closed = False

    async def __aiter__(self):
        for f in self._frames:
            yield f
        if self._hold:
            self._release = asyncio.Event()
            await self._release.wait()

    async def aclose(self):
        self.closed = True
        if self._release is not None:
            self._release.set()


def _fake_client(stream=None, **over):
    c = MagicMock()
    c.up = AsyncMock(return_value={"status": "started"})
    c.pause = AsyncMock(return_value={"status": "paused"})
    c.resume = AsyncMock(return_value={"status": "running"})
    c.rm = AsyncMock(return_value={"status": "removed"})
    c.status = AsyncMock(return_value={"container": "running", "exit_code": None})
    c.list_sandboxes = AsyncMock(return_value=[])
    c.probe = AsyncMock(return_value={"ok": True})
    c.send_stdin = AsyncMock(return_value={"status": "ok"})
    c.write_file = AsyncMock(return_value={"status": "written"})
    c.read_file = AsyncMock(return_value=b"")
    c.list_files = AsyncMock(return_value=[])
    c.open_stream = AsyncMock(return_value=stream if stream is not None else FakeStream())
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _session_dir(tmp_path: Path, email: str = "a@b.com", chat_id: str = "chat1") -> Path:
    """The real host layout WorkdirManager produces."""
    root = tmp_path / "users" / email
    (root / "workspace").mkdir(parents=True)
    sdir = root / "sessions" / chat_id
    sdir.mkdir(parents=True)
    return sdir


ENV = {
    "AGNES_SERVER": "http://app:8000",
    "AGNES_SESSION_ID": "chat1",
    "AGNES_USER_EMAIL": "a@b.com",
    "HOME": "/home/user",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}
ARGV = ["python3", "/work/runner.py", "--session-id", "chat1"]


def _provider(client, **over):
    kwargs = {
        "image": "agnes-chat-sandbox:dev",
        "network": "agnes-apps",
        "mem_limit": "2g",
        "cpus": 1.0,
        "pids_limit": 512,
        "egress_mode": "open",
        "max_total_sandboxes": 10,
    }
    kwargs.update(over)
    with patch("app.chat.docker_provider.SandboxRunnerClient", return_value=client):
        return DockerSandboxProvider(**kwargs)


# ---------------------------------------------------------------------------
# naming + provider flags
# ---------------------------------------------------------------------------


def test_container_name_is_deterministic_and_sanitized():
    a = container_name("chat_ABC-123")
    assert a.startswith("agnes-chatsbx-")
    assert a == container_name("chat_ABC-123")
    # Everything outside [A-Za-z0-9._-] is collapsed so the name is a legal
    # Docker container name, and the hash suffix keeps it collision-proof.
    weird = container_name("chat/../evil id")
    assert "/" not in weird and " " not in weird
    assert weird != a


def test_provider_declares_it_syncs_the_workspace():
    """Bind-mounted workspace: the manager must not push a tarball."""
    assert DockerSandboxProvider.syncs_workspace is True


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


def test_spawn_builds_a_hardened_spec_with_both_mounts(tmp_path: Path):
    async def _run():
        client = _fake_client()
        prov = _provider(client)
        sdir = _session_dir(tmp_path)
        handle = await prov.spawn(workdir=sdir, env=dict(ENV), argv=list(ARGV))

        name, spec = client.up.await_args.args
        assert name == container_name("chat1")
        assert spec["name"] == name
        assert spec["image"] == "agnes-chat-sandbox:dev"
        assert spec["cmd"] == ARGV
        assert spec["working_dir"] == "/work"
        assert spec["network"] == "agnes-apps"
        assert spec["internal_network"] is False
        assert spec["mem_limit"] == "2g"
        assert spec["cpus"] == 1.0
        assert spec["pids_limit"] == 512
        assert spec["labels"] == {OWNER_LABEL: "1", SESSION_LABEL: "chat1"}
        # Mount 1: session dir -> /work. Mount 2: the user's workspace at the
        # SAME absolute path the manager sees, so the session dir's symlinks
        # (.claude, CLAUDE.md, snapshots, ...) resolve inside the container.
        ws = tmp_path / "users" / "a@b.com" / "workspace"
        assert spec["mounts"] == [
            {"source": str(sdir), "target": "/work", "mode": "rw"},
            {"source": str(ws), "target": str(ws), "mode": "rw"},
        ]
        assert handle.sandbox_id == name

    asyncio.run(_run())


def test_spawn_passes_env_through_without_adding_secrets(tmp_path: Path):
    """The env the manager hands over is secret-free by construction (broker
    tickets ride stdin); the provider must not add key material of its own."""

    async def _run():
        client = _fake_client()
        prov = _provider(client)
        await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        _, spec = client.up.await_args.args
        assert spec["env"] == ENV
        assert "ANTHROPIC_API_KEY" not in spec["env"]
        assert "AGNES_TOKEN" not in spec["env"]
        assert "APPS_RUNNER_TOKEN" not in spec["env"]

    asyncio.run(_run())


def test_spawn_runs_as_the_owner_of_the_session_dir(tmp_path: Path):
    """Bind mounts carry host ownership: the container must run as the uid that
    owns the session dir or every write into /work is EACCES."""

    async def _run():
        client = _fake_client()
        prov = _provider(client)
        sdir = _session_dir(tmp_path)
        st = os.stat(sdir)
        await prov.spawn(workdir=sdir, env=dict(ENV), argv=list(ARGV))
        _, spec = client.up.await_args.args
        assert spec["user"] == f"{st.st_uid}:{st.st_gid}"

    asyncio.run(_run())


def test_spawn_falls_back_to_the_image_user_for_root_owned_dirs(tmp_path: Path, monkeypatch):
    """Never run the sandbox as root (D7) — fall back to the image's own
    non-root account and let the operator fix ownership."""

    async def _run():
        client = _fake_client()
        prov = _provider(client)
        sdir = _session_dir(tmp_path)
        monkeypatch.setattr("app.chat.docker_provider._owner_ids", lambda p: (0, 0))
        await prov.spawn(workdir=sdir, env=dict(ENV), argv=list(ARGV))
        _, spec = client.up.await_args.args
        assert spec["user"] == "1000:1000"

    asyncio.run(_run())


def test_profile_session_mounts_symlink_targets_not_the_workspace(tmp_path: Path):
    """A profile session's `.claude`/`CLAUDE.md` are COPIES (WorkdirManager
    materializes them precisely so the profiled agent cannot write the shared
    originals). Mounting the whole workspace would hand those originals back
    read-write — the session dir is the isolation boundary, mirroring the
    session dir. Only the data symlink targets may be mounted: snapshots
    writable (`agnes snapshot create` lands there), the rest read-only."""

    async def _run():
        sdir = _session_dir(tmp_path)
        ws = tmp_path / "users" / "a@b.com" / "workspace"
        for name in ("snapshots", "scripts"):
            (ws / name).mkdir()
            (sdir / name).symlink_to(ws / name)
        # The profile marker: a REAL .claude dir (copied), not a symlink.
        (sdir / ".claude").mkdir()
        (sdir / "CLAUDE.md").write_text("persona")

        client = _fake_client(FakeStream(hold=True))
        prov = _provider(client)
        handle = await prov.spawn(workdir=sdir, env=dict(ENV), argv=list(ARGV))
        _, spec = client.up.await_args.args
        by_source = {m["source"]: m for m in spec["mounts"]}
        assert str(ws) not in by_source, "the shared workspace root must not be mounted for a profile session"
        assert by_source[str(ws / "snapshots")]["mode"] == "rw"
        assert by_source[str(ws / "scripts")]["mode"] == "ro"
        assert by_source[str(sdir)]["target"] == "/work"
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_profile_session_ignores_agent_planted_symlinks(tmp_path: Path):
    """/work is agent-writable, so the mount list must come from the fixed
    allowlist WorkdirManager links — never from scanning the session dir. A
    previous run that re-pointed `snapshots` at the workspace `.claude` (or
    planted extra links) must not get those targets mounted on respawn."""

    async def _run():
        sdir = _session_dir(tmp_path)
        ws = tmp_path / "users" / "a@b.com" / "workspace"
        (ws / ".claude").mkdir()
        (ws / "secrets").mkdir()
        (ws / "scripts").mkdir()
        # Profile marker.
        (sdir / ".claude").mkdir()
        (sdir / "CLAUDE.md").write_text("persona")
        # Legitimate link WorkdirManager made:
        (sdir / "scripts").symlink_to(ws / "scripts")
        # Hostile links a previous sandbox run could have planted:
        (sdir / "snapshots").symlink_to(ws / ".claude")  # allowlisted name, wrong target
        (sdir / "evil").symlink_to(ws / "secrets")  # non-allowlisted name

        client = _fake_client(FakeStream(hold=True))
        prov = _provider(client)
        handle = await prov.spawn(workdir=sdir, env=dict(ENV), argv=list(ARGV))
        _, spec = client.up.await_args.args
        sources = {m["source"] for m in spec["mounts"]}
        assert sources == {str(sdir), str(ws / "scripts")}
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_ephemeral_co_session_mounts_only_the_ephemeral_dir(tmp_path: Path):
    """SR-6: a co-drive session must never see a personal workspace — with
    bind mounts that invariant is structural (there is no second mount)."""

    async def _run():
        client = _fake_client()
        prov = _provider(client)
        sdir = tmp_path / "ephemeral_sessions" / "chat1"
        sdir.mkdir(parents=True)
        await prov.spawn(workdir=sdir, env=dict(ENV), argv=list(ARGV))
        _, spec = client.up.await_args.args
        assert spec["mounts"] == [{"source": str(sdir), "target": "/work", "mode": "rw"}]

    asyncio.run(_run())


def test_spawn_stages_runner_py_into_the_session_dir(tmp_path: Path):
    """/work is the bind-mounted session dir, so runner.py is a plain host
    write — no docker round trip, and it stays owned by the gateway uid."""

    async def _run():
        client = _fake_client()
        prov = _provider(client)
        sdir = _session_dir(tmp_path)
        await prov.spawn(workdir=sdir, env=dict(ENV), argv=list(ARGV))
        staged = sdir / "runner.py"
        assert staged.exists()
        assert "AGNES_SESSION_ID" in staged.read_text()

    asyncio.run(_run())


def test_spawn_removes_the_container_when_the_attach_fails(tmp_path: Path):
    """up() succeeded but no handle ever reaches ChatManager, so none of its
    teardown paths know the container name — spawn must clean up its own
    half-built sandbox or it lingers against the host-wide cap."""

    async def _run():
        from app.chat.sandbox_runner_client import SandboxRunnerUnavailable

        client = _fake_client()
        client.open_stream = AsyncMock(side_effect=SandboxRunnerUnavailable("sidecar restarted"))
        prov = _provider(client)
        with pytest.raises(SandboxRunnerUnavailable):
            await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        client.up.assert_awaited_once()
        client.rm.assert_awaited_once_with(container_name("chat1"))

    asyncio.run(_run())


def test_spawn_refuses_without_an_image(tmp_path: Path):
    async def _run():
        prov = _provider(_fake_client(), image="")
        with pytest.raises(RuntimeError, match="chat.docker_image"):
            await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))

    asyncio.run(_run())


def test_spawn_refuses_without_a_session_id(tmp_path: Path):
    async def _run():
        prov = _provider(_fake_client())
        env = {k: v for k, v in ENV.items() if k != "AGNES_SESSION_ID"}
        with pytest.raises(RuntimeError, match="AGNES_SESSION_ID"):
            await prov.spawn(workdir=_session_dir(tmp_path), env=env, argv=list(ARGV))

    asyncio.run(_run())


def test_spawn_refuses_past_the_global_sandbox_cap(tmp_path: Path):
    """Docker sandboxes contend with the gateway host (remote engines offload compute),
    so there is a host-wide ceiling on top of concurrency_per_user."""

    async def _run():
        client = _fake_client()
        client.list_sandboxes = AsyncMock(
            return_value=[{"name": f"agnes-chatsbx-x{i}", "chat_id": f"x{i}"} for i in range(2)]
        )
        prov = _provider(client, max_total_sandboxes=2)
        with pytest.raises(RuntimeError, match="docker_max_total_sandboxes"):
            await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        client.up.assert_not_awaited()

    asyncio.run(_run())


def test_spawn_cap_ignores_this_chat_own_leftover_container(tmp_path: Path):
    """A respawn for the same chat replaces its container — it must not be
    counted against the cap."""

    async def _run():
        client = _fake_client()
        client.list_sandboxes = AsyncMock(return_value=[{"name": container_name("chat1"), "chat_id": "chat1"}])
        prov = _provider(client, max_total_sandboxes=1)
        await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        client.up.assert_awaited_once()

    asyncio.run(_run())


def test_spawn_cap_ignores_stopped_leftovers(tmp_path: Path):
    """`docker_max_total_sandboxes` is documented as a ceiling on LIVE
    sandboxes, but the sidecar lists with all=True (the orphan sweep needs
    exited leftovers) — exited containers whose gateway died before removing
    them must not eat capacity until spawns fail on an idle host."""

    async def _run():
        client = _fake_client()
        client.list_sandboxes = AsyncMock(
            return_value=[
                {"name": "agnes-chatsbx-dead1", "chat_id": "d1", "status": "stopped"},
                {"name": "agnes-chatsbx-live1", "chat_id": "l1", "status": "running"},
            ]
        )
        prov = _provider(client, max_total_sandboxes=2)
        await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        client.up.assert_awaited_once()

    asyncio.run(_run())


def test_egress_mode_none_uses_an_internal_network(tmp_path: Path):
    async def _run():
        client = _fake_client()
        prov = _provider(client, egress_mode="none")
        await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        _, spec = client.up.await_args.args
        assert spec["network"] == "agnes-apps-internal"
        assert spec["internal_network"] is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# streams
# ---------------------------------------------------------------------------


def test_spawn_attaches_with_replay_but_resume_does_not(tmp_path: Path):
    """The runner can emit `runner_ready` before the gateway attaches, so the
    first attach replays what the container already wrote; a post-unpause
    reattach must not, or every frame since session start arrives twice."""

    async def _run():
        client = _fake_client(FakeStream(hold=True))
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        assert client.open_stream.await_args.kwargs["replay"] is True
        await handle.kill(grace_sec=0.01)

        client.status = AsyncMock(return_value={"container": "paused", "exit_code": None})
        resumed = await prov.resume(sandbox_id="agnes-chatsbx-x", runner_pid=1, env={})
        assert client.open_stream.await_args.kwargs["replay"] is False
        await resumed.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_handle_stdout_and_stderr_are_fed_from_the_attach_stream(tmp_path: Path):
    async def _run():
        stream = FakeStream(
            [
                ("stdout", b'{"type":"runner_ready"}\n'),
                ("stdout", b'{"type":"done"}\n'),
                ("stderr", b"warning\n"),
            ]
        )
        client = _fake_client(stream)
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))

        assert await handle.stdout.readline() == b'{"type":"runner_ready"}\n'
        assert await handle.stdout.readline() == b'{"type":"done"}\n'
        assert await handle.stderr.readline() == b"warning\n"

    asyncio.run(_run())


def test_handle_stdin_writes_go_to_the_sidecar(tmp_path: Path):
    async def _run():
        client = _fake_client(FakeStream(hold=True))
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))

        handle.stdin.write(b'{"type":"user_msg","text":"hello"}\n')
        await handle.stdin.drain()

        name, data = client.send_stdin.await_args.args
        assert name == container_name("chat1")
        assert data == b'{"type":"user_msg","text":"hello"}\n'
        # A second drain with nothing buffered must not re-send.
        await handle.stdin.drain()
        assert client.send_stdin.await_count == 1
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_wait_returns_the_container_exit_code(tmp_path: Path):
    async def _run():
        client = _fake_client(FakeStream())  # stream ends => container exited
        client.status = AsyncMock(return_value={"container": "stopped", "exit_code": 3})
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        assert await handle.wait() == 3

    asyncio.run(_run())


def test_wait_reports_a_crash_when_the_attach_drops_but_the_container_lives(tmp_path: Path):
    async def _run():
        client = _fake_client(FakeStream())
        client.status = AsyncMock(return_value={"container": "running", "exit_code": None})
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        # Non-zero => ChatManager treats it as a crash and respawns, rather
        # than leaving the session wired to a dead stream.
        assert await handle.wait() == -1

    asyncio.run(_run())


def test_kill_removes_the_container_and_unblocks_readers(tmp_path: Path):
    async def _run():
        stream = FakeStream(hold=True)
        client = _fake_client(stream)
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))

        await handle.kill(grace_sec=0.05)

        client.rm.assert_awaited_once()
        assert stream.closed is True
        # EOF, not a hang, for anything still on readline().
        assert await handle.stdout.readline() == b""
        # An intentional kill is not a crash.
        assert await handle.wait() == 0

    asyncio.run(_run())


def test_kill_survives_a_sidecar_failure(tmp_path: Path):
    """Teardown is best-effort — a sidecar blip must not wedge kill()."""

    async def _run():
        from app.chat.sandbox_runner_client import SandboxRunnerUnavailable

        client = _fake_client(FakeStream(hold=True))
        client.rm = AsyncMock(side_effect=SandboxRunnerUnavailable("down"))
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        await handle.kill(grace_sec=0.05)
        assert await handle.stdout.readline() == b""

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# pause / resume / destroy / keepalive
# ---------------------------------------------------------------------------


def test_pause_detaches_then_pauses_the_container(tmp_path: Path):
    async def _run():
        stream = FakeStream(hold=True)
        client = _fake_client(stream)
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))

        await prov.pause(handle)

        assert stream.closed is True
        client.pause.assert_awaited_once_with(container_name("chat1"))

    asyncio.run(_run())


def test_detach_cancels_the_pump_before_closing_the_stream(tmp_path: Path):
    """httpx responses are not task-safe: by the time aclose() runs the pump
    must be finished, not suspended mid-read on the same response — pause/kill
    sit on ChatManager's critical path, where a BusyResourceError or a hung
    aclose would wedge the whole reaper sweep."""

    async def _run():
        events: list[str] = []

        class OrderedStream(FakeStream):
            async def __aiter__(self):
                try:
                    await asyncio.Event().wait()  # a real attach only ends on container exit
                except asyncio.CancelledError:
                    events.append("reader_cancelled")
                    raise
                yield  # pragma: no cover — unreachable; makes this an async generator

            async def aclose(self):
                events.append("closed")
                await super().aclose()

        client = _fake_client(OrderedStream())
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        await asyncio.sleep(0)  # let the pump task reach its first read
        await prov.pause(handle)
        assert events == ["reader_cancelled", "closed"]

    asyncio.run(_run())


def test_pump_releases_the_stream_when_the_container_exits_on_its_own(tmp_path: Path):
    """The crash-respawn path replaces the handle without kill(), so the pump
    itself must release the attach stream (and the AsyncClient it owns) when
    the container exits — otherwise every crash respawn leaks one client."""

    async def _run():
        stream = FakeStream([("stdout", b"bye\n")])
        client = _fake_client(stream)
        client.status = AsyncMock(return_value={"container": "stopped", "exit_code": 3})
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        assert await handle.wait() == 3
        assert stream.closed is True
        assert handle._stream is None

    asyncio.run(_run())


def test_pause_propagates_a_sidecar_failure(tmp_path: Path):
    """ChatManager reverts to ACTIVE and kills when pause fails — it must see
    the error rather than a silently un-paused sandbox."""

    async def _run():
        from app.chat.sandbox_runner_client import SandboxRunnerError

        client = _fake_client(FakeStream(hold=True))
        client.pause = AsyncMock(side_effect=SandboxRunnerError(502, "docker_error"))
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        with pytest.raises(SandboxRunnerError):
            await prov.pause(handle)

    asyncio.run(_run())


def test_resume_unpauses_and_reattaches(tmp_path: Path):
    async def _run():
        stream = FakeStream([("stdout", b"back\n")], hold=True)
        client = _fake_client(stream)
        client.status = AsyncMock(return_value={"container": "paused", "exit_code": None})
        prov = _provider(client)

        handle = await prov.resume(sandbox_id="agnes-chatsbx-chat1-abc", runner_pid=1, env={})

        client.resume.assert_awaited_once_with("agnes-chatsbx-chat1-abc")
        assert handle.sandbox_id == "agnes-chatsbx-chat1-abc"
        assert handle.pid == 1
        assert await handle.stdout.readline() == b"back\n"
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_resume_unpauses_before_attaching(tmp_path: Path):
    """The order is forced by the daemon: attach on a paused container is
    refused with 409 ("unpause the container before attach"), so unpause must
    come first — the sub-second wake-up window before the attach completes is
    an accepted, documented reattach gap. Pinned here so a future
    "attach-first to save wake-up output" rewrite (tried once, dead on the
    daemon's 409) doesn't come back without addressing that."""

    async def _run():
        calls: list[str] = []
        client = _fake_client()
        client.status = AsyncMock(return_value={"container": "paused", "exit_code": None})

        async def _open_stream(name, *, replay=False):
            calls.append("attach")
            return FakeStream(hold=True)

        async def _unpause(name):
            calls.append("unpause")
            return {"status": "running"}

        client.open_stream = _open_stream
        client.resume = _unpause
        prov = _provider(client)
        handle = await prov.resume(sandbox_id="agnes-chatsbx-x", runner_pid=1, env={})
        assert calls == ["unpause", "attach"]
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_resume_propagates_an_attach_failure_after_unpause():
    """An attach failure after a successful unpause still raises out of
    resume() — ChatManager's fallback then destroys the (now running)
    container and respawns fresh; nothing is opened that could leak."""

    async def _run():
        from app.chat.sandbox_runner_client import SandboxRunnerError

        client = _fake_client()
        client.status = AsyncMock(return_value={"container": "paused", "exit_code": None})
        client.open_stream = AsyncMock(side_effect=SandboxRunnerError(502, "docker_error"))
        prov = _provider(client)
        with pytest.raises(SandboxRunnerError):
            await prov.resume(sandbox_id="agnes-chatsbx-x", runner_pid=1, env={})
        client.resume.assert_awaited_once_with("agnes-chatsbx-x")

    asyncio.run(_run())


def test_resume_raises_when_the_container_is_gone():
    """Host reboot / manual prune: raising is the contract — ChatManager's
    _respawn_fresh then rebuilds the session with restore-context."""

    async def _run():
        client = _fake_client()
        client.status = AsyncMock(return_value={"container": "absent", "exit_code": None})
        prov = _provider(client)
        with pytest.raises(RuntimeError, match="cannot resume"):
            await prov.resume(sandbox_id="agnes-chatsbx-gone", runner_pid=1, env={})
        client.resume.assert_not_awaited()

    asyncio.run(_run())


def test_resume_raises_when_the_sidecar_is_unreachable():
    async def _run():
        from app.chat.sandbox_runner_client import SandboxRunnerUnavailable

        client = _fake_client()
        client.status = AsyncMock(side_effect=SandboxRunnerUnavailable("down"))
        prov = _provider(client)
        with pytest.raises(SandboxRunnerUnavailable):
            await prov.resume(sandbox_id="agnes-chatsbx-x", runner_pid=1, env={})

    asyncio.run(_run())


def test_resume_reattaches_an_already_running_container():
    """A gateway restart leaves the container running (never paused) — reattach
    rather than refusing."""

    async def _run():
        client = _fake_client(FakeStream(hold=True))
        client.status = AsyncMock(return_value={"container": "running", "exit_code": None})
        prov = _provider(client)
        handle = await prov.resume(sandbox_id="agnes-chatsbx-x", runner_pid=7, env={})
        client.resume.assert_not_awaited()
        assert handle.pid == 7
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_destroy_removes_without_resuming():
    async def _run():
        client = _fake_client()
        prov = _provider(client)
        await prov.destroy(sandbox_id="agnes-chatsbx-dead")
        client.rm.assert_awaited_once_with("agnes-chatsbx-dead")
        client.resume.assert_not_awaited()

    asyncio.run(_run())


def test_destroy_swallows_errors():
    """Paused-TTL reaper path — an already-gone sandbox must not break the sweep."""

    async def _run():
        from app.chat.sandbox_runner_client import SandboxRunnerUnavailable

        client = _fake_client()
        client.rm = AsyncMock(side_effect=SandboxRunnerUnavailable("down"))
        prov = _provider(client)
        await prov.destroy(sandbox_id="agnes-chatsbx-dead")

    asyncio.run(_run())


def test_keepalive_is_a_noop(tmp_path: Path):
    """Protocol: "No-op for local providers" — there is no external timeout to
    extend, so nothing must reach the sidecar."""

    async def _run():
        client = _fake_client(FakeStream(hold=True))
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        await prov.keepalive(handle, timeout_seconds=2100)
        client.status.assert_not_awaited()
        client.up.assert_awaited_once()
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_list_sandboxes_exposes_the_ownership_sweep_input():
    async def _run():
        client = _fake_client()
        client.list_sandboxes = AsyncMock(
            return_value=[{"name": "agnes-chatsbx-a", "chat_id": "a", "age_seconds": 900.0, "status": "running"}]
        )
        prov = _provider(client)
        rows = await prov.list_sandboxes()
        assert rows[0]["chat_id"] == "a"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# file staging + the artifact-harvest `files` shim
# ---------------------------------------------------------------------------


def test_stage_file_writes_through_the_sidecar(tmp_path: Path):
    """The CLI wheel and restore-context land at container-only paths
    (/tmp/...), which no bind mount covers — they must go over the sidecar."""

    async def _run():
        client = _fake_client(FakeStream(hold=True))
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        await prov.stage_file(handle, "/tmp/agnes-cli/agnes.whl", b"WHEEL")
        client.write_file.assert_awaited_once_with(container_name("chat1"), "/tmp/agnes-cli/agnes.whl", b"WHEEL")
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_files_shim_matches_the_shape_artifact_harvest_consumes(tmp_path: Path):
    """`artifact_harvest` reads `handle.files.list(path)` (EntryInfo-shaped
    objects with .name/.path/.type) and `.read(path, format="bytes")`."""

    async def _run():
        client = _fake_client(FakeStream(hold=True))
        client.list_files = AsyncMock(
            return_value=[
                {"name": "a.csv", "path": "/work/outputs/a.csv", "type": "FILE"},
                {"name": "sub", "path": "/work/outputs/sub", "type": "DIR"},
            ]
        )
        client.read_file = AsyncMock(return_value=b"a,b\n")
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))

        entries = await handle.files.list("/work/outputs")
        assert [e.name for e in entries] == ["a.csv", "sub"]
        assert entries[0].path == "/work/outputs/a.csv"

        from app.chat.artifact_harvest import _entry_type

        assert _entry_type(entries[0]) == "FILE"
        assert _entry_type(entries[1]) == "DIR"

        assert await handle.files.read("/work/outputs/a.csv", format="bytes") == b"a,b\n"
        assert await handle.files.read("/work/outputs/a.csv") == b"a,b\n"
        await handle.files.write("/work/outputs/b.csv", b"x")
        client.write_file.assert_awaited_once()
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_handle_implements_the_sandbox_handle_protocol(tmp_path: Path):
    async def _run():
        client = _fake_client(FakeStream(hold=True))
        prov = _provider(client)
        handle = await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))
        for attr in ("pid", "sandbox_id", "stdin", "stdout", "stderr", "wait", "kill"):
            assert hasattr(handle, attr)
        assert isinstance(handle, DockerSandboxHandle)
        # PID 1: the runner IS the container's init process.
        assert handle.pid == 1
        await handle.kill(grace_sec=0.01)

    asyncio.run(_run())


def test_provider_satisfies_the_sandbox_provider_protocol():
    from app.chat.provider import SandboxProvider

    prov = _provider(_fake_client())
    assert isinstance(prov, SandboxProvider)


def test_spawn_cap_raises_the_typed_capacity_error(tmp_path: Path):
    """The cap is a "this host is full" signal, not a broken spawn.

    ChatManager reclaims a paused sandbox and retries on this specific
    error, so it has to be distinguishable from every other RuntimeError
    spawn can raise. Still a RuntimeError subclass — callers that only
    catch the base class keep working.
    """

    async def _run():
        client = _fake_client()
        client.list_sandboxes = AsyncMock(
            return_value=[{"name": f"agnes-chatsbx-x{i}", "chat_id": f"x{i}"} for i in range(2)]
        )
        prov = _provider(client, max_total_sandboxes=2)
        with pytest.raises(SandboxCapacityError):
            await prov.spawn(workdir=_session_dir(tmp_path), env=dict(ENV), argv=list(ARGV))

    asyncio.run(_run())
