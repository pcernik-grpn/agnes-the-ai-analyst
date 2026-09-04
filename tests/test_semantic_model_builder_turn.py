"""What one semantic-model builder turn is allowed to do.

The fifth builder-turn endpoint. Its siblings are ``tests/test_agent_builder_
turns.py``, ``tests/test_entity_builder_turns.py`` and
``tests/test_package_builder_turns.py``, and most of the contract is the
same: a message in, a sanitized patch out, model output treated as untrusted
input.

What is different here, and what these tests exist to pin:

  - grounding reaches past ids into real DATA — a dataset's ``source`` must
    resolve to a table this CALLER can read (RBAC-filtered, unlike
    ``package_builder`` which is admin-only), and
  - the patch nests lists of objects (datasets, metrics) rather than flat id
    lists, which means "progress" has to be computed against the
    ACCUMULATED draft, never against one turn's delta alone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.semantic_model_builder import (
    PATCHABLE,
    _keyed_upsert,
    _merged_draft_for_progress,
    _sanitize_dataset,
    _sanitize_patch,
    _table_candidates,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_table(name: str = "t_smb_test") -> str:
    """Insert a row into ``table_registry`` and return its id — same helper
    shape ``tests/test_api_data_packages.py`` uses, so grounding tests do not
    depend on a real connector pipeline."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    repo = TableRegistryRepository(conn)
    table_id = f"tbl_{name}"
    repo.register(
        id=table_id,
        name=name,
        source_type="keboola",
        source_table=f"in.c-test.{name}",
        bucket="in.c-test",
        query_mode="local",
    )
    conn.close()
    return table_id


@pytest.fixture
def client(seeded_app):
    return seeded_app["client"], seeded_app["admin_token"], seeded_app["analyst_token"]


def _turn(client, token=None, **kw):
    c, admin, _ = client
    body = {"message": "a model over our orders table"}
    body.update(kw)
    return c.post("/api/semantic-models/builder/turn", json=body, headers=_auth(token or admin))


class TestItProposesAndNeverWrites:
    def test_a_turn_answers_with_a_patch(self, client):
        r = _turn(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"]
        assert isinstance(body["patch"], dict)

    def test_it_creates_no_model(self, client):
        c, admin, _ = client
        before = c.get("/api/admin/semantic-models", headers=_auth(admin)).json()
        _turn(client)
        after = c.get("/api/admin/semantic-models", headers=_auth(admin)).json()
        assert before == after, "a turn changed the stored model list"

    def test_an_empty_message_mid_conversation_is_refused(self, client):
        r = _turn(client, message="   ", history=[{"role": "user", "text": "hi"}])
        assert r.status_code == 400
        assert r.json()["detail"]["kind"] == "empty_message"

    def test_an_empty_first_message_opens_the_conversation(self, client):
        r = _turn(client, message="", history=[])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"]
        assert body["engine"] in ("stub", "model")
        assert body["slots"], "an opening turn must report what is still open"
        assert not all(s["known"] for s in body["slots"])

    def test_metrics_is_not_a_slot(self):
        """Same reasoning package_builder gives for omitting `groups`: a slot
        for it could never be settled, and the panel's own copy already says
        datasets alone are a useful model."""
        from app.api.semantic_model_builder import _SLOTS

        assert "metrics" not in {s.key for s in _SLOTS}
        assert "metrics" in PATCHABLE


class TestAnyoneSignedInMayDraft:
    """Drafting is not admin-gated, even though `apply` (Save) treats the two
    roles differently — matching the apply endpoint's own asymmetry."""

    def test_a_non_admin_can_run_a_turn(self, client):
        c, _, analyst = client
        r = _turn(client, token=analyst)
        assert r.status_code == 200, r.text

    def test_an_anonymous_caller_is_refused(self, shared_app):
        r = TestClient(shared_app).post("/api/semantic-models/builder/turn", json={"message": "hi"})
        assert r.status_code in (401, 403)


class TestSanitizerIsTheTrustBoundary:
    def test_a_source_not_in_candidates_drops_the_whole_dataset(self):
        out = _sanitize_dataset({"name": "orders", "source": "made_up_table"}, table_ids={"tbl_real"})
        assert out is None

    def test_a_source_in_candidates_survives(self):
        out = _sanitize_dataset({"name": "orders", "source": "tbl_real"}, table_ids={"tbl_real"})
        assert out == {"name": "orders", "source": "tbl_real"}

    def test_no_source_yet_is_fine(self):
        """A dataset can be named before it is bound to a table."""
        out = _sanitize_dataset({"name": "orders"}, table_ids={"tbl_real"})
        assert out == {"name": "orders"}

    def test_grounding_unavailable_does_not_block_authoring(self):
        """An empty candidate set (nothing registered, or nothing this caller
        can read) degrades grounding to 'unavailable' rather than refusing a
        source outright — same posture package_builder documents for its own
        best-effort metrics read."""
        out = _sanitize_dataset({"name": "orders", "source": "anything"}, table_ids=set())
        assert out == {"name": "orders", "source": "anything"}

    def test_an_unnamed_dataset_is_dropped(self):
        assert _sanitize_dataset({"source": "tbl_real"}, table_ids={"tbl_real"}) is None
        assert _sanitize_dataset({"name": "  "}, table_ids=set()) is None

    def test_fields_are_sanitized_too(self):
        out = _sanitize_dataset(
            {
                "name": "orders",
                "fields": [
                    {"name": "order_id", "is_primary_key": True},
                    {"name": ""},  # dropped: no name
                    {"datatype": "text"},  # dropped: no name
                    "not-a-dict",  # dropped
                ],
            },
            table_ids=set(),
        )
        assert out["fields"] == [{"name": "order_id", "is_primary_key": True}]

    def test_unknown_dataset_keys_are_dropped(self):
        out = _sanitize_dataset({"name": "orders", "owner": "someone", "sql": "DROP TABLE x"}, table_ids=set())
        assert out == {"name": "orders"}

    def test_a_fabricated_top_level_key_is_dropped(self):
        assert _sanitize_patch({"name": "ok", "apply": True}, table_ids=set()) == {"name": "ok"}

    def test_a_non_dict_patch_is_survivable(self):
        for raw in (None, [], "nope", 3):
            assert _sanitize_patch(raw, table_ids=set()) == {}

    def test_a_dataset_list_with_only_bad_entries_is_omitted_entirely(self):
        """`datasets` should not appear at all rather than as `[]` — the
        client-side merge treats a present key as 'something changed'."""
        out = _sanitize_patch({"datasets": [{"name": "x", "source": "bogus"}]}, table_ids={"tbl_real"})
        assert "datasets" not in out

    def test_metric_dialect_defaults_survive_untouched_when_absent(self):
        from app.api.semantic_model_builder import _sanitize_metric

        out = _sanitize_metric({"name": "revenue", "expression": "SUM(amount)"})
        assert out == {"name": "revenue", "expression": "SUM(amount)"}
        assert "dialect" not in out


class TestProgressReflectsTheAccumulatedDraftNotTheDelta:
    """The one genuinely new mechanism here: builder_core.merged_draft does a
    flat dict.update, which would silently REPLACE the dataset list with just
    this turn's delta. `_merged_draft_for_progress` must not do that."""

    def test_keyed_upsert_adds_a_new_entry_by_name(self):
        existing = [{"name": "orders", "source": "tbl_orders"}]
        merged = _keyed_upsert(existing, [{"name": "customers", "source": "tbl_customers"}])
        assert [d["name"] for d in merged] == ["orders", "customers"]

    def test_keyed_upsert_merges_into_an_existing_entry_by_name(self):
        existing = [{"name": "orders", "source": "tbl_orders", "description": "old"}]
        merged = _keyed_upsert(existing, [{"name": "orders", "description": "new"}])
        assert merged == [{"name": "orders", "source": "tbl_orders", "description": "new"}]

    def test_a_second_turns_delta_still_reports_the_first_datasets_progress(self):
        draft = {"datasets": [{"name": "orders", "source": "tbl_orders", "fields": [{"name": "id"}]}]}
        # This turn's patch only ADDS a second dataset — it must not make the
        # first one disappear from what "known" is computed against.
        patch = {"datasets": [{"name": "customers", "source": "tbl_customers"}]}
        merged = _merged_draft_for_progress(draft, patch)
        assert {d["name"] for d in merged["datasets"]} == {"orders", "customers"}

    def test_scalar_fields_still_flat_overwrite(self):
        draft = {"name": "old_name"}
        merged = _merged_draft_for_progress(draft, {"name": "new_name"})
        assert merged["name"] == "new_name"

    def test_an_absent_patch_key_leaves_the_draft_untouched(self):
        draft = {"datasets": [{"name": "orders"}], "metrics": [{"name": "revenue"}]}
        merged = _merged_draft_for_progress(draft, {"name": "x"})
        assert merged["datasets"] == draft["datasets"]
        assert merged["metrics"] == draft["metrics"]


class TestGroundingIsRbacFiltered:
    def test_a_table_the_caller_cannot_read_is_never_offered(self, monkeypatch):
        table_id = _register_table("hidden_from_analyst")
        monkeypatch.setattr(
            "src.rbac.get_accessible_tables",
            lambda user, conn: [] if user.get("email") == "analyst@example.com" else None,
        )
        from src.db import get_system_db

        conn = get_system_db()
        try:
            candidates = _table_candidates({"email": "analyst@example.com"}, conn)
            assert table_id not in {t["id"] for t in candidates}
            admin_candidates = _table_candidates({"email": "admin@example.com"}, conn)
            assert table_id in {t["id"] for t in admin_candidates}
        finally:
            conn.close()

    def test_a_broken_metric_repo_still_answers(self, client, monkeypatch):
        """Grounding is an accelerator, never a precondition."""

        def boom():
            raise RuntimeError("metric_definitions does not exist")

        monkeypatch.setattr("src.repositories.metric_repo", boom)
        r = _turn(client)
        assert r.status_code == 200, r.text


class TestDegradingWithoutAModel:
    def test_no_credential_answers_503_with_an_actionable_hint(self, client, monkeypatch):
        monkeypatch.setattr("app.api.semantic_model_builder.stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.semantic_model_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("no credential")),
        )
        r = _turn(client)
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert detail["kind"] == "builder_llm_unavailable"
        assert "by hand" in detail["hint"]

    def test_a_provider_error_is_not_a_500(self, client, monkeypatch):
        monkeypatch.setattr("app.api.semantic_model_builder.stub_enabled", lambda: False)
        monkeypatch.setattr(
            "app.api.semantic_model_builder._llm_turn",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert _turn(client).status_code == 502


class TestTheStubNeverInventsAnUngroundedSource:
    def test_the_stub_only_proposes_a_dataset_from_real_candidates(self, client):
        table_id = _register_table("orders_stub_test")
        r = _turn(client, message="a model over orders_stub_test")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["engine"] != "stub":
            pytest.skip("this instance is not running the scripted stub")
        datasets = body["patch"].get("datasets") or []
        for ds in datasets:
            assert ds.get("source") in (None, table_id) or ds.get("source") == table_id
