"""``GET /api/admin/conversations/export`` on Postgres (design 2026-09-08
§3.12).

PG-side by necessity, not by preference: the export reads ``llm_calls`` and
``chat_message_feedback``, both Postgres-only tables (A3 PG-first ratchet)
with no DuckDB sibling. The DuckDB side's contract — everyone gets a typed
``501 requires_postgres_backend`` — is pinned in
``tests/test_conversation_export_api.py``; the record builder itself
(shape, ordering, pseudonymization, cost_status) is pinned in
``tests/test_conversation_export.py`` against fakes. This file exercises
only what needs a real backend: the admin gate, the content-export policy
gate, and keyset cursor pagination end to end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

_EXPORT = "/api/admin/conversations/export"
_BASE = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pg_client(state_backend, seeded_app_both):
    """The PG half of ``seeded_app_both`` — skips outright on DuckDB rather
    than asserting anything there (that contract lives in
    ``tests/test_conversation_export_api.py``)."""
    if state_backend != "pg":
        pytest.skip("PG-only feature — see tests/test_conversation_export_api.py for the DuckDB 501 contract")
    return seeded_app_both


@pytest.fixture(autouse=True)
def _content_export_full(monkeypatch):
    """Default every test in this file to an ON, `full` content-export
    policy — the pagination/format tests care about the export mechanics,
    not the policy gate (which gets its own dedicated tests below)."""
    import app.api.conversations_export as mod

    monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "full")


def _seed_session(pg_engine, *, index: int, user_email: str = "analyst@test.com", surface: str = "web"):
    """One chat session with one user message, `last_message_at` pinned to
    a deterministic, strictly-increasing timestamp so keyset pagination is
    reproducible rather than relying on wall-clock resolution between
    sequential `create_session` calls.
    """
    from app.chat.types import Surface
    from src.repositories import chat_message_repo, chat_session_repo

    session = chat_session_repo().create_session(user_email=user_email, surface=Surface(surface))
    chat_message_repo().append_message(
        session_id=session.id, role="user", content=f"hello {index}", turn_id=f"t{index}"
    )

    ts = _BASE + timedelta(minutes=index)
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE chat_sessions SET last_message_at = :ts WHERE id = :id"),
            {"ts": ts, "id": session.id},
        )
        conn.execute(
            sa.text("UPDATE chat_messages SET created_at = :ts WHERE session_id = :id"),
            {"ts": ts, "id": session.id},
        )
    return session.id


class TestAdminGate:
    def test_requires_authentication(self, pg_client):
        assert pg_client["client"].get(_EXPORT).status_code == 401

    def test_refuses_a_non_admin(self, pg_client):
        resp = pg_client["client"].get(_EXPORT, headers=_auth(pg_client["analyst_token"]))
        assert resp.status_code == 403


class TestContentPolicyGate:
    def test_403_when_the_policy_is_off(self, pg_client, monkeypatch):
        import app.api.conversations_export as mod
        from src.observability.content_policy import ContentExportPolicy

        monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "off")
        monkeypatch.setattr(
            mod,
            "load_content_export_policy",
            lambda: ContentExportPolicy(
                mode="off",
                placement="",
                basis="",
                approved_by="",
                approved_at="",
                requested_mode="off",
                warnings=(),
            ),
        )
        resp = pg_client["client"].get(_EXPORT, params={"since": "2026-01-01"}, headers=_auth(pg_client["admin_token"]))
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["detail"]["error"] == "content_export_disabled"
        assert body["detail"]["reason"] == "mode_off"

    def test_403_names_no_basis_when_a_mode_was_requested_without_one(self, pg_client, monkeypatch):
        import app.api.conversations_export as mod
        from src.observability.content_policy import NO_BASIS_WARNING, ContentExportPolicy

        monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "off")
        monkeypatch.setattr(
            mod,
            "load_content_export_policy",
            lambda: ContentExportPolicy(
                mode="off",
                placement="",
                basis="",
                approved_by="",
                approved_at="",
                requested_mode="full",
                warnings=(NO_BASIS_WARNING,),
            ),
        )
        resp = pg_client["client"].get(_EXPORT, params={"since": "2026-01-01"}, headers=_auth(pg_client["admin_token"]))
        assert resp.status_code == 403
        assert resp.json()["detail"]["reason"] == "no_basis"

    def test_403_names_workload_excluded(self, pg_client, monkeypatch):
        import app.api.conversations_export as mod
        from src.observability.content_policy import ContentExportPolicy

        monkeypatch.setattr(mod, "content_export_mode", lambda workload=None: "off")
        monkeypatch.setattr(
            mod,
            "load_content_export_policy",
            lambda: ContentExportPolicy(
                mode="full",
                placement="operator",
                basis="contract",
                approved_by="ops",
                approved_at="2026-01-01",
                requested_mode="full",
                warnings=(),
                workloads=("builder",),
            ),
        )
        resp = pg_client["client"].get(_EXPORT, params={"since": "2026-01-01"}, headers=_auth(pg_client["admin_token"]))
        assert resp.status_code == 403
        assert resp.json()["detail"]["reason"] == "workload_excluded"


class TestValidation:
    def test_since_is_required(self, pg_client):
        resp = pg_client["client"].get(_EXPORT, headers=_auth(pg_client["admin_token"]))
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "since_required"

    def test_limit_over_the_cap_is_refused(self, pg_client):
        resp = pg_client["client"].get(
            _EXPORT,
            params={"since": "2026-01-01", "limit": 501},
            headers=_auth(pg_client["admin_token"]),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_limit"

    def test_a_malformed_cursor_is_a_400(self, pg_client):
        resp = pg_client["client"].get(
            _EXPORT,
            params={"since": "2026-01-01", "cursor": "not-a-real-cursor"},
            headers=_auth(pg_client["admin_token"]),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_cursor"


class TestPaginationAndFormat:
    def test_jsonl_streams_three_pages_of_two_via_the_cursor(self, pg_client, pg_engine):
        ids = [_seed_session(pg_engine, index=i) for i in range(1, 6)]  # 5 sessions

        client = pg_client["client"]
        admin = _auth(pg_client["admin_token"])
        params = {"since": "2026-01-01T00:00:00+00:00", "until": "2026-02-01T00:00:00+00:00", "limit": 2}

        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            resp = client.get(_EXPORT, params=page_params, headers=admin)
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"].startswith("application/x-ndjson")
            lines = [ln for ln in resp.text.splitlines() if ln.strip()]
            seen.extend(json.loads(ln)["thread_id"] for ln in lines)
            pages += 1
            cursor = resp.headers.get("x-next-cursor")
            if not cursor:
                break

        assert seen == ids  # ascending (last_message_at, id) order, no dupes/gaps
        assert pages == 3  # 2 + 2 + 1

    def test_format_json_returns_one_array_with_next_cursor(self, pg_client, pg_engine):
        ids = [_seed_session(pg_engine, index=i) for i in range(11, 14)]  # 3 sessions

        resp = pg_client["client"].get(
            _EXPORT,
            params={
                "since": "2026-01-01T00:00:00+00:00",
                "until": "2026-02-01T00:00:00+00:00",
                "limit": 2,
                "format": "json",
            },
            headers=_auth(pg_client["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [r["thread_id"] for r in body["data"]] == ids[:2]
        assert body["count"] == 2
        assert body["next_cursor"]
        assert resp.headers.get("x-next-cursor") == body["next_cursor"]

        resp2 = pg_client["client"].get(
            _EXPORT,
            params={
                "since": "2026-01-01T00:00:00+00:00",
                "until": "2026-02-01T00:00:00+00:00",
                "limit": 2,
                "format": "json",
                "cursor": body["next_cursor"],
            },
            headers=_auth(pg_client["admin_token"]),
        )
        body2 = resp2.json()
        assert [r["thread_id"] for r in body2["data"]] == ids[2:]
        assert body2["next_cursor"] is None
        assert "x-next-cursor" not in {k.lower() for k in resp2.headers}

    def test_surface_and_agent_id_filters_narrow_the_window(self, pg_client, pg_engine):
        from app.chat.types import Surface
        from src.repositories import chat_message_repo, chat_session_repo

        web_id = _seed_session(pg_engine, index=21, surface="web")
        slack_session = chat_session_repo().create_session(user_email="analyst@test.com", surface=Surface.SLACK_DM)
        chat_message_repo().append_message(session_id=slack_session.id, role="user", content="hi", turn_id="t22")
        with pg_engine.begin() as conn:
            ts = _BASE + timedelta(minutes=22)
            conn.execute(
                sa.text("UPDATE chat_sessions SET last_message_at = :ts WHERE id = :id"),
                {"ts": ts, "id": slack_session.id},
            )

        resp = pg_client["client"].get(
            _EXPORT,
            params={
                "since": "2026-01-01T00:00:00+00:00",
                "until": "2026-02-01T00:00:00+00:00",
                "surface": "web",
                "limit": 50,
            },
            headers=_auth(pg_client["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        lines = [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]
        thread_ids = {r["thread_id"] for r in lines}
        assert web_id in thread_ids
        assert slack_session.id not in thread_ids


class TestCostStatusEndToEnd:
    def test_ledger_when_an_llm_calls_row_exists_else_unavailable(self, pg_client, pg_engine):
        from src.observability.llm_context import LlmCallContext
        from src.observability.llm_record import build_record
        from src.repositories import llm_calls_repo

        with_ledger = _seed_session(pg_engine, index=31)
        without_ledger = _seed_session(pg_engine, index=32)

        record = build_record(
            kind="completion",
            context=LlmCallContext(workload="chat", session_id=with_ledger, turn_id="t31", user_id="user_1"),
            provider="anthropic",
            upstream="anthropic",
            model_requested="claude-haiku-4-5",
            model_response=None,
            usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0},
            latency_ms=5,
            status="ok",
        )
        llm_calls_repo().insert_batch([record.to_row()])

        resp = pg_client["client"].get(
            _EXPORT,
            params={"since": "2026-01-01T00:00:00+00:00", "until": "2026-02-01T00:00:00+00:00", "limit": 50},
            headers=_auth(pg_client["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        by_id = {json.loads(ln)["thread_id"]: json.loads(ln) for ln in resp.text.splitlines() if ln.strip()}
        assert by_id[with_ledger]["cost_status"] == "ledger"
        assert by_id[with_ledger]["llm_run_count"] == 1
        assert by_id[without_ledger]["cost_status"] == "unavailable"


class TestAuditRow:
    def test_export_writes_one_audit_row_never_content(self, pg_client, pg_engine):
        _seed_session(pg_engine, index=41)

        resp = pg_client["client"].get(
            _EXPORT,
            params={"since": "2026-01-01T00:00:00+00:00", "until": "2026-02-01T00:00:00+00:00", "limit": 50},
            headers=_auth(pg_client["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

        with pg_engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM audit_log WHERE action = 'conversations.export' ORDER BY timestamp DESC LIMIT 1"
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"])
        assert params["delivery"] == "pull"
        assert params["content_mode"] == "full"
        assert params["count"] >= 1
        dumped = json.dumps(params)
        assert "hello 41" not in dumped
