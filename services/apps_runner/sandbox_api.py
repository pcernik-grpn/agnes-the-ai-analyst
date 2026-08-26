"""apps-runner — chat-sandbox (`/sandboxes/*`) half of the Docker socket API.

Same posture as `/apps/*` (see ``services/apps_runner/api.py``): the sidecar is
the only process holding the Docker socket, it holds no policy, and every route
is token-gated with ``X-Runner-Token`` (fail-closed). The Agnes gateway decides
*what* to run — image, mounts, limits, argv — this module only translates that
into Docker calls. Its caller is ``app/chat/sandbox_runner_client.py``.

Two narrowings on top of the `/apps/*` posture, because these containers run
agent-authored code with ``permission_mode=bypassPermissions`` inside:

- **Name confinement.** Every route only addresses containers matching
  ``agnes-chatsbx-*`` (``_SANDBOX_NAME_RE``), so this API can never touch a data
  app, a compose service, or an operator's own container.
- **Mount confinement.** Bind sources must be absolute, ``..``-free, and outside
  the system prefixes in ``_DENY_MOUNT_PREFIXES`` (notably ``/var/run``, home of
  the Docker socket), capped at ``_MAX_MOUNTS``. Sources are translated through
  ``_resolve_host_path`` exactly like the `/apps/*` config-dir mount.

Attach transport: chunked NDJSON on ``GET /sandboxes/{name}/stream`` (one frame
per demultiplexed Docker chunk, payload base64 so byte-exactness survives) plus
one ``POST /sandboxes/{name}/stdin`` per host→runner JSON line. WebSocket was
the other candidate; NDJSON+POST won because this sidecar is a *sync* FastAPI
app (the Docker SDK's attach socket is blocking, and Starlette already runs sync
generators on a worker thread), stdin frames are low-rate JSON lines, and
reattach after ``docker unpause`` is just a new GET. The contract — byte-exact
JSONL both ways, reattachable — is the same either way.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import io
import json
import os
import re
import socket as _socket
import tarfile
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import PurePosixPath

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])

#: Container-name prefix every chat sandbox carries. Mirrors the `/apps/*`
#: ``agnes-dataapp-`` convention; enforced on every route (see module docstring).
SANDBOX_NAME_PREFIX = "agnes-chatsbx-"
_SANDBOX_NAME_RE = re.compile(rf"^{re.escape(SANDBOX_NAME_PREFIX)}[A-Za-z0-9][A-Za-z0-9._-]{{0,80}}$")

#: Ownership label the gateway stamps on every sandbox (orphan reconciliation).
OWNER_LABEL = "agnes.chat-sandbox"
SESSION_LABEL = "agnes.chat-session"

#: A chat sandbox needs at most six binds: the session dir, plus EITHER the
#: user's whole workspace (plain sessions) OR the individual data symlink
#: targets a profile session is allowed to see (snapshots / scripts /
#: scaffolds / CLAUDE.local.md — the settings originals stay unmounted).
#: Anything beyond that is a malformed or hostile spec.
_MAX_MOUNTS = 6
_DENY_MOUNT_PREFIXES = ("/var/run", "/var/lib/docker", "/etc", "/proc", "/sys", "/dev", "/boot", "/root")

#: Ceiling on a single ``op=read`` tar so a runaway file in the outputs dir
#: can't exhaust the sidecar. Bounds the on-disk spool (the tar never sits in
#: memory — this process runs under a small cgroup limit next to the data-app
#: control plane), and stays above the artifact-harvest cap
#: (``chat.agent_api_artifact_max_bytes``, 25 MB default) so harvest-sized
#: reads always pass.
_MAX_FILE_READ_BYTES = 64 * 1024 * 1024

#: RAM threshold before an ``op=read`` spool spills to disk.
_READ_SPOOL_RAM_BYTES = 8 * 1024 * 1024

#: Frames buffered between an attach's reader thread and its HTTP response.
#: When it fills, the reader blocks — kernel socket buffers then throttle the
#: container's stdout exactly like the pre-thread design did.
_STREAM_QUEUE_MAX = 1024


def _api():
    """The `/apps/*` module, imported lazily.

    Lazy because ``api`` imports *this* module at its bottom to mount the
    router; a module-level back-import would make the cycle order-dependent.
    Going through the module object (rather than importing the helpers by
    value) also means tests that monkeypatch ``api._docker`` — the established
    sidecar seam — cover these handlers too.
    """
    from services.apps_runner import api as _mod

    return _mod


def _docker_errors(fn):
    """Delegate to ``api._docker_errors`` at call time (single source of truth
    for ImageNotFound → 400 / APIError → 502 mapping, no import-time cycle),
    wrapping ``fn`` once on first use rather than on every request.

    The outer wrapper must match ``fn``'s sync/async nature — FastAPI decides
    threadpool-vs-await from the registered callable, and a sync wrapper around
    a coroutine function would hand Starlette an unawaited coroutine.
    """

    wrapped = None

    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            nonlocal wrapped
            if wrapped is None:
                wrapped = _api()._docker_errors(fn)
            return await wrapped(*args, **kwargs)

        return awrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        nonlocal wrapped
        if wrapped is None:
            wrapped = _api()._docker_errors(fn)
        return wrapped(*args, **kwargs)

    return wrapper


def _guard(name: str, x_runner_token: str | None):
    """Token + name gate every route runs first. Returns the Docker client."""
    _api()._check_token(x_runner_token)
    if not _SANDBOX_NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="bad_sandbox_name")
    return _api()._docker()


def _require_container(name: str):
    c = _api()._container(name)
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    return c


def _safe_abs_path(path: str) -> str:
    """An absolute, ``..``-free POSIX path, or 400."""
    if not path or not path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise HTTPException(status_code=400, detail="bad_path")
    return path


def _validate_mounts(mounts) -> dict:
    """Turn the spec's mount list into docker-py's ``volumes`` dict.

    Sources are host-namespace-translated (``_resolve_host_path``) because a
    containerized gateway computes them in *its* mount namespace, while the
    daemon resolves bind sources in the host's.
    """
    if not isinstance(mounts, list) or len(mounts) > _MAX_MOUNTS:
        raise HTTPException(status_code=400, detail="bad_mount")
    volumes: dict = {}
    for m in mounts:
        if not isinstance(m, dict):
            raise HTTPException(status_code=400, detail="bad_mount")
        source = str(m.get("source") or "")
        target = str(m.get("target") or "")
        mode = str(m.get("mode") or "rw")
        if mode not in ("rw", "ro"):
            raise HTTPException(status_code=400, detail="bad_mount")
        for p in (source, target):
            if not p.startswith("/") or p == "/" or ".." in PurePosixPath(p).parts:
                raise HTTPException(status_code=400, detail="bad_mount")
        if any(source == d or source.startswith(d + "/") for d in _DENY_MOUNT_PREFIXES):
            raise HTTPException(status_code=400, detail="bad_mount")
        volumes[_api()._resolve_host_path(source)] = {"bind": target, "mode": mode}
    return volumes


def _state(container) -> str:
    """``running`` | ``paused`` | ``stopped`` — same folding as `/apps/*`."""
    if container.status == "paused":
        return "paused"
    if container.status == "running":
        return "running"
    return "stopped"


def _age_seconds(container) -> float:
    """Container age from ``attrs["Created"]``; 0.0 when unparseable.

    0.0 is the safe default: the orphan sweep skips young containers, so a
    parse failure can never make it reap a sandbox that is still being wired up.

    ``fromisoformat`` on Python ≥3.11 (this repo's floor) accepts the whole
    RFC3339Nano surface Docker emits — bare ``Z``, no fraction, or 1–9
    fractional digits (Go trims trailing zeros) — so the raw value parses
    directly. A hand-rolled fraction normalizer here previously corrupted
    short fractions by borrowing digits from the UTC offset, which made the
    sweep read such a container as brand new forever.
    """
    raw = str((container.attrs or {}).get("Created") or "")
    if not raw:
        return 0.0
    try:
        created = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - created).total_seconds())


# ---------------------------------------------------------------------------
# probe (declared first: a literal path segment, never a {name})
# ---------------------------------------------------------------------------


@router.get("/probe")
def sandbox_probe(image: str = "", x_runner_token: str | None = Header(default=None)):
    """Readiness probe for the docker chat provider: daemon reachable + image
    present. Never raises on a Docker failure — the classified ``{ok, detail}``
    is what the boot gate and the admin test-connections surface render."""
    _api()._check_token(x_runner_token)
    import docker.errors

    daemon = False
    image_present = False
    detail = ""
    try:
        _api()._docker().ping()
        daemon = True
    except Exception as exc:  # noqa: BLE001 — classify, never raise to the caller
        detail = f"docker daemon unreachable: {exc}"
    if daemon:
        if not image:
            detail = "no sandbox image configured (chat.docker_image)"
        else:
            try:
                _api()._docker().images.get(image)
                image_present = True
                detail = "docker sandbox runner ready"
            except docker.errors.ImageNotFound:
                detail = f"sandbox image {image} not present on the Docker host — build it first"
            except Exception as exc:  # noqa: BLE001
                detail = f"docker image lookup failed: {exc}"
    return {"ok": daemon and image_present, "daemon": daemon, "image": image_present, "detail": detail}


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


@router.post("/{name}/up")
@_docker_errors
def sandbox_up(name: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    """Create + start a chat sandbox container from ``payload["spec"]``.

    Deliberately NO restart policy: a runner that dies must stay dead so
    ChatManager owns the respawn decision (crash counting, restore-context).
    Replaces any container of the same name — the gateway's names are
    deterministic per chat, so this doubles as leftover cleanup.
    """
    client = _guard(name, x_runner_token)
    spec = payload["spec"]
    if str(spec.get("name") or "") != name:
        raise HTTPException(status_code=400, detail="bad_sandbox_name")
    prefix = os.environ.get("CHAT_SANDBOX_IMAGE_PREFIX", "")
    image = str(spec.get("image") or "")
    if not prefix or not image.startswith(prefix + ":"):
        raise HTTPException(status_code=400, detail="image_not_allowed")
    volumes = _validate_mounts(spec.get("mounts") or [])

    network = str(spec.get("network") or "")
    if network:
        # Docker's `name` network filter is a SUBSTRING match, so a request
        # for `agnes-apps` also matches `agnes-apps-internal` — left as-is
        # that both skips creating a network that does not exist (every
        # spawn then 502s on an unknown network) and runs the Internal check
        # below against a network the sandbox will never join
        # (Devin Review on #1148).
        existing_nets = [n for n in client.networks.list(names=[network]) if getattr(n, "name", n) == network]
        if not existing_nets:
            net_kwargs: dict = {"driver": "bridge"}
            if spec.get("internal_network"):
                # `internal` bridges have no route off the host: the sandbox
                # can only reach containers attached to the same network.
                net_kwargs["internal"] = True
            client.networks.create(network, **net_kwargs)
        elif spec.get("internal_network"):
            # The isolation must hold when the network already exists too — a
            # same-named non-internal bridge (pre-created by an operator, or
            # left by an older version) would silently hand a no-egress
            # sandbox full internet access. Fail closed; the operator removes
            # or renames the conflicting network.
            attrs = getattr(existing_nets[0], "attrs", None) or {}
            if not attrs.get("Internal"):
                raise HTTPException(status_code=400, detail="network_not_internal")

    old = _api()._container(name)
    if old is not None:
        old.remove(force=True)

    pids_limit = int(spec.get("pids_limit") or 0)
    client.containers.run(
        image,
        name=name,
        detach=True,
        # docker-init (tini) as PID 1: the runner shells out constantly
        # (agent Bash tool, `claude` CLI, pip) and Python does not reap
        # re-parented grandchildren — without an init each orphaned child
        # would linger as a zombie against the pids limit for the life of
        # the session. (A microVM-style sandbox with its own init would not.)
        init=True,
        # Keeps the container's stdin open across attach/detach cycles so the
        # gateway can push `user_msg` / `ticket_push` frames at any time
        # (StdinOnce stays false — docker only closes stdin on detach when it
        # is set).
        stdin_open=True,
        tty=False,
        command=spec.get("cmd") or None,
        working_dir=str(spec.get("working_dir") or "") or None,
        environment=spec.get("env") or {},
        labels=spec.get("labels") or {},
        network=network or None,
        volumes=volumes,
        mem_limit=spec.get("mem_limit") or None,
        nano_cpus=int(float(spec.get("cpus") or 1.0) * 1e9),
        pids_limit=pids_limit or None,
        user=str(spec.get("user") or "") or None,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        # Linux needs the explicit mapping for `host.docker.internal` to
        # resolve; bare-host operators point AGNES_INTERNAL_URL at it.
        extra_hosts={"host.docker.internal": "host-gateway"},
    )
    return {"status": "started"}


@router.post("/{name}/pause")
@_docker_errors
def sandbox_pause(name: str, x_runner_token: str | None = Header(default=None)):
    _guard(name, x_runner_token)
    _require_container(name).pause()
    return {"status": "paused"}


@router.post("/{name}/resume")
@_docker_errors
def sandbox_resume(name: str, x_runner_token: str | None = Header(default=None)):
    _guard(name, x_runner_token)
    _require_container(name).unpause()
    return {"status": "running"}


@router.post("/{name}/rm")
@_docker_errors
def sandbox_rm(
    name: str,
    payload: dict | None = Body(default=None),
    x_runner_token: str | None = Header(default=None),
):
    """Remove the sandbox. Idempotent: an already-absent one is a success.

    ``payload["grace_sec"] > 0`` stops the container first (SIGTERM, then
    SIGKILL after the grace) so a runner that installed a handler can flush;
    the default force-removes immediately.
    """
    _guard(name, x_runner_token)
    c = _api()._container(name)
    if c is None:
        return {"status": "absent"}
    grace = float((payload or {}).get("grace_sec") or 0)
    if grace > 0:
        try:
            c.stop(timeout=int(grace) or 1)
        except Exception:  # noqa: BLE001, S110 — a stop failure must not block removal
            pass
    c.remove(force=True)
    return {"status": "removed"}


@router.get("/{name}/status")
@_docker_errors
def sandbox_status(name: str, x_runner_token: str | None = Header(default=None)):
    """``running`` | ``paused`` | ``stopped`` | ``absent`` (+ exit code once
    stopped, which the provider's ``wait()`` reports to ChatManager)."""
    _guard(name, x_runner_token)
    c = _api()._container(name)
    if c is None:
        return {"container": "absent", "exit_code": None}
    state = _state(c)
    exit_code = None
    if state == "stopped":
        raw = ((c.attrs or {}).get("State") or {}).get("ExitCode")
        exit_code = int(raw) if raw is not None else None
    return {"container": state, "exit_code": exit_code}


@router.get("")
@_docker_errors
def list_sandboxes(x_runner_token: str | None = Header(default=None)):
    """Every container carrying the ownership label — the input to the
    gateway's orphan reconciliation sweep."""
    _api()._check_token(x_runner_token)
    rows = []
    for c in _api()._docker().containers.list(all=True, filters={"label": f"{OWNER_LABEL}=1"}):
        labels = c.labels or {}
        rows.append(
            {
                "name": c.name,
                "status": _state(c),
                "chat_id": labels.get(SESSION_LABEL, ""),
                "age_seconds": _age_seconds(c),
            }
        )
    return {"sandboxes": rows}


# ---------------------------------------------------------------------------
# streams
# ---------------------------------------------------------------------------


def _shutdown_attach_socket(sock) -> None:
    """Best-effort shutdown+close of an attach socket.

    ``shutdown`` (not just ``close``) is what reliably wakes a reader thread
    blocked mid-``read`` on the other side of the hijacked connection; the
    held HTTP response (see ``_attach_buffered_reader``) is closed in between
    so its own destructor doesn't later trip over an already-closed file.
    """
    raw = getattr(sock, "_sock", None)
    if raw is not None and hasattr(raw, "shutdown"):
        with contextlib.suppress(Exception):
            raw.shutdown(_socket.SHUT_RDWR)
    response = getattr(sock, "_response", None)
    if response is not None:
        with contextlib.suppress(Exception):
            response.close()
    with contextlib.suppress(Exception):
        sock.close()


def _attach_buffered_reader(sock):
    """The buffered file object over an ``attach_socket`` connection, or
    ``None`` when the transport doesn't expose one.

    Reading the raw socket that ``attach_socket`` returns is WRONG for a
    replay attach: http.client parses the response headers through a
    ``BufferedReader`` whose read-ahead can pull the first stream bytes into
    its buffer — with ``logs=1`` the daemon writes the backlog immediately
    after the headers, so the replayed frames (e.g. the runner's
    ``runner_ready``) routinely sit in that buffer and a raw-socket reader
    never sees them. docker-py keeps the response alive on the socket as
    ``sock._response`` (its own GC guard); its ``raw._fp.fp`` is that
    ``BufferedReader`` — reading from it drains the buffer first, then the
    socket, in order.
    """
    response = getattr(sock, "_response", None)
    try:
        return response.raw._fp.fp  # noqa: SLF001 — the same shape docker-py's CancellableStream digs
    except AttributeError:
        return None


def _demuxed_frames(read):
    """Docker's non-tty attach multiplexing over a blocking ``read(n)``:
    yields ``(stream_id, payload)`` per frame, ends on EOF/short read."""
    while True:
        header = read(8)
        if len(header) < 8:
            return
        n = int.from_bytes(header[4:8], "big")
        payload = read(n) if n else b""
        if payload:
            yield header[0], payload
        if n and len(payload) < n:
            return


@router.get("/{name}/stream")
@_docker_errors
async def sandbox_stream(
    name: str,
    replay: bool = False,
    x_runner_token: str | None = Header(default=None),
):
    """Attach to the container and stream demultiplexed output as NDJSON.

    One line per Docker chunk: ``{"stream": "stdout"|"stderr", "data": <b64>}``.

    ``replay`` decides whether Docker first re-sends what the container already
    wrote. The gateway sets it on the FIRST attach after create — the runner
    emits ``runner_ready`` (and can emit whole frames) in the milliseconds
    between start and attach, and without replay those frames are gone. On a
    post-``unpause`` reattach it must stay off, or every frame since session
    start would be delivered a second time.

    The attach socket is read on a DEDICATED thread, never the shared anyio
    worker pool: a quiet chat session emits nothing for minutes, and Starlette
    drives a sync generator with one pool token held across each blocking
    ``next()`` — a handful of live attaches would starve every sync handler in
    the process, including the ``/apps/*`` data-app control plane. Worse, a
    dropped client leaves such a pool thread blocked in ``recv`` until the
    container next speaks; here teardown shuts the socket down, which ends the
    reader thread immediately.

    Reader errors end the stream (the response has already started, so there
    is no status code left to change).
    """
    # Token + name checks are pure computation and stay inline; everything
    # that talks to the daemon (client handshake, container lookup, the
    # attach POST + hijack) runs on a worker thread — this handler is async
    # (so its streaming side can live off the shared pool), and a blocking
    # SDK call here would stall the sidecar's whole event loop, freezing
    # `/apps/*` and every other request while the daemon answers.
    _api()._check_token(x_runner_token)
    if not _SANDBOX_NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="bad_sandbox_name")

    def _open_attach():
        container = _require_container(name)
        # The raw hijacked connection (rather than `attach(stream=True)`'s
        # generator): holding the socket directly is what lets teardown
        # unblock the reader thread with a shutdown(). Frames are read
        # through the connection's BufferedReader, never the raw socket —
        # see _attach_buffered_reader for why raw reads lose replayed frames.
        return container.attach_socket(params={"stdout": 1, "stderr": 1, "stream": 1, "logs": 1 if replay else 0})

    sock = await asyncio.to_thread(_open_attach)
    fp = _attach_buffered_reader(sock)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX)
    stop = threading.Event()

    def _enqueue(item: str | None) -> bool:
        """Backpressured put from the reader thread, abortable via ``stop``."""
        fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        while True:
            try:
                fut.result(timeout=0.5)
                return True
            except TimeoutError:
                if stop.is_set():
                    fut.cancel()
                    return False
            except Exception:
                return False

    def _sock_read(n: int) -> bytes:
        """Exact-n fallback read off the raw socket (non-unix transports,
        where no buffered reader is exposed; header read-ahead loss does not
        arise there because nothing pre-reads the hijacked stream)."""
        buf = b""
        while len(buf) < n:
            chunk = sock.read(n - len(buf)) if hasattr(sock, "read") else sock.recv(n - len(buf))
            if not chunk:
                return buf
            buf += chunk
        return buf

    def _read_frames() -> None:
        read = fp.read if fp is not None else _sock_read
        try:
            for stream_id, payload in _demuxed_frames(read):
                stream_name = "stderr" if stream_id == 2 else "stdout"
                line = json.dumps({"stream": stream_name, "data": base64.b64encode(payload).decode()}) + "\n"
                if not _enqueue(line):
                    return
        except Exception:  # noqa: BLE001, S110 — container gone / daemon blip ends the stream
            pass
        finally:
            # Best-effort EOF sentinel; suppressed because the loop itself may
            # already be gone on process shutdown.
            with contextlib.suppress(Exception):
                if not stop.is_set():
                    _enqueue(None)

    threading.Thread(target=_read_frames, name=f"chatsbx-attach-{name}", daemon=True).start()

    async def _frames():
        try:
            while True:
                line = await queue.get()
                if line is None:
                    return
                yield line
        finally:
            # Runs on natural end AND on client disconnect (Starlette acloses
            # the generator) — either way the reader thread must not outlive
            # the response blocked in recv.
            stop.set()
            _shutdown_attach_socket(sock)

    return StreamingResponse(_frames(), media_type="application/x-ndjson")


@router.post("/{name}/stdin")
@_docker_errors
def sandbox_stdin(name: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    """Write one frame to the container's stdin.

    A fresh attach socket per frame (rather than a pinned one) keeps the
    sidecar stateless; safe because ``up`` creates the container with
    ``stdin_open=True`` and StdinOnce false, so detaching never EOFs the
    runner's stdin.
    """
    _guard(name, x_runner_token)
    container = _require_container(name)
    data = base64.b64decode(payload.get("data_b64") or "")
    sock = container.attach_socket(params={"stdin": 1, "stream": 1})
    try:
        raw = getattr(sock, "_sock", sock)
        if hasattr(raw, "sendall"):
            raw.sendall(data)
        else:  # pragma: no cover — SDK transports that hand back a file object
            raw.write(data)
            raw.flush()
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001, S110 — best-effort close; the write already landed
            pass
    return {"status": "ok", "bytes": len(data)}


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


@router.post("/{name}/files")
@_docker_errors
def sandbox_write_file(name: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    """Write one file into the container (agnes CLI wheel, restore-context).

    The archive is rooted at ``/`` with the path relative, so tar creates any
    missing intermediate directories (``/tmp/agnes-cli/``) — ``put_archive``
    against a non-existent directory would otherwise fail.
    """
    _guard(name, x_runner_token)
    container = _require_container(name)
    path = _safe_abs_path(str(payload.get("path") or ""))
    data = base64.b64decode(payload.get("content_b64") or "")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=path.lstrip("/"))
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    container.put_archive("/", buf.getvalue())
    return {"status": "written", "bytes": len(data)}


class _ChunkReader:
    """Minimal read-only file object over ``get_archive``'s chunk iterator,
    for streaming (``r|``) tarfile parses — nothing accumulates past one
    chunk plus tarfile's own block buffer."""

    def __init__(self, chunks) -> None:
        self._chunks = iter(chunks)
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


def _stream_member_bytes(bits) -> StreamingResponse:
    """``op=read``: spool the tar to a temp file, stream the member back raw.

    The spool spills to disk past ``_READ_SPOOL_RAM_BYTES`` and is capped at
    ``_MAX_FILE_READ_BYTES`` (→ 413), so the sidecar's peak memory stays a
    chunk-sized constant regardless of file size — the previous in-memory
    ``blob`` + tar copy + base64-JSON encoding held ~4× the file size at once,
    enough for one large agent-written file to OOM the whole sidecar.
    """
    spool = tempfile.SpooledTemporaryFile(max_size=_READ_SPOOL_RAM_BYTES)
    try:
        total = 0
        for chunk in bits:
            total += len(chunk)
            if total > _MAX_FILE_READ_BYTES:
                raise HTTPException(status_code=413, detail="file_too_large")
            spool.write(chunk)
        spool.seek(0)
        tar = tarfile.open(fileobj=spool)
        member = next((m for m in tar if m.isfile()), None)
        fh = tar.extractfile(member) if member is not None else None
        if fh is None:
            raise HTTPException(status_code=404, detail="not_found")
    except BaseException:
        spool.close()
        raise

    def _content():
        try:
            while True:
                chunk = fh.read(512 * 1024)
                if not chunk:
                    return
                yield chunk
        finally:
            spool.close()

    return StreamingResponse(_content(), media_type="application/octet-stream")


@router.get("/{name}/files")
@_docker_errors
def sandbox_read_files(
    name: str,
    path: str,
    op: str = "list",
    x_runner_token: str | None = Header(default=None),
):
    """``op=list`` → the directory's immediate entries; ``op=read`` → one
    file's raw bytes (``application/octet-stream``).

    Both ride ``get_archive`` (a tar of the path) so no in-container exec is
    needed — the sandbox stays a single-process container. Neither buffers
    the tar in memory (this process runs under a small cgroup limit next to
    the data-app control plane): ``read`` spools to a size-capped temp file
    and streams the member out; ``list`` parses the tar as a pure stream,
    discarding file contents as they pass, so listing a directory holding
    more data than the read cap works instead of aborting with 413 (a
    directory's archive is its whole recursive subtree).
    """
    import docker.errors

    _guard(name, x_runner_token)
    container = _require_container(name)
    path = _safe_abs_path(path).rstrip("/") or "/"
    try:
        bits, _stat = container.get_archive(path)
    except docker.errors.NotFound as exc:
        raise HTTPException(status_code=404, detail="not_found") from exc

    if op == "read":
        return _stream_member_bytes(bits)

    root = PurePosixPath(path).name
    entries = []
    with tarfile.open(fileobj=_ChunkReader(bits), mode="r|") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != root or len(parts) != 2:
                continue
            entries.append(
                {
                    "name": parts[1],
                    "path": f"{path}/{parts[1]}",
                    "type": "DIR" if member.isdir() else "FILE",
                }
            )
    return {"entries": entries}
