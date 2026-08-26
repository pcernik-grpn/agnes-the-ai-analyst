"""Docker-backed SandboxProvider — self-hosted chat sandboxes.

Each chat session runs in a local Docker container.
Every Docker call goes through the apps-runner sidecar
(``app/chat/sandbox_runner_client.py`` → ``services/apps_runner/sandbox_api.py``)
so the gateway process never touches ``/var/run/docker.sock`` — the socket
confinement invariant this repo enforces everywhere else.

Three defining traits of this provider, all deliberate:

**The workspace is bind-mounted, not uploaded** (``syncs_workspace = True``).
The per-session dir becomes ``/work`` and the user's workspace is mounted at the
*same absolute path* the gateway sees, so the session dir's symlinks (``.claude``,
``CLAUDE.md``, ``snapshots``, …) resolve natively inside the container. No
100 MB cap, no per-spawn tar.gz, and files the agent writes persist on the host.
Ephemeral co-drive sessions get the ephemeral dir and nothing else, so SR-6
("never persist back into a personal workspace") holds structurally.

**Pause is ``docker pause``, not a memory snapshot.** Process memory survives
while the daemon does; it does not survive a daemon restart or host reboot.
``resume`` therefore *raises* on anything unexpected and lets ChatManager's
existing ``_respawn_fresh`` fallback rebuild the session with restore-context —
no new degradation path.

**Hardening replaces the microVM boundary.** ``runner.py`` runs with
``permission_mode="bypassPermissions"``; a container is a weaker boundary than a
microVM, so the sidecar always applies ``cap_drop: ALL``, ``no-new-privileges``,
a pids limit, memory/CPU limits, a fixed non-root user, and an image-prefix
allowlist. The spawn env stays exactly what ChatManager handed over — secret-free
by construction (broker tickets ride stdin) and it must stay that way.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Reused rather than re-implemented: the queue-backed reader is the exact
# StreamReader shim ChatManager's pump expects, and its readline() buffering is
# subtle enough that a second copy would drift.
from app.chat.provider import SANDBOX_WORKDIR, _StreamReaderAdapter

# Module-level so unit tests can ``patch("app.chat.docker_provider.SandboxRunnerClient")``.
from app.chat.config import sandbox_can_reach_directly
from app.chat.sandbox_runner_client import SandboxRunnerClient
from app.chat.workdir import WORKSPACE_LINK_ENTRIES

logger = logging.getLogger(__name__)

#: Workspace entries a PROFILE session's sandbox may see, with their mount
#: mode — a fixed allowlist derived from WorkdirManager's own link list
#: (everything it links except the profile-owned ``.claude``/``CLAUDE.md``).
#: ``snapshots`` stays writable (``agnes snapshot create`` lands there); the
#: rest are read-only. Deliberately NOT discovered from the session dir —
#: see the profile branch in ``DockerSandboxProvider._mounts``.
_PROFILE_MOUNT_MODES = {
    entry: ("rw" if entry == "snapshots" else "ro")
    for entry in WORKSPACE_LINK_ENTRIES
    if entry not in (".claude", "CLAUDE.md")
}

#: Container-name prefix; the sidecar refuses to address anything else.
CONTAINER_NAME_PREFIX = "agnes-chatsbx-"

#: Ownership labels — the input to the gateway's orphan-reconciliation sweep.
OWNER_LABEL = "agnes.chat-sandbox"
SESSION_LABEL = "agnes.chat-session"

#: Suffix appended to ``chat.docker_network`` for ``docker_egress_mode: none``.
INTERNAL_NETWORK_SUFFIX = "-internal"

#: Fallback container user when the session dir is root-owned. The sandbox must
#: never run as root (D7); the image's own account owns the writable dirs.
DEFAULT_SANDBOX_USER = "1000:1000"

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_SLUG_MAX = 32


def _owner_ids(path: Path) -> tuple[int, int]:
    """``(uid, gid)`` owning ``path``. Its own function so the container-user
    fallback is unit-testable without patching ``os.stat`` process-wide."""
    st = os.stat(path)
    return int(st.st_uid), int(st.st_gid)


def container_name(chat_id: str) -> str:
    """Deterministic, collision-proof container name for a chat session.

    Deterministic so a respawn reuses (and therefore replaces) the previous
    container instead of leaking it; the sha256 suffix keeps two chat ids that
    sanitize to the same slug apart.
    """
    slug = _NAME_SAFE_RE.sub("-", chat_id).strip("-.")[:_NAME_SLUG_MAX] or "session"
    digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:8]
    return f"{CONTAINER_NAME_PREFIX}{slug}-{digest}"


@dataclass
class EntryInfo:
    """``EntryInfo``-shaped row for the ``files.list`` shim (the file-API
    contract ``app.chat.artifact_harvest`` consumes)."""

    name: str
    path: str
    type: str


class _SandboxFiles:
    """The sandbox file-API surface ``app.chat.artifact_harvest`` consumes
    (``.list(path)`` / ``.read(path, format=...)`` / ``.write(path, data)``),
    backed by the sidecar's file endpoints so headless runs and the agent API
    work unchanged under this provider."""

    def __init__(self, client: SandboxRunnerClient, name: str) -> None:
        self._client = client
        self._name = name

    async def list(self, path: str) -> list[EntryInfo]:
        rows = await self._client.list_files(self._name, path)
        return [
            EntryInfo(
                name=str(r.get("name") or ""),
                path=str(r.get("path") or ""),
                type=str(r.get("type") or "FILE"),
            )
            for r in rows
        ]

    async def read(self, path: str, format: str = "bytes") -> Any:
        data = await self._client.read_file(self._name, path)
        if format == "bytes":
            return data
        return data.decode("utf-8", errors="replace")

    async def write(self, path: str, data: bytes | str) -> None:
        await self._client.write_file(self._name, path, data)


class _StdinWriter:
    """``asyncio.StreamWriter``-shaped adapter over ``POST /sandboxes/*/stdin``.

    Buffers on ``write`` and ships one sidecar call per ``drain`` — matching
    how ChatManager writes (one JSON line, then drain,
    under ``live._stdin_lock``).
    """

    def __init__(self, client: SandboxRunnerClient, name: str) -> None:
        self._client = client
        self._name = name
        self._buf = bytearray()
        self._closed = False

    def write(self, data) -> None:
        if self._closed:
            raise RuntimeError("stdin closed")
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        self._buf.extend(data)

    async def drain(self) -> None:
        if not self._buf:
            return
        payload = bytes(self._buf)
        self._buf.clear()
        await self._client.send_stdin(self._name, payload)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None


@dataclass
class DockerSandboxHandle:
    """SandboxHandle over one chat-sandbox container.

    ``pid`` is always 1: the runner is the container's init process, so the
    ``runner_pid`` ChatManager persists carries no extra information here — the
    container name (``sandbox_id``) is the whole address.
    """

    pid: int
    sandbox_id: str
    stdin: _StdinWriter
    stdout: _StreamReaderAdapter
    stderr: _StreamReaderAdapter
    _client: SandboxRunnerClient
    _stream: Any = None
    _pump_task: asyncio.Task | None = None
    _exit_event: asyncio.Event = field(default_factory=asyncio.Event)
    _detached: bool = False

    @property
    def files(self) -> _SandboxFiles:
        return _SandboxFiles(self._client, self.sandbox_id)

    # --- stream plumbing ------------------------------------------------

    def _start_pump(self) -> None:
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        cancelled = False
        try:
            async for stream, payload in self._stream:
                if stream == "stderr":
                    self.stderr.feed(payload)
                else:
                    self.stdout.feed(payload)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.debug("sandbox %s attach stream failed", self.sandbox_id, exc_info=True)
        finally:
            # The attach only ends when the container exits (or the daemon
            # drops us) — either way nothing more will arrive, and nothing
            # else will ever release the stream's HTTP client: the manager's
            # crash-respawn path replaces this handle without kill(), so a
            # close left to _detach alone leaks one AsyncClient per respawn.
            # On cancellation _detach owns the close instead — an await here
            # would just be re-cancelled mid-cleanup.
            if not cancelled:
                stream, self._stream = self._stream, None
                if stream is not None:
                    with contextlib.suppress(Exception):
                        await stream.aclose()
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exit_event.set()

    async def _detach(self) -> None:
        """Close the attach without touching the container (pause / kill).

        Cancel the pump FIRST, close the stream after: the pump sits
        suspended inside the response's byte iterator, and httpx responses
        are not task-safe — ``aclose()`` under a concurrent reader is
        undefined at the httpcore/anyio layer (worst case a hang or
        ``BusyResourceError`` on the critical path of pause/kill). After the
        await the pump is finished and the stream has exactly one owner.
        """
        self._detached = True
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.aclose()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exit_event.set()

    # --- SandboxHandle Protocol -----------------------------------------

    async def wait(self) -> int:
        """Block until the runner is gone; return its exit code.

        0 when *we* detached (pause / kill — an intentional teardown
        ChatManager must not treat as a crash). Otherwise the container's exit
        code, or -1 when the attach dropped while the container kept running
        (unknown state → let the manager respawn rather than serve a session
        wired to a dead stream).
        """
        await self._exit_event.wait()
        if self._detached:
            return 0
        try:
            status = await self._client.status(self.sandbox_id)
        except Exception:
            logger.debug("sandbox %s status lookup failed after stream end", self.sandbox_id, exc_info=True)
            return -1
        if status.get("container") == "running":
            return -1
        code = status.get("exit_code")
        return int(code) if code is not None else -1

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        """Detach, then stop + remove the container. Best-effort throughout:
        a sidecar outage must never wedge session teardown."""
        await self._detach()
        try:
            await asyncio.wait_for(
                self._client.rm(self.sandbox_id, grace_sec=grace_sec),
                timeout=max(grace_sec, 1.0) + 10.0,
            )
        except Exception:
            logger.warning("sandbox %s removal failed during kill", self.sandbox_id, exc_info=True)


class DockerSandboxProvider:
    """SandboxProvider implementation backed by local Docker containers.

    Constructor params mirror the ``chat.docker_*`` config block:

    image:
        Sandbox image tag (``chat.docker_image``). Empty → ``spawn()`` raises
        at spawn time rather than failing deep inside a session.
    network:
        Docker network the sandbox joins; must be one the Agnes app is also
        attached to so ``AGNES_SERVER`` resolves.
    mem_limit / cpus / pids_limit:
        Always-set resource bounds (D7) — local sandboxes contend with the
        gateway host, unlike offloaded remote compute.
    egress_mode:
        ``open`` (normal bridge, internet reachable), ``none`` (an
        ``internal`` bridge: only containers on that network are reachable),
        or ``allowlist`` (the ``none`` internal bridge PLUS the
        services/egress_proxy sidecar: HTTP(S)_PROXY env points sandboxes
        at the proxy, which enforces the hostname allowlist with a
        post-resolution IP re-check for DNS-rebinding/metadata
        protection).
    egress_proxy_url:
        Where sandboxes find the proxy in ``allowlist`` mode; must resolve
        on the internal network (compose service name).
    max_total_sandboxes:
        Host-wide ceiling checked at spawn, on top of ``concurrency_per_user``.
    """

    syncs_workspace: bool = True  # the workspace is bind-mounted, not pushed

    def __init__(
        self,
        *,
        image: str,
        network: str = "agnes-apps",
        mem_limit: str = "2g",
        cpus: float = 1.0,
        pids_limit: int = 512,
        egress_mode: str = "open",
        egress_proxy_url: str = "",
        max_total_sandboxes: int = 10,
        upload_runner: bool = True,
    ) -> None:
        self._image = image
        self._network = network
        self._mem_limit = mem_limit
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._egress_mode = egress_mode
        self._egress_proxy_url = egress_proxy_url
        self._max_total_sandboxes = max_total_sandboxes
        self._upload_runner = upload_runner
        self._client = SandboxRunnerClient()

    # --- spec building --------------------------------------------------

    def _network_name(self) -> str:
        # ``allowlist`` rides the same internal bridge as ``none`` — the
        # network itself is the enforcement layer (no route out); the
        # egress-proxy sidecar dual-homed onto it is the policy layer.
        if self._egress_mode in ("none", "allowlist"):
            return f"{self._network}{INTERNAL_NETWORK_SUFFIX}"
        return self._network

    def _egress_env(self, spawn_env: "dict[str, str] | None" = None) -> dict[str, str]:
        """Proxy env for ``allowlist`` mode (empty otherwise).

        Cooperative for well-behaved tools (curl, pip, git, httpx all
        honor these); a tool that ignores them gets no route, not open
        egress. NO_PROXY keeps loopback (the in-sandbox broker relay)
        and the Agnes server itself (reached directly over the shared
        internal network, per ``AGNES_SERVER``) off the proxy.
        """
        if self._egress_mode != "allowlist" or not self._egress_proxy_url:
            return {}
        no_proxy = ["127.0.0.1", "localhost"]
        server = (spawn_env or {}).get("AGNES_SERVER", "")
        if server:
            host = urlparse(server).hostname
            # Only bypass the proxy for a host the sandbox can actually reach
            # directly — i.e. one on the shared internal network. NO_PROXY
            # takes PRECEDENCE over the proxy env, so listing a public host
            # here forces a direct connection that a no-route-out network can
            # never make, and the operator cannot recover by allowlisting it.
            # `agnes_server_url()` prefers SERVER_URL (public, set on most
            # deployments for OAuth) over AGNES_INTERNAL_URL, so that is the
            # common case, not an exotic one (Devin Review on #1148).
            #
            # "Directly reachable" is defined once in app/chat/config.py and
            # shared with the boot check, so the two cannot drift: a dotless
            # service name, the docker host alias, or a private IP literal.
            if host and host not in no_proxy and sandbox_can_reach_directly(host):
                no_proxy.append(host)
        p = self._egress_proxy_url
        joined = ",".join(no_proxy)
        return {
            "HTTP_PROXY": p,
            "HTTPS_PROXY": p,
            "http_proxy": p,
            "https_proxy": p,
            "NO_PROXY": joined,
            "no_proxy": joined,
        }

    @staticmethod
    def _workspace_dir(workdir: Path) -> Path | None:
        """The per-user workspace backing ``workdir``'s symlinks, if any.

        ``WorkdirManager`` lays sessions out as
        ``<data>/users/<safe_email>/sessions/<chat_id>`` with symlinks into
        ``<data>/users/<safe_email>/workspace``. Co-drive sessions live under
        ``<data>/ephemeral_sessions/<chat_id>`` and have no personal workspace
        at all — the ``sessions`` parent check is what keeps SR-6 structural.
        """
        if workdir.parent.name != "sessions":
            return None
        candidate = workdir.parent.parent / "workspace"
        return candidate if candidate.is_dir() else None

    def _container_user(self, workdir: Path) -> str:
        """``uid:gid`` the container runs as — the owner of the bind-mounted
        session dir, so writes into ``/work`` and through the workspace
        symlinks land with host-correct ownership."""
        try:
            uid, gid = _owner_ids(workdir)
        except OSError:
            logger.warning("cannot stat %s; falling back to the image's sandbox user", workdir)
            return DEFAULT_SANDBOX_USER
        if uid == 0:
            logger.warning(
                "session dir %s is root-owned; running the sandbox as %s instead "
                "(the sandbox never runs as root) — chown the data dir to the "
                "Agnes service account if the agent hits permission errors",
                workdir,
                DEFAULT_SANDBOX_USER,
            )
            return DEFAULT_SANDBOX_USER
        return f"{uid}:{gid}"

    def _mounts(self, workdir: Path) -> list[dict]:
        mounts = [{"source": str(workdir), "target": SANDBOX_WORKDIR, "mode": "rw"}]
        workspace = self._workspace_dir(workdir)
        if workspace is None:
            return mounts
        if not (workdir / ".claude").is_symlink() and (workdir / ".claude").is_dir():
            # Profile session: WorkdirManager COPIED `.claude`/`CLAUDE.md`
            # into the session dir precisely so the profiled agent works on
            # private settings — mounting the whole workspace would hand it
            # the shared originals anyway (the session dir is the isolation
            # boundary). Mount just
            # the fixed data entries WorkdirManager itself links, so
            # `snapshots` (writable — `agnes snapshot create` lands there)
            # and the read-only extras resolve while the workspace's
            # settings/hook files stay unreachable.
            #
            # The marker above reads agent-writable state (/work), so a
            # plain session whose agent swaps its `.claude` symlink for a
            # real dir gets classified as a profile session on respawn.
            # That mis-classification only ever REMOVES mounts (workspace
            # root withheld) — never adds privilege — and a fresh chat
            # starts from a clean prepare_session_dir, so it self-corrects.
            #
            # The allowlist is NEVER inferred by scanning the session dir:
            # /work is agent-writable, so a symlink the previous run planted
            # (e.g. `snapshots` re-pointed at the workspace `.claude`) would
            # otherwise get itself mounted on the next respawn. Each entry is
            # mounted only if its session-dir symlink still points exactly at
            # the workspace path WorkdirManager linked.
            for entry, mode in _PROFILE_MOUNT_MODES.items():
                link = workdir / entry
                expected = workspace / entry
                if not link.is_symlink():
                    continue
                try:
                    if link.resolve() != expected.resolve() or not expected.exists():
                        continue
                except OSError:
                    continue
                mounts.append({"source": str(expected), "target": str(expected), "mode": mode})
            return mounts
        # Mounted at its own absolute path (not a fixed container path) so
        # the session dir's symlinks resolve without dereferencing.
        mounts.append({"source": str(workspace), "target": str(workspace), "mode": "rw"})
        return mounts

    def _stage_runner(self, workdir: Path) -> None:
        """Write ``runner.py`` into the session dir.

        A plain host write: ``/work`` *is* this directory, so no Docker round
        trip is needed and the file keeps the gateway's ownership (a
        ``put_archive`` copy would land root-owned and block later cleanup).
        Kept out of the image so the runner version always matches the server (Q2).
        """
        try:
            src = Path(__file__).with_name("runner.py").read_text(encoding="utf-8")
            (workdir / "runner.py").write_text(src, encoding="utf-8")
        except OSError:
            logger.exception(
                "failed to stage runner.py into %s; the container will fail at its argv path",
                workdir,
            )

    # --- SandboxProvider Protocol ---------------------------------------

    async def spawn(
        self,
        *,
        workdir: Path,
        env: dict[str, str],
        argv: list[str],
    ) -> DockerSandboxHandle:
        if not self._image:
            raise RuntimeError("chat.docker_image missing — refusing to spawn chat sandbox")
        chat_id = (env.get("AGNES_SESSION_ID") or "").strip()
        if not chat_id:
            raise RuntimeError("AGNES_SESSION_ID missing from sandbox env — refusing to spawn chat sandbox")
        name = container_name(chat_id)

        if self._max_total_sandboxes > 0:
            # Count LIVE sandboxes only — the knob is documented as a ceiling
            # on live ones. The sidecar lists with all=True (the orphan sweep
            # needs exited leftovers), so exited-but-not-yet-reaped containers
            # would otherwise eat capacity until spawns fail on an idle host.
            existing = [
                row
                for row in await self._client.list_sandboxes()
                if row.get("name") != name and row.get("status") != "stopped"
            ]
            if len(existing) >= self._max_total_sandboxes:
                raise RuntimeError(
                    f"docker_max_total_sandboxes reached ({len(existing)}/{self._max_total_sandboxes}) "
                    "— refusing to spawn chat sandbox",
                )

        if self._upload_runner:
            self._stage_runner(workdir)

        spec = {
            "name": name,
            "image": self._image,
            "labels": {OWNER_LABEL: "1", SESSION_LABEL: chat_id},
            "network": self._network_name(),
            "internal_network": self._egress_mode in ("none", "allowlist"),
            # Passed through verbatim — see the module docstring. The
            # allowlist-mode proxy vars ride along; the spawn env stays
            # secret-free either way (guard-pinned).
            "env": {**self._egress_env(env), **dict(env)},
            "cmd": list(argv),
            "working_dir": SANDBOX_WORKDIR,
            "user": self._container_user(workdir),
            "mem_limit": self._mem_limit,
            "cpus": self._cpus,
            "pids_limit": self._pids_limit,
            "mounts": self._mounts(workdir),
        }
        await self._client.up(name, spec)
        try:
            # replay=True: the runner can emit `runner_ready` (or a whole first
            # frame) in the milliseconds between container start and this attach.
            return await self._attach(name, pid=1, replay=True)
        except BaseException:
            # The container exists but no handle ever reaches ChatManager, so
            # none of its teardown paths know this name — remove it here or a
            # failed attach leaves a live sandbox eating the host-wide cap.
            with contextlib.suppress(Exception):
                await self._client.rm(name)
            raise

    async def _attach(self, name: str, *, pid: int, replay: bool = False) -> DockerSandboxHandle:
        stream = await self._client.open_stream(name, replay=replay)
        handle = DockerSandboxHandle(
            pid=pid,
            sandbox_id=name,
            stdin=_StdinWriter(self._client, name),
            stdout=_StreamReaderAdapter(),
            stderr=_StreamReaderAdapter(),
            _client=self._client,
            _stream=stream,
        )
        handle._start_pump()
        return handle

    async def pause(self, handle: DockerSandboxHandle) -> None:
        """Detach, then ``docker pause``.

        Honest semantics: the runner process and its memory survive only
        as long as the daemon does — a daemon restart or host reboot loses them,
        and the next resume raises into ``_respawn_fresh``.
        """
        await handle._detach()
        await self._client.pause(handle.sandbox_id)

    async def resume(
        self,
        *,
        sandbox_id: str,
        runner_pid: int,
        env: dict[str, str],
    ) -> DockerSandboxHandle:
        """``docker unpause`` + reattach.

        The order is forced: the daemon refuses ``POST /containers/{id}/attach``
        on a paused container (409 "unpause the container before attach"), so
        attaching first — which would close the wake-up race the way
        ``spawn()``'s ``replay=True`` closes the start race — is not possible
        at any layer. The sub-second unpause→attach window is an accepted v1
        reattach gap (documented with the others in docs/cloud-chat.md); an
        idle runner emits nothing spontaneously, so it only matters for a
        session paused mid-turn. No replay on the reattach: the container's
        whole log buffer is still there, and re-sending it would re-deliver
        every frame since session start.

        Raises on anything unexpected (container gone, stopped, daemon
        unreachable) — ChatManager's existing resume-failure fallback turns that
        into a fresh spawn with restore-context.
        """
        status = await self._client.status(sandbox_id)
        state = status.get("container")
        if state == "paused":
            await self._client.resume(sandbox_id)
        elif state != "running":
            raise RuntimeError(f"docker sandbox {sandbox_id} is {state!r} — cannot resume")
        return await self._attach(sandbox_id, pid=runner_pid)

    async def keepalive(self, handle: DockerSandboxHandle, *, timeout_seconds: int) -> None:
        """No-op: a local container has no external timeout to extend."""
        return

    async def destroy(self, *, sandbox_id: str) -> None:
        """Remove a paused sandbox without resuming it (paused-TTL reaper)."""
        try:
            await self._client.rm(sandbox_id)
        except Exception:
            logger.info("destroy: sandbox removal failed for %s", sandbox_id, exc_info=True)

    # --- duck-typed extras ChatManager uses -----------------------------

    async def stage_file(self, handle: DockerSandboxHandle, path: str, data: bytes | str) -> None:
        """Provider-mediated file staging (agnes CLI wheel, restore-context).

        These land at container-only paths under ``/tmp`` that no bind mount
        covers, so they ride the sidecar rather than a host write.
        """
        await self._client.write_file(handle.sandbox_id, path, data)

    async def list_sandboxes(self) -> list[dict]:
        """Every container carrying the ownership label — input to
        ``ChatManager.reap_orphan_sandboxes``."""
        return await self._client.list_sandboxes()
