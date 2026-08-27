"""What one data-package builder turn is allowed to do.

The third builder-turn endpoint. Its siblings are
``tests/test_agent_builder_turns.py`` and
``tests/test_entity_builder_turns.py``, and most of the contract is the same:
a message in, a sanitized patch out, model output treated as untrusted input.

What is different is the stake. A data package is the unit governed data
reaches analysts through, and creating one writes GRANTS — "share it with the
sales team" is a sentence that widens who can see tables. Two rules follow,
and both are tested here rather than left to review:

  - the endpoint only ever PROPOSES (there is no `apply` flag, not even one
    defaulting to false), and
  - the candidate lists come from the SERVER, so a caller cannot enlarge the
    set of groups a turn may propose.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.package_builder import PATCHABLE, _sanitize_patch


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(seeded_app):
    """Admin is Admin-GROUP membership, not a flag — `seeded_app` already
    seeds the four role users and their tokens, so use it rather than
    hand-rolling a half-correct admin."""
    return seeded_app["client"], seeded_app["admin_token"], seeded_app["analyst_token"]


def _turn(client, token=None, **kw):
    c, admin, _ = client
    body = {"message": "our sales pipeline tables"}
    body.update(kw)
    return c.post("/api/admin/data-packages/builder/turn", json=body, headers=_auth(token or admin))


class TestItProposesAndNeverWrites:
    def test_a_turn_answers_with_a_patch(self, client):
        r = _turn(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"]
        assert isinstance(body["patch"], dict)

    def test_there_is_no_apply_flag_to_find(self, client):
        """Not a default that could be flipped — the parameter does not exist.
        An admin should never learn what a conversation granted by reading it
        back afterwards."""
        from app.api.package_builder import PackageTurnRequest

        assert "apply" not in PackageTurnRequest.model_fields

    def test_it_creates_no_package(self, client):
        c, admin, _ = client
        before = c.get("/api/admin/data-packages", headers=_auth(admin)).json()
        _turn(client)
        after = c.get("/api/admin/data-packages", headers=_auth(admin)).json()
        assert before == after, "a turn changed the package list"

    def test_an_empty_message_is_refused(self, client):
        r = _turn(client, message="   ")
        assert r.status_code == 400
        assert r.json()["detail"]["kind"] == "empty_message"

    def test_the_scripted_engine_never_proposes_a_group(self, client):
        """The stub must not be the thing that teaches this flow to hand out
        access — it runs on every dev machine and in every test."""
        body = _turn(client).json()
        assert not body["patch"].get("groups")


class TestOnlyAnAdminMayRunOne:
    def test_an_analyst_is_refused(self, client):
        c, _, analyst = client
        r = c.post(
            "/api/admin/data-packages/builder/turn",
            json={"message": "give me everything"},
            headers=_auth(analyst),
        )
        assert r.status_code in (401, 403)

    def test_an_anonymous_caller_is_refused(self, shared_app):
        r = TestClient(shared_app).post("/api/admin/data-packages/builder/turn", json={"message": "hi"})
        assert r.status_code in (401, 403)


class TestSanitizerIsTheTrustBoundary:
    IDS = {"table_ids": {"t1", "t2"}, "group_ids": {"g-sales"}}

    def test_a_fabricated_group_never_survives(self):
        """The one thing here that must never reach a grant."""
        out = _sanitize_patch({"groups": ["g-sales", "g-everyone", "admin"]}, **self.IDS)
        assert out["groups"] == ["g-sales"]

    def test_a_table_the_instance_does_not_have_is_dropped(self):
        out = _sanitize_patch({"tables": ["t1", "t99"]}, **self.IDS)
        assert out["tables"] == ["t1"]

    def test_ids_are_deduped_in_the_order_proposed(self):
        out = _sanitize_patch({"tables": ["t2", "t1", "t2"]}, **self.IDS)
        assert out["tables"] == ["t2", "t1"]

    def test_a_non_list_is_not_coerced(self):
        """A bare string is not a one-element grant list."""
        assert _sanitize_patch({"groups": "g-sales"}, **self.IDS) == {}

    def test_non_string_members_are_dropped(self):
        out = _sanitize_patch({"groups": [{"id": "g-sales"}, None, 7, "g-sales"]}, **self.IDS)
        assert out["groups"] == ["g-sales"]

    def test_unknown_fields_are_dropped(self):
        out = _sanitize_patch({"name": "ok", "visibility": "public", "owner": "someone"}, **self.IDS)
        assert out == {"name": "ok"}

    def test_slug_is_not_patchable(self):
        """It is derived from the name and the drawer keeps them in step; a
        model writing one directly could only desynchronise them."""
        assert "slug" not in PATCHABLE
        assert _sanitize_patch({"slug": "hijacked"}, **self.IDS) == {}

    def test_a_non_dict_patch_is_survivable(self):
        for raw in (None, [], "nope", 3):
            assert _sanitize_patch(raw, **self.IDS) == {}


class TestDegradingWithoutAModel:
    def test_no_credential_answers_503_with_an_actionable_hint(self, client, monkeypatch):
        monkeypatch.setattr("app.api.package_builder._stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.package_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("no credential")),
        )
        r = _turn(client)
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert detail["kind"] == "builder_llm_unavailable"
        assert "by hand" in detail["hint"]

    def test_a_provider_error_is_not_a_500(self, client, monkeypatch):
        monkeypatch.setattr("app.api.package_builder._stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.package_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert _turn(client).status_code == 502


class TestWhatTheModelIsToldAboutTheInstance:
    """The candidate block is the builder's only knowledge of Agnes.

    A turn is one LLM call with no tool loop, so whatever is NOT in this
    block does not exist as far as the model is concerned. It used to be
    ``id``, ``name`` and 160 characters of description, which left "the
    opportunity tables for sales" answerable only by matching names.

    These pin the facts that were added and, just as importantly, that the
    block is still assembled server-side and still bounded.
    """

    def _prompt_for(self, tables, groups=()):
        from app.api.package_builder import _prompt

        return _prompt(message="sales pipeline", history=[], draft={}, tables=list(tables), groups=list(groups))

    def test_source_and_query_mode_reach_the_prompt(self):
        text = self._prompt_for(
            [
                {
                    "id": "t1",
                    "name": "opportunities",
                    "description": "",
                    "source_type": "keboola",
                    "query_mode": "local",
                    "distributable": True,
                    "metrics": [],
                }
            ]
        )
        assert "source=keboola" in text
        assert "mode=local" in text

    def test_metrics_name_what_a_table_is_for(self):
        """The signal that survives a missing description."""
        text = self._prompt_for(
            [
                {
                    "id": "t1",
                    "name": "fct_deals",
                    "description": "",
                    "source_type": "keboola",
                    "query_mode": "local",
                    "distributable": True,
                    "metrics": ["revenue/mrr", "sales/win_rate"],
                }
            ]
        )
        assert "metrics=revenue/mrr,sales/win_rate" in text

    def test_a_server_only_table_is_flagged_not_forbidden(self):
        """`data_packages.py` does not refuse a remote row, so the prompt must
        not claim it is disallowed — only that analysts get no local copy."""
        text = self._prompt_for(
            [
                {
                    "id": "t1",
                    "name": "web_sessions",
                    "description": "",
                    "source_type": "bigquery",
                    "query_mode": "remote",
                    "distributable": False,
                    "metrics": [],
                }
            ]
        )
        assert "NOT-synced-to-laptops" in text
        assert "packaging it is allowed" in text

    def test_a_bare_candidate_still_renders(self):
        """Enrichment is additive: a dict carrying only the three original
        keys must not raise — the stub path and older callers build these."""
        text = self._prompt_for([{"id": "t1", "name": "orders", "description": "all orders"}])
        assert "id=t1" in text
        assert "all orders" in text

    def test_a_truncated_metric_list_says_so(self):
        """A silently cut list reads to the model as "these are all of them" —
        the same failure MAX_CANDIDATES announces its way out of."""
        from app.api.package_builder import _MAX_METRICS_PER_TABLE

        shown = [f"m{i}" for i in range(_MAX_METRICS_PER_TABLE)]
        text = self._prompt_for(
            [
                {
                    "id": "t1",
                    "name": "fct_deals",
                    "description": "",
                    "metrics": shown,
                    "metrics_total": _MAX_METRICS_PER_TABLE + 4,
                }
            ]
        )
        assert "(+4 more)" in text

    def test_an_untruncated_metric_list_claims_no_remainder(self):
        text = self._prompt_for(
            [{"id": "t1", "name": "fct_deals", "description": "", "metrics": ["revenue"], "metrics_total": 1}]
        )
        assert "metrics=revenue" in text
        assert "more)" not in text

    def test_no_tables_means_no_advice_about_reading_them(self):
        """On an empty instance the block would otherwise explain how to read
        a list that is not there."""
        text = self._prompt_for([])
        assert "Read those facts" not in text

    def test_the_block_stays_bounded(self):
        from app.api.package_builder import MAX_CANDIDATES

        many = [
            {"id": f"t{i}", "name": f"table_{i}", "description": "", "metrics": []} for i in range(MAX_CANDIDATES + 25)
        ]
        text = self._prompt_for(many)
        assert "…and 25 more not listed" in text
        assert f"id=t{MAX_CANDIDATES + 5}" not in text


class TestGroundingIsBestEffort:
    """A failed metrics read must cost grounding, never the turn."""

    def test_a_broken_metric_repo_still_answers(self, client, monkeypatch):
        def boom():
            raise RuntimeError("metric_definitions does not exist")

        monkeypatch.setattr("src.repositories.metric_repo", boom)
        r = _turn(client)
        assert r.status_code == 200, r.text

    def test_candidates_still_come_from_the_server(self, client):
        """The invariant the whole module exists for: enrichment did not turn
        the table list into something a caller can influence."""
        from app.api.package_builder import _candidates

        pool = _candidates()
        assert set(pool) == {"tables", "groups"}
        for row in pool["tables"]:
            assert {"id", "name", "description", "query_mode", "distributable"} <= set(row)
