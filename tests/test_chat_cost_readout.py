"""`GET /api/admin/telemetry/chat-cost` — measured chat cost, cached vs uncached.

The route exists because the alternative is modelling, and the model that
gets built by hand almost always charges a re-read of a cached prefix at the
full input rate. On a long agent session that single term dominates the
bill, so getting it wrong by ~10x is enough to reverse the conclusion of a
cost comparison. These tests pin the parts that keep the answer honest:
admin-gated, model-aware pricing, and a zero that is never allowed to
masquerade as a measurement.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.llm_pricing import cost_usd


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestGating:
    def test_non_admin_is_refused(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/chat-cost", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code in (401, 403)

    def test_anonymous_is_refused(self, seeded_app):
        c: TestClient = seeded_app["client"]
        assert c.get("/api/admin/telemetry/chat-cost").status_code in (401, 403)

    def test_bad_window_is_a_clean_400(self, seeded_app):
        c: TestClient = seeded_app["client"]
        r = c.get(
            "/api/admin/telemetry/chat-cost",
            params={"window": "forever"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 400
        assert "window" in r.json()["detail"]


class TestAnswersOrFailsClean:
    def test_answers_or_declares_what_is_missing(self, seeded_app):
        """Three legitimate outcomes, no fourth: 200 with measured numbers,
        501 when the app-state backend has no prompt-cache columns (they are
        Postgres-only under the A3 freeze), or 503 when this process has no
        chat repository at all. Never a raw 500, and never cache-blind zeros
        presented as "this workload used no cache"."""
        c: TestClient = seeded_app["client"]
        r = c.get("/api/admin/telemetry/chat-cost", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code in (200, 501, 503), r.text
        if r.status_code == 501:
            assert r.json()["error"] == "requires_postgres_backend"
        elif r.status_code == 503:
            assert "chat_unavailable" in r.json()["detail"]

    def test_windows_are_accepted(self, seeded_app):
        c: TestClient = seeded_app["client"]
        for window in ("1d", "7d", "30d", "all"):
            r = c.get(
                "/api/admin/telemetry/chat-cost",
                params={"window": window},
                headers=_auth(seeded_app["admin_token"]),
            )
            assert r.status_code in (200, 501, 503), (window, r.text)


class _StubRepo:
    """Minimal stand-in for ChatRepository.cost_breakdown."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    def cost_breakdown(self, *, since=None, user_email=None, limit=50):
        self.calls.append({"since": since, "user_email": user_email, "limit": limit})
        return self.rows


class TestPayload:
    """Route logic against a stubbed repository — the arithmetic and the
    honesty markers, without needing a live Postgres."""

    @staticmethod
    def _get(seeded_app, rows, **params):
        c: TestClient = seeded_app["client"]
        stub = _StubRepo(rows)
        c.app.state.chat_repo = stub  # restored by the fixture's teardown
        r = c.get(
            "/api/admin/telemetry/chat-cost",
            params=params,
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        return r.json(), stub

    def test_prices_each_session_by_the_model_it_actually_ran_on(self, seeded_app):
        """One averaged rate across models is how a cost report ends up
        confidently wrong; each row carries its own model."""
        rows = [
            {
                "session_id": "s-sonnet",
                "user_email": "a@test.com",
                "model": "claude-sonnet-5",
                "messages": 2,
                "cache_recorded_messages": 2,
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "last_message_at": None,
            },
            {
                "session_id": "s-opus",
                "user_email": "a@test.com",
                "model": "claude-opus-5",
                "messages": 2,
                "cache_recorded_messages": 2,
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "last_message_at": None,
            },
        ]
        body, _ = self._get(seeded_app, rows)
        by_id = {s["session_id"]: s for s in body["sessions"]}
        assert by_id["s-sonnet"]["cost_usd"] == 3.0
        assert by_id["s-opus"]["cost_usd"] == 5.0
        assert body["totals"]["cost_usd"] == 8.0

    def test_cached_reads_are_priced_as_cached(self, seeded_app):
        """The term a hand-built model overcharges ~10x. 10M cached reads on
        Sonnet 5 is $3, not $30."""
        rows = [
            {
                "session_id": "s1",
                "user_email": "a@test.com",
                "model": "claude-sonnet-5",
                "messages": 40,
                "cache_recorded_messages": 40,
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read_tokens": 10_000_000,
                "cache_creation_tokens": 0,
                "last_message_at": None,
            }
        ]
        body, _ = self._get(seeded_app, rows)
        assert body["totals"]["cost_usd"] == 3.0
        assert body["totals"]["cached_input_share"] == 1.0

    def test_unrecorded_cache_is_reported_as_unavailable_not_as_zero(self, seeded_app):
        """The single most important honesty marker in the payload: a row
        with no cache figures must not read as a measured zero."""
        rows = [
            {
                "session_id": "old",
                "user_email": "a@test.com",
                "model": "claude-sonnet-5",
                "messages": 10,
                "cache_recorded_messages": 0,
                "tokens_in": 100,
                "tokens_out": 50,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "last_message_at": None,
            }
        ]
        body, _ = self._get(seeded_app, rows)
        assert body["sessions"][0]["cache_accounting"] == "unavailable"
        assert any("NOT zero" in note for note in body["notes"])

    def test_partial_recording_is_labelled_partial(self, seeded_app):
        rows = [
            {
                "session_id": "mixed",
                "user_email": "a@test.com",
                "model": "claude-sonnet-5",
                "messages": 10,
                "cache_recorded_messages": 4,
                "tokens_in": 100,
                "tokens_out": 50,
                "cache_read_tokens": 900,
                "cache_creation_tokens": 0,
                "last_message_at": None,
            }
        ]
        body, _ = self._get(seeded_app, rows)
        assert body["sessions"][0]["cache_accounting"] == "partial"

    def test_window_and_user_reach_the_repository(self, seeded_app):
        _, stub = self._get(seeded_app, [], window="30d", user="a@test.com", limit=7)
        call = stub.calls[-1]
        assert call["user_email"] == "a@test.com"
        assert call["limit"] == 7
        assert call["since"] is not None

    def test_window_all_asks_for_everything(self, seeded_app):
        _, stub = self._get(seeded_app, [], window="all")
        assert stub.calls[-1]["since"] is None

    def test_each_row_states_the_rates_it_was_priced_at(self, seeded_app):
        """A cost figure nobody can re-derive is just another model. The rates
        travel with the row."""
        rows = [
            {
                "session_id": "s1",
                "user_email": "a@test.com",
                "model": "claude-sonnet-5",
                "messages": 1,
                "cache_recorded_messages": 1,
                "tokens_in": 10,
                "tokens_out": 10,
                "cache_read_tokens": 10,
                "cache_creation_tokens": 10,
                "last_message_at": None,
            }
        ]
        body, _ = self._get(seeded_app, rows)
        priced = body["sessions"][0]["priced_as"]
        assert priced["input_per_mtok"] == 3.0
        assert priced["cache_read_per_mtok"] == 0.3
        assert priced["cache_write_per_mtok"] == 3.75


class TestPricingIsTheSharedOne:
    """The route must not grow its own arithmetic — that is how the two
    constants this work replaced drifted from reality in the first place."""

    def test_route_prices_through_llm_pricing(self):
        import inspect

        import app.api.admin_usage as mod

        src = inspect.getsource(mod.chat_cost)
        assert "cost_usd(" in src
        # No hand-rolled per-million arithmetic in the route body.
        assert "1_000_000" not in src
        assert "/ 1000000" not in src

    def test_a_cached_heavy_session_is_cheap_not_expensive(self):
        """The property the whole feature turns on, asserted directly: a
        session that re-reads a large cached prefix costs a fraction of what
        the same volume of uncached input would."""
        as_cached = cost_usd(model="claude-sonnet-5", cache_read_tokens=10_000_000)
        as_uncached = cost_usd(model="claude-sonnet-5", input_tokens=10_000_000)
        assert as_cached < as_uncached / 5
