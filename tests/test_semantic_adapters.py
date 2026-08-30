import pytest

from src.semantic.adapters import UnknownAdapter, get_adapter


def test_native_adapter_returns_documents_untouched():
    text = "version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n"
    out = get_adapter("native").extract({"documents": [text]})
    assert out == [text], "the adapter must not re-serialize; byte-identical or bust"


def test_unknown_adapter_names_the_available_ones():
    with pytest.raises(UnknownAdapter) as exc:
        get_adapter("nope")
    assert "native" in str(exc.value)


def test_every_connector_adapter_is_mirrored_in_the_coverage_map():
    """`SEMANTIC_ADAPTER_BY_SOURCE_TYPE` is a hand-maintained mirror of the
    adapter registry, and a source type absent from it reports its semantic
    column as ``not_applicable`` — "no adapter exists for this source type"
    — which is a lie the moment one does. That is exactly how the Databricks
    metric-view adapter shipped registered, wired into the connect wizard,
    and still reported as having no adapter at all.

    ``native`` is the one legitimate absence: it serves the git/upload
    transports, which are not bound to a `source_connections` row. Anything
    else new has to be added to the map — or added to this exemption with a
    reason, deliberately.
    """
    from src.semantic.adapters import _REGISTRY
    from src.semantic.coverage import SEMANTIC_ADAPTER_BY_SOURCE_TYPE

    not_connection_backed = {"native"}
    registered = set(_REGISTRY) - not_connection_backed
    mapped = set(SEMANTIC_ADAPTER_BY_SOURCE_TYPE.values())

    assert registered - mapped == set(), (
        f"adapter(s) {sorted(registered - mapped)} are registered but named by no source type in "
        "SEMANTIC_ADAPTER_BY_SOURCE_TYPE (src/semantic/coverage.py) — every connection whose "
        "type they serve will report semantic coverage as 'not_applicable'"
    )
    assert mapped - registered == set(), (
        f"SEMANTIC_ADAPTER_BY_SOURCE_TYPE names adapter(s) {sorted(mapped - registered)} that are "
        "not registered in src/semantic/adapters — those connections' semantic column would "
        "score against an adapter that cannot run"
    )


class TestUnconfiguredReason:
    """The optional pre-flight hook the sources sweep asks before importing:
    "is the connector behind this source configured at all right now?".

    Optional by design — an adapter that does not implement it is simply
    always ready — and best-effort: it is a PRE-check, so it may never be the
    thing that fails a source. Whatever it cannot answer, the import answers
    for real.
    """

    def test_an_adapter_without_the_hook_is_always_ready(self):
        from src.semantic.adapters import unconfigured_reason

        assert unconfigured_reason({"adapter": "native", "kind": "upload", "config": {}}) is None

    def test_an_unknown_adapter_is_left_to_the_import_to_reject(self):
        """A source naming an adapter that is not registered must still reach
        `import_source`, which raises UnknownAdapter and records it on the
        row. Swallowing it here as "skipped" would hide a broken source."""
        from src.semantic.adapters import unconfigured_reason

        assert unconfigured_reason({"adapter": "nope", "kind": "connection", "config": {}}) is None

    def test_a_raising_hook_is_treated_as_ready(self):
        from src.semantic.adapters import register_adapter, unconfigured_reason

        class Exploding:
            def extract(self, config):
                return []

            def unconfigured_reason(self, config):
                raise RuntimeError("vault unreachable")

        register_adapter("test_exploding_precheck", Exploding())
        try:
            assert unconfigured_reason({"adapter": "test_exploding_precheck", "config": {}}) is None
        finally:
            from src.semantic.adapters import _REGISTRY

            _REGISTRY.pop("test_exploding_precheck", None)

    def test_the_databricks_adapter_reports_an_unconfigured_workspace(self, monkeypatch):
        from src.semantic.adapters import unconfigured_reason

        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda *a, **k: None,
        )
        reason = unconfigured_reason({"adapter": "databricks_metric_views", "config": {}})
        assert reason is not None
        assert "not configured" in reason.lower()

    def test_the_databricks_adapter_is_ready_when_the_workspace_resolves(self, monkeypatch):
        from src.semantic.adapters import unconfigured_reason

        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda *a, **k: {"host": "example.cloud.databricks.com", "warehouse_id": "w1", "token": "t"},
        )
        assert unconfigured_reason({"adapter": "databricks_metric_views", "config": {}}) is None

    def test_the_snowflake_adapter_reports_an_unconfigured_account(self, monkeypatch):
        """The same shape as Databricks, and not hypothetical: the
        /admin/data-sources wizard auto-registers a `snowflake_semantic`
        source on connect, so deconfiguring Snowflake leaves exactly the same
        source-outlives-its-connector row behind."""
        from src.semantic.adapters import unconfigured_reason

        monkeypatch.setattr(
            "connectors.snowflake.settings.resolve_snowflake_settings",
            lambda *a, **k: None,
        )
        reason = unconfigured_reason({"adapter": "snowflake_semantic", "config": {}})
        assert reason is not None
        assert "not configured" in reason.lower()

    def test_the_snowflake_adapter_is_ready_when_the_account_resolves(self, monkeypatch):
        from src.semantic.adapters import unconfigured_reason

        monkeypatch.setattr(
            "connectors.snowflake.settings.resolve_snowflake_settings",
            lambda *a, **k: {"account": "acct", "user": "u", "database": "db"},
        )
        assert unconfigured_reason({"adapter": "snowflake_semantic", "config": {}}) is None

    @pytest.mark.parametrize(
        "adapter,resolver,legacy_config",
        [
            (
                "databricks_metric_views",
                "connectors.databricks.semantic_layer.resolve_databricks_settings",
                "data_source.databricks",
            ),
            (
                "snowflake_semantic",
                "connectors.snowflake.settings.resolve_snowflake_settings",
                "data_source.snowflake",
            ),
        ],
    )
    def test_the_reason_names_both_places_a_connector_is_configured(
        self, monkeypatch, adapter, resolver, legacy_config
    ):
        """Both resolvers are ROW-FIRST: the `source_connections` row the
        /admin/data-sources wizard writes, with the `data_source.*` yaml as
        the legacy fallback. A reason naming only the yaml sends an admin who
        configured the connector through the wizard off to edit a file that
        was never the source of truth for them.
        """
        from src.semantic.adapters import unconfigured_reason

        monkeypatch.setattr(resolver, lambda *a, **k: None)
        reason = unconfigured_reason({"adapter": adapter, "config": {}}) or ""
        assert "Admin → Data sources" in reason
        assert legacy_config in reason
