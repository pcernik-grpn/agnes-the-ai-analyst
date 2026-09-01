"""The internal usage tables must describe themselves to an LLM.

Both the chat agent and the local Claude Code workspace discover these
tables through ``agnes catalog`` / ``/api/v2/schema`` and the data-package
guidance fields — whatever is written there is the ONLY documentation an
agent gets before writing SQL. Three past defects this file pins:

* the table descriptions claimed "Also available locally for analysis"
  (no local path ever existed — the tables are server-side only),
* every column came back with ``description: ""``, so an agent had to
  guess what ``active_seconds`` vs ``wall_seconds`` or
  ``cache_creation_tokens`` mean,
* the seeded ``agnes-usage`` package carried no ``when_to_use`` /
  ``example_questions`` guidance at all.
"""

from __future__ import annotations

from connectors.internal.access import INTERNAL_TABLES, INTERNAL_TABLES_BY_ID


class TestTableDescriptions:
    def test_no_description_claims_local_availability(self):
        for t in INTERNAL_TABLES:
            assert "locally" not in t.description.lower(), (
                f"{t.registry_id}: internal tables are server-side only; "
                "the description must not advertise a local path"
            )

    def test_every_table_says_own_rows_scoping(self):
        # The single most important fact for an agent composing SQL: the
        # result set is already filtered to the caller.
        for t in INTERNAL_TABLES:
            assert "your own" in t.description.lower() or "own rows" in t.description.lower(), (
                f"{t.registry_id}: description must state the own-rows scoping"
            )


class TestColumnDescriptions:
    def test_every_declared_table_documents_its_token_columns(self):
        for rid in ("agnes_sessions", "agnes_turns"):
            cols = INTERNAL_TABLES_BY_ID[rid].column_descriptions
            for c in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            ):
                assert cols.get(c), f"{rid}.{c} has no description"

    def test_ambiguous_session_columns_are_disambiguated(self):
        cols = INTERNAL_TABLES_BY_ID["agnes_sessions"].column_descriptions
        assert cols.get("active_seconds") and cols.get("wall_seconds")
        assert cols["active_seconds"] != cols["wall_seconds"]

    def test_schema_endpoint_serves_the_descriptions(self, seeded_app, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from connectors.internal.registry import ensure_internal_tables_registered

        ensure_internal_tables_registered()
        client = seeded_app["client"]
        headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
        r = client.get("/api/v2/schema/agnes_sessions", headers=headers)
        assert r.status_code == 200
        by_name = {c["name"]: c for c in r.json()["columns"]}
        assert by_name["input_tokens"]["description"], "schema endpoint dropped the column description"


class TestPackageGuidance:
    def test_seeded_package_carries_llm_guidance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from connectors.internal.registry import (
            USAGE_PACKAGE_SLUG,
            ensure_internal_package_seeded,
            ensure_internal_tables_registered,
        )
        from src.repositories import data_packages_repo

        fresh = ensure_internal_tables_registered()
        ensure_internal_package_seeded(newly_registered=fresh)
        pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
        assert pkg is not None
        assert pkg.get("when_to_use"), "when_to_use guidance missing"
        assert pkg.get("when_not_to_use"), "when_not_to_use guidance missing"
        assert pkg.get("example_questions"), "example_questions missing"
        assert pkg.get("long_description"), "long_description missing"
        # The guidance must state the load-bearing fact: server-side only,
        # and any `agnes pull` mention must be a negation, never a promise.
        joined = " ".join(
            [pkg.get("long_description") or ""]
            + list(pkg.get("when_to_use") or [])
            + list(pkg.get("when_not_to_use") or [])
        ).lower()
        assert "server-side" in joined
        for sentence in joined.split("."):
            if "agnes pull" in sentence:
                assert "never" in sentence or "not" in sentence or "only" in sentence, (
                    f"guidance promises agnes pull without negation: {sentence!r}"
                )
