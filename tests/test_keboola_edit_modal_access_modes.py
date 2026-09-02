"""The Keboola access-mode picker is ONE definition, shared by both modals (#1979).

Item 5 of the admin setup-flow review: `/admin/tables` had two modals that
pick a Keboola table's extraction mode and they disagreed.

  * The Register wizard (`register_table_form.js` `CONNECTORS.keboola.modes`)
    offered four modes and called the materialized-with-a-filter one
    "Filtered export (Storage API)", posting a JSON filter spec.
  * The Edit modal (`admin_tables.html` `#editKeboolaModal`) offered three,
    called the same mode "Custom SQL", and posted a raw SELECT — which the
    server refuses outright (`app/api/admin.py`: "must be a JSON filter spec
    … not SQL"), so that widget could not produce a saveable row at all.

Both modals now render their option list from the SAME
`CONNECTORS.keboola.modes` array through the SAME `renderModeCards` helper,
each option carrying the `query_mode` it produces, so a label or an outcome
can no longer drift between them.

Also pinned here: the Edit modal's pre-selection, which used to open a
`remote` row on "Whole table" (and silently convert it to materialized on
Save), and a JSON-filter row on a textarea labelled SQL.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_REGISTER_JS = Path("app/web/static/js/register_table_form.js")
_ADMIN_TABLES = Path("app/web/templates/admin_tables.html")

# Marker comments in admin_tables.html delimiting the Keboola edit-modal
# helpers that are pure (no DOM, no globals) and therefore runnable under
# plain `node` — the same "run the shipped code, don't copy it" pattern
# tests/test_register_table_form.py uses for the selection helpers.
_SLICE_START = "// ── #1979 pure edit-modal helpers — node-tested slice start ──"
_SLICE_END = "// ── #1979 pure edit-modal helpers — node-tested slice end ──"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — these helpers need a runtime")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _load_register_js() -> str:
    """Load the SHIPPED register_table_form.js into a node global scope.

    The module is a browser IIFE that touches no DOM at load time — it only
    publishes `window.RegisterTableForm` — so aliasing `window` to `global`
    is enough to exercise the real connector config and helpers.
    """
    return "global.window = global;\nrequire(%r);\n" % str(_REGISTER_JS.resolve())


def _edit_helpers_slice() -> str:
    text = _ADMIN_TABLES.read_text(encoding="utf-8")
    start = text.index(_SLICE_START)
    end = text.index(_SLICE_END)
    return text[start:end]


def _run_edit_helper(expr: str) -> object:
    script = _load_register_js() + _edit_helpers_slice() + "\nprocess.stdout.write(JSON.stringify(" + expr + "));\n"
    return json.loads(_node_run(script))


# ── one definition, two modals ───────────────────────────────────────────


def test_the_four_keboola_modes_carry_their_query_mode_in_one_place():
    """`CONNECTORS.keboola.modes` is the single definition: value, label and
    the `query_mode` the option actually produces, in that one array."""
    modes = _run_edit_helper("window.RegisterTableForm.CONNECTORS.keboola.modes")
    assert [m["value"] for m in modes] == ["whole", "direct", "custom", "remote"]
    assert [m["title"] for m in modes] == [
        "Whole table (extension)",
        "Direct extract (Storage API)",
        "Filtered export (Storage API)",
        "Live (remote)",
    ]
    assert [m["queryMode"] for m in modes] == ["materialized", "local", "materialized", "remote"]


def test_every_mode_renders_the_query_mode_it_produces():
    """(c) — each option carries a secondary line naming its outcome, built
    by one shared formatter so both modals print the same string."""
    lines = _run_edit_helper(
        "window.RegisterTableForm.CONNECTORS.keboola.modes.map(window.RegisterTableForm.modeOutcome)"
    )
    assert lines == [
        "→ query_mode: materialized",
        "→ query_mode: local",
        "→ query_mode: materialized",
        "→ query_mode: remote",
    ]


def test_the_edit_modal_renders_its_radios_from_the_shared_definition(seeded_app):
    """The Edit modal no longer hard-codes its option markup: it mounts an
    empty group and fills it through `RegisterTableForm.renderModeCards`
    with `CONNECTORS.keboola.modes`. A label added to the wizard therefore
    shows up in Edit with no second edit."""
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    assert 'id="editKbSyncModeGroup"' in html
    assert "RegisterTableForm.renderModeCards(" in html
    assert "CONNECTORS.keboola.modes" in html
    # The radios themselves are still named editKbSyncMode (the save path
    # reads them by name) — but rendered, not authored.
    assert "editKbSyncMode" in html

    # No hand-written copy of any option label survives in the template.
    for label in (
        "Whole table (extension)",
        "Direct extract (Storage API)",
        "Filtered export (Storage API)",
        "Live (remote)",
    ):
        assert f"<strong>{label}</strong>" not in html, (
            f"{label!r} is authored in admin_tables.html again — the option list must come "
            "from CONNECTORS.keboola.modes so the two modals cannot drift"
        )
    # The mislabel this item fixes.
    assert "Custom SQL</strong>" not in html


def test_the_edit_modal_header_names_the_row_s_current_mode(seeded_app):
    """ "Currently: Filtered export (Storage API) → query_mode: materialized"
    — built from the same definition + formatter as the option cards."""
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert 'id="editKbCurrentMode"' in html
    assert "Currently: " in html


# ── pre-selection: the four registry shapes ──────────────────────────────


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"query_mode": "remote", "bucket": "in.c-sales", "source_table": "orders"}, "remote"),
        ({"query_mode": "local", "bucket": "in.c-sales", "source_table": "orders"}, "direct"),
        (
            {
                "query_mode": "materialized",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "source_query": '{"where_filters": [{"column": "date", "operator": "ge", "values": ["2026-01-01"]}]}',
            },
            "custom",
        ),
        ({"query_mode": "materialized", "bucket": "in.c-sales", "source_table": "orders"}, "whole"),
        (
            {
                "query_mode": "materialized",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "source_query": 'SELECT * FROM kbc."in.c-sales"."orders"',
            },
            "whole",
        ),
        (
            {
                "query_mode": "materialized",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "source_query": 'SELECT id FROM kbc."in.c-sales"."orders" WHERE x = 1',
            },
            "custom",
        ),
    ],
    ids=["remote", "local", "json-filter", "no-filter", "legacy-auto-select", "legacy-hand-written-sql"],
)
def test_pre_selection_opens_the_mode_the_row_actually_is(row, expected):
    """The bug: a `remote` row fell through to "Whole table", and Save then
    converted it to materialized without telling anyone. A JSON-filter row
    landed in a textarea labelled SQL.

    The one deliberate exception stays: a row carrying the auto-synthesized
    `SELECT * FROM kbc."b"."t"` (the legacy whole-table shape the server now
    refuses) still opens on "Whole table", whose Save clears it — that is the
    only escape hatch such a row has.
    """
    assert _run_edit_helper("_editKbModeForRow(%s)" % json.dumps(row)) == expected


# ── the filtered-export widget: a JSON spec, never SQL ───────────────────


def test_filtered_export_builds_a_where_filters_spec_from_the_builder():
    """Same output shape as the wizard's `custom` branch — the structured
    builder's rows wrapped as `{"where_filters": [...]}`."""
    rows = '[{"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]}]'
    produced = _run_edit_helper("_editKbFilterSourceQuery(false, '', %s)" % json.dumps(rows))
    assert json.loads(produced) == {
        "where_filters": [{"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]}]
    }


def test_an_empty_filter_builder_means_a_full_table_export():
    """No filter rows → NULL source_query, exactly like "Whole table"."""
    assert _run_edit_helper("_editKbFilterSourceQuery(false, '', '[]')") is None


def test_the_advanced_editor_passes_a_hand_written_json_spec_through():
    """The advanced escape hatch accepts the exact spec the server takes —
    `where_filters`, `columns`, `changed_since`, `limit`, `file_type` — not
    just what the structured builder can draw."""
    raw = '{"columns": ["id", "created_at"], "changed_since": "-2 days"}'
    produced = _run_edit_helper("_editKbFilterSourceQuery(true, %s, '[]')" % json.dumps(raw))
    assert json.loads(produced) == {"columns": ["id", "created_at"], "changed_since": "-2 days"}


@pytest.mark.parametrize("sql", ["SELECT * FROM kbc.x.y", "  with t as (select 1) select * from t"])
def test_the_advanced_editor_refuses_sql_with_the_server_s_own_words(sql):
    """The owner asked for "Advanced: raw SQL" — but a Keboola materialized
    row exports through the Storage API, whose filter is a JSON spec, so the
    server 422s any source_query starting with SELECT/WITH. The advanced
    editor is therefore the JSON spec, and it rejects SQL client-side with
    the message the server would have answered."""
    script = (
        _load_register_js()
        + _edit_helpers_slice()
        + "\ntry { _editKbFilterSourceQuery(true, %s, '[]'); process.stdout.write('\"no-throw\"'); }\n"
        % json.dumps(sql)
        + "catch (e) { process.stdout.write(JSON.stringify(e.message)); }\n"
    )
    message = json.loads(_node_run(script))
    assert "JSON filter spec" in message
    assert "not SQL" in message


def test_the_client_side_rejection_is_the_server_s_message_verbatim(seeded_app):
    """Not a paraphrase: the string the JS refuses with is the same one the
    API answers with, so an operator who sees it can search for it once."""
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_kb_msg",
            "source_type": "keboola",
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
        },
    )
    assert r.status_code == 201, r.text
    r = c.put(
        "/api/admin/registry/orders_kb_msg",
        headers=auth,
        json={"query_mode": "materialized", "source_query": 'SELECT * FROM kbc."in.c-sales"."orders" WHERE x = 1'},
    )
    assert r.status_code == 422, r.text
    server_message = " ".join(str(r.json()["detail"]).split())

    js_message = json.loads(
        _node_run(
            _load_register_js()
            + "process.stdout.write(JSON.stringify(window.RegisterTableForm.KEBOOLA_FILTER_NOT_SQL_MESSAGE));"
        )
    )
    assert " ".join(js_message.split()) == server_message


def test_a_legacy_non_json_source_query_is_shown_not_dropped():
    """A row saved by the old "Custom SQL" widget (or any non-JSON string)
    opens in the advanced editor, with a warning — never silently discarded,
    and never fed to a structured builder that cannot represent it."""
    shape = _run_edit_helper("_editKbFilterSpecShape('SELECT 1')")
    assert shape["raw"] is True
    assert shape["value"] == "SELECT 1"
    assert shape["warning"], "a legacy source_query must arrive with an explanation"

    shape = _run_edit_helper('_editKbFilterSpecShape(\'{"columns": ["id"]}\')')
    assert shape["raw"] is True, "a spec key the builder cannot draw belongs in the advanced editor"
    assert shape["warning"]

    shape = _run_edit_helper(
        '_editKbFilterSpecShape(\'{"where_filters": [{"column": "a", "operator": "eq", "values": ["1"]}]}\')'
    )
    assert shape["raw"] is False
    assert json.loads(shape["filters"]) == [{"column": "a", "operator": "eq", "values": ["1"]}]
    assert not shape["warning"]


def test_the_filtered_export_payload_is_accepted_by_the_update_endpoint(seeded_app):
    """End-to-end on the shape the Edit modal now posts: the JSON spec the
    builder produces survives `PUT /api/admin/registry/{id}` — the exact
    request the old "Custom SQL" textarea could never satisfy."""
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_kb_filtered",
            "source_type": "keboola",
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
        },
    )
    assert r.status_code == 201, r.text

    source_query = _run_edit_helper(
        "_editKbFilterSourceQuery(false, '', %s)"
        % json.dumps('[{"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]}]')
    )
    r = c.put(
        "/api/admin/registry/orders_kb_filtered",
        headers=auth,
        json={
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "source_query": source_query,
        },
    )
    assert r.status_code == 200, r.text
    rows = c.get("/api/admin/registry", headers=auth).json()
    row = next(t for t in rows["tables"] if t["id"] == "orders_kb_filtered")
    assert json.loads(row["source_query"]) == {
        "where_filters": [{"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]}]
    }


# ── (b) Live (remote) in the Edit modal ──────────────────────────────────


def test_edit_remote_branch_posts_query_mode_remote_and_clears_server_only():
    """`server_only` is validated against the MERGED record, so an edit that
    flips a server-only row to Live must send `server_only: false` — omitting
    it keeps the stored `true` and the PUT 422s."""
    html = _ADMIN_TABLES.read_text(encoding="utf-8")
    start = html.index("function _buildKeboolaEditPayload(")
    end = html.index("\n    function ", start)
    branch = html[start:end]
    assert "query_mode: 'remote'" in branch
    assert "server_only: false" in branch


def test_switching_a_server_only_keboola_row_to_live_is_accepted(seeded_app):
    """The payload the Edit modal's Live branch posts, end to end."""
    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_kb_live",
            "source_type": "keboola",
            "query_mode": "local",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "server_only": True,
        },
    )
    assert r.status_code == 201, r.text

    r = c.put(
        "/api/admin/registry/orders_kb_live",
        headers=auth,
        json={
            "query_mode": "remote",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "primary_key": [],
            "server_only": False,
        },
    )
    assert r.status_code == 200, r.text

    rows = c.get("/api/admin/registry", headers=auth).json()
    row = next(t for t in rows["tables"] if t["id"] == "orders_kb_live")
    assert row["query_mode"] == "remote"
    assert not row.get("server_only")


def test_live_mode_disables_the_server_only_checkbox(seeded_app):
    """Mirrors the wizard, which hides server-only for `remote` entirely —
    the server refuses `server_only=true` + `remote` (422)."""
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    start = html.index("function onEditKbSyncModeChange(")
    end = html.index("\n    function ", start)
    handler = html[start:end]
    assert "editKbServerOnly" in handler
    assert "remote" in handler
