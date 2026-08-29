"""FastAPI route serving the per-user bare repo over git smart-HTTP.

Registered at `/marketplace.git/{path:path}` (GET + POST). Claude Code
registers the URL:

    /plugin marketplace add https://x:<PAT>@host/marketplace.git/

git CLI does not speak Bearer tokens — it only sends HTTP Basic. By
convention (same as GitHub PATs) the username is ignored and the password
field carries the bearer token. We extract it, validate via the shared
`resolve_token_to_user`, resolve the caller's filtered bare repo via
`git_backend.ensure_repo_for_user`, then hand the request off to the real
`git http-backend` CLI binary, run as an OS subprocess speaking the CGI
protocol (the same mechanism nginx+fcgiwrap / Apache mod_cgi use to serve
git smart-HTTP).

This used to be served by dulwich's pure-Python `HTTPGitApplication` bridged
into ASGI via `a2wsgi.WSGIMiddleware`. Even though the WSGI call was offloaded
to a thread pool, it stayed inside the single uvicorn OS process — and
dulwich's smart-HTTP pack generation is CPU-heavy pure-Python work that holds
the GIL for seconds at a time, starving the asyncio event loop (health checks,
every other request) even though it looked "off-thread". Running the real
`git http-backend` binary as a genuine subprocess releases the GIL completely
during pack generation; the parent process only awaits async subprocess I/O.

Repo *building* (`git_backend.ensure_repo_for_user`) is untouched — that part
is fast, ETag-cached, and ends before the DB connection closes. Only the
*serving* step changed.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.auth.pat_resolver import resolve_token_to_user
from app.marketplace_server import git_backend
from src.audit_helpers import log_safe
from src.db import get_system_db

logger = logging.getLogger(__name__)

router = APIRouter()

_GIT_HTTP_BACKEND = ("git", "http-backend")


def token_from_basic_auth(auth_header: Optional[str]) -> Optional[str]:
    """Extract the password (= PAT in our scheme) from an HTTP Basic header.

    Username is discarded; git CLI typically sends `x`, `x-access-token`,
    `git`, etc. Returns None for missing / malformed / non-Basic headers.
    """
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    _, _, password = decoded.partition(":")
    return password or None


def _unauthorized() -> Response:
    return Response(
        content=b"authentication required\n",
        status_code=401,
        media_type="text/plain; charset=utf-8",
        headers={"WWW-Authenticate": 'Basic realm="agnes-marketplace"'},
    )


def _server_error() -> Response:
    return Response(
        content=b"internal server error\n",
        status_code=500,
        media_type="text/plain; charset=utf-8",
    )


def _build_cgi_env(request: Request, path: str, repo_path: Path, remote_user: Optional[str], body_length: int) -> dict:
    """Build the CGI/1.1 environment `git http-backend` expects.

    URL translation per `man git-http-backend`: the backend concatenates
    `GIT_PROJECT_ROOT` + `PATH_INFO` to find the repo on disk. Our
    `repo_path` already points at the exact bare repo for this caller (not a
    directory of repos), so `GIT_PROJECT_ROOT=repo_path` + `PATH_INFO=/<path>`
    resolves to `<repo_path>/<path>` — e.g. `<repo_path>/info/refs`.

    `body_length` is `len()` of the buffered request body actually written to
    the subprocess's stdin, not the client's `Content-Length` header — a
    chunked-transfer request (no `Content-Length` at all) would otherwise
    leave `CONTENT_LENGTH` unset and `git http-backend` reads a zero-length
    body, failing the fetch even though the body was received in full.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_PROJECT_ROOT": str(repo_path),
            "PATH_INFO": f"/{path}",
            "REQUEST_METHOD": request.method,
            "QUERY_STRING": request.url.query or "",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "GATEWAY_INTERFACE": "CGI/1.1",
        }
    )
    content_type = request.headers.get("content-type")
    if content_type:
        env["CONTENT_TYPE"] = content_type
    if body_length:
        env["CONTENT_LENGTH"] = str(body_length)
    if remote_user:
        env["REMOTE_USER"] = remote_user
    # Modern git (protocol v2) negotiation — forward the client's declared
    # protocol version if present, same as the Apache/nginx recipes in
    # `man git-http-backend` (SetEnvIf Git-Protocol ... GIT_PROTOCOL=$0).
    git_protocol = request.headers.get("git-protocol")
    if git_protocol:
        env["GIT_PROTOCOL"] = git_protocol
    return env


def _parse_cgi_status(header_block: bytes) -> tuple[int, list[tuple[str, str]]]:
    """Split the CGI response preamble into (status_code, header_pairs).

    `git http-backend` emits a `Status: <code> <reason>` header per the CGI
    convention (not a raw HTTP status line). Absence of `Status:` implies 200
    per the CGI spec.
    """
    status_code = 200
    headers: list[tuple[str, str]] = []
    for line in header_block.split(b"\r\n" if b"\r\n" in header_block else b"\n"):
        line = line.strip(b"\r\n")
        if not line:
            continue
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        name_s = name.strip().decode("latin-1")
        value_s = value.strip().decode("latin-1")
        if name_s.lower() == "status":
            try:
                status_code = int(value_s.split(" ", 1)[0])
            except ValueError:
                status_code = 200
            continue
        headers.append((name_s, value_s))
    return status_code, headers


async def _read_cgi_headers(stdout: asyncio.StreamReader) -> bytes:
    """Read stdout up to (and including) the blank line separating CGI
    headers from the body."""
    header_bytes = b""
    while True:
        line = await stdout.readline()
        if not line:
            break
        header_bytes += line
        if line in (b"\r\n", b"\n"):
            break
    return header_bytes


async def _drain_stderr(stderr: asyncio.StreamReader, chunks: list[bytes]) -> None:
    """Read `stderr` to EOF concurrently with stdout, buffering chunks.

    `git http-backend`'s stdout and stderr are two independent OS pipes with
    their own (~64KB on Linux) kernel buffers. If we only read stderr after
    the process exits (or after stdout hits EOF), a child that writes enough
    to stderr to fill its pipe blocks on that `write()` — and since it's
    blocked, it never produces more stdout either, so a sequential
    stdout-then-stderr reader deadlocks forever. Draining both streams
    concurrently (this coroutine running alongside the stdout read loop)
    avoids that.
    """
    while True:
        chunk = await stderr.read(65536)
        if not chunk:
            break
        chunks.append(chunk)


async def _run_git_http_backend(env: dict, body: bytes) -> tuple[int, list[tuple[str, str]], AsyncIterator[bytes]]:
    """Run `git http-backend` as a subprocess, feed it *body* on stdin, and
    return (status_code, headers, body_stream). The GIL is not held by the
    parent process during pack generation — the heavy lifting happens in a
    real OS child process."""
    proc = await asyncio.create_subprocess_exec(
        *_GIT_HTTP_BACKEND,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    # Start draining stderr immediately, concurrently with everything below —
    # see `_drain_stderr` for why this must not happen sequentially after
    # stdout EOF / process exit.
    stderr_chunks: list[bytes] = []
    stderr_task = asyncio.ensure_future(_drain_stderr(proc.stderr, stderr_chunks))

    try:
        # Feed the request body. The child can exit — or close its stdin —
        # before reading any of it: `git http-backend` never reads stdin for
        # an `info/refs` GET, and a child that dies on a bad env loses the
        # race against this write. On uvloop that surfaces as
        # `RuntimeError: unable to perform operation on <WriteUnixTransport
        # closed=True ...>; the handler is closed`; on vanilla asyncio as
        # BrokenPipeError/ConnectionResetError out of `drain()`. None of
        # those are fatal by themselves — the child may have already written
        # its full response, and a genuine crash is diagnosed below by the
        # empty-header-block check (stderr + exit code in the log).
        if body:
            try:
                proc.stdin.write(body)
                await proc.stdin.drain()
            except (RuntimeError, BrokenPipeError, ConnectionResetError) as exc:
                logger.warning(
                    "git http-backend stopped reading stdin before the request body was fully written: %s",
                    exc,
                )
        try:
            proc.stdin.close()
        except (RuntimeError, BrokenPipeError, ConnectionResetError):
            pass

        header_block = await _read_cgi_headers(proc.stdout)
    except BaseException:
        # Anything raised while reading headers — a malformed/over-long
        # header line, or the awaiting task itself being cancelled — means
        # `body_stream()` never gets created, so its `finally` (the only
        # other place that kills the child and awaits stderr_task) never
        # runs either. Without this, an interrupted fetch during this brief
        # pre-streaming window leaks a hung `git http-backend` process and
        # an orphaned drain task. Clean up the same way body_stream()'s
        # finally does, then propagate the original failure.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        stderr_task.cancel()
        try:
            await stderr_task
        except (asyncio.CancelledError, Exception):
            pass
        raise

    if not header_block:
        # stdout hit EOF before a single byte was written — a well-behaved
        # git http-backend always emits at least a Status:/Content-Type
        # header, so this means the process crashed or was killed before
        # producing any output. _parse_cgi_status defaults to 200 absent a
        # Status: header, which would otherwise report this hard failure to
        # the client as an empty-but-successful response (the prior dulwich
        # path returned a 500 here). Raise instead — the caller's except
        # Exception already converts this into a 500 via _server_error().
        await proc.wait()
        await stderr_task
        stderr = b"".join(stderr_chunks)
        logger.error(
            "git http-backend produced no output before exiting %s: %s",
            proc.returncode,
            stderr.decode("utf-8", errors="replace"),
        )
        raise RuntimeError(f"git http-backend produced no output (exit {proc.returncode})")
    status_code, headers = _parse_cgi_status(header_block)

    async def body_stream() -> AsyncIterator[bytes]:
        assert proc.stdout is not None
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            # A client disconnect mid-stream raises GeneratorExit here via
            # StreamingResponse's aclose(), *before* the child has finished
            # writing its pack output. Since nothing is reading proc.stdout
            # anymore, a child with more than a pipe buffer's worth of pack
            # data left to write blocks on write() and never exits on its
            # own — kill it instead of waiting for a natural exit that may
            # never come. Signalling an already-exited (zombie) process is a
            # harmless no-op, so no race condition to guard beyond the
            # ProcessLookupError case (fully reaped between the check and
            # the call).
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            await stderr_task
            if proc.returncode not in (0, None):
                stderr = b"".join(stderr_chunks)
                logger.error(
                    "git http-backend exited %s: %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace"),
                )

    return status_code, headers, body_stream()


async def _marketplace_git(path: str, request: Request):
    token = token_from_basic_auth(request.headers.get("authorization"))
    if not token:
        return _unauthorized()

    # resolve_token_to_user / ensure_repo_for_user route through the
    # repository factory and ignore ``conn``; on Postgres pass None so the
    # system DuckDB is never opened (forbidden invariant).
    from src.repositories import use_pg

    conn = None
    try:
        conn = None if use_pg() else get_system_db()
    except Exception:
        logger.exception("get_system_db() failed")
        return _server_error()

    def _resolve_and_build_repo():
        """Sync — auth (DB read) + repo build (DB read + disk hashing, and
        on a cache miss a full dulwich repo build). Run via
        `run_in_threadpool` so this CPU/IO-heavy work never lands on the
        shared event loop: previously it ran inside `a2wsgi.WSGIMiddleware`'s
        own thread-pool offload, so moving it back onto the loop here would
        silently reintroduce the exact health-check-stalling regression this
        change set out to fix — just one call earlier in the request.
        """
        # Git channel doesn't need the reason — just auth yes/no.
        user, _reason = resolve_token_to_user(conn, token)
        if not user:
            return None, None
        # Restricted principals (co-session / agent-session) have no
        # marketplace-git surface: reject them outright instead of letting
        # ``ensure_repo_for_user`` or the ``user.get(...)`` calls below
        # AttributeError into a 500. Mirrors the agent-PAT rejection this
        # route already performs.
        from app.auth.session_principal import PRINCIPAL_TYPES

        if isinstance(user, PRINCIPAL_TYPES):
            return None, None
        try:
            repo_path = git_backend.ensure_repo_for_user(conn, user)
        except Exception:
            # ``user`` may be a restricted principal (frozen dataclass, no
            # ``.get``) — never let the diagnostic label raise inside except.
            label = user.get("email") or user.get("id") if isinstance(user, dict) else type(user).__name__
            logger.exception("Failed to build repo for user %r", label)
            return user, None
        return user, repo_path

    try:
        user, repo_path = await run_in_threadpool(_resolve_and_build_repo)
    finally:
        # DB touchpoints (resolve_token_to_user, ensure_repo_for_user) are
        # both done — close before spawning the subprocess, which only reads
        # the pre-built bare repo directory from disk and never touches
        # DuckDB.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if user is None:
        return _unauthorized()
    if repo_path is None:
        return _server_error()

    # Log AFTER the PAT Basic-auth resolution above so user_id is real —
    # never before. Classified fetch vs push by the git smart-HTTP endpoint
    # name, not by HTTP method: both info/refs and git-upload-pack
    # negotiation (fetch) can arrive as GET or POST.
    action = "marketplace.git_push" if path.rstrip("/").endswith("git-receive-pack") else "marketplace.git_fetch"
    log_safe(
        user_id=user.get("id") if isinstance(user, dict) else None,
        action=action,
        resource=f"marketplace.git:{user.get('id') if isinstance(user, dict) else 'unknown'}",
    )

    body = await request.body()
    remote_user = user.get("email") or user.get("id")
    env = _build_cgi_env(request, path, repo_path, remote_user, len(body))

    try:
        status_code, headers, stream = await _run_git_http_backend(env, body)
    except FileNotFoundError:
        logger.exception("git http-backend binary not found")
        return _server_error()
    except Exception:
        logger.exception("git http-backend failed for user %r", user.get("id"))
        return _server_error()

    return StreamingResponse(stream, status_code=status_code, headers=dict(headers))


# Registered as two distinct routes (not one `methods=["GET", "POST"]` route)
# so each method gets its own `operation_id` — a single multi-method APIRoute
# shares one `unique_id` across all its methods, which trips FastAPI's
# duplicate-operation-id warning during schema generation.
router.add_api_route(
    "/marketplace.git/{path:path}",
    _marketplace_git,
    methods=["GET"],
    operation_id="marketplace_git_get",
)
router.add_api_route(
    "/marketplace.git/{path:path}",
    _marketplace_git,
    methods=["POST"],
    operation_id="marketplace_git_post",
)
