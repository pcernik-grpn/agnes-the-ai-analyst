"""``GET /api/admin/telemetry/llm-cost|llm-calls|feedback`` — admin gate and
the typed 501 on the frozen DuckDB backend (LLM observability design
2026-09-08 §3.4/§3.5).

``llm_calls_repo()`` and ``chat_message_feedback_repo()`` are Postgres-only
under the A3 ratchet, resolved as FastAPI dependencies, so on a DuckDB-backed
instance every one of these three routes fails clean BEFORE any query
parameter is even looked at — a bad ``window``/``by`` still 501s here, never a
400. The 400s themselves, and the actual grouping/paging/filtering, are
asserted on Postgres in ``tests/db_pg/test_admin_llm_cost_pg.py``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestLlmCostGating:
    def test_non_admin_is_refused(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/llm-cost", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code in (401, 403)

    def test_anonymous_is_refused(self, seeded_app):
        c: TestClient = seeded_app["client"]
        assert c.get("/api/admin/telemetry/llm-cost").status_code in (401, 403)

    def test_answers_a_typed_501_on_duckdb(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/llm-cost", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 501, r.text
        assert r.json()["error"] == "requires_postgres_backend"

    def test_a_bad_window_or_by_still_501s_before_validation(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get(
            "/api/admin/telemetry/llm-cost",
            params={"window": "nonsense", "by": "nonsense"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 501, r.text
        assert r.json()["error"] == "requires_postgres_backend"


class TestLlmCallsGating:
    def test_non_admin_is_refused(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/llm-calls", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code in (401, 403)

    def test_answers_a_typed_501_on_duckdb_even_with_no_id(self, seeded_app):
        """No session/turn/job/user id given — on Postgres this would be a
        400 (the ``one of ... is required`` check), but the repo dependency
        raises first on DuckDB, so it is a 501 here regardless."""
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/llm-calls", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 501, r.text
        assert r.json()["error"] == "requires_postgres_backend"


class TestFeedbackGating:
    def test_non_admin_is_refused(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/feedback", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code in (401, 403)

    def test_answers_a_typed_501_on_duckdb(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/feedback", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 501, r.text
        assert r.json()["error"] == "requires_postgres_backend"


class _StubChatRepo:
    """Minimal stand-in for ChatRepository.cost_breakdown — lets the
    chat-cost note assertion below run without needing a live Postgres."""

    def cost_breakdown(self, *, since=None, user_email=None, limit=50):
        return []


class TestChatCostNotesPointAtLlmCost:
    def test_chat_cost_notes_mention_the_cross_workload_view(self, seeded_app):
        c: TestClient = seeded_app["client"]
        c.app.state.chat_repo = _StubChatRepo()  # restored by the fixture's teardown
        r = c.get("/api/admin/telemetry/chat-cost", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        notes = r.json()["notes"]
        assert any("llm-cost" in n for n in notes), notes
