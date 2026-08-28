"""/admin/tables — one connectedness signal, followable copy, per-project
Keboola Discover.

Bug (live-instance report): an instance with a Keboola project connected
through the `/admin/data-sources` registry-connection wizard still saw
"Keboola is not connected" on THIS page — the banners, tooltips, and the
Discover-helpers guard all read the legacy single-source `data_source_type`
scalar (default 'local' == unset) instead of the registry. Three compounding
faults, closed here:

1. Wrong signal — banners/tooltips/Databricks-banner keyed on the scalar
   instead of the registry-aware `connected_sources`.
2. Unfollowable advice — both banners pointed at a token field that does
   not exist on /admin/server-config.
3. A genuinely broken capability — the Discover JS helpers were only ever
   EMITTED on a `data_source_type == 'keboola'` instance, and even then
   always hit the *instance-level* Keboola project, never a specific
   registered one.

`app/web/router.py`'s `_connected_sources()` (a sibling change, landed
alongside this one) unions the `source_connections` registry, the legacy
scalar when it names a real source, and instance-side bq/snowflake/
databricks credential probes into the `connected_sources` context key this
file exercises. `_inject_ctx` shims that key directly into the /admin/tables
render via `setdefault` — belt-and-braces for a `connected_sources` the real
route hasn't shipped yet (a real value the route sets always wins, making
the shim a no-op once it has). Registry-backed scenarios below ALSO seed a
real `source_connections` row so the assertions hold against the actual
`_connected_sources()` implementation, not just the shim.
"""

import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_instance_config_cache():
    """Every test in this file that monkeypatches `load_instance_config`
    calls `reset_cache()` itself before making the request, but the cache
    also needs clearing on the way OUT — otherwise a fake config from one
    test can leak into the next one in the same worker process."""
    from app.instance_config import reset_cache

    reset_cache()
    yield
    reset_cache()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _inject_ctx(monkeypatch, **extra):
    """Shim: injects extra template-context keys into the /admin/tables
    render without touching app/web/router.py (owned by a sibling change).
    `setdefault` — a key the real route already sets wins."""
    import app.web.router as router_mod

    original = router_mod.templates.TemplateResponse

    def _wrapped(request, name, context=None, *args, **kwargs):
        if name == "admin_tables.html" and context is not None:
            context = dict(context)
            for k, v in extra.items():
                context.setdefault(k, v)
        return original(request, name, context, *args, **kwargs)

    monkeypatch.setattr(router_mod.templates, "TemplateResponse", _wrapped)


@pytest.fixture
def keboola_connection():
    """A real `source_connections` registry row (source_type='keboola'),
    torn down after the test. Proves the fix against the ACTUAL
    `_connected_sources()` union, not just the `_inject_ctx` shim."""
    from src.repositories import source_connections_repo

    conn_id = f"kbtest-{uuid.uuid4().hex[:8]}"
    source_connections_repo().create(
        id=conn_id,
        name=f"Keboola Test {conn_id[-4:]}",
        source_type="keboola",
        config={"stack_url": "https://connection.keboola.com"},
    )
    try:
        yield conn_id
    finally:
        source_connections_repo().delete(conn_id)


@pytest.fixture
def databricks_connection():
    """A real `source_connections` registry row (source_type='databricks')."""
    from src.repositories import source_connections_repo

    conn_id = f"dbxtest-{uuid.uuid4().hex[:8]}"
    source_connections_repo().create(
        id=conn_id,
        name=f"Databricks Test {conn_id[-4:]}",
        source_type="databricks",
        config={},
    )
    try:
        yield conn_id
    finally:
        source_connections_repo().delete(conn_id)


def _set_data_source_type(monkeypatch, fake_cfg):
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()


def _kb_edit_section(html):
    start = html.index('id="editKeboolaModal"')
    end = html.index('id="editModal"')
    return html[start:end]


class TestOneConnectednessSignal:
    """Part A/B: banners, tooltips and the Discover-emit guard all read
    `connected_sources`, and the copy is true + followable.

    D4 — one registration flow: the pre-fix per-modal Jinja banners this
    class scraped (`{% if 'keboola' not in connected_sources %}` etc.,
    Keboola- and Databricks-only) moved into the shared register drawer as
    ONE generic, client-side check — `register_table_form.js`'s
    `_applyConnectivityWarning`, which reads the SAME `connected_sources`
    union off `document.body.dataset.connectedSources` (server-rendered
    exactly as before — see `test_body_attrs_and_js_var_use_connected_sources`
    below, unaffected) but now covers all four connectors, not just two.
    The EDIT modal's own banner is untouched and still asserted here."""

    def test_registry_connected_instance_no_banner_and_discover_emitted(
        self, seeded_app, monkeypatch, keboola_connection
    ):
        """The reported bug: a non-keboola scalar, but Keboola IS reachable
        via a REAL registry connection. `connected_sources` says so — the
        EDIT modal's banner must disappear and Discover must be live."""
        _set_data_source_type(monkeypatch, {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}})
        _inject_ctx(monkeypatch, connected_sources=["keboola"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

        edit = _kb_edit_section(html)
        assert "Keboola is not connected" not in edit
        assert 'data-tooltip="Keboola not connected' not in edit
        assert 'onclick="discoverKeboolaBuckets(' in html
        assert 'onclick="discoverKeboolaTables(' in html
        assert 'onclick="prefillFromKeboolaTable(' in html
        assert "keboola" in html.split('data-connected-sources="', 1)[1].split('"', 1)[0].split(",")

    def test_scalar_only_instance_still_works_regression(self, seeded_app, monkeypatch):
        """Pre-existing single-source instance (data_source.type='keboola',
        no registry row at all): `connected_sources` unions the scalar, so
        this must not regress relative to the pre-fix behaviour."""
        _set_data_source_type(monkeypatch, {"data_source": {"type": "keboola", "keboola": {}}})
        _inject_ctx(monkeypatch, connected_sources=["keboola"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

        assert "Keboola is not connected" not in _kb_edit_section(html)
        assert 'onclick="discoverKeboolaBuckets(' in html
        assert 'onclick="discoverKeboolaTables(' in html
        assert 'onclick="prefillFromKeboolaTable(' in html

    def test_genuinely_unconnected_instance_still_warns(self, seeded_app, monkeypatch):
        """Keboola unreachable by BOTH routes — the EDIT modal's warning +
        disabled buttons must survive the migration, off `connected_sources`
        instead of the scalar, with followable copy."""
        _set_data_source_type(monkeypatch, {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}})
        _inject_ctx(monkeypatch, connected_sources=["bigquery"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

        edit = _kb_edit_section(html)
        assert "Keboola is not connected" in edit
        assert 'data-tooltip="Keboola not connected' in edit
        assert 'onclick="discoverKeboolaBuckets(' not in html
        assert 'onclick="discoverKeboolaTables(' not in html
        assert 'onclick="prefillFromKeboolaTable(' not in html
        assert 'data-connected-sources="bigquery"' in html

    def test_warning_copy_is_followable(self, seeded_app, monkeypatch):
        """The rewritten copy must point at an action that exists
        (/admin/data-sources) and drop the promise of a token field that
        does not exist on /admin/server-config."""
        _set_data_source_type(monkeypatch, {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}})
        _inject_ctx(monkeypatch, connected_sources=["bigquery"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

        edit = _kb_edit_section(html)
        assert "/admin/data-sources" in edit
        assert "cfg-s-data_source" not in edit
        assert "Set your token in" not in edit
        assert "set token in Instance settings" not in edit

    def test_register_drawer_connectivity_warning_is_generic_across_connectors(self):
        """D4's replacement for the register-side halves of the four tests
        above: one JS function, all four connectors, same union — verified
        by static inspection (see test_register_table_form.py for the
        broader shared-drawer structural coverage)."""
        js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
        fn_start = js.index("function _applyConnectivityWarning(sourceType)")
        fn_body = js[fn_start : js.index("\n  }", fn_start)]
        # Reads the SAME body attr the pre-fix banners' server-side
        # `connected_sources` context populates.
        assert "document.body.dataset.connectedSources" in fn_body
        # Absent (setup.html) → skip, not a false "not connected" flash.
        assert "raw === undefined" in fn_body
        # Followable copy, not the removed token-field promise.
        assert "Connect it from Data sources" in fn_body
        assert "cfg-s-data_source" not in fn_body
        # Called on every open(), for every connector — not gated to
        # Keboola/Databricks the way the two Jinja banners were.
        assert "_applyConnectivityWarning(sourceType)" in js

    def test_databricks_banner_registry_only_connection_suppresses_it(
        self, seeded_app, monkeypatch, databricks_connection
    ):
        """Belt-and-braces removed: a REAL registry-only Databricks
        connection (scalar naming something else entirely) must be
        reflected in the union the register drawer's JS reads."""
        _set_data_source_type(monkeypatch, {"data_source": {"type": "snowflake", "snowflake": {}}})
        _inject_ctx(monkeypatch, connected_sources=["databricks"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert "databricks" in html.split('data-connected-sources="', 1)[1].split('"', 1)[0].split(",")

    def test_databricks_banner_still_warns_when_unreachable(self, seeded_app, monkeypatch):
        _set_data_source_type(monkeypatch, {"data_source": {"type": "snowflake", "snowflake": {}}})
        _inject_ctx(monkeypatch, connected_sources=[])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert "databricks" not in html.split('data-connected-sources="', 1)[1].split('"', 1)[0].split(",")

    def test_body_attrs_and_js_var_use_connected_sources(self, seeded_app, monkeypatch):
        """Structural check on the JS consumers named in the bug report: body
        dataset attr, CONNECTED_SOURCES var, renderConnectorNotice.

        Every *connectedness* claim reads the union. The register-split-button
        shortcut (`_dbxIsOnlyConnectedSource`) is the one consumer that does
        NOT: it asks the narrower "is Databricks the only source somebody
        registered here", so it keeps reading the registry-only
        CONNECTED_SOURCE_TYPES — see
        `test_dbx_shortcut_reads_registry_only_source_types` below.
        """
        _inject_ctx(monkeypatch, connected_sources=["keboola"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

        assert "data-connected-sources=" in html
        assert "var CONNECTED_SOURCES = " in html
        assert "CONNECTED_SOURCES.indexOf('keboola')" in html

    def test_dbx_shortcut_reads_registry_only_source_types(self, seeded_app, monkeypatch):
        """The one-click "register a Databricks table" shortcut must not be
        disarmed by an unrelated credential.

        Pointing `_dbxIsOnlyConnectedSource` at the CONNECTED_SOURCES union
        broke it for every Databricks instance that also had, say, a BigQuery
        service account in the vault: the union then holds two entries and the
        `length === 1` test never fires. The registry-only list answers the
        question the shortcut actually asks.
        """
        _inject_ctx(monkeypatch, connected_sources=["bigquery", "databricks"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

        assert "data-connected-source-types=" in html
        assert "var CONNECTED_SOURCE_TYPES = " in html
        shortcut = html.split("function _dbxIsOnlyConnectedSource(", 1)[1].split("}", 1)[0]
        assert "CONNECTED_SOURCE_TYPES.filter(Boolean)" in shortcut
        assert "CONNECTED_SOURCES." not in shortcut


class TestPerProjectDiscover:
    """Part C: a Keboola project selector, wired to the registry-aware
    per-connection tables endpoint, with connection_id riding along in the
    register payload."""

    def test_project_selector_present_in_register_and_edit_modals(self, seeded_app):
        """D4 — one registration flow: the register-side project picker
        (`#kbProjectSelect` / `#kbProjectPickerGroup`) moved into the shared
        drawer as `#rtfConnectionPicker` / `#rtfConnectionPickerField` —
        generic (`hasConnectionPicker` in CONNECTORS.keboola), not a
        Keboola-only clone of the field. The EDIT modal's own picker is
        unchanged."""
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="rtfConnectionPicker"' in html
        assert 'id="rtfConnectionPickerField"' in html
        assert 'id="editKbProjectSelect"' in html
        assert 'id="editKbProjectPickerGroup"' in html
        js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
        assert "hasConnectionPicker: true" in js

    def test_project_picker_populated_from_registry_endpoint(self, seeded_app):
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        # Edit modal: unchanged.
        assert "function _populateKbProjectPicker(" in html
        assert "_populateKbProjectPicker('editKb', table.connection_id" in html
        # Register (shared drawer): its own connection-picker populator,
        # generic across every connector with `hasConnectionPicker: true`.
        js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
        assert "async function _populateConnectionPicker()" in js
        assert "/api/admin/source-connections?source_type=" in js

    def test_register_payload_carries_connection_id_when_project_selected(self, seeded_app):
        js = Path("app/web/static/js/register_table_form.js").read_text(encoding="utf-8")
        kb_start = js.index("keboola: {")
        kb_end = js.index("\n    bigquery: {", kb_start)
        kb_config = js[kb_start:kb_end]
        assert "connection_id: s.connectionId" in kb_config
        # `s.connectionId` is `state.connectionId` (set by onConnectionChange
        # reading #rtfConnectionPicker), collected in _collectSettings.
        assert "connectionId: state.connectionId || null" in js

    def test_both_discover_response_shapes_normalize(self, seeded_app, monkeypatch):
        """Legacy `{tables: [...]}` and per-connection `{buckets: [{tables:
        [...]}, ...]}` both feed the same table-listing code path."""
        _set_data_source_type(monkeypatch, {"data_source": {"type": "keboola", "keboola": {}}})
        _inject_ctx(monkeypatch, connected_sources=["keboola"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert "function _kbTablesForBucket(" in html
        start = html.index("function discoverKeboolaTables")
        end = html.index("function ", start + len("function discoverKeboolaTables"))
        body = html[start:end]
        assert "_kbTablesForBucket(data, bucket)" in body
        # Both endpoints are reachable from this function depending on
        # selection — the registry-scoped one AND the legacy fallback.
        assert "/api/admin/source-connections/" in body
        assert "/api/admin/discover-tables?source=keboola" in body

    def test_discover_error_names_which_project_failed(self, seeded_app, monkeypatch):
        _set_data_source_type(monkeypatch, {"data_source": {"type": "keboola", "keboola": {}}})
        _inject_ctx(monkeypatch, connected_sources=["keboola"])
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert "function _kbErrorMessage(" in html
        assert "_kbErrorMessage(d, connId)" in html

    def test_legacy_option_only_offered_when_scalar_names_keboola(self, seeded_app):
        """The one place besides modal-routing where the raw scalar still
        has to be read directly: the legacy discover-tables endpoint routes
        by it, not by the registry, so offering it as a picker option when
        the scalar names something else would silently query the wrong
        project (the exact bug a naive guard-flip would have reintroduced)."""
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert "Instance-level Keboola (legacy)" in html
        assert "if (DATA_SOURCE_TYPE === 'keboola') {" in html
