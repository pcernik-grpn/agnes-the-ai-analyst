"""A semantic source that syncs successfully and imports nothing must not
read as indistinguishable from one that owns models (issue #1707).

``last_sync_status='ok'`` alone answers "did the fetch work", never "did it
bring anything back" — so a `connection` source scoped at a database with no
semantic views looks exactly like a healthy one. The count of models the
source OWNS is what tells the two apart, and it is derived at read time from
the same ``(source, source_ref)`` provenance the importer stamps and the
pruner deletes on (``src/semantic/transports.py`` ``resolve_provenance`` →
``src/semantic/importer.py`` ``repo.upsert(source=…, source_ref=…)``).

Nothing is persisted: ``semantic_sources`` is a frozen pre-A3 pair and the
DuckDB ladder is frozen, so there is no column to add and no count to keep in
sync with the models table.
"""

from __future__ import annotations

import pytest

from src.semantic.ownership import owned_model_counts, with_owned_model_count

DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def system_db(e2e_env):
    """DATA_DIR isolation — same adaptation as tests/test_semantic_transports.py."""
    return e2e_env


def _source(source_id: str, *, kind: str = "connection", adapter: str = "native", config: dict | None = None) -> dict:
    from src.repositories import semantic_source_repo

    return semantic_source_repo().create(
        id=source_id,
        kind=kind,
        name=f"name-{source_id}",
        adapter=adapter,
        config=config or {},
    )


def _model(model_id: str, *, source: str, source_ref: str | None, status: str = "valid") -> None:
    from src.repositories import semantic_model_repo

    semantic_model_repo().upsert(
        id=model_id,
        slug=model_id,
        name=model_id,
        description=None,
        document=DOC,
        document_json={"semantic_model": [{"name": model_id}]},
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{model_id}",
        source=source,
        source_ref=source_ref,
        status=status,
        validation_errors=None,
        validated_at=None,
    )


class TestTheHelper:
    def test_counts_under_the_provenance_the_importer_stamps(self, system_db):
        """Default provenance is ``ossie_<kind>`` / the source's own id."""
        src = _source("ss_a", kind="connection")
        _model("m1", source="ossie_connection", source_ref="ss_a")
        _model("m2", source="ossie_connection", source_ref="ss_a")

        assert owned_model_counts([src]) == {"ss_a": 2}

    def test_a_source_that_imported_nothing_counts_zero(self, system_db):
        src = _source("ss_empty", kind="connection")

        assert owned_model_counts([src]) == {"ss_empty": 0}

    def test_a_sibling_sources_models_are_never_counted(self, system_db):
        """Ownership is per-provenance: a source's count never includes rows
        stamped with another source's ``(source, source_ref)``."""
        a = _source("ss_a", kind="git")
        b = _source("ss_b", kind="git")
        _model("m1", source="ossie_git", source_ref="ss_a")
        _model("m2", source="ossie_git", source_ref="ss_a")
        _model("m3", source="ossie_git", source_ref="ss_b")

        assert owned_model_counts([a, b]) == {"ss_a": 2, "ss_b": 1}

    def test_a_kind_mismatch_is_not_counted(self, system_db):
        """``ossie_git`` and ``ossie_connection`` are different scopes even
        for the same source id — the label is half the key."""
        src = _source("ss_a", kind="connection")
        _model("m1", source="ossie_git", source_ref="ss_a")

        assert owned_model_counts([src]) == {"ss_a": 0}

    def test_a_legacy_provenance_override_is_honored(self, system_db):
        """A migrated source keeps stamping — and therefore keeps owning —
        the legacy label's scope, not ``ossie_connection``/<its id>."""
        src = _source(
            "ss_kbc",
            kind="connection",
            adapter="keboola_metastore",
            config={
                "connection_id": "conn-1",
                "provenance": {"source": "keboola_metastore", "source_ref": "conn-1"},
            },
        )
        _model("m1", source="keboola_metastore", source_ref="conn-1")
        _model("m2", source="ossie_connection", source_ref="ss_kbc")

        assert owned_model_counts([src]) == {"ss_kbc": 1}

    def test_an_unresolvable_provenance_reports_none_not_a_confident_zero(self, system_db):
        """"Cannot say" and "owns nothing" are different statements.

        ``resolve_provenance``'s legacy branch validates the claimed ref
        against LIVE state (the default Keboola connection), so a source that
        still owns rows can start failing to resolve. Rendering that as
        "0 models / imported nothing" would be a wrong number stated
        confidently, so it reports ``None`` — and reading the list must not
        blow up on it either.
        """
        src = _source(
            "ss_bad",
            kind="connection",
            adapter="native",
            config={"provenance": {"source": "someone_elses_scope"}},
        )

        assert owned_model_counts([src]) == {"ss_bad": None}

    def test_an_unresolvable_provenance_is_logged(self, system_db, caplog):
        """Reported as unknown AND findable: a silent null is a mystery."""
        import logging

        src = _source(
            "ss_bad",
            kind="connection",
            adapter="native",
            config={"provenance": {"source": "someone_elses_scope"}},
        )

        with caplog.at_level(logging.WARNING, logger="src.semantic.ownership"):
            owned_model_counts([src])

        assert any("ss_bad" in r.getMessage() for r in caplog.records), caplog.text

    def test_an_unresolvable_provenance_does_not_poison_its_siblings(self, system_db):
        """One bad row must not cost every other row its count."""
        bad = _source(
            "ss_bad",
            kind="connection",
            adapter="native",
            config={"provenance": {"source": "someone_elses_scope"}},
        )
        good = _source("ss_good", kind="git")
        _model("m1", source="ossie_git", source_ref="ss_good")

        assert owned_model_counts([bad, good]) == {"ss_bad": None, "ss_good": 1}

    def test_with_owned_model_count_annotates_without_mutating_the_row(self, system_db):
        src = _source("ss_a", kind="git")
        _model("m1", source="ossie_git", source_ref="ss_a")

        annotated = with_owned_model_count([src])

        assert annotated[0]["owned_model_count"] == 1
        assert "owned_model_count" not in src, "the repo row itself is left alone"


class TestTheListEndpoint:
    def test_every_source_carries_its_owned_model_count(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_full", kind="git")
        _source("ss_empty", kind="git")
        _model("m1", source="ossie_git", source_ref="ss_full")
        _model("m2", source="ossie_git", source_ref="ss_full")

        resp = c.get("/api/admin/semantic-sources", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        by_id = {r["id"]: r for r in resp.json()}
        assert by_id["ss_full"]["owned_model_count"] == 2
        assert by_id["ss_empty"]["owned_model_count"] == 0

    def test_a_source_synced_ok_with_no_models_is_distinguishable(self, seeded_app):
        """The exact finding: two sources, both ``last_sync_status='ok'``, one
        owning nothing. The status field alone cannot tell them apart."""
        from src.repositories import semantic_source_repo

        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_full", kind="connection")
        _source("ss_empty", kind="connection")
        _model("m1", source="ossie_connection", source_ref="ss_full")
        semantic_source_repo().record_sync("ss_full", status="ok", error=None)
        semantic_source_repo().record_sync("ss_empty", status="ok", error=None)

        by_id = {r["id"]: r for r in c.get("/api/admin/semantic-sources", headers=_auth(token)).json()}

        assert by_id["ss_full"]["last_sync_status"] == by_id["ss_empty"]["last_sync_status"] == "ok"
        assert by_id["ss_full"]["owned_model_count"] == 1
        assert by_id["ss_empty"]["owned_model_count"] == 0

    def test_the_field_survives_the_enabled_only_filter(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="git")
        _model("m1", source="ossie_git", source_ref="ss_a")

        resp = c.get("/api/admin/semantic-sources?enabled_only=true", headers=_auth(token))

        assert [r["owned_model_count"] for r in resp.json()] == [1]


class TestTheWriteEndpoints:
    """A caller that renders a POST/PUT response into the same table must not
    have to special-case a field the GET always sends."""

    def test_create_returns_the_field(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]

        resp = c.post(
            "/api/admin/semantic-sources",
            json={"kind": "git", "name": "Fresh", "adapter": "native", "config": {"repo_url": "https://x/y.git"}},
            headers=_auth(token),
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["owned_model_count"] == 0, "nothing has synced yet"

    def test_update_returns_the_field(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="git")
        _model("m1", source="ossie_git", source_ref="ss_a")

        resp = c.put("/api/admin/semantic-sources/ss_a", json={"enabled": False}, headers=_auth(token))

        assert resp.status_code == 200, resp.text
        assert resp.json()["owned_model_count"] == 1

    def test_a_no_op_update_still_returns_the_field(self, seeded_app):
        """The empty-body early return is its own code path."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="git")

        resp = c.put("/api/admin/semantic-sources/ss_a", json={}, headers=_auth(token))

        assert resp.status_code == 200, resp.text
        assert resp.json()["owned_model_count"] == 0


class TestTheSingleSourceEndpoint:
    def test_get_one_source_carries_the_same_field(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="git")
        _model("m1", source="ossie_git", source_ref="ss_a")

        resp = c.get("/api/admin/semantic-sources/ss_a", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        assert resp.json()["owned_model_count"] == 1

    def test_get_one_empty_source_reports_zero_not_a_missing_field(self, seeded_app):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_empty", kind="git")

        body = c.get("/api/admin/semantic-sources/ss_empty", headers=_auth(token)).json()

        assert body["owned_model_count"] == 0

    def test_an_unresolvable_source_reports_null_over_the_wire(self, seeded_app):
        """The null survives JSON — the clients branch on it (page renders
        "—", CLI renders "-"), so it must not be coerced to 0 in transit."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source(
            "ss_bad",
            kind="connection",
            adapter="native",
            config={"provenance": {"source": "someone_elses_scope"}},
        )

        body = c.get("/api/admin/semantic-sources/ss_bad", headers=_auth(token)).json()

        assert body["owned_model_count"] is None
