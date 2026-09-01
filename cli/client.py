"""HTTP client wrapper for CLI — handles auth, retries, streaming."""

import atexit
import glob
import json as _json
import os
import platform
import re
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cli.config import _config_dir, get_server_url, get_token
from cli.server_moved import is_redirect, moved_server_message
from cli.update_check import _installed_version, _version_lt

# User-Agent is invariant for the life of the process — installed
# version doesn't change, OS doesn't change. Cache it at import time so
# every `get_client()` call doesn't re-do the importlib.metadata lookup
# + `platform.system()` call. (Reviewer note: do NOT cache the
# `_installed_version` lookup inside `_check_version_headers` — tests
# patch `cli.client._installed_version` and a cached value would defeat
# the patch. The hook keeps calling it; network cost dwarfs the lookup.)
_USER_AGENT = f"agnes/{_installed_version()} ({platform.system().lower()})"


# PID-suffixed tmp / part files — see `_download_chunked` and
# `_download_single_stream`. We extract the embedded PID and reap any
# leftover whose process is no longer alive on every pull. Without this,
# every SIGKILL'd pull leaks files indefinitely (devil's-advocate R3
# finding #1).
_PID_SUFFIX_RE = re.compile(r"\.(\d+)\.(?:tmp|part\d+)$")


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID exists. POSIX-only;
    Windows users get the conservative `True` (file kept) which means
    no reaping but also no false-deletion of a live sibling."""
    if pid <= 0:
        return False
    try:
        # Signal 0 = no-op kill; raises ProcessLookupError when PID is
        # gone, PermissionError when the PID exists but isn't ours
        # (still alive, just owned by someone else — keep the file).
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        # Anything else (e.g. AttributeError on Windows where os.kill
        # exists but signal 0 isn't supported the same way): be
        # conservative and don't reap.
        return True


def _reap_dead_pid_leftovers(target_path: str) -> None:
    """Remove `<target>.{pid}.tmp` and `<target>.{pid}.partN` files
    whose embedded PID is no longer alive. Called at the start of every
    download to keep the parquet directory tidy across SIGKILL'd or
    crashed prior runs. Never raises — leaked file is preferable to
    failing the new pull on a permission error."""
    candidates = glob.glob(f"{target_path}.*.tmp") + glob.glob(f"{target_path}.*.part*")
    for path in candidates:
        m = _PID_SUFFIX_RE.search(path)
        if not m:
            continue
        try:
            pid = int(m.group(1))
        except ValueError:
            continue
        if _is_pid_alive(pid):
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


# Retry policy for transient failures during stream downloads. Scoped to
# network issues and 5xx — 4xx (auth, 404, 400) is NOT retried. Tunable via
# env for tests; defaults sit in the "one flaky network blip" window.
_RETRY_ATTEMPTS = int(os.environ.get("AGNES_STREAM_RETRIES", "3"))
_RETRY_BACKOFFS_S = (0.3, 1.0, 3.0)  # seconds before attempt 2, 3, 4

# Long-running query timeout. /api/query forwards to BigQuery for remote
# tables, where SELECTs routinely run for minutes. The default 30s HTTP
# timeout dies long before BQ finishes. Operators tune via AGNES_QUERY_TIMEOUT.
QUERY_TIMEOUT_S = float(os.environ.get("AGNES_QUERY_TIMEOUT", "300"))

# Range-chunked parallel download — see `stream_download` docstring. Defaults
# tuned for the corp-VPN per-flow rate-limiting case (single-stream throttled
# but N parallel range requests scale linearly). Disabled implicitly for
# files below the threshold or when the server doesn't advertise byte-range
# support. Operators can hard-disable by setting parallelism to 1.
_CHUNK_PARALLELISM = max(
    1,
    min(
        16,
        int(
            os.environ.get("AGNES_PULL_CHUNK_PARALLELISM", "4"),
        ),
    ),
)
_CHUNK_THRESHOLD_BYTES = int(
    os.environ.get("AGNES_PULL_CHUNK_THRESHOLD_BYTES", str(50 * 1024 * 1024)),
)


# ── Transport-error translation ─────────────────────────────────────────
# Pavel's Issue #185 Phase 3B caught the failure mode: when httpx raises
# `ReadTimeout` / `ConnectError` / `RemoteProtocolError` and the CLI
# command doesn't catch it, Typer dumps a five-frame Python traceback to
# the analyst's terminal. That looks like a CLI bug to a non-Python user
# and obscures the actionable signal ("server slow, try snapshot create").
# Translate transport exceptions to `AgnesTransportError` with a typed
# user-facing message, log the full traceback to `~/.config/agnes/last-
# error.log` for debug, and let the top-level CLI handler render the
# clean message + exit non-zero.

_LOG_FILE = _config_dir() / "last-error.log"


class AgnesTransportError(Exception):
    """Network / transport failure with a user-actionable message.

    Raised by the api_* / stream_download helpers when httpx surfaces a
    connection / timeout / protocol error. The CLI's top-level Typer
    handler catches this, prints `.user_message` (NOT the traceback),
    and exits non-zero. Full traceback goes to ``~/.config/agnes/last-
    error.log`` so an operator can recover it for support.
    """

    #: Process exit code the top-level handler uses for this error. The
    #: transport default is 1; the two response hooks below raise with 2,
    #: which is the code they exited with when they still called
    #: ``sys.exit`` themselves.
    exit_code = 1

    def __init__(self, user_message: str, *, hint: str = "", logfile_path: Path | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.hint = hint
        self.logfile_path = logfile_path


class RedirectHardStop(BaseException):
    """A redirect the CLI will not follow. Ends the command unless caught BY NAME.

    Detected in an httpx *response event hook*, which runs deep inside
    somebody else's ``api_get(...)`` call. It used to end the process there
    and then, with ``sys.stderr.write`` + ``sys.exit(2)``. That works for a
    command built around a single request, and silently voids the two
    callers built to survive a failed one, because ``SystemExit`` derives
    from ``BaseException`` and so walks straight through ``except Exception``:

    - ``agnes diagnose`` runs many checks and records a row per failure. On
      an unreachable server it prints its full checklist and exits 0. On a
      redirect it printed nothing at all — no checks, empty ``--json``
      stdout, exit 2 — from the one command whose purpose is to tell you
      what is wrong.
    - ``agnes update`` wraps every step so one failure cannot abort the
      run. The hard stop punched through ``_run_step`` too, ending the
      process before the report was ever written.

    **Deriving from ``BaseException`` is the whole design**, not an oversight.
    A first version of this subclassed ``AgnesTransportError`` — an ordinary
    ``Exception`` — which fixed those two callers by making the stop visible
    to *every* ``except Exception`` in the CLI. Devin Review on #1277 listed
    what that cost: ``agnes diagnose system`` printed ``Cannot reach server:
    <…answered HTTP 308…>``, a heading contradicting its own body and the
    exact lie this line of work exists to remove; ``agnes query`` relabelled
    it ``Query error:``; ``agnes pull`` filed it as a manifest row; and
    ``agnes chat`` kept the REPL alive so every following turn hit it again.
    All of them exited 1 where they had exited 2.

    Keeping it off the ``Exception`` branch inverts that: nothing catches it
    by accident, so every command that has not opted in behaves exactly as
    before, and an aggregator opts in with an explicit ``except
    RedirectHardStop``. A broad handler added later cannot silently
    reintroduce the swallowing, either.

    The version floor next door stays an unconditional ``sys.exit`` — see
    ``_check_version_headers``.
    """

    #: Process exit code the top-level handler uses — the code this
    #: condition exited with when it called ``sys.exit`` itself.
    exit_code = 2

    def __init__(self, user_message: str, *, hint: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.hint = hint


def _log_traceback(exc: BaseException, *, context: str) -> Path:
    """Append a timestamped traceback to ``~/.config/agnes/last-error.log``
    and return the path. Best-effort — never raises (a logging failure
    must not mask the original error)."""
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"\n=== {ts} {context} ===\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass
    return _LOG_FILE


def _translate_transport_error(
    exc: Exception,
    *,
    context: str,
    timeout_s: float | None = None,
) -> AgnesTransportError:
    """Map httpx transport exceptions to user-facing CLI messages. The
    mapping is intentionally pragmatic — analysts care about "what do I
    do next", not the gRPC / TCP detail.

    `timeout_s`, when supplied, is the actual httpx timeout used by the
    failing call so the ReadTimeout message reports the real wait window
    (a `agnes catalog` GET dies at 30s, not 300s — Devin Review on PR
    #188 caught the original signature hardcoding `QUERY_TIMEOUT_S`,
    which only matches `agnes query --remote`)."""
    log = _log_traceback(exc, context=context)
    if isinstance(exc, httpx.ReadTimeout):
        wait_s = timeout_s if timeout_s is not None else QUERY_TIMEOUT_S
        # The "long-running BQ" advisory only makes sense when the call
        # actually hit the query path (timeout ≥ ~60s). For short calls
        # (the 30s default on `agnes catalog` etc.) it's just confusing.
        if wait_s >= 60:
            hint = (
                "If this is `agnes query --remote` against a heavy BQ view, "
                "the underlying BQ job took longer than the wait window. Try:\n"
                "  • narrow the WHERE (especially the partition column from `agnes catalog --json`)\n"
                "  • `agnes snapshot create <table> ... --estimate` to materialize once + query locally\n"
                "  • set AGNES_QUERY_TIMEOUT=600 for a longer client-side wait\n"
                f"Full traceback: {log}"
            )
        else:
            hint = f"Server is slow or unreachable. Check `agnes status`; re-run if transient.\nFull traceback: {log}"
        return AgnesTransportError(
            f"Server didn't respond within the read timeout ({wait_s:.0f}s) for {context}.",
            hint=hint,
            logfile_path=log,
        )
    if isinstance(exc, httpx.ConnectError):
        return AgnesTransportError(
            f"Can't reach the agnes server for {context}.",
            hint=(
                "Check the server URL with `agnes status`, network reachability "
                "(VPN / DNS / firewall), and the TLS-trust setup if this is a "
                f"corporate-CA deployment.\nFull traceback: {log}"
            ),
            logfile_path=log,
        )
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return AgnesTransportError(
            f"Connection broke mid-flight on {context}.",
            hint=(
                "Usually a transient network blip. Re-run the command. If it "
                f"keeps happening, check `agnes status`.\nFull traceback: {log}"
            ),
            logfile_path=log,
        )
    if isinstance(exc, httpx.TimeoutException):
        return AgnesTransportError(
            f"Network timeout on {context}.",
            hint=f"Re-run; if persistent, check the server.\nFull traceback: {log}",
            logfile_path=log,
        )
    # Anything else: re-wrap with a generic message so the CLI doesn't
    # dump the traceback. We'd prefer a typed translation; if you hit
    # this branch, add a clause above.
    return AgnesTransportError(
        f"Unexpected error on {context}: {type(exc).__name__}.",
        hint=f"Full traceback: {log}",
        logfile_path=log,
    )


def _check_moved_server(response: "httpx.Response") -> None:
    """Hard-stop with an actionable message when the API answers a redirect.

    A deployment that changes hostname leaves the old name answering
    ``308`` for a while. httpx does not follow redirects by default, so
    the 3xx reached the caller as an ordinary response and
    ``cli.error_render`` printed ``HTTP 308:`` — the body of a redirect is
    empty, so the destination and the remedy were both missing, on every
    command the user tried.

    Following it is deliberately NOT the fix: httpx strips
    ``Authorization`` on a cross-origin hop
    (``httpx._client.Client._redirect_headers``), so the retry would land
    unauthenticated and report ``401 Not authenticated`` — the same
    failure wearing a more confusing name, and a silent credential-scope
    change for anyone whose old hostname is no longer theirs. Naming the
    new address and stopping keeps the token on the host the user
    configured.
    """
    if not is_redirect(response.status_code):
        return
    # The wording lives in `cli/server_moved.py` so this client and
    # `cli/v2_client.py` cannot drift apart — teaching only one of them is
    # exactly how ten command modules kept answering a redirect with
    # `internal CLI error (JSONDecodeError)` after #1225.
    message = moved_server_message(
        response.status_code,
        response.headers.get("Location", ""),
        get_server_url(),
    )
    raise RedirectHardStop(message)


def _check_version_headers(response: "httpx.Response") -> None:
    """Hard-stop the CLI when the server reports we're below min_version.

    Drift warnings (`local < latest`) are already printed by the
    update_check root callback in cli/main.py — no need to nag again on
    every API call. This hook only enforces the hard floor.
    """
    # Recursion barrier: `agnes self-upgrade` sets this for the duration
    # of the upgrade. Without it, a /api/* call inside the install flow
    # could exit 2 with "Run: agnes self-upgrade" — inside agnes
    # self-upgrade. The sentinel is process-local and propagates to
    # subprocesses via the explicit env= passed to the smoke test.
    if os.environ.get("AGNES_SELF_UPGRADE_IN_PROGRESS") == "1":
        return
    latest = response.headers.get("X-Agnes-Latest-Version")
    minv = response.headers.get("X-Agnes-Min-Version")
    if not latest or not minv:
        return
    local = _installed_version()
    if local == "unknown":
        return
    if _version_lt(local, minv):
        # Deliberately still an unconditional exit, unlike the redirect above.
        # An earlier draft of #1277 made this catchable too, on the grounds
        # that it is "the same defect one line over". It is not: a redirect
        # means this request went nowhere, while a version floor means the
        # SERVER has refused this CLI outright. Devin Review on #1277 pointed
        # at the consequence — `agnes update`'s `_run_step` recorded the floor
        # as one error row and carried on running every remaining convergence
        # step against a server that had just declared this binary too old.
        # Refusing to proceed is the entire point of the check.
        sys.stderr.write(
            f"error: agnes {local} is incompatible with server {latest} "
            f"(min required: {minv}). Run: agnes self-upgrade\n"
        )
        sys.exit(2)


def get_client(timeout: float = 30.0) -> httpx.Client:
    """Get an authenticated httpx client.

    This factory creates a fresh client per call — used by the small
    `api_*` helpers (one request, then close). The big-stream path
    (`stream_download`) routes through `_get_shared_client()` to amortize
    TLS handshakes and HTTP/2 multiplexing across N parquet downloads.

    Wires `_check_version_headers` as a response event hook: every
    metadata call sees the server's `X-Agnes-{Latest,Min}-Version`
    headers and hard-stops if our local version is below the floor.
    Hook is intentionally NOT wired on `_get_shared_client()` — that
    client backs streaming parquet downloads where a `sys.exit(2)`
    mid-stream would leak per-thread part files.
    """
    token = get_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=get_server_url(),
        headers={**headers, "User-Agent": _USER_AGENT},
        timeout=timeout,
        event_hooks={"response": [_check_moved_server, _check_version_headers]},
    )


# ── Shared persistent client ────────────────────────────────────────────
# `agnes pull` issues N stream_download calls — one per parquet — plus
# (with chunked downloads) M Range requests per file. Without pooling,
# each call performs a fresh TLS handshake; with HTTP/2 enabled, all
# those requests multiplex over a single TCP connection. The shared
# client is created lazily on first stream-download request, kept alive
# for the duration of the process, and closed at exit.
#
# HTTP/2 requires the optional `h2` package. If it's unavailable (slim
# install), we fall back to HTTP/1.1 — pooling alone still saves the
# handshake cost — and never raise. The CLI must not crash on `agnes
# pull` because of an h2 import error.

_SHARED_CLIENT: httpx.Client | None = None
_SHARED_CLIENT_LOCK = threading.Lock()


def _get_shared_client() -> httpx.Client:
    """Lazily create + return a process-wide httpx.Client.

    Pool defaults: keep up to 32 keepalive connections (covers the
    chunk-parallelism cap of 16 × 2 simultaneous files comfortably) and
    cap the total at 64 so a runaway loop can't open thousands of
    sockets. HTTP/2 is opt-in via httpx's `http2=True` and gracefully
    degrades when the `h2` extra is missing.
    """
    global _SHARED_CLIENT
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None:
            return _SHARED_CLIENT
        token = get_token()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        limits = httpx.Limits(
            max_keepalive_connections=32,
            max_connections=64,
        )
        try:
            client = httpx.Client(
                base_url=get_server_url(),
                headers=headers,
                timeout=300.0,
                http2=True,
                limits=limits,
            )
        except (ImportError, RuntimeError):
            # `h2` not installed → httpx raises; fall back to HTTP/1.1.
            # Pooling alone still amortizes the TLS handshake.
            client = httpx.Client(
                base_url=get_server_url(),
                headers=headers,
                timeout=300.0,
                limits=limits,
            )
        _SHARED_CLIENT = client
        return client


def _close_shared_client() -> None:
    """Close the shared client and clear the slot. Safe to call twice."""
    global _SHARED_CLIENT
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None:
            try:
                _SHARED_CLIENT.close()
            except Exception:
                pass
            _SHARED_CLIENT = None


atexit.register(_close_shared_client)


def api_get(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.get(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"GET {path}", timeout_s=timeout) from exc


def api_post(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.post(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"POST {path}", timeout_s=timeout) from exc


def api_delete(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.delete(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"DELETE {path}", timeout_s=timeout) from exc


def api_patch(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.patch(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"PATCH {path}", timeout_s=timeout) from exc


def api_put(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.put(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"PUT {path}", timeout_s=timeout) from exc


# Cap for the raw-body fallback in `error_detail` — an HTML error page
# from a proxy can be tens of KB; the terminal only needs enough to
# identify it.
_ERROR_DETAIL_TEXT_CAP = 300


def error_detail(resp: httpx.Response) -> str:
    """Human-readable error out of an HTTP error response. Never raises.

    The app returns FastAPI-style ``{"detail": ...}`` JSON, but the body
    that actually reaches the CLI can be anything — an HTML 403/404 from a
    reverse proxy, an empty 502, plain text. Calling ``resp.json()`` bare
    in an error path turns those into a JSONDecodeError traceback, which
    is how `agnes admin config-surface` used to die on a proxy error page.
    """
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("detail") is not None:
        detail = body["detail"]
        return detail if isinstance(detail, str) else _json.dumps(detail)
    text = " ".join((resp.text or "").split())
    if len(text) > _ERROR_DETAIL_TEXT_CAP:
        text = text[:_ERROR_DETAIL_TEXT_CAP] + "…"
    return f"HTTP {resp.status_code}" + (f": {text}" if text else "")


def error_detail_object(resp: httpx.Response) -> Any:
    """The parsed ``detail`` payload in whatever shape the server sent it
    (dict, str, list), or ``None`` when the body is not JSON or carries no
    ``detail`` key. For callers that branch on structured detail — e.g.
    version-conflict 409s. Never raises."""
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        return body.get("detail")
    return None


# ── SSE streaming POST ──────────────────────────────────────────────────
# `agnes chat` (cli/commands/chat.py) is the sole consumer today: it posts
# one user turn to `/api/v1/sessions/{id}/messages` and reads the AG-UI
# event stream back (`app/api/agent_sse.py`'s vocabulary — RUN_STARTED,
# TEXT_MESSAGE_CONTENT, TOOL_CALL_START/END, RUN_FINISHED, RUN_ERROR).
#
# Read timeout: the server's own `_IDLE_TIMEOUT_S` (app/api/agent_sessions.py,
# 300s) is the ceiling on how long it will go between frames before forcing
# a terminal RUN_ERROR itself — our client-side read timeout must comfortably
# exceed that or we'd time out first and misreport a transport error for
# what is actually a slow-but-healthy turn. Connect timeout stays short
# (an unreachable server should fail fast, not hang for 5 minutes).
_SSE_CONNECT_TIMEOUT_S = 10.0
_SSE_READ_TIMEOUT_S = 320.0


class ApiSseError(Exception):
    """The initial POST for an SSE stream came back non-2xx (before any
    event was ever yielded) — e.g. `404 session_not_found`, `409
    turn_in_flight`, `503 chat_disabled`. Carries the parsed body (falls
    back to raw text) so the caller can render it the same way `_fail`/
    `render_error` render any other API error."""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body


def api_post_sse(path: str, json: dict | None = None) -> Iterator[dict]:
    """POST ``path`` and yield each Server-Sent Event on the
    ``text/event-stream`` response as a parsed dict.

    Only ``data:`` field lines contribute to the payload; ``id:``/
    ``event:``/comment lines are metadata the event's own ``"type"`` key
    already carries, so they're skipped rather than reconstructed. Per the
    SSE spec, consecutive ``data:`` lines belonging to one record are
    buffered and joined with ``"\\n"``, and the record is only dispatched
    (JSON-parsed + yielded) on the blank line that terminates it. Coupling
    note: today's sole producer, ``app.api.agent_sse.sse_bytes``, always
    emits exactly one ``data:`` line per record — this buffering exists for
    spec-correctness (and any future producer that wraps long payloads)
    rather than because the current server needs it. A record left
    unterminated by a final blank line (stream cut off mid-record) is
    dropped rather than force-parsed — that's indistinguishable from any
    other truncated-stream case and is handled by the caller checking for a
    terminal event (see ``cli/commands/chat.py::_send_turn``).

    Malformed ``data:`` payloads (fails to parse as JSON) are counted and,
    if any occurred, a one-line warning is written to stderr when the
    stream ends — silently dropping them would otherwise leave no trace
    that events were lost.

    Raises:
        ApiSseError: the response status was >= 400 — read entirely before
            raising so the connection is released; no partial events are
            yielded in that case.
        AgnesTransportError: any httpx transport failure (connect refused,
            read timeout, connection reset mid-stream), translated the same
            way every other ``api_*`` helper does.

    A generator: nothing happens until the caller starts iterating. The
    underlying request stays open only while the caller keeps pulling
    events — breaking out of the loop (or calling ``.close()`` on the
    returned generator explicitly, which the caller should do from a
    ``finally`` to guarantee it regardless of *where* an exception lands)
    triggers ``GeneratorExit`` inside this function, unwinding the ``with
    client.stream(...)`` block and closing the connection.
    """
    malformed_count = 0
    try:
        with (
            get_client(
                timeout=httpx.Timeout(
                    connect=_SSE_CONNECT_TIMEOUT_S,
                    read=_SSE_READ_TIMEOUT_S,
                    write=30.0,
                    pool=_SSE_CONNECT_TIMEOUT_S,
                )
            ) as client,
            client.stream("POST", path, json=json) as response,
        ):
            if response.status_code >= 400:
                response.read()
                try:
                    body = response.json()
                except Exception:
                    body = response.text
                raise ApiSseError(response.status_code, body)
            data_lines: list[str] = []
            for line in response.iter_lines():
                if line == "":
                    # Blank line: record boundary. Dispatch only if we
                    # actually buffered a data field since the last
                    # dispatch — a stray blank line (or one between
                    # comment/event/id-only lines) is a no-op.
                    if data_lines:
                        payload = "\n".join(data_lines)
                        data_lines = []
                        try:
                            event = _json.loads(payload)
                        except ValueError:
                            malformed_count += 1
                            continue
                        yield event
                    continue
                if not line.startswith("data:"):
                    # event:/id:/retry:/comment lines — metadata we
                    # don't need to reconstruct.
                    continue
                value = line[len("data:") :]
                value = value.removeprefix(" ")
                data_lines.append(value)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(
            exc,
            context=f"POST {path} (stream)",
            timeout_s=_SSE_READ_TIMEOUT_S,
        ) from exc
    finally:
        if malformed_count:
            sys.stderr.write(
                f"warning: skipped {malformed_count} malformed SSE data record(s) that failed to parse as JSON\n"
            )


def _is_transient(exc: Exception) -> bool:
    """Worth retrying? Network blip or 5xx — yes. Auth / 4xx — no."""
    if isinstance(
        exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.TimeoutException)
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _read_chunk_threshold_bytes() -> int:
    """Re-read threshold each call so tests / operators can flip it via
    env var without restarting the process."""
    try:
        return int(
            os.environ.get(
                "AGNES_PULL_CHUNK_THRESHOLD_BYTES",
                str(_CHUNK_THRESHOLD_BYTES),
            )
        )
    except ValueError:
        return _CHUNK_THRESHOLD_BYTES


def _read_chunk_parallelism() -> int:
    """Re-read parallelism each call (same rationale as threshold). Floor 1,
    ceiling 16."""
    try:
        n = int(
            os.environ.get(
                "AGNES_PULL_CHUNK_PARALLELISM",
                str(_CHUNK_PARALLELISM),
            )
        )
    except ValueError:
        n = _CHUNK_PARALLELISM
    return max(1, min(16, n))


def _probe_range_support(client: httpx.Client, path: str) -> tuple[int, bool]:
    """Send HEAD; return (content-length, accepts-byte-ranges).

    `(0, False)` means "we couldn't tell — fall back to single-stream".
    Never raises; transport errors during the probe are treated as
    "no chunking, try the GET instead and let it surface the failure
    in the normal retry loop".

    Probe order: HEAD first (cheap, idempotent), then GET-with-tiny-range
    fallback. The HEAD path covers Caddy's `file_server` (which advertises
    HEAD) and Caddy's `reverse_proxy` (which forwards HEAD upstream). The
    GET-fallback covers the dev `docker compose up` deployment where
    requests go straight to FastAPI's GET-only `/api/data/{tid}/download`
    route — FastAPI returns **405 Method Not Allowed** to a HEAD on a
    GET-only route, which without this fallback would silently disable
    chunked download for every dev / non-TLS install. The GET-with-Range
    probe asks for 1 byte so the server response is bounded; we discard
    the body and read only the headers + status code.
    """
    try:
        resp = client.head(path)
        status = getattr(resp, "status_code", 200)
        if status < 400:
            size = int(resp.headers.get("content-length", "0") or 0)
            accepts = resp.headers.get("accept-ranges", "").lower() == "bytes"
            if size > 0:
                return (size, accepts)
        # HEAD failed (405 from GET-only route is the common case in
        # non-Caddy deployments) or returned 0-length — fall through to
        # the tiny-Range GET probe.
    except Exception:
        pass
    try:
        with client.stream("GET", path, headers={"Range": "bytes=0-0"}) as resp:
            status = getattr(resp, "status_code", 0)
            if status not in (200, 206):
                return (0, False)
            # Drain the 1-byte body so the connection is reusable.
            for _ in resp.iter_bytes():
                pass
            # Content-Range on a 206 response carries the total: `bytes 0-0/12345`.
            # On a 200 response the server didn't honor Range — content-length is the total.
            if status == 206:
                cr = resp.headers.get("content-range", "")
                if "/" in cr:
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                        return (total, True)
                    except ValueError:
                        return (0, False)
                return (0, False)
            # status == 200 → server ignored Range; we can read content-length but
            # accept-ranges is False (or missing) so the caller will not chunk.
            size = int(resp.headers.get("content-length", "0") or 0)
            accepts = resp.headers.get("accept-ranges", "").lower() == "bytes"
            return (size, accepts)
    except Exception:
        return (0, False)


class _RangeNotHonored(Exception):
    """Internal sentinel — server returned 200 instead of 206 to a Range
    request. Caller catches and falls back to the single-stream path."""


class _RangeNotSatisfiable(Exception):
    """Internal sentinel — server returned 416 Range Not Satisfiable to a
    resume (or otherwise mis-ranged) request: our offset no longer lines
    up with what the server has, e.g. a stale/oversized leftover part or
    tmp file, or the source changed size between attempts. Caller drops
    whatever partial bytes it was resuming from and retries the same
    target from scratch — see `_download_chunk` / `_download_single_stream`.
    """

    def __init__(self, message: str = "server returned 416 Range Not Satisfiable") -> None:
        super().__init__(message)


def _download_chunk(
    client: httpx.Client,
    path: str,
    start: int,
    end: int,
    part_path: Path,
    progress_callback,
) -> None:
    """Stream `bytes=start-end` to `part_path`. Caller deals with retry +
    cleanup. Raises on any failure (HTTPStatusError on non-206/416
    response, httpx.* on transport blip, `_RangeNotHonored` if server
    returned 200 instead of 206 — chunked path can't trust that result —
    `_RangeNotSatisfiable` if server returned 416 to our Range).

    Resume (issue #1309): if `part_path` already holds `have` bytes from
    an earlier failed attempt at this SAME `(start, end)` — the caller's
    retry loop calls us again for the same chunk without clearing the
    file — request only the missing tail (`bytes={start+have}-{end}`)
    and append rather than re-fetching + overwriting bytes we already
    have. `have == 0` (no part file, or a first attempt) requests
    `bytes={start}-{end}` exactly as before this resume support existed.
    A `have` bigger than the range itself is a stale/corrupt leftover —
    never used to compute a negative or inverted range; treated the same
    as no leftover at all (restart at `start`). `have` exactly equal to
    the range length means an earlier attempt already wrote every byte
    of this chunk — nothing left to fetch.
    """
    range_len = end - start + 1
    have = 0
    if part_path.exists():
        try:
            have = part_path.stat().st_size
        except OSError:
            have = 0
        if have > range_len:
            # Stale/corrupt leftover bigger than what we're about to
            # request — restart clean rather than compute a negative or
            # inverted range from it.
            have = 0
    if have and have >= range_len:
        return
    resume = have > 0
    headers = {"Range": f"bytes={start + have}-{end}"}
    with client.stream("GET", path, headers=headers) as response:
        if response.status_code == 416:
            # Our offset doesn't line up with what the server has. Don't
            # touch part_path here — the caller (`_attempt_chunk`) owns
            # clearing it before the next retry.
            raise _RangeNotSatisfiable()
        # Server didn't honor the Range at all (fresh or resume) — RFC
        # says it MAY return 200 with the full body instead. We can't
        # safely splice that into one part of N, and must never append
        # it onto bytes we already hold, so abort the whole chunked path
        # and let the caller fall back to a clean single-stream download.
        if response.status_code == 200:
            raise _RangeNotHonored()
        response.raise_for_status()
        mode = "ab" if resume else "wb"
        with open(part_path, mode) as f:
            for piece in response.iter_bytes(chunk_size=65536):
                f.write(piece)
                if progress_callback and piece:
                    progress_callback(len(piece))


def _download_chunked(
    client: httpx.Client,
    path: str,
    target_path: str,
    total_size: int,
    parallelism: int,
    progress_callback,
) -> int:
    """Range-based parallel download. Returns total bytes written.

    Raises `_RangeNotHonored` on the first 200-instead-of-206 response so
    the caller can fall back. All other exceptions propagate.

    Each chunk's own retry loop (`_attempt_chunk`, below) resumes from
    whatever bytes a prior failed attempt already wrote to that chunk's
    part file instead of restarting it — see `_download_chunk`.

    Cleanup discipline: every part file we create gets removed before
    return (success or failure). The destination is written via the
    caller's `<target>.tmp` and renamed atomically.
    """
    target = Path(target_path)
    # Reap leftovers from previously SIGKILL'd / crashed pulls before we
    # start writing — without this, PID-suffixed files from dead PIDs
    # accumulate forever on disk (devil's-advocate R3 finding #1).
    _reap_dead_pid_leftovers(target_path)
    # Per-process tmp + part suffixes (devil's-advocate R2 finding #2):
    # if two `agnes pull` invocations target the same parquet
    # concurrently (e.g. SessionStart hook + manual run, or two
    # terminals), bare `<target>.tmp` and `<target>.partN` paths would
    # collide — one process's part-write yanks the other's in-progress
    # write, manifest hash check then fails spuriously. Including PID
    # in the suffix makes each invocation's intermediate files
    # disjoint; the final `os.replace` to the bare target is atomic so
    # last-writer-wins, both processes succeed individually.
    pid = os.getpid()
    tmp_path = Path(f"{target_path}.{pid}.tmp")
    parallelism = max(1, parallelism)
    # Build chunks — last chunk takes the remainder.
    chunk_size = total_size // parallelism
    if chunk_size <= 0:
        chunk_size = total_size  # tiny file, single chunk
        parallelism = 1
    ranges = []
    for i in range(parallelism):
        start = i * chunk_size
        end = (start + chunk_size - 1) if i < parallelism - 1 else (total_size - 1)
        ranges.append((i, start, end))

    part_paths = [Path(f"{target_path}.{pid}.part{i}") for i, _, _ in ranges]
    # Pre-clean any leftovers from a prior run of THIS process.
    for p in part_paths:
        p.unlink(missing_ok=True)

    def _attempt_chunk(i: int, start: int, end: int) -> None:
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS + 1):
            try:
                _download_chunk(
                    client,
                    path,
                    start,
                    end,
                    part_paths[i],
                    progress_callback,
                )
                return
            except _RangeNotHonored:
                # Don't retry — server policy, not a transport blip.
                raise
            except _RangeNotSatisfiable as exc:
                # 416: whatever partial bytes we were resuming from no
                # longer line up with what the server has. Drop them so
                # the next attempt asks for the whole range fresh, then
                # retry like any other transient failure.
                part_paths[i].unlink(missing_ok=True)
                last_exc = exc
                if attempt == _RETRY_ATTEMPTS:
                    break
                time.sleep(_RETRY_BACKOFFS_S[min(attempt, len(_RETRY_BACKOFFS_S) - 1)])
            except Exception as exc:
                last_exc = exc
                if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                    break
                time.sleep(_RETRY_BACKOFFS_S[min(attempt, len(_RETRY_BACKOFFS_S) - 1)])
        assert last_exc is not None
        raise last_exc

    try:
        if parallelism == 1:
            _attempt_chunk(*ranges[0])
        else:
            # Use a thread pool so each chunk gets its own concurrent
            # request slot on the (HTTP/2-multiplexed when available)
            # shared client. httpx.Client is thread-safe for stream().
            with ThreadPoolExecutor(max_workers=parallelism) as ex:
                futs = [ex.submit(_attempt_chunk, *r) for r in ranges]
                for fut in as_completed(futs):
                    fut.result()  # propagate first error

        # Concatenate parts → tmp_path → atomic rename.
        tmp_path.unlink(missing_ok=True)
        total_written = 0
        with open(tmp_path, "wb") as out:
            for p in part_paths:
                with open(p, "rb") as inp:
                    while True:
                        block = inp.read(65536)
                        if not block:
                            break
                        out.write(block)
                        total_written += len(block)
        os.replace(tmp_path, target)
        return total_written
    finally:
        # Always clean up part files + any stray tmp.
        for p in part_paths:
            p.unlink(missing_ok=True)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _download_single_stream(
    client: httpx.Client,
    path: str,
    target_path: str,
    progress_callback,
    headers_out: dict | None = None,
) -> int:
    """Original single-stream path with retry. Used when chunking is
    disabled (small file, no range support, or fallback after 200-on-Range).

    Resume (issue #1309): if `<target>.<pid>.tmp` already holds `have`
    bytes from an earlier failed attempt in THIS SAME retry loop, request
    only the missing tail (`bytes={have}-`, open-ended — the total size
    isn't tracked here) and append instead of re-streaming the whole
    body. A fresh attempt (no tmp file yet, `have == 0`) sends no Range
    header at all — byte-for-byte the same request as before this resume
    support existed. Two server responses fall back to a full truncate-
    and-rewrite rather than trusting/appending what comes back:
      - 200: the server ignored the Range and is sending the FULL body
        from byte 0 — written in "wb" (not "ab") mode.
      - 416: our offset doesn't line up with what the server has (a
        stale/oversized leftover — there's no total size to bounds-check
        against up front, so this is also how "too big" gets caught — or
        the source changed) — the partial file is dropped and the next
        retry attempt starts clean.
    """
    # Same dead-PID reap as `_download_chunked` so leftovers from
    # crashed prior pulls don't accumulate indefinitely.
    _reap_dead_pid_leftovers(target_path)
    # Per-process tmp suffix — same rationale as `_download_chunked`
    # (devil's-advocate R2 finding #2): concurrent `agnes pull`
    # invocations against the same target dir must not yank each
    # other's in-progress writes.
    tmp_path = Path(f"{target_path}.{os.getpid()}.tmp")
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        have = 0
        if tmp_path.exists():
            try:
                have = tmp_path.stat().st_size
            except OSError:
                have = 0
        resume = have > 0
        headers = {"Range": f"bytes={have}-"} if resume else None
        try:
            with client.stream("GET", path, headers=headers) as response:
                if resume and response.status_code == 416:
                    raise _RangeNotSatisfiable()
                response.raise_for_status()
                if headers_out is not None:
                    # Last attempt wins: on a resume the earlier attempt's header
                    # describes bytes we only partly kept. Going through
                    # `httpx.Headers` normalizes keys to lowercase in the plain
                    # dict; readers re-wrap in `httpx.Headers` for
                    # case-insensitive lookups.
                    headers_out.clear()
                    headers_out.update(httpx.Headers(response.headers))
                resumed = resume and response.status_code == 206
                if not resumed:
                    tmp_path.unlink(missing_ok=True)
                mode = "ab" if resumed else "wb"
                total = have if resumed else 0
                with open(tmp_path, mode) as f:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        f.write(chunk)
                        total += len(chunk)
                        if progress_callback:
                            progress_callback(len(chunk))
            os.replace(tmp_path, target_path)
            return total
        except _RangeNotSatisfiable as exc:
            tmp_path.unlink(missing_ok=True)
            last_exc = exc
            if attempt == _RETRY_ATTEMPTS:
                break
            time.sleep(_RETRY_BACKOFFS_S[min(attempt, len(_RETRY_BACKOFFS_S) - 1)])
        except Exception as exc:
            last_exc = exc
            if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                break
            time.sleep(_RETRY_BACKOFFS_S[min(attempt, len(_RETRY_BACKOFFS_S) - 1)])
    tmp_path.unlink(missing_ok=True)
    assert last_exc is not None
    raise last_exc


def stream_download(path: str, target_path: str, progress_callback=None, headers_out: dict | None = None) -> int:
    """Stream a file to `target_path` atomically and with retries.

    Two paths:
    1. **Chunked parallel** — when the server advertises `accept-ranges:
       bytes` and `content-length` exceeds `AGNES_PULL_CHUNK_THRESHOLD_BYTES`
       (default 50 MB), split into N range requests
       (`AGNES_PULL_CHUNK_PARALLELISM`, default 4, capped 1..16) and
       download in parallel. Concatenate the part files into `<target>.tmp`,
       then `os.replace`. Falls back to single-stream if the server
       responds 200 instead of 206 to a Range probe.
    2. **Single-stream** — for small files, no range support, or fallback
       from the chunked path. Same atomic-rename + retry semantics as
       before.

    Durability properties (unchanged):
    - Writes to `<target>.tmp`, then `os.replace` on success. The real
      target file never exists in a half-written state.
    - Retries up to `_RETRY_ATTEMPTS` on transient errors (network blip,
      5xx); 4xx (auth/404) is raised immediately.
    - No hash check here — that's the caller's job. Pass `headers_out` to
      receive the response headers of the single-stream path; a partition
      part is served with `X-Agnes-Content-MD5`, the md5 of the bytes that
      response actually carried, which `cli/lib/pull.py` prefers over the
      manifest hash as the integrity arbiter. Deliberately NOT populated on
      the chunked path: those bytes are spliced from N range responses, so
      no single response describes the whole file, and the caller correctly
      falls back to the manifest hash there. At the default 50 MiB
      AGNES_PULL_CHUNK_THRESHOLD_BYTES chunking engages well above any
      partition-part size today — but an operator lowering the threshold
      below a part's size, or a part outgrowing the server's 8 MiB
      buffering cap, reverts that part to manifest-only verification.

    Resume on retry (issue #1309): a within-call retry (chunked or
    single-stream) probes how many bytes the previous attempt already
    wrote — its `.partN` file or `.tmp` file — and requests only the
    missing tail via `Range`, appending rather than restarting from byte
    0. Falls back to a full truncate-and-rewrite if the server answers
    200 (ignored the Range entirely) or 416 (the offset no longer lines
    up with what the server has, e.g. a stale/oversized leftover or a
    source that changed between attempts).

    Threading: the chunked path uses a ThreadPoolExecutor sized to the
    parallelism. httpx.Client.stream() is safe to call concurrently from
    multiple threads on a single client (the connection pool serializes
    the underlying socket access; HTTP/2 multiplexes streams when the
    `h2` extra is installed).
    """
    # Use the shared persistent client when available — one TLS
    # handshake amortized across N stream_download calls within the same
    # process, and HTTP/2 stream multiplexing across the chunk Range
    # requests within a single download. Falls back to a fresh per-call
    # client if shared-client construction fails (e.g. `h2` install
    # broken at runtime). Devil's-advocate R2 finding #1: scope the
    # try/except to *only* the shared-client construction — the actual
    # download must NOT be retried under this except, otherwise hard
    # failures (401/403/404/5xx) waste a full second download attempt
    # and revoked-PAT cases don't fail-fast.
    try:
        client = _get_shared_client()
    except Exception:
        with get_client(timeout=300.0) as client:
            return _stream_download_via(client, path, target_path, progress_callback, headers_out)
    return _stream_download_via(client, path, target_path, progress_callback, headers_out)


def _stream_download_via(
    client: httpx.Client,
    path: str,
    target_path: str,
    progress_callback,
    headers_out: dict | None = None,
) -> int:
    """The shared body of `stream_download` parameterized on the client.
    Split out so tests can inject a fake client."""
    threshold = _read_chunk_threshold_bytes()
    parallelism = _read_chunk_parallelism()

    total_size = 0
    accepts_ranges = False
    if parallelism > 1:
        total_size, accepts_ranges = _probe_range_support(client, path)

    # Sanity bound on the advertised total size (devil's-advocate R1
    # finding #4): a misconfigured proxy or buggy server returning a
    # wildly inflated `Content-Length` would make us split into huge
    # `Range: bytes=N-M` requests; the server then clamps each to actual
    # bytes available, and we end up with overlapping bytes from the
    # start of the file in every part → corrupt assembled output (caught
    # later by manifest hash check, but only after wasted bandwidth).
    # 100 GiB is the operational ceiling for any single materialized
    # parquet on a typical Agnes deployment; values above suggest a
    # server / proxy bug rather than a legitimate huge file. Drop to
    # single-stream (which can't be confused by overlapping chunks).
    SANE_MAX_TOTAL = 100 * 1024**3  # 100 GiB
    if total_size > SANE_MAX_TOTAL:
        total_size = 0
        accepts_ranges = False

    use_chunked = parallelism > 1 and accepts_ranges and total_size > threshold

    try:
        if use_chunked:
            try:
                return _download_chunked(
                    client,
                    path,
                    target_path,
                    total_size,
                    parallelism,
                    progress_callback,
                )
            except _RangeNotHonored:
                # Server lied / proxy stripped the Range — fall through.
                pass
        return _download_single_stream(
            client,
            path,
            target_path,
            progress_callback,
            headers_out,
        )
    except httpx.HTTPStatusError:
        # 4xx / 5xx response from the server — re-raise verbatim so the
        # caller's status-code handling + the rich server error body
        # reach the analyst (Devin Review on PR #188).
        raise
    except httpx.HTTPError as exc:
        raise _translate_transport_error(
            exc,
            context=f"GET {path} (stream → {target_path})",
            timeout_s=300.0,
        ) from exc
