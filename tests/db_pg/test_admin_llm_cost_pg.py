"""``GET /api/admin/telemetry/llm-cost|llm-calls|feedback`` on Postgres — the
LLM observability read surfaces (design 2026-09-08 §3.4/§3.5).

``llm_calls`` and ``chat_message_feedback`` are Postgres-only under the A3
ratchet, so Postgres is the only backend that can serve any of this. The
DuckDB side's contract — the admin gate and the typed 501 — is pinned in
``tests/test_admin_llm_cost_api.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.auth.jwt import create_access_token
from src.observability.llm_context import LlmCallContext
from src.observability.llm_record import build_record

from ._parity_sweep_util import build_seeded_client

_USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 200, "cache_creation_tokens": 0}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_calls(now):
    """Six rows: two workloads (chat/builder), two users, three models, one
    row 40 days old (excluded from a 30d window). Three of the six carry
    ``session_id="s1"`` at distinct, ordered timestamps for the llm-calls
    pagination test.
    """
    from src.repositories import llm_calls_repo

    specs = [
        ("chat", "u1", "claude-sonnet-5", "s1", now),
        ("chat", "u1", "claude-sonnet-5", "s1", now - timedelta(minutes=1)),
        ("chat", "u2", "claude-opus-5", "s1", now - timedelta(minutes=2)),
        ("builder", "u2", "claude-haiku-4-5", "s2", now - timedelta(minutes=3)),
        ("builder", "u2", "claude-haiku-4-5", "s2", now - timedelta(minutes=4)),
        ("builder", "u1", "claude-haiku-4-5", "s2", now - timedelta(days=40)),
    ]
    records = []
    for workload, user_id, model, session_id, created_at in specs:
        ctx = LlmCallContext(workload=workload, user_id=user_id, session_id=session_id)
        rec = build_record(
            kind="completion",
            context=ctx,
            provider="anthropic",
            upstream="anthropic",
            model_requested=model,
            model_response=model,
            usage=_USAGE,
            latency_ms=10,
            status="ok",
            created_at=created_at,
        )
        records.append(rec)
    llm_calls_repo().insert_batch([r.to_row() for r in records])
    return records


class TestLlmCost:
    def test_groups_by_workload_sums_match_and_windows_it(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        now = datetime.now(UTC)
        records = _seed_calls(now)

        r = client.get(
            "/api/admin/telemetry/llm-cost", params={"window": "all", "by": "workload"}, headers=_auth(admin_token)
        )
        assert r.status_code == 200, r.text
        body = r.json()
        groups = {g["key"]: g for g in body["groups"]}
        assert set(groups) == {"chat", "builder"}
        assert groups["chat"]["calls"] == 3
        assert groups["builder"]["calls"] == 3

        chat_cost = round(sum(rec.cost_usd for rec in records if rec.workload == "chat"), 6)
        builder_cost = round(sum(rec.cost_usd for rec in records if rec.workload == "builder"), 6)
        assert groups["chat"]["cost_usd"] == chat_cost
        assert groups["builder"]["cost_usd"] == builder_cost
        assert body["totals"]["cost_usd"] == round(chat_cost + builder_cost, 6)

        # cached_input_share = cache_read / (input + cache_read + cache_creation);
        # every seeded row carries the same usage shape, so the group share
        # equals the per-row share regardless of group size.
        assert groups["chat"]["cached_input_share"] == round(200 / 1200, 4)
        assert groups["builder"]["cached_input_share"] == round(200 / 1200, 4)

        assert sorted(groups["builder"]["priced_models"]) == ["claude-haiku-4-5"]
        assert sorted(groups["chat"]["priced_models"]) == ["claude-opus-5", "claude-sonnet-5"]

        # window=30d excludes the 40-day-old builder row; window=all includes it.
        r30 = client.get(
            "/api/admin/telemetry/llm-cost", params={"window": "30d", "by": "workload"}, headers=_auth(admin_token)
        )
        assert r30.status_code == 200, r30.text
        groups30 = {g["key"]: g for g in r30.json()["groups"]}
        assert groups30["builder"]["calls"] == 2

    def test_groups_by_user_and_by_model(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        now = datetime.now(UTC)
        _seed_calls(now)

        by_user = client.get(
            "/api/admin/telemetry/llm-cost", params={"window": "all", "by": "user"}, headers=_auth(admin_token)
        ).json()
        assert {g["key"] for g in by_user["groups"]} == {"u1", "u2"}

        by_model = client.get(
            "/api/admin/telemetry/llm-cost", params={"window": "all", "by": "model"}, headers=_auth(admin_token)
        ).json()
        assert {g["key"] for g in by_model["groups"]} == {"claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"}

    def test_rejects_bad_window_and_by(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)

        r1 = client.get("/api/admin/telemetry/llm-cost", params={"by": "nonsense"}, headers=_auth(admin_token))
        assert r1.status_code == 400

        r2 = client.get("/api/admin/telemetry/llm-cost", params={"window": "2w"}, headers=_auth(admin_token))
        assert r2.status_code == 400

    def test_non_admin_is_refused(self, tmp_path, monkeypatch, pg_engine):
        client, _ = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        analyst_token = create_access_token("analyst1", "analyst@test.com")
        r = client.get("/api/admin/telemetry/llm-cost", headers=_auth(analyst_token))
        assert r.status_code == 403


class TestLlmCalls:
    def test_requires_one_id(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        r = client.get("/api/admin/telemetry/llm-calls", headers=_auth(admin_token))
        assert r.status_code == 400

    def test_pages_newest_first_with_a_before_cursor(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        now = datetime.now(UTC)
        _seed_calls(now)

        r1 = client.get(
            "/api/admin/telemetry/llm-calls",
            params={"session_id": "s1", "limit": 2},
            headers=_auth(admin_token),
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert len(body1["rows"]) == 2
        assert body1["next_before"] is not None
        first_page_ids = {row["id"] for row in body1["rows"]}

        r2 = client.get(
            "/api/admin/telemetry/llm-calls",
            params={"session_id": "s1", "before": body1["next_before"]},
            headers=_auth(admin_token),
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert len(body2["rows"]) == 1
        assert body2["rows"][0]["id"] not in first_page_ids
        # A non-empty page always carries its own last row's cursor — the
        # only null case is an EMPTY page, not "the last real page".
        assert body2["next_before"] is not None

        r3 = client.get(
            "/api/admin/telemetry/llm-calls",
            params={"session_id": "s1", "before": body2["next_before"]},
            headers=_auth(admin_token),
        )
        assert r3.status_code == 200, r3.text
        body3 = r3.json()
        assert body3["rows"] == []
        assert body3["next_before"] is None

    def test_an_empty_result_names_the_next_step(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        r = client.get("/api/admin/telemetry/llm-calls", params={"session_id": "nope"}, headers=_auth(admin_token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"] == []
        assert body["notes"]

    def test_pages_with_the_composite_before_id_cursor(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        now = datetime.now(UTC)
        _seed_calls(now)

        r1 = client.get(
            "/api/admin/telemetry/llm-calls",
            params={"session_id": "s1", "limit": 2},
            headers=_auth(admin_token),
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert len(body1["rows"]) == 2
        assert body1["next_before"] is not None
        assert body1["next_before_id"] is not None
        assert body1["next_before_id"] == body1["rows"][-1]["id"]
        first_page_ids = {row["id"] for row in body1["rows"]}

        r2 = client.get(
            "/api/admin/telemetry/llm-calls",
            params={"session_id": "s1", "before": body1["next_before"], "before_id": body1["next_before_id"]},
            headers=_auth(admin_token),
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert len(body2["rows"]) == 1
        assert body2["rows"][0]["id"] not in first_page_ids


class TestFeedback:
    def test_lists_and_filters_by_verdict(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        from src.repositories import chat_message_feedback_repo

        chat_message_feedback_repo().upsert(session_id="c1", turn_id="t1", user_id="u1", verdict="up")
        chat_message_feedback_repo().upsert(
            session_id="c1", turn_id="t2", user_id="u1", verdict="down", comment="wrong number"
        )

        r = client.get("/api/admin/telemetry/feedback", headers=_auth(admin_token))
        assert r.status_code == 200, r.text
        assert len(r.json()["rows"]) == 2

        r_down = client.get("/api/admin/telemetry/feedback", params={"verdict": "down"}, headers=_auth(admin_token))
        assert r_down.status_code == 200, r_down.text
        rows = r_down.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["verdict"] == "down"
        assert rows[0]["comment"] == "wrong number"

    def test_bad_verdict_and_window_are_400(self, tmp_path, monkeypatch, pg_engine):
        client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        assert (
            client.get(
                "/api/admin/telemetry/feedback", params={"verdict": "sideways"}, headers=_auth(admin_token)
            ).status_code
            == 400
        )
        assert (
            client.get("/api/admin/telemetry/feedback", params={"window": "2w"}, headers=_auth(admin_token)).status_code
            == 400
        )

    def test_non_admin_is_refused(self, tmp_path, monkeypatch, pg_engine):
        client, _ = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
        analyst_token = create_access_token("analyst1", "analyst@test.com")
        r = client.get("/api/admin/telemetry/feedback", headers=_auth(analyst_token))
        assert r.status_code == 403
