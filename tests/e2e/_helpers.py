"""Shared helpers for the cloud-chat E2E suite.

These wrap the docker-compose Agnes container with a small API client
that:

  * Bootstraps an admin user on first call (the test instance starts
    with an empty users table, so ``POST /auth/bootstrap`` is the
    canonical way in — no test-only login bypass needed).
  * Issues REST calls as Bearer auth.
  * Connects to chat WebSockets with the same Bearer token attached as
    a sub-protocol-style query param (the ws_url returned from
    ``POST /api/chat/sessions`` already embeds a one-shot ticket).
  * Shells into the container via ``docker compose exec`` to inspect
    per-user workspace files for tests that verify file-system side
    effects (F.1, F.7).

Everything here is intentionally synchronous (`websockets.sync.client`)
so the tests read like the in-process ``TestClient.websocket_connect``
flow in tests/test_chat_api_ws.py and don't pull in pytest-asyncio
inside the E2E suite.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import pytest


def skip_unless_chat_sessions_possible() -> None:
    """Skip tests that create chat sessions unless AGNES_E2E_DOCKER=1.

    Fake-agent mode (AGNES_E2E_FAKE_AGENT=1) only removes the Anthropic
    dependency — every chat session still spawns a real docker sandbox
    container through the apps-runner sidecar, and without that topology
    (sidecar reachable + `agnes-chat-sandbox` image built) the boot gates
    in app/main.py leave ``chat_manager`` unset so POST /api/chat/sessions
    503s with ``chat_disabled``. AGNES_E2E_DOCKER=1 is the operator's
    statement that the topology is up (mirrors
    tests/e2e/test_docker_sandbox_smoke.py's gate).
    """
    if not os.environ.get("AGNES_E2E_DOCKER"):
        pytest.skip(
            "chat sessions spawn real docker sandboxes — set AGNES_E2E_DOCKER=1 "
            "with the apps-runner sidecar up and the agnes-chat-sandbox image built"
        )


_COMPOSE_FILE = Path(__file__).parent / "docker-compose.e2e.yml"
_COMPOSE_SERVICE = "agnes"


# ---------------------------------------------------------------------------
# Bootstrap + REST helpers
# ---------------------------------------------------------------------------


class AgnesClient:
    """Thin Bearer-auth REST + WS client for the docker-compose container.

    Holds onto the JWT minted by ``POST /auth/bootstrap`` (admin) so
    every subsequent call carries an Authorization header. The same
    token works for the WS handshake via the per-session one-shot
    ticket flow (``ws_url`` already includes ``?ticket=...``), so we
    don't need to inject the token into the WS URL itself.
    """

    def __init__(self, base_url: str, *, email: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.token = token

    # -- REST ----------------------------------------------------------------

    def _req(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict]:
        url = self.base_url + path
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                payload = json.loads(body) if body else {}
                return resp.status, payload
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {"raw": body.decode("utf-8", "replace")}
            return exc.code, payload

    def post(self, path: str, json_body: Optional[dict] = None) -> tuple[int, dict]:
        return self._req("POST", path, json_body=json_body)

    def get(self, path: str) -> tuple[int, dict]:
        return self._req("GET", path)

    def delete(self, path: str) -> tuple[int, dict]:
        return self._req("DELETE", path)

    # -- Chat session helpers ------------------------------------------------

    def create_chat_session(self, *, surface: str = "web") -> dict:
        status, body = self.post("/api/chat/sessions", {"surface": surface})
        assert status == 201, f"create_chat_session: {status} {body!r}"
        return body

    def ws_url_for(self, create_response: dict) -> str:
        """Build the absolute ws:// URL from a POST /sessions response.

        ``ws_url`` is returned as a relative path (``/api/chat/...``);
        we replace the http(s):// of base_url with ws(s)://.
        """
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}{create_response['ws_url']}"


# ---------------------------------------------------------------------------
# Bootstrap (idempotent — re-runs are 403 once the password is set)
# ---------------------------------------------------------------------------


def bootstrap_admin(base_url: str, *, email: str, password: str) -> AgnesClient:
    """Bootstrap an admin via ``POST /auth/bootstrap`` and return an AgnesClient.

    Falls back to ``POST /auth/token`` if the bootstrap endpoint is
    already locked (i.e. the previous session's user survived because
    the docker volume wasn't torn down). The combination covers
    session-scoped fixture reuse as well as cold starts.
    """
    body = {"email": email, "password": password}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/auth/bootstrap",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
            return AgnesClient(base_url, email=email, token=payload["access_token"])
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        # Bootstrap already used — try password login.
        login_body = {"email": email, "password": password}
        login_req = urllib.request.Request(
            base_url.rstrip("/") + "/auth/password/login",
            data=json.dumps(login_body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(login_req, timeout=30) as resp:
            payload = json.loads(resp.read())
            return AgnesClient(base_url, email=email, token=payload["access_token"])


# ---------------------------------------------------------------------------
# WebSocket pump
# ---------------------------------------------------------------------------


def pump_until(
    ws_client,
    *,
    predicate,
    max_frames: int = 200,
    timeout_per_frame: float = 10.0,
) -> list[dict]:
    """Receive JSON frames from a `websockets.sync.client.ClientConnection`
    until ``predicate(frame)`` returns True or we hit ``max_frames``.

    Returns the list of frames seen (including the matching one). Raises
    AssertionError if the predicate never matched.

    Centralized here so every Phase F test file uses the same waiting
    loop — avoids subtle differences in how each file handles
    interleaved ``ready`` / ``runner_ready`` / ``tool_call`` /
    ``assistant_message`` framing.
    """
    seen: list[dict] = []
    for _ in range(max_frames):
        raw = ws_client.recv(timeout=timeout_per_frame)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        seen.append(frame)
        if predicate(frame):
            return seen
    raise AssertionError(
        f"predicate never matched after {max_frames} frames; frame types seen: {[f.get('type') for f in seen]}"
    )


# ---------------------------------------------------------------------------
# docker exec helpers (for verifying container-side state)
# ---------------------------------------------------------------------------


def docker_exec(argv: Iterable[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a command inside the running `agnes` compose service.

    Wraps ``docker compose -f ... exec -T agnes <argv>`` so tests can
    grep file-system state inside the container (workspace init markers,
    snapshot files, etc.) without needing a separate volume mount on
    the host.

    ``-T`` disables the pseudo-TTY allocation that ``exec`` requests by
    default — necessary because pytest runs without a TTY and docker
    aborts otherwise.
    """
    cmd = [
        "docker",
        "compose",
        "-f",
        str(_COMPOSE_FILE),
        "exec",
        "-T",
        _COMPOSE_SERVICE,
        *argv,
    ]
    return subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)


def container_exec_python(code: str, *, timeout: float = 30.0) -> str:
    """Run a Python snippet inside the agnes compose container; return stdout.

    Acceptance tests use this to verify DB side effects directly where the
    DuckDB files live (audit_log rows, analytics sums) — e.g.
    ``int(container_exec_python(snippet).strip())``. The container image ships
    ``python`` (3.13). Asserts on non-zero exit so a broken snippet surfaces
    its stderr instead of a confusing parse error on empty stdout.
    """
    proc = docker_exec(["python", "-c", code], timeout=timeout)
    assert proc.returncode == 0, (
        f"container python snippet failed (rc={proc.returncode}); stderr: {proc.stderr.decode('utf-8', 'replace')!r}"
    )
    return proc.stdout.decode("utf-8", "replace")


def container_path_exists(path: str) -> bool:
    """Return True iff ``path`` exists inside the agnes compose container.

    Bool-returning companion to :func:`assert_container_path_exists` — used by
    acceptance tests (e.g. test_sarah_day_one) that want to branch on or embed
    the existence check in their own assertion messages rather than rely on
    the helper's fixed message.
    """
    return docker_exec(["test", "-e", path]).returncode == 0


def assert_container_path_exists(path: str) -> None:
    """Assert a path exists inside the agnes compose container.

    Used by F.1, F.7, and F.8 to verify file-system side effects of the
    chat session.
    """
    proc = docker_exec(["test", "-e", path])
    assert proc.returncode == 0, (
        f"expected path {path!r} to exist in the agnes container; "
        f"`test -e` returned {proc.returncode} "
        f"(stderr: {proc.stderr.decode('utf-8', 'replace')!r})"
    )


# ---------------------------------------------------------------------------
# E2E-only credentials
# ---------------------------------------------------------------------------

# A throwaway email shared across all Phase F tests. Two reasons we
# pick a single email rather than ``per-test+secrets.token_hex``:
#
#   1. The user_workdir is created on first chat; F.1 asserts the
#      workspace was rebuilt from the bundled template. Using the same
#      email across tests lets us also exercise the "already
#      initialized" branch in subsequent tests, which is the realistic
#      production path.
#   2. RBAC grants and BQ budget counters are keyed by user_email; a
#      stable identity keeps the budget assertions in F.6 simple.
#
# If two tests need *different* users (e.g. to test concurrency or
# isolation), they can append a randomized suffix themselves.
E2E_USER_EMAIL = "e2e@agnes.local"
E2E_USER_PASSWORD = "e2e-password-at-least-12-chars-aaaaaa"


def random_session_marker() -> str:
    """Return a 6-hex token suitable for embedding in chat prompts to
    correlate WS frames with the originating test (helpful when one
    chat session is reused across multiple test functions)."""
    return secrets.token_hex(3)
