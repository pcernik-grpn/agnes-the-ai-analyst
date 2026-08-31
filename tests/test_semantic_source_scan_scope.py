"""A semantic source must say WHAT it scanned, not only that the scan worked
(finding A17 on #1707).

Proven live: a Snowflake semantic view existed, the role Agnes connects as had
no privilege on it, ``SHOW SEMANTIC VIEWS`` came back empty, and the source
reported ``last_sync_status='ok'`` with zero owned models — identical to a
correctly-scoped source pointed at an upstream that genuinely holds nothing.
The owned-model count (#1821) separates "imported nothing" from "imported
something"; it cannot separate "nothing is there" from "I cannot see it". The
scope string can, because it names the database, schema and ROLE whose grants
the admin then checks.

Derived from the source's own config at READ time (``src/semantic/
scan_scope.py``) — the scope does not change between runs, so nothing is
persisted and no column is added to ``semantic_sources`` (a frozen pre-A3
pair).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.semantic.scan_scope import SCAN_SCOPE_FIELD, scan_scope, scan_scopes, with_scan_scope

DOC = (
    "version: '0.2.0.dev0'\n"
    "semantic_model:\n"
    "  - name: retail\n"
    "    datasets:\n"
    "      - name: orders\n"
    "        source: db.public.orders\n"
)

_SNOWFLAKE_SETTINGS = "connectors.snowflake.settings.resolve_snowflake_settings"
_DATABRICKS_SETTINGS = "connectors.databricks.semantic_layer.resolve_databricks_settings"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def system_db(e2e_env):
    """DATA_DIR isolation — same adaptation as tests/test_semantic_transports.py."""
    return e2e_env


def _row(kind: str, *, adapter: str = "native", config: dict | None = None) -> dict:
    """A semantic-source-shaped dict, without touching the database."""
    return {"id": "ss_x", "kind": kind, "adapter": adapter, "config": config or {}}


def _source(source_id: str, *, kind: str = "connection", adapter: str = "native", config: dict | None = None) -> dict:
    from src.repositories import semantic_source_repo

    return semantic_source_repo().create(
        id=source_id,
        kind=kind,
        name=f"name-{source_id}",
        adapter=adapter,
        config=config or {},
    )


def _connection(connection_id: str, *, source_type: str = "keboola", config: dict | None = None) -> None:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=connection_id,
        name=f"conn-{connection_id}",
        source_type=source_type,
        config=config or {},
    )


class TestTheGitTransport:
    """A git source is scoped by the TRANSPORT, not the adapter — the adapter
    only ever sees documents the clone already produced."""

    def test_repository_and_ref(self):
        scope = scan_scope(_row("git", config={"repo_url": "https://example.com/acme/semantics.git", "ref": "main"}))

        assert scope == "https://example.com/acme/semantics.git @ main matching '**/*.yaml'"

    def test_no_ref_says_default_branch_rather_than_nothing(self):
        scope = scan_scope(_row("git", config={"repo_url": "https://example.com/acme/semantics.git"}))

        assert scope == "https://example.com/acme/semantics.git @ default branch matching '**/*.yaml'"

    def test_a_credential_embedded_in_the_url_is_never_rendered(self):
        """A repo URL that still carries a PAT from before the marketplace
        fix must not reach an admin page through this field."""
        scope = scan_scope(
            _row("git", config={"repo_url": "https://someone:ghp_secret@example.com/acme/semantics.git"})
        )

        assert "ghp_secret" not in scope
        assert "someone" not in scope
        assert scope == "https://example.com/acme/semantics.git @ default branch matching '**/*.yaml'"

    def test_a_token_env_name_is_not_echoed_either(self):
        scope = scan_scope(
            _row("git", config={"repo_url": "https://example.com/a.git", "token_env": "SEMANTIC_REPO_TOKEN"})
        )

        assert "SEMANTIC_REPO_TOKEN" not in scope

    def test_no_repo_url_is_unknown_not_a_made_up_scope(self):
        assert scan_scope(_row("git", config={})) is None

    def test_an_explicit_glob_is_part_of_the_scope(self):
        """The clone is only half the scan: `_documents_from_clone` reads the
        files the glob matches, so a glob is as much "what was looked at" as
        the ref is."""
        scope = scan_scope(
            _row(
                "git",
                config={
                    "repo_url": "https://example.com/acme/semantics.git",
                    "ref": "main",
                    "glob": "models/*.yaml",
                },
            )
        )

        assert scope == "https://example.com/acme/semantics.git @ main matching 'models/*.yaml'"

    def test_the_default_glob_is_stated_too_because_it_is_the_one_that_surprises(self):
        """A repository of `*.yml` documents against the default `**/*.yaml`
        clones fine, matches nothing and syncs `ok` with zero models — the
        git-shaped A17. Hiding the pattern exactly when nobody chose it would
        hide the likeliest cause."""
        scope = scan_scope(_row("git", config={"repo_url": "https://example.com/acme/semantics.git"}))

        assert "matching '**/*.yaml'" in scope

    def test_the_default_it_states_is_the_one_the_fetch_uses(self):
        """Sourced from the transport, not restated — the two cannot drift
        into advertising different defaults."""
        from src.semantic.transports import _DEFAULT_GLOB

        scope = scan_scope(_row("git", config={"repo_url": "https://example.com/acme/semantics.git"}))

        assert f"matching '{_DEFAULT_GLOB}'" in scope


class TestUrlsAreRenderedWithoutCredentials:
    """`_without_credentials` is structural, not token-dependent: the value
    most in need of sanitising is a repo URL carrying a PAT from before the
    marketplace fix, and the token to redact against may already be rotated."""

    @staticmethod
    def _git(repo_url: str) -> str:
        return scan_scope(_row("git", config={"repo_url": repo_url}))

    def test_scheme_less_userinfo_is_stripped(self):
        """`urlparse` finds no hostname here, which is why the marketplace
        helper could not be reused as-is."""
        assert self._git("someone:ghp_secret@example.com/acme/a.git").startswith("example.com/acme/a.git @")

    def test_userinfo_containing_an_unencoded_slash_is_stripped(self):
        """A base64 token contains "/", which pushes the "@" past the first
        path separator — the case a naive authority split misses entirely."""
        scope = self._git("https://someone:pa/ss+tok@example.com/acme/a.git")

        assert "pa/ss+tok" not in scope
        assert scope.startswith("https://example.com/acme/a.git @")

    def test_a_userinfo_containing_an_at_sign_is_stripped_whole(self):
        scope = self._git("https://someone@corp:tok@example.com/acme/a.git")

        assert "someone@corp" not in scope
        assert "tok" not in scope

    def test_a_path_that_merely_contains_an_at_sign_keeps_its_host(self):
        """An npm-style `/@scope/` path is not a credential; mangling it into
        a different host would be a wrong claim of its own."""
        assert self._git("https://example.com/@acme/semantics.git").startswith(
            "https://example.com/@acme/semantics.git @"
        )

    def test_a_port_is_not_mistaken_for_a_password(self):
        assert self._git("https://example.com:8443/scm/@team/a.git").startswith(
            "https://example.com:8443/scm/@team/a.git @"
        )

    def test_a_query_string_is_dropped(self):
        """Nothing should ever put a secret there, which is exactly why a
        value that does must not be rendered."""
        assert "token=" not in self._git("https://example.com/a.git?token=ghp_secret")


class TestTheUploadTransport:
    def test_the_document_count(self):
        assert scan_scope(_row("upload", config={"documents": [DOC, DOC]})) == "uploaded documents (2)"

    def test_an_upload_source_carrying_nothing_says_zero(self):
        """"Nothing was uploaded" is a real, checkable state — not unknown."""
        assert scan_scope(_row("upload", config={})) == "uploaded documents (0)"


class TestSnowflakeSemanticViews:
    """The finding's own adapter: database, schema (or the whole database)
    and the role, because ``SHOW SEMANTIC VIEWS`` shows only what the role
    can see."""

    def _scope(self, config: dict, settings: dict | None) -> str | None:
        with patch(_SNOWFLAKE_SETTINGS, return_value=settings):
            return scan_scope(_row("connection", adapter="snowflake_semantic", config=config))

    def test_database_schema_and_role(self):
        scope = self._scope(
            {"database": "ESHOP_DEMO", "schema": "RAW"},
            {"database": "ESHOP_DEMO", "role": "ESHOP_DEMO_ROLE"},
        )

        assert scope == "ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE"

    def test_no_schema_means_the_whole_database_and_says_so(self):
        scope = self._scope({"database": "ESHOP_DEMO"}, {"database": "ESHOP_DEMO", "role": "ESHOP_DEMO_ROLE"})

        assert scope == "ESHOP_DEMO (whole database) as ESHOP_DEMO_ROLE"

    def test_the_database_falls_back_to_the_connections_own(self):
        """``extract`` resolves it the same way; the scope must not claim a
        narrower or different one than the sync will use."""
        scope = self._scope({"schema": "RAW"}, {"database": "ESHOP_DEMO", "role": "ESHOP_DEMO_ROLE"})

        assert scope == "ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE"

    def test_a_like_pattern_narrows_the_scan_and_is_stated(self):
        scope = self._scope(
            {"database": "ESHOP_DEMO", "schema": "RAW", "like": "SV_%"},
            {"database": "ESHOP_DEMO", "role": "ESHOP_DEMO_ROLE"},
        )

        assert scope == "ESHOP_DEMO.RAW matching 'SV_%' as ESHOP_DEMO_ROLE"

    def test_no_configured_role_is_named_as_the_default_not_omitted(self):
        """Silence would read as "no role involved"; the connection always
        connects as some role, and which one is the whole question."""
        scope = self._scope({"database": "ESHOP_DEMO", "schema": "RAW"}, {"database": "ESHOP_DEMO", "role": ""})

        assert scope == "ESHOP_DEMO.RAW as the connection's default role"

    def test_an_unconfigured_instance_states_the_scope_it_can_and_invents_no_role(self):
        scope = self._scope({"database": "ESHOP_DEMO", "schema": "RAW"}, None)

        assert scope == "ESHOP_DEMO.RAW"

    def test_nothing_configured_anywhere_is_unknown(self):
        assert self._scope({}, None) is None

    def test_a_settings_lookup_that_raises_costs_the_role_not_the_row(self):
        with patch(_SNOWFLAKE_SETTINGS, side_effect=RuntimeError("vault down")):
            scope = scan_scope(_row("connection", adapter="snowflake_semantic", config={"database": "ESHOP_DEMO"}))

        assert scope == "ESHOP_DEMO (whole database)"

    def test_no_credential_reaches_the_scope_string(self):
        scope = self._scope(
            {"database": "ESHOP_DEMO", "schema": "RAW"},
            {
                "database": "ESHOP_DEMO",
                "role": "ESHOP_DEMO_ROLE",
                "user": "AGNES_SVC",
                "account": "acct-xyz",
                "password": "hunter2",
                "token_env": "SNOWFLAKE_PASSWORD",
            },
        )

        for secret in ("hunter2", "SNOWFLAKE_PASSWORD", "acct-xyz", "AGNES_SVC"):
            assert secret not in scope


class TestTheKeboolaMetastore:
    def test_the_project_the_pinned_connection_is_bound_to(self, system_db):
        _connection("conn-1", config={"stack_url": "https://connection.example.com", "project_id": 4321,
                                      "project_name": "Demo Project"})

        scope = scan_scope(_row("connection", adapter="keboola_metastore", config={"connection_id": "conn-1"}))

        assert scope == "Keboola project 4321 (Demo Project)"

    def test_an_unnamed_project_still_reports_its_id(self, system_db):
        _connection("conn-1", config={"stack_url": "https://connection.example.com", "project_id": 4321})

        scope = scan_scope(_row("connection", adapter="keboola_metastore", config={"connection_id": "conn-1"}))

        assert scope == "Keboola project 4321"

    def test_a_connection_with_no_project_binding_says_so(self, system_db):
        _connection("conn-1", config={"stack_url": "https://connection.example.com"})

        scope = scan_scope(_row("connection", adapter="keboola_metastore", config={"connection_id": "conn-1"}))

        assert "no project bound" in scope

    def test_a_connection_that_no_longer_exists_is_named_as_gone(self, system_db):
        """The sync raises on exactly this; the scope must not read as an
        ordinary empty upstream."""
        scope = scan_scope(_row("connection", adapter="keboola_metastore", config={"connection_id": "conn-gone"}))

        assert scope == "Keboola connection conn-gone (no longer registered)"

    def test_a_connection_of_another_type_is_never_called_a_keboola_project(self, system_db):
        """Same check `_connection_master_credentials` makes before reading a
        Metastore: a source re-pointed at a BigQuery connection has no Keboola
        project at all, and claiming one would state a scope that does not
        exist."""
        _connection("conn-bq", source_type="bigquery", config={"project_id": 4321})

        scope = scan_scope(_row("connection", adapter="keboola_metastore", config={"connection_id": "conn-bq"}))

        assert "Keboola project" not in scope
        assert scope == "connection conn-bq (a bigquery connection, not a Keboola one)"

    def test_two_sources_on_one_connection_are_one_lookup(self, system_db):
        """The registry read goes through the same per-batch cache the
        settings resolvers use."""
        import src.repositories as repositories

        _connection("conn-1", config={"project_id": 4321})
        rows = [
            {
                "id": f"ss_{i}",
                "kind": "connection",
                "adapter": "keboola_metastore",
                "config": {"connection_id": "conn-1"},
            }
            for i in range(3)
        ]

        with patch.object(repositories, "source_connections_repo", wraps=repositories.source_connections_repo) as repo:
            scopes = scan_scopes(rows)

        assert repo.call_count == 1
        assert set(scopes.values()) == {"Keboola project 4321"}

    def test_an_unreadable_registry_is_not_reported_as_a_deleted_connection(self, system_db):
        """"I could not read the registry" and "this connection is gone" are
        different statements — the second would send an admin looking for a
        deletion that never happened."""
        with patch("src.repositories.source_connections_repo", side_effect=RuntimeError("state db unreadable")):
            scope = scan_scope(_row("connection", adapter="keboola_metastore", config={"connection_id": "conn-1"}))

        assert scope is None

    def test_the_legacy_environment_credential_path_is_named(self, system_db):
        scope = scan_scope(_row("connection", adapter="keboola_metastore", config={"legacy_credentials": True}))

        assert scope == "Keboola project of the legacy environment credentials"

    def test_a_config_with_neither_scope_key_is_unknown(self, system_db):
        assert scan_scope(_row("connection", adapter="keboola_metastore", config={})) is None

    def test_a_storage_token_in_the_config_is_never_echoed(self, system_db):
        """The login-triggered sync passes ``{url, token}`` inline. A stored
        row should never carry one, but if it does it must not be rendered."""
        scope = scan_scope(
            _row(
                "connection",
                adapter="keboola_metastore",
                config={"url": "https://connection.example.com", "token": "kbc-token-secret"},
            )
        )

        assert scope is None


class TestDatabricksMetricViews:
    def _scope(self, config: dict, settings: dict | None) -> str | None:
        with patch(_DATABRICKS_SETTINGS, return_value=settings):
            return scan_scope(_row("connection", adapter="databricks_metric_views", config=config))

    def test_the_workspace_and_its_catalogs(self):
        scope = self._scope(
            {"catalogs": ["main", "sales"]},
            {"host": "example-workspace.cloud.databricks.com", "catalogs": ["main"], "token": "dapi-secret"},
        )

        assert scope == "Unity Catalog catalogs main, sales on example-workspace.cloud.databricks.com"
        assert "dapi-secret" not in scope

    def test_a_single_catalog_reads_singular(self):
        scope = self._scope({"catalogs": ["main"]}, {"host": "example-workspace.cloud.databricks.com"})

        assert scope == "Unity Catalog catalog main on example-workspace.cloud.databricks.com"

    def test_the_catalogs_fall_back_to_the_connections_own(self):
        scope = self._scope({}, {"host": "example-workspace.cloud.databricks.com", "catalogs": ["main", "sales"]})

        assert scope == "Unity Catalog catalogs main, sales on example-workspace.cloud.databricks.com"

    def test_a_comma_separated_string_is_read_the_way_the_adapter_reads_it(self):
        scope = self._scope({"catalogs": "main, sales"}, {"host": "example-workspace.cloud.databricks.com"})

        assert scope == "Unity Catalog catalogs main, sales on example-workspace.cloud.databricks.com"

    def test_no_catalog_configured_is_stated_not_hidden(self):
        """``extract`` refuses to sync there — a blank scope would hide the
        reason the source imports nothing."""
        scope = self._scope({}, {"host": "example-workspace.cloud.databricks.com"})

        assert scope == "Unity Catalog (no catalog configured) on example-workspace.cloud.databricks.com"

    def test_an_unconfigured_workspace_is_unknown(self):
        assert self._scope({}, None) is None


class TestTheDispatch:
    def test_an_unknown_adapter_reports_nothing_rather_than_guessing(self):
        assert scan_scope(_row("connection", adapter="some_future_adapter", config={"database": "X"})) is None

    def test_an_unknown_kind_reports_nothing(self):
        assert scan_scope(_row("carrier_pigeon", config={"repo_url": "https://example.com/a.git"})) is None

    def test_a_native_connection_source_is_scoped_by_its_documents(self):
        assert scan_scope(_row("connection", adapter="native", config={"documents": [DOC]})) == (
            "uploaded documents (1)"
        )

    def test_a_native_connection_source_with_no_documents_is_unknown(self):
        """Unlike an upload source, there is no "nothing was uploaded" claim
        to make here — the row simply says nothing about a scope."""
        assert scan_scope(_row("connection", adapter="native", config={})) is None

    def test_a_malformed_config_does_not_raise(self):
        assert scan_scope({"id": "ss_x", "kind": "git", "adapter": "native", "config": "not-a-dict"}) is None

    def test_a_resolver_that_raises_is_logged_and_reported_unknown(self, caplog):
        import logging

        with patch(
            "src.repositories.source_connections_repo",
            side_effect=RuntimeError("state db unreadable"),
        ):
            with caplog.at_level(logging.WARNING, logger="src.semantic.scan_scope"):
                scope = scan_scope(
                    {
                        "id": "ss_boom",
                        "kind": "connection",
                        "adapter": "keboola_metastore",
                        "config": {"connection_id": "conn-1"},
                    }
                )

        assert scope is None
        assert any("ss_boom" in r.getMessage() for r in caplog.records), caplog.text


class TestTheBatchHelpers:
    def test_scan_scopes_keys_on_the_source_id(self):
        rows = [
            {"id": "ss_a", "kind": "git", "adapter": "native", "config": {"repo_url": "https://example.com/a.git"}},
            {"id": "ss_b", "kind": "upload", "adapter": "native", "config": {"documents": [DOC]}},
        ]

        assert scan_scopes(rows) == {
            "ss_a": "https://example.com/a.git @ default branch matching '**/*.yaml'",
            "ss_b": "uploaded documents (1)",
        }

    def test_connection_settings_are_resolved_once_for_the_whole_batch(self):
        """Ten Snowflake sources must not mean ten vault reads."""
        rows = [
            {"id": f"ss_{i}", "kind": "connection", "adapter": "snowflake_semantic", "config": {"schema": "RAW"}}
            for i in range(3)
        ]

        with patch(_SNOWFLAKE_SETTINGS, return_value={"database": "ESHOP_DEMO", "role": "R"}) as resolve:
            scopes = scan_scopes(rows)

        assert resolve.call_count == 1
        assert set(scopes.values()) == {"ESHOP_DEMO.RAW as R"}

    def test_with_scan_scope_annotates_without_mutating_the_row(self):
        row = {"id": "ss_a", "kind": "upload", "adapter": "native", "config": {"documents": [DOC]}}

        annotated = with_scan_scope([row])

        assert annotated[0][SCAN_SCOPE_FIELD] == "uploaded documents (1)"
        assert SCAN_SCOPE_FIELD not in row, "the repo row itself is left alone"

    def test_an_unknown_scope_is_an_explicit_none_not_a_missing_key(self):
        annotated = with_scan_scope([{"id": "ss_a", "kind": "git", "adapter": "native", "config": {}}])

        assert annotated[0][SCAN_SCOPE_FIELD] is None


class TestTheListEndpoint:
    def test_every_source_carries_its_scan_scope(self, seeded_app, system_db):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_git", kind="git", config={"repo_url": "https://example.com/acme/semantics.git", "ref": "main"})
        _source("ss_upload", kind="upload", config={"documents": [DOC]})

        resp = c.get("/api/admin/semantic-sources", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        by_id = {r["id"]: r for r in resp.json()}
        assert by_id["ss_git"]["scan_scope"] == "https://example.com/acme/semantics.git @ main matching '**/*.yaml'"
        assert by_id["ss_upload"]["scan_scope"] == "uploaded documents (1)"

    def test_the_scope_rides_beside_the_owned_model_count(self, seeded_app, system_db):
        """A17 is an ADDITION to #1821, not a replacement: "ok · 0 models"
        plus "scanned X" is what makes the misconfiguration checkable."""
        from src.repositories import semantic_source_repo

        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source(
            "ss_sf",
            kind="connection",
            adapter="snowflake_semantic",
            config={"database": "ESHOP_DEMO", "schema": "RAW"},
        )
        semantic_source_repo().record_sync("ss_sf", status="ok", error=None)

        with patch(_SNOWFLAKE_SETTINGS, return_value={"database": "ESHOP_DEMO", "role": "ESHOP_DEMO_ROLE"}):
            row = c.get("/api/admin/semantic-sources", headers=_auth(token)).json()[0]

        assert row["last_sync_status"] == "ok"
        assert row["owned_model_count"] == 0
        assert row["scan_scope"] == "ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE"

    def test_an_underivable_scope_is_null_over_the_wire(self, seeded_app, system_db):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_unknown", kind="connection", adapter="native", config={})

        body = c.get("/api/admin/semantic-sources", headers=_auth(token)).json()

        assert body[0]["scan_scope"] is None

    def test_the_field_survives_the_enabled_only_filter(self, seeded_app, system_db):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="upload", config={"documents": [DOC]})

        resp = c.get("/api/admin/semantic-sources?enabled_only=true", headers=_auth(token))

        assert [r["scan_scope"] for r in resp.json()] == ["uploaded documents (1)"]


class TestTheOtherSourceResponses:
    """Every read AND write response has the same shape, so a caller
    rendering any of them into the same table never special-cases a field."""

    def test_create_returns_the_field(self, seeded_app, system_db):
        c, token = seeded_app["client"], seeded_app["admin_token"]

        resp = c.post(
            "/api/admin/semantic-sources",
            json={
                "kind": "git",
                "name": "Fresh",
                "adapter": "native",
                "config": {"repo_url": "https://example.com/acme/semantics.git", "ref": "main"},
            },
            headers=_auth(token),
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["scan_scope"] == "https://example.com/acme/semantics.git @ main matching '**/*.yaml'"

    def test_update_returns_the_field(self, seeded_app, system_db):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="upload", config={"documents": [DOC]})

        resp = c.put("/api/admin/semantic-sources/ss_a", json={"enabled": False}, headers=_auth(token))

        assert resp.status_code == 200, resp.text
        assert resp.json()["scan_scope"] == "uploaded documents (1)"

    def test_a_no_op_update_still_returns_the_field(self, seeded_app, system_db):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="upload", config={"documents": [DOC]})

        resp = c.put("/api/admin/semantic-sources/ss_a", json={}, headers=_auth(token))

        assert resp.status_code == 200, resp.text
        assert resp.json()["scan_scope"] == "uploaded documents (1)"

    def test_get_one_source_carries_the_same_field(self, seeded_app, system_db):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        _source("ss_a", kind="git", config={"repo_url": "https://example.com/acme/semantics.git"})

        resp = c.get("/api/admin/semantic-sources/ss_a", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        assert (
            resp.json()["scan_scope"]
            == "https://example.com/acme/semantics.git @ default branch matching '**/*.yaml'"
        )


class TestTheHealthReportsSourcesBlock:
    """``compute_semantic_layer_health`` itself is Postgres-only (the mute
    overlay); the per-source projection it builds is not, so it is pinned
    here directly and end-to-end in tests/db_pg/test_semantic_layer_health_pg.py.
    """

    def test_each_source_reports_what_it_scanned(self, system_db):
        from src.semantic.coverage import _sync_status

        sources = [
            _source("ss_git", kind="git", config={"repo_url": "https://example.com/acme/semantics.git", "ref": "main"}),
            _source("ss_unknown", kind="connection", adapter="native", config={}),
        ]

        by_id = {s["source_id"]: s for s in _sync_status(sources)}

        assert by_id["ss_git"]["scan_scope"] == "https://example.com/acme/semantics.git @ main matching '**/*.yaml'"
        assert by_id["ss_unknown"]["scan_scope"] is None

    def test_the_scope_sits_beside_the_owned_model_count(self, system_db):
        from src.semantic.coverage import _sync_status

        source = _source("ss_upload", kind="upload", config={"documents": [DOC]})

        row = _sync_status([source])[0]

        assert row["owned_model_count"] == 0
        assert row["scan_scope"] == "uploaded documents (1)"
