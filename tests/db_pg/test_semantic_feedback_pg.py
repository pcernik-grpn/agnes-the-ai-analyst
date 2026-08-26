"""Semantic-layer feedback — repository, endpoints and CLI, on Postgres.

PG-side by necessity, not by preference: ``semantic_feedback`` is a
Postgres-only table (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend
discipline"), so Postgres is the only backend on which any of this can run at
all. There is no DuckDB sibling to parametrize against, which is why this file
follows ``tests/db_pg/test_resource_source_tags_pg.py`` /
``test_mcp_sources_contract.py``'s PG-only shape rather than the cross-engine
contract shape.

The DuckDB side's contract — who is refused, and that everyone else meets a
typed ``501 requires_postgres_backend`` rather than a crash — is pinned in
``tests/test_semantic_feedback_endpoint.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_SUBMIT = "/api/semantic-feedback"
_QUEUE = "/api/admin/semantic-feedback"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.semantic_feedback_pg import SemanticFeedbackPgRepository

    return SemanticFeedbackPgRepository(pg_engine)


class TestTheRepository:
    def test_create_returns_the_stored_row_as_open(self, repo):
        row = repo.create(
            question="What was net revenue last quarter?",
            sql="SELECT SUM(amount) FROM orders",
            metric_id="revenue/net",
            model_content_hash="abc123",
            comment="Gross, not net — the refunds are missing.",
            created_by="analyst@test.com",
        )
        assert row["id"]
        assert row["question"] == "What was net revenue last quarter?"
        assert row["sql"] == "SELECT SUM(amount) FROM orders"
        assert row["metric_id"] == "revenue/net"
        assert row["model_content_hash"] == "abc123"
        assert row["comment"] == "Gross, not net — the refunds are missing."
        assert row["created_by"] == "analyst@test.com"
        assert row["created_at"] is not None
        # A report is open until somebody says otherwise — never
        # pre-resolved, and never a NULL status the queue filter would drop.
        assert row["status"] == "open"
        assert row["resolved_at"] is None
        assert row["resolved_by"] is None
        assert row["resolution_note"] is None

    def test_only_the_question_is_required(self, repo):
        """An agent that noticed something is wrong but has no SQL, no metric
        and no model in hand must still be able to file it — everything except
        the question is optional context."""
        row = repo.create(question="Why is churn undefined?", created_by=None)
        assert row["question"] == "Why is churn undefined?"
        assert row["sql"] is None
        assert row["metric_id"] is None
        assert row["comment"] is None
        assert row["created_by"] is None
        assert row["status"] == "open"

    def test_list_returns_every_report_when_unfiltered(self, repo):
        a = repo.create(question="a", created_by="x")
        b = repo.create(question="b", created_by="x")

        assert {r["id"] for r in repo.list()} == {a["id"], b["id"]}

    def test_list_filters_on_status(self, repo):
        open_row = repo.create(question="still wrong", created_by="x")
        done_row = repo.create(question="was wrong", created_by="x")
        assert repo.resolve(done_row["id"], resolved_by="admin@test.com", resolution_note="added the metric") is True

        assert [r["id"] for r in repo.list(status="open")] == [open_row["id"]]
        assert [r["id"] for r in repo.list(status="resolved")] == [done_row["id"]]

    def test_resolve_records_who_closed_it_and_why(self, repo):
        row = repo.create(question="mrr looks doubled", created_by="analyst@test.com")

        assert repo.resolve(row["id"], resolved_by="admin@test.com", resolution_note="deduped the join") is True

        stored = repo.get(row["id"])
        assert stored is not None
        assert stored["status"] == "resolved"
        assert stored["resolved_by"] == "admin@test.com"
        assert stored["resolution_note"] == "deduped the join"
        assert stored["resolved_at"] is not None

    def test_resolve_is_a_guarded_transition(self, repo):
        """Second resolve reports False rather than overwriting the first
        admin's note — the endpoint turns that into 409, so the queue never
        loses who actually fixed it."""
        row = repo.create(question="q", created_by="x")
        assert repo.resolve(row["id"], resolved_by="first@test.com", resolution_note="mine") is True
        assert repo.resolve(row["id"], resolved_by="second@test.com", resolution_note="no, mine") is False

        stored = repo.get(row["id"])
        assert stored is not None
        assert stored["resolved_by"] == "first@test.com"
        assert stored["resolution_note"] == "mine"

    def test_resolve_of_an_unknown_id_reports_false(self, repo):
        assert repo.resolve("sfb_nope", resolved_by="admin@test.com", resolution_note=None) is False

    def test_get_of_an_unknown_id_is_none(self, repo):
        assert repo.get("sfb_nope") is None

    def test_a_resolution_note_is_optional(self, repo):
        row = repo.create(question="q", created_by="x")
        assert repo.resolve(row["id"], resolved_by="admin@test.com", resolution_note=None) is True
        stored = repo.get(row["id"])
        assert stored is not None
        assert stored["status"] == "resolved"
        assert stored["resolution_note"] is None


class TestTheEndpointsOnPostgres:
    def test_a_non_admin_can_file_a_report(self, state_backend, seeded_app_both):
        """The whole point of the channel: the person who SAW the bad answer
        files it. An admin-only submit would mean the only people who can
        report a wrong number are the ones who never see it in an analysis."""
        if state_backend != "pg":
            pytest.skip("PG-only feature — the DuckDB contract is the typed 501")

        resp = seeded_app_both["client"].post(
            _SUBMIT,
            json={
                "question": "What was MRR in June?",
                "sql": "SELECT SUM(mrr) FROM subs",
                "metric_id": "revenue/mrr",
                "comment": "Counted cancelled subs.",
            },
            headers=_auth(seeded_app_both["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "open"
        assert body["created_by"] == "analyst@test.com"

    def test_a_filed_report_reaches_the_admin_queue(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]

        client.post(
            _SUBMIT,
            json={"question": "Why is churn undefined?"},
            headers=_auth(seeded_app_both["analyst_token"]),
        )

        resp = client.get(_QUEUE, headers=_auth(seeded_app_both["admin_token"]))
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert [i["question"] for i in items] == ["Why is churn undefined?"]

    def test_the_queue_filters_on_status(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        admin = _auth(seeded_app_both["admin_token"])

        first = client.post(_SUBMIT, json={"question": "one"}, headers=admin).json()
        client.post(_SUBMIT, json={"question": "two"}, headers=admin)
        client.post(f"{_QUEUE}/{first['id']}/resolve", json={"resolution_note": "fixed"}, headers=admin)

        open_items = client.get(f"{_QUEUE}?status=open", headers=admin).json()["items"]
        assert [i["question"] for i in open_items] == ["two"]

        resolved_items = client.get(f"{_QUEUE}?status=resolved", headers=admin).json()["items"]
        assert [i["question"] for i in resolved_items] == ["one"]

    def test_an_unknown_status_filter_is_refused_rather_than_silently_empty(self, state_backend, seeded_app_both):
        """An empty list would read as "nothing to do", which is the opposite
        of the truth when the filter itself was a typo."""
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = seeded_app_both["client"].get(f"{_QUEUE}?status=opne", headers=_auth(seeded_app_both["admin_token"]))
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "unknown_status"

    def test_resolve_stamps_the_admin_and_the_note(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        admin = _auth(seeded_app_both["admin_token"])

        filed = client.post(
            _SUBMIT, json={"question": "mrr doubled"}, headers=_auth(seeded_app_both["analyst_token"])
        ).json()

        resp = client.post(f"{_QUEUE}/{filed['id']}/resolve", json={"resolution_note": "deduped"}, headers=admin)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "resolved"
        assert body["resolved_by"] == "admin@test.com"
        assert body["resolution_note"] == "deduped"

    def test_resolving_twice_is_a_409(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        admin = _auth(seeded_app_both["admin_token"])

        filed = client.post(_SUBMIT, json={"question": "q"}, headers=admin).json()
        assert client.post(f"{_QUEUE}/{filed['id']}/resolve", json={}, headers=admin).status_code == 200

        again = client.post(f"{_QUEUE}/{filed['id']}/resolve", json={}, headers=admin)
        assert again.status_code == 409
        assert again.json()["detail"]["error"] == "already_resolved"

    def test_resolving_an_unknown_report_is_a_404(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = seeded_app_both["client"].post(
            f"{_QUEUE}/sfb_nope/resolve", json={}, headers=_auth(seeded_app_both["admin_token"])
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "unknown_feedback"

    def test_an_empty_question_is_refused(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = seeded_app_both["client"].post(
            _SUBMIT, json={"question": "   "}, headers=_auth(seeded_app_both["analyst_token"])
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "missing_question"

    def test_the_queue_refuses_a_non_admin_on_postgres_too(self, state_backend, seeded_app_both):
        """The 403 in `tests/test_semantic_feedback_endpoint.py` is asserted on
        DuckDB, where a PG-only route could conceivably 501 before the gate.
        Re-assert it here so the gate is proven on the backend that can
        actually serve the route."""
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        analyst = _auth(seeded_app_both["analyst_token"])

        assert client.get(_QUEUE, headers=analyst).status_code == 403
        assert client.post(f"{_QUEUE}/sfb_x/resolve", json={}, headers=analyst).status_code == 403


class TestTheCli:
    """`agnes semantic-model feedback …` — all three verbs, per the design
    decision that a report must be fileable from every surface (UI, chat, MCP,
    CLI), not just the ones an admin uses."""

    def test_submit_then_list_round_trips(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        submitted = cli_client_both["invoke"](
            [
                "semantic-model",
                "feedback",
                "submit",
                "What was net revenue?",
                "--metric",
                "revenue/net",
                "--comment",
                "gross, not net",
            ]
        )
        assert submitted.exit_code == 0, submitted.output
        assert "Filed" in submitted.output

        listed = cli_client_both["invoke"](["semantic-model", "feedback", "list", "--json"])
        assert listed.exit_code == 0, listed.output
        items = json.loads(listed.output)["items"]
        assert [i["question"] for i in items] == ["What was net revenue?"]
        assert items[0]["metric_id"] == "revenue/net"

    def test_list_prints_the_open_queue_by_default(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        invoke = cli_client_both["invoke"]

        invoke(["semantic-model", "feedback", "submit", "churn is undefined"])
        result = invoke(["semantic-model", "feedback", "list"])
        assert result.exit_code == 0, result.output
        assert "churn is undefined" in result.output

    def test_resolve_closes_it_and_the_note_survives(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        invoke = cli_client_both["invoke"]

        invoke(["semantic-model", "feedback", "submit", "mrr doubled"])
        listed = json.loads(invoke(["semantic-model", "feedback", "list", "--json"]).output)
        feedback_id = listed["items"][0]["id"]

        resolved = invoke(["semantic-model", "feedback", "resolve", feedback_id, "--note", "deduped the join"])
        assert resolved.exit_code == 0, resolved.output

        after = json.loads(invoke(["semantic-model", "feedback", "list", "--status", "resolved", "--json"]).output)
        assert after["items"][0]["resolution_note"] == "deduped the join"

    def test_an_empty_queue_says_so_instead_of_printing_nothing(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        result = cli_client_both["invoke"](["semantic-model", "feedback", "list"])
        assert result.exit_code == 0, result.output
        assert "No feedback" in result.output

    def test_resolving_an_unknown_id_hints_the_next_step(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        result = cli_client_both["invoke"](["semantic-model", "feedback", "resolve", "sfb_nope"])
        assert result.exit_code == 1
        assert "feedback list" in result.output
