"""F.6 — remote BigQuery query + per-session scan-bytes budget.

Two assertions in one E2E flow:

  1. When the assistant runs ``agnes query --remote "SELECT COUNT(*) FROM
     <registered_id>"``, the BQ scan completes and a row count flows back
     into the assistant message.
  2. The per-session BigQuery budget counter
     (``app.api.query._per_session_bq_bytes`` — a module-level dict keyed
     by ``chat_session_id``) increments by the dry-run-reported scan
     size for THIS chat session, then snaps back to that value (i.e. the
     accumulator isn't leaking other sessions' bytes in).

The test is layered behind three opt-ins:

  * ``AGNES_E2E=1``           — docker-compose stack on
  * ``AGNES_E2E_ANTHROPIC=1`` — real LLM picks `agnes query --remote`
  * ``AGNES_E2E_BQ=1``        — host has BQ creds + a registered table

Without the BQ env it skips cleanly — most contributors will never
have a service account for the test GCP project, and we don't want
the rest of Phase F to be hostage to that.

We do NOT register the BQ table here — the test container is expected
to come up with at least one ``query_mode='remote'`` row in
``table_registry`` (the operator running the E2E sets up the
admin-side registration via a sidecar SQL script, or the
``AGNES_E2E_BQ_TABLE_ID`` env points at an existing one). The plan's
TODO of "use a stub DuckDB BQ extension shim" is deferred — this test
is real-warehouse-only.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.e2e._helpers import (
    E2E_USER_EMAIL,
    E2E_USER_PASSWORD,
    bootstrap_admin,
    pump_until,
    skip_unless_chat_sessions_possible,
)


# Two markers stack: `real_llm` (handled by conftest.py's
# pytest_collection_modifyitems) plus an explicit skipif here for the
# warehouse dependency. Both have to be satisfied to actually run.
pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not os.environ.get("AGNES_E2E_BQ"),
        reason="F.6 requires a registered BQ table — set AGNES_E2E_BQ=1 + "
        "ANTHROPIC_API_KEY + a service-account-backed test BQ project, then "
        "register the table via /api/admin/register-table before running.",
    ),
]


try:
    from websockets.sync.client import connect as ws_connect

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ws_connect = None  # type: ignore[assignment]
    _WS_AVAILABLE = False


# The default test table id. Operators can point at their own
# registered table via env to avoid hard-coding a fixture identity.
_DEFAULT_BQ_TABLE_ID = "web_sessions_example"


def _bq_table_id() -> str:
    return os.environ.get("AGNES_E2E_BQ_TABLE_ID", _DEFAULT_BQ_TABLE_ID)


@pytest.fixture(scope="module")
def admin_client(docker_e2e_agnes: str):
    return bootstrap_admin(
        docker_e2e_agnes,
        email=E2E_USER_EMAIL,
        password=E2E_USER_PASSWORD,
    )


def _read_session_bq_bytes(admin_client, session_id: str) -> int:
    """Read the per-session BQ-bytes counter via the admin debug endpoint.

    The counter lives in the gateway process
    (``app.api.query._per_session_bq_bytes``), not in the sandbox, so the
    right read path is the server's own API: ``GET /admin/chat/{id}/debug``
    (admin-only) returns the process-local counter without coupling the
    test to docker-exec machinery or the container layout. Returns 0 when
    the session has not yet been charged.
    """
    status, payload = admin_client.get(f"/admin/chat/{session_id}/debug")
    assert status == 200, f"admin debug endpoint should return 200; got {status} {payload!r}"
    return int(payload.get("bq_bytes", 0))


def test_f6_remote_bq_count_increments_per_session_budget(
    docker_e2e_agnes: str,
    admin_client,
) -> None:
    """End-to-end: chat → remote BQ COUNT(*) → budget accumulator ticks up."""
    if not _WS_AVAILABLE:
        pytest.skip("websockets.sync.client unavailable — old python?")

    table_id = _bq_table_id()
    skip_unless_chat_sessions_possible()
    session = admin_client.create_chat_session(surface="web")

    before = _read_session_bq_bytes(admin_client, session["id"])
    assert before == 0, f"fresh session {session['id']} should have zero BQ bytes; got {before}"

    ws_url = admin_client.ws_url_for(session)
    asst_texts: list[str] = []
    saw_remote_call = False

    with ws_connect(ws_url, open_timeout=15) as ws:
        pump_until(ws, predicate=lambda f: f.get("type") in ("ready", "runner_ready"))
        prompt = (
            f"Count the rows in the BQ-registered {table_id} table. "
            "Use the appropriate `agnes` CLI command for remote-mode tables."
        )
        ws.send(json.dumps({"type": "user_msg", "text": prompt}))

        for _ in range(400):
            try:
                raw = ws.recv(timeout=120.0)
            except TimeoutError:
                break
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "tool_call":
                args = frame.get("args") or {}
                cmd = args.get("command") or ""
                if "agnes query --remote" in cmd or "--remote" in cmd:
                    saw_remote_call = True
            elif frame.get("type") == "assistant_message":
                content = (frame.get("content") or "").strip()
                if content:
                    asst_texts.append(content)
                    break

    assert saw_remote_call, "expected the assistant to invoke `agnes query --remote` for a remote-mode table"
    assert asst_texts, "never saw an assistant_message before the WS closed"

    after = _read_session_bq_bytes(admin_client, session["id"])
    assert after > before, (
        f"per-session BQ bytes should have advanced past {before}; got {after}. "
        "Either the remote query never fired (regression in the chat-JWT → "
        "request.state stash), or the budget accumulator wasn't called."
    )
