"""Silencing a health check — repository, endpoints and CLI, on Postgres.

PG-side by necessity, not by preference: ``semantic_health_mutes`` is a
Postgres-only table (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend
discipline"), so Postgres is the only backend on which any of this can run at
all. There is no DuckDB sibling to parametrize against, which is why this file
follows ``tests/db_pg/test_semantic_feedback_pg.py`` /
``test_resource_source_tags_pg.py``'s PG-only shape rather than the
cross-engine contract shape.

The one invariant every test here circles: a mute is a SIGNATURE, never a
disappearance. Who silenced the check, when, and why is stored on the row and
comes back out of every read — an admin who mutes something is on the record
for it, and the next admin can see whose judgement they are inheriting.

The DuckDB side's contract — who is refused, and that everyone else meets a
typed ``501 requires_postgres_backend`` rather than a crash — is pinned in
``tests/test_semantic_health_mutes_endpoint.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_MUTES = "/api/admin/semantic-layer/mutes"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.fixture
def repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.semantic_health_mutes_pg import SemanticHealthMutesPgRepository

    return SemanticHealthMutesPgRepository(pg_engine)


class TestTheScopeGrammar:
    """``scope`` is a string, so the grammar is the only thing standing between
    "muted the metrics domain" and a row that silences nothing forever."""

    def test_it_reads_all_three_forms(self):
        from src.models.semantic_health_mutes import parse_mute_scope

        assert parse_mute_scope("source:conn-a") == ("conn-a", None)
        assert parse_mute_scope("domain:metrics") == (None, "metrics")
        assert parse_mute_scope("source:conn-a:domain:metrics") == ("conn-a", "metrics")

    def test_it_refuses_a_scope_that_names_nothing(self):
        from src.models.semantic_health_mutes import parse_mute_scope

        for bad in ("", "   ", "everything", "source:", ":metrics", "domain:", "source:a:domain:", "sources:a"):
            with pytest.raises(ValueError):
                parse_mute_scope(bad)

    def test_it_refuses_a_scope_long_enough_to_be_a_paste(self):
        from src.models.semantic_health_mutes import parse_mute_scope

        with pytest.raises(ValueError):
            parse_mute_scope("domain:" + "x" * 500)


class TestTheRepository:
    def test_create_returns_the_stored_row_with_the_signature_on_it(self, repo):
        row = repo.create(
            scope="source:conn-a:domain:metrics",
            reason="Tracked in the Q3 modelling epic; no metric until it lands.",
            muted_by="admin@test.com",
        )
        assert row["id"]
        assert row["scope"] == "source:conn-a:domain:metrics"
        assert row["reason"] == "Tracked in the Q3 modelling epic; no metric until it lands."
        # The three columns that make this a signature rather than a silence.
        assert row["muted_by"] == "admin@test.com"
        assert row["muted_at"] is not None
        # No expiry = muted until somebody unmutes it, deliberately: an admin
        # who knows about a gap should not be forced to re-affirm it weekly.
        assert row["expires_at"] is None

    def test_a_reason_is_optional_but_the_author_is_not(self, repo):
        """``reason`` may be empty — an admin muting a check they are visibly
        working on should not be blocked by a text box. ``muted_by`` is the
        part that cannot be empty: an anonymous mute is exactly the silent
        disappearance this feature exists to prevent."""
        row = repo.create(scope="domain:glossary", muted_by="admin@test.com")
        assert row["reason"] is None
        assert row["muted_by"] == "admin@test.com"

    def test_list_active_hides_an_expired_mute(self, repo):
        live = repo.create(scope="domain:metrics", muted_by="a@test.com")
        repo.create(
            scope="domain:glossary",
            muted_by="a@test.com",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        assert [m["id"] for m in repo.list_active()] == [live["id"]]

    def test_list_active_keeps_a_mute_that_has_not_expired_yet(self, repo):
        row = repo.create(
            scope="domain:metrics",
            muted_by="a@test.com",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        assert [m["id"] for m in repo.list_active()] == [row["id"]]

    def test_list_all_still_shows_the_expired_one(self, repo):
        """The silence ends at the expiry; the RECORD of who silenced it does
        not. An admin asking "who muted this last quarter" must still get an
        answer."""
        live = repo.create(scope="domain:metrics", muted_by="a@test.com")
        gone = repo.create(
            scope="domain:glossary",
            muted_by="b@test.com",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        assert {m["id"] for m in repo.list_all()} == {live["id"], gone["id"]}

    def test_find_active_for_scope_is_how_a_duplicate_is_caught(self, repo):
        row = repo.create(scope="domain:metrics", muted_by="a@test.com")

        found = repo.find_active_for_scope("domain:metrics")
        assert found is not None and found["id"] == row["id"]
        assert repo.find_active_for_scope("domain:glossary") is None

    def test_an_expired_mute_does_not_block_re_muting_the_same_scope(self, repo):
        """No UNIQUE on ``scope``, on purpose: an expiry that permanently
        poisoned its own scope would mean a one-week mute costs you the ability
        to ever mute that check again without deleting the record."""
        repo.create(
            scope="domain:metrics",
            muted_by="a@test.com",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert repo.find_active_for_scope("domain:metrics") is None

        again = repo.create(scope="domain:metrics", muted_by="b@test.com")
        assert repo.find_active_for_scope("domain:metrics")["id"] == again["id"]

    def test_delete_removes_it_and_reports_whether_it_did(self, repo):
        row = repo.create(scope="domain:metrics", muted_by="a@test.com")

        assert repo.delete(row["id"]) is True
        assert repo.get(row["id"]) is None
        # False rather than a silent success, so the endpoint can answer 404
        # instead of reporting an unmute that unmuted nothing.
        assert repo.delete(row["id"]) is False

    def test_get_of_an_unknown_id_is_none(self, repo):
        assert repo.get("shm_nope") is None


class TestTheEndpointsOnPostgres:
    def _mute(self, client, admin, **body):
        return client.post(_MUTES, json=body, headers=admin)

    def test_an_admin_can_mute_a_check_and_is_recorded_as_its_author(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
        client = seeded_app_both["client"]

        resp = self._mute(
            client,
            _auth(seeded_app_both["admin_token"]),
            scope="domain:glossary",
            reason="No glossary until the terminology review finishes.",
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["scope"] == "domain:glossary"
        assert body["reason"] == "No glossary until the terminology review finishes."
        # Taken from the authenticated caller, never from the request body —
        # a mute you can attribute to somebody else is not a signature.
        assert body["muted_by"] == "admin@test.com"
        assert body["muted_at"]

    def test_the_author_cannot_be_forged_through_the_body(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = self._mute(
            seeded_app_both["client"],
            _auth(seeded_app_both["admin_token"]),
            scope="domain:metrics",
            muted_by="somebody.else@test.com",
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["muted_by"] == "admin@test.com"

    def test_a_muted_check_is_listed_with_who_when_and_why(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        admin = _auth(seeded_app_both["admin_token"])

        self._mute(client, admin, scope="domain:metrics", reason="deliberate")

        resp = client.get(_MUTES, headers=admin)
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["scope"] == "domain:metrics"
        assert items[0]["reason"] == "deliberate"
        assert items[0]["muted_by"] == "admin@test.com"
        assert items[0]["muted_at"]

    def test_the_list_hides_an_expired_mute_unless_asked(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        admin = _auth(seeded_app_both["admin_token"])

        live = self._mute(client, admin, scope="domain:metrics").json()
        # Straight to the repository: the endpoint refuses an expiry in the
        # past (see below), which is the correct refusal but leaves no way to
        # ARRIVE at an expired row through the API.
        from src.repositories import semantic_health_mutes_repo

        expired = semantic_health_mutes_repo().create(
            scope="domain:glossary",
            muted_by="admin@test.com",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        active = client.get(_MUTES, headers=admin).json()["items"]
        assert [m["id"] for m in active] == [live["id"]]

        everything = client.get(f"{_MUTES}?include_expired=true", headers=admin).json()["items"]
        assert {m["id"] for m in everything} == {live["id"], expired["id"]}

    def test_unmuting_removes_it(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        admin = _auth(seeded_app_both["admin_token"])

        muted = self._mute(client, admin, scope="domain:metrics").json()

        assert client.delete(f"{_MUTES}/{muted['id']}", headers=admin).status_code == 204
        assert client.get(_MUTES, headers=admin).json()["items"] == []

    def test_unmuting_something_that_is_not_muted_is_a_404(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = seeded_app_both["client"].delete(f"{_MUTES}/shm_nope", headers=_auth(seeded_app_both["admin_token"]))
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "unknown_mute"

    def test_a_scope_that_names_nothing_is_refused(self, state_backend, seeded_app_both):
        """A stored ``"everything"`` would sit in the list looking like a mute
        while silencing nothing — the worst of both readings."""
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = self._mute(seeded_app_both["client"], _auth(seeded_app_both["admin_token"]), scope="everything")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_scope"

    def test_muting_a_source_that_does_not_exist_is_a_404(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = self._mute(seeded_app_both["client"], _auth(seeded_app_both["admin_token"]), scope="source:nope")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "unknown_source"

    def test_the_synthetic_local_bucket_can_be_muted(self, state_backend, seeded_app_both):
        """``__local__`` is a real row in the coverage report (registered tables
        with no connection) but not a ``source_connections`` id — refusing it
        would leave the one row an admin most often wants to mute unmutable."""
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = self._mute(seeded_app_both["client"], _auth(seeded_app_both["admin_token"]), scope="source:__local__")
        assert resp.status_code == 201, resp.text

    def test_a_connected_source_can_be_muted(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        from src.repositories import source_connections_repo

        source_connections_repo().create(
            id="conn-a",
            name="Production Project",
            source_type="keboola",
            config={"stack_url": "https://connection.example.com"},
            created_by="test",
        )

        resp = self._mute(
            seeded_app_both["client"],
            _auth(seeded_app_both["admin_token"]),
            scope="source:conn-a:domain:metrics",
        )
        assert resp.status_code == 201, resp.text

    def test_an_expiry_already_in_the_past_is_refused(self, state_backend, seeded_app_both):
        """It would store a mute that silences nothing from the moment it is
        written — indistinguishable, in the list, from one that works."""
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = self._mute(
            seeded_app_both["client"],
            _auth(seeded_app_both["admin_token"]),
            scope="domain:metrics",
            expires_at=_iso(timedelta(hours=-1)),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "expires_in_past"

    def test_an_expiry_in_the_future_is_stored(self, state_backend, seeded_app_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        resp = self._mute(
            seeded_app_both["client"],
            _auth(seeded_app_both["admin_token"]),
            scope="domain:metrics",
            expires_at=_iso(timedelta(days=7)),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["expires_at"] is not None

    def test_muting_the_same_scope_twice_is_a_409(self, state_backend, seeded_app_both):
        """ "Already muted" is information the admin asked for. Two rows for one
        scope would make the list read as two independent judgements when it is
        one, and unmuting would then only half-work."""
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        admin = _auth(seeded_app_both["admin_token"])

        first = self._mute(client, admin, scope="domain:metrics", reason="first").json()

        again = self._mute(client, admin, scope="domain:metrics", reason="second")
        assert again.status_code == 409
        body = again.json()["detail"]
        assert body["error"] == "already_muted"
        # Names the row that is in the way, so the caller can read or delete it
        # without a second round-trip to find it.
        assert body["mute_id"] == first["id"]

    def test_the_routes_refuse_a_non_admin_on_postgres_too(self, state_backend, seeded_app_both):
        """The 403s in `tests/test_semantic_health_mutes_endpoint.py` are
        asserted on DuckDB, where a PG-only route could conceivably 501 before
        the gate. Re-assert them on the backend that can actually serve."""
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        client = seeded_app_both["client"]
        analyst = _auth(seeded_app_both["analyst_token"])

        assert client.get(_MUTES, headers=analyst).status_code == 403
        assert client.post(_MUTES, json={"scope": "domain:metrics"}, headers=analyst).status_code == 403
        assert client.delete(f"{_MUTES}/shm_x", headers=analyst).status_code == 403


class TestTheCli:
    """`agnes admin semantic mute|unmute|mutes` — the same three verbs the API
    and the MCP tools carry, so an admin working from a terminal is not the one
    surface that has to open a browser to silence a known gap.

    Under `admin` since #1707 Block 6: every mute endpoint is `require_admin`,
    which the old `agnes semantic-model …` home did not say. The old spelling
    still runs as a hidden deprecated alias (covered in
    `tests/test_cli_semantic_consolidation.py`)."""

    def test_mute_then_list_shows_who_and_why(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        invoke = cli_client_both["invoke"]

        muted = invoke(["admin", "semantic", "mute", "domain:glossary", "--reason", "terminology review in flight"])
        assert muted.exit_code == 0, muted.output
        assert "Muted" in muted.output

        listed = invoke(["admin", "semantic", "mutes"])
        assert listed.exit_code == 0, listed.output
        assert "domain:glossary" in listed.output
        assert "admin@test.com" in listed.output
        assert "terminology review in flight" in listed.output

    def test_mutes_json_carries_the_rows(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        invoke = cli_client_both["invoke"]

        invoke(["admin", "semantic", "mute", "domain:metrics"])
        listed = invoke(["admin", "semantic", "mutes", "--json"])
        assert listed.exit_code == 0, listed.output
        items = json.loads(listed.output)["items"]
        assert [m["scope"] for m in items] == ["domain:metrics"]

    def test_unmute_removes_it(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        invoke = cli_client_both["invoke"]

        invoke(["admin", "semantic", "mute", "domain:metrics"])
        mute_id = json.loads(invoke(["admin", "semantic", "mutes", "--json"]).output)["items"][0]["id"]

        unmuted = invoke(["admin", "semantic", "unmute", mute_id])
        assert unmuted.exit_code == 0, unmuted.output

        after = json.loads(invoke(["admin", "semantic", "mutes", "--json"]).output)
        assert after["items"] == []

    def test_an_empty_list_says_so_instead_of_printing_nothing(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        result = cli_client_both["invoke"](["admin", "semantic", "mutes"])
        assert result.exit_code == 0, result.output
        assert "No muted checks" in result.output

    def test_an_expiry_is_accepted_as_iso_8601(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        invoke = cli_client_both["invoke"]

        result = invoke(["admin", "semantic", "mute", "domain:metrics", "--expires", _iso(timedelta(days=3))])
        assert result.exit_code == 0, result.output

        items = json.loads(invoke(["admin", "semantic", "mutes", "--json"]).output)["items"]
        assert items[0]["expires_at"] is not None

    def test_a_malformed_scope_names_the_three_forms(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        result = cli_client_both["invoke"](["admin", "semantic", "mute", "everything"])
        assert result.exit_code == 1
        assert "source:" in result.output and "domain:" in result.output

    def test_unmuting_an_unknown_id_hints_the_next_step(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")

        result = cli_client_both["invoke"](["admin", "semantic", "unmute", "shm_nope"])
        assert result.exit_code == 1
        assert "agnes admin semantic mutes" in result.output

    def test_muting_the_same_scope_twice_names_the_existing_mute(self, state_backend, cli_client_both):
        if state_backend != "pg":
            pytest.skip("PG-only feature")
        invoke = cli_client_both["invoke"]

        invoke(["admin", "semantic", "mute", "domain:metrics", "--reason", "first"])
        again = invoke(["admin", "semantic", "mute", "domain:metrics", "--reason", "second"])
        assert again.exit_code == 1
        assert "Already muted" in again.output
