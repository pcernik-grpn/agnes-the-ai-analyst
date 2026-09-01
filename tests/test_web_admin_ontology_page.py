"""GET /admin/ontology (fact-graph-over-Collections §13.2, "Ontology builder").

Zero new navigation: reachable only via the link on /admin/semantic-layer,
no admin-nav entry of its own. When the `facts` flag is off, or the active
backend is DuckDB (drafts are PG-only), the page renders an explanatory
empty state rather than 404ing or crashing -- same posture as /apps when
data_apps is disabled.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_TEMPLATE_SRC = Path("app/web/templates/ontology_builder.html").read_text(encoding="utf-8")


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
# Document-sample collection picker: `GET /api/collections` returns
# `{"items": [...]}`, not a bare array -- the picker's fetch handler must not
# assume the shape. Run for real under `node` (same pattern as
# tests/test_admin_data_sources_page.py's TestSemanticLayerCellNoTokenAction)
# rather than merely grepping the source, since the whole bug was a shape
# assumption that string assertions alone would not have caught either.
# ---------------------------------------------------------------------------


def _extract_function(tpl: str, signature: str) -> str:
    start = tpl.index(signature)
    depth = 0
    started = False
    for i in range(start, len(tpl)):
        ch = tpl[i]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return tpl[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {signature!r}")


#: A DOM stub minimal enough for `loadCollections`/`filesForCollection`:
#: `document.getElementById` hands back a plain object with a settable
#: `innerHTML`, and `fetch` is routed by URL so both the collections list
#: and the per-collection files list can be exercised. `console.error` is
#: captured (not silenced) so a test can assert the failure path actually
#: logs, instead of swallowing the underlying error the way the bug did.
_HARNESS = """
const domStore = {};
function elFor(id) {
  if (!domStore[id]) domStore[id] = { id, innerHTML: '' };
  return domStore[id];
}
const document = { getElementById(id) { return elFor(id); } };
const consoleErrors = [];
console.error = function () {
  consoleErrors.push(Array.prototype.slice.call(arguments).map(String).join(' '));
};

let _collectionsResult = null;   // { ok, body } | { reject: true }
let _filesResult = null;         // { ok, body } | { reject: true }
function fetch(url) {
  if (String(url).indexOf('/api/collections/') === 0) {
    if (_filesResult.reject) return Promise.reject(new Error('network down'));
    return Promise.resolve({ ok: _filesResult.ok, json: () => Promise.resolve(_filesResult.body) });
  }
  if (_collectionsResult.reject) return Promise.reject(new Error('network down'));
  return Promise.resolve({ ok: _collectionsResult.ok, json: () => Promise.resolve(_collectionsResult.body) });
}
"""


def _node_run(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout)


def _run_load_collections(collections_result: dict) -> dict:
    esc_fn = _extract_function(_TEMPLATE_SRC, "function esc(s) {")
    load_fn = _extract_function(_TEMPLATE_SRC, "function loadCollections() {")
    script = (
        _HARNESS
        + f"\n_collectionsResult = {json.dumps(collections_result)};\n"
        + "var collectionsCache = [];\n"
        + esc_fn
        + "\n"
        + load_fn
        + "\nloadCollections().then(function () {\n"
        + "  console.log(JSON.stringify({\n"
        + "    innerHTML: elFor('ont-collection-select').innerHTML,\n"
        + "    errors: consoleErrors,\n"
        + "    cache: collectionsCache,\n"
        + "  }));\n"
        + "});\n"
    )
    return _node_run(script)


def test_load_collections_populates_picker_from_items_shape():
    """`GET /api/collections` returns `{"items": [...]}` -- the real,
    verified-live shape. The regression this guards: treating the response
    as a bare array makes `({}).map` throw, which the `.catch` turns into a
    false "Could not load collections" for a perfectly good 200 response."""
    result = _run_load_collections(
        {
            "ok": True,
            "reject": False,
            "body": {"items": [{"id": "c1", "name": "Handbook"}, {"id": "c2", "name": "Runbooks"}]},
        }
    )
    assert "Could not load collections" not in result["innerHTML"]
    assert '<option value="c1">Handbook</option>' in result["innerHTML"]
    assert '<option value="c2">Runbooks</option>' in result["innerHTML"]
    assert result["cache"] == [
        {"id": "c1", "name": "Handbook", "files": None},
        {"id": "c2", "name": "Runbooks", "files": None},
    ]


def test_load_collections_still_accepts_a_bare_array():
    """Tolerant of both shapes rather than swapping one brittle assumption
    for another -- a bare-array response (e.g. a future/alternate endpoint)
    must keep working too."""
    result = _run_load_collections(
        {"ok": True, "reject": False, "body": [{"id": "c1", "name": "Handbook"}]},
    )
    assert "Could not load collections" not in result["innerHTML"]
    assert '<option value="c1">Handbook</option>' in result["innerHTML"]


def test_load_collections_network_failure_logs_and_shows_honest_message():
    """The `.catch` must not silently swallow the error -- a network failure
    (or any other exception) has to reach `console.error` so the underlying
    cause is visible without opening the network tab, while the user-facing
    message stays an honest "could not load", not a mislabeled success."""
    result = _run_load_collections({"reject": True, "body": None})
    assert "Could not load collections" in result["innerHTML"]
    assert result["errors"], "expected the failure to be logged, not swallowed"


def test_files_for_collection_handles_files_wrapped_shape():
    """`GET /api/collections/{id}/files` returns `{"files": [...]}` -- the
    same response-shape mistake, one call away. Exercised through
    `filesForCollection`, which populates `collectionsCache` from a prior
    (successful) `loadCollections()` call."""
    esc_fn = _extract_function(_TEMPLATE_SRC, "function esc(s) {")
    load_fn = _extract_function(_TEMPLATE_SRC, "function loadCollections() {")
    files_fn = _extract_function(_TEMPLATE_SRC, "function filesForCollection(id) {")
    collections_result = {"ok": True, "reject": False, "body": {"items": [{"id": "c1", "name": "Handbook"}]}}
    files_result = {
        "ok": True,
        "reject": False,
        "body": {"files": [{"file_id": "f1", "filename": "policy.pdf"}]},
    }
    script = (
        _HARNESS
        + f"\n_collectionsResult = {json.dumps(collections_result)};\n"
        + f"_filesResult = {json.dumps(files_result)};\n"
        + "var collectionsCache = [];\n"
        + esc_fn
        + "\n"
        + load_fn
        + "\n"
        + files_fn
        + "\nloadCollections().then(function () { return filesForCollection('c1'); }).then(function (files) {\n"
        + "  console.log(JSON.stringify({ files, errors: consoleErrors }));\n"
        + "});\n"
    )
    result = _node_run(script)
    assert result["files"] == [{"file_id": "f1", "filename": "policy.pdf"}]
