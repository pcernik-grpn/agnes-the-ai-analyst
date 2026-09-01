"""GET /admin/ontology (fact-graph-over-Collections §13.2, "Ontology builder").

Zero new navigation: reachable only via the link on /admin/semantic-layer,
no admin-nav entry of its own. When the `facts` flag is off, or the active
backend is DuckDB (drafts are PG-only), the page renders an explanatory
empty state rather than 404ing or crashing -- same posture as /apps when
data_apps is disabled.

The builder's client-side behavior lives in one inline ``<script>`` (no
separate ``.js`` file). Following ``tests/test_chat_tool_rendering_ui.py``'s
pattern: pure helper functions are extracted by name and run node-executed
against the SHIPPED source (no copies); DOM-touching wiring is pinned
structurally via substring assertions on the same source, since this repo
carries no jsdom dependency.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_TEMPLATE_PATH = Path("app/web/templates/ontology_builder.html")
_TEMPLATE_SRC = _TEMPLATE_PATH.read_text(encoding="utf-8")


def _script_source() -> str:
    """The inline ``<script>`` body under ``{% block scripts %}`` -- no
    Jinja inside it, so it is plain, node-executable JS as-is."""
    start = _TEMPLATE_SRC.index("<script>", _TEMPLATE_SRC.index("{% block scripts %}")) + len("<script>")
    end = _TEMPLATE_SRC.index("</script>", start)
    return _TEMPLATE_SRC[start:end]


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_anon_redirects(seeded_app):
    r = seeded_app["client"].get("/admin/ontology", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_non_admin_403(seeded_app):
    r = seeded_app["client"].get("/admin/ontology", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403


def test_admin_facts_disabled_renders_explanatory_empty_state(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_FACTS_ENABLED", raising=False)
    r = seeded_app["client"].get("/admin/ontology", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "facts.enabled" in r.text
    # The builder shell itself must not render when the feature is off.
    assert 'id="ont-app"' not in r.text


def test_admin_facts_enabled_duckdb_backend_renders_postgres_empty_state(seeded_app, monkeypatch):
    """Draft persistence is PG-only (A3 ratchet) -- on a DuckDB-backed
    instance the page must say so, not crash or render a dead-end builder."""
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    r = seeded_app["client"].get("/admin/ontology", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "Postgres" in r.text
    assert 'id="ont-app"' not in r.text


def test_semantic_layer_page_links_to_ontology_builder(seeded_app):
    """The ONLY entry point (spec §13.2: "zero new navigation")."""
    r = seeded_app["client"].get("/admin/semantic-layer", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "/admin/ontology" in r.text


def test_template_extends_admin_page_base():
    assert '{% extends "base_admin_page.html" %}' in _TEMPLATE_SRC


def test_template_save_is_the_only_write_comment_present():
    """Documentation smoke check: the Save-only-write invariant is stated in
    the template itself, not just in code review memory."""
    assert "Save is the only write" in _TEMPLATE_SRC


# ---------------------------------------------------------------------------
# Section 3 (relationship types) — a description field, mirroring entity types
# ---------------------------------------------------------------------------


def test_relationship_row_renders_a_description_field():
    """Entity types (section 2) have a description input; relationship types
    (section 3) must too -- otherwise the only way to explain what an edge
    MEANS is to mislabel it on an entity's description, which is the wrong
    place and does not reach the extraction prompt for the edge itself."""
    js = _script_source()
    esc_fn = _slice(js, "function esc(s) {", "function setStatus(")
    row_fn = _slice(js, "function _edgeRowHtml(", "function renderEdgeRows(")
    script = (
        esc_fn
        + row_fn
        + """
    var withDescription = _edgeRowHtml('owned_by', {src: 'a', dst: 'b', description: 'who financially controls whom'});
    var withoutDescription = _edgeRowHtml('related_to', {src: 'a', dst: 'b'});
    process.stdout.write(JSON.stringify({withDescription: withDescription, withoutDescription: withoutDescription}));
    """
    )
    res = json.loads(_node_run(script))
    assert 'data-field="description"' in res["withDescription"]
    assert "who financially controls whom" in res["withDescription"]
    # No description set -> an empty field, never the literal string "undefined".
    assert 'data-field="description" value=""' in res["withoutDescription"]


def test_relationship_description_edit_is_wired_to_the_draft():
    """Structural pin (DOM-touching wiring, no jsdom in this repo): editing
    the description input must write ``draft.edge_types[name].description``,
    the same shape ``_draft_to_ontology_dict``/``translate_ontology`` already
    read on the backend."""
    js = _script_source()
    render_fn = _slice(
        js,
        "function renderEdgeRows(",
        "document.addEventListener('DOMContentLoaded', function () {\n    document.getElementById('ont-add-edge')",
    )
    assert 'data-field="description"' in render_fn
    assert "draft.edge_types[name].description = e.target.value" in render_fn


# ---------------------------------------------------------------------------
# Section 4 (document sample) — adding a document never fails silently
# ---------------------------------------------------------------------------


def test_unwrap_list_response_reads_the_envelope_key_not_the_bare_array():
    """``GET /api/collections`` returns ``{"items": [...]}`` and
    ``GET /api/collections/{id}/files`` returns ``{"files": [...]}``
    (``app/api/collections.py``) -- never a bare array. Treating the
    response as a bare array makes ``Array.prototype.map`` throw inside an
    unawaited ``.then()``, so the picker silently falls back to an error
    state (or, for the per-collection file list, throws with no fallback at
    all) no matter which document is picked -- reproducing the reported
    "nothing happens" with a document add."""
    js = _script_source()
    fn = _slice(js, "function _unwrapListResponse(", "function loadCollections(")
    script = (
        fn
        + """
    process.stdout.write(JSON.stringify({
      wrappedItems: _unwrapListResponse({items: [1, 2, 3]}, 'items'),
      wrappedFiles: _unwrapListResponse({files: ['a']}, 'files'),
      okFalseFallback: _unwrapListResponse([], 'items'),
      nullish: _unwrapListResponse(null, 'items'),
    }));
    """
    )
    res = json.loads(_node_run(script))
    assert res["wrappedItems"] == [1, 2, 3]
    assert res["wrappedFiles"] == ["a"]
    assert res["okFalseFallback"] == []
    assert res["nullish"] == []


def test_load_collections_and_files_for_collection_use_the_unwrap_helper():
    """Structural pin: it is not enough for the helper to exist correctly --
    both call sites that hit the real envelope-shaped endpoints must actually
    use it, or the pure-function test above would pass while the real bug
    stays live."""
    js = _script_source()
    load_collections = _slice(js, "function loadCollections(", "function filesForCollection(")
    files_for_collection = _slice(js, "function filesForCollection(", "function wireDocumentSample(")
    assert "_unwrapListResponse(rows, 'items')" in load_collections
    assert "_unwrapListResponse(rows, 'files')" in files_for_collection


def test_sample_duplicate_matches_by_collection_and_file_id_not_by_name():
    """Two distinct files can share a filename (different paths in the same
    collection) -- the duplicate check must key on the real identifier
    (collection_id + file_id), never on the display name, or two genuinely
    different documents would look like the same one."""
    js = _script_source()
    fn = _slice(js, "function sampleDuplicate(", "function wireDocumentSample(")
    script = (
        fn
        + """
    var sample = [
      {collection_id: 'c1', file_id: 'f1', name: 'col / report.md'},
      {collection_id: 'c1', file_id: 'f2', name: 'col / report.md'},
    ];
    process.stdout.write(JSON.stringify({
      sameNameDifferentFile: sampleDuplicate(sample, 'c1', 'f3'),
      trueDuplicate: sampleDuplicate(sample, 'c1', 'f1'),
      otherCollection: sampleDuplicate(sample, 'c2', 'f1'),
      emptySample: sampleDuplicate(null, 'c1', 'f1'),
    }));
    """
    )
    res = json.loads(_node_run(script))
    assert res["sameNameDifferentFile"] is None, "a third file with the same name as an existing one is not a duplicate"
    assert res["trueDuplicate"]["file_id"] == "f1"
    assert res["otherCollection"] is None
    assert res["emptySample"] is None


def test_add_doc_handler_toasts_instead_of_silently_doing_nothing():
    """A rejected add (nothing picked, or a real duplicate) must tell the
    operator -- the reported bug was that the sample count simply did not
    change, with no error and no toast."""
    js = _script_source()
    handler = _slice(
        js, "document.getElementById('ont-add-doc').addEventListener('click'", "function renderSampleList("
    )
    assert handler.count("toast(") >= 2, "both the missing-selection and the duplicate branch must toast"
    assert "sampleDuplicate(" in handler


# ---------------------------------------------------------------------------
# Create tab — Import is never a silent no-op, and the file-read race is closed
# ---------------------------------------------------------------------------


def test_import_validation_message_for_empty_paste():
    js = _script_source()
    fn = _slice(js, "function _importValidationError(", "function wireCreateTab(")
    script = (
        fn
        + """
    process.stdout.write(JSON.stringify({
      empty: _importValidationError(''),
      whitespace: _importValidationError('   \\n  '),
      missing: _importValidationError(undefined),
      real: _importValidationError('node_types: {}'),
    }));
    """
    )
    res = json.loads(_node_run(script))
    assert res["empty"], "pressing Import on an empty textarea must say something"
    assert res["whitespace"]
    assert res["missing"]
    assert res["real"] is None


def test_import_click_handler_toasts_instead_of_silently_returning():
    js = _script_source()
    handler = _slice(
        js,
        "document.getElementById('ont-import-btn').addEventListener('click'",
        "Array.prototype.forEach.call(document.querySelectorAll('.ont-tab')",
    )
    assert "_importValidationError(" in handler
    assert "toast(" in handler
    # The old bug: `if (!text.trim()) return;` with nothing else -- guard
    # against a silent early return sneaking back in.
    assert "if (!text.trim()) return;" not in handler


def test_file_read_disables_import_until_the_read_completes():
    """A user who picks a file and immediately clicks Import can otherwise
    race the asynchronous FileReader and hit the empty-textarea path for a
    file that has not finished loading yet."""
    js = _script_source()
    handler = _slice(
        js,
        "document.getElementById('ont-file').addEventListener('change'",
        "document.getElementById('ont-import-btn').addEventListener('click'",
    )
    disable_at = handler.index("importBtn.disabled = true")
    read_at = handler.index("reader.readAsText")
    onload_at = handler.index("reader.onload")
    assert disable_at < read_at, "Import must be disabled BEFORE the async read starts, not after"
    assert "importBtn.disabled = false" in handler[onload_at:]
