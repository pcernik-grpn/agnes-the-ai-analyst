"""A warehouse-only metric's "not locally runnable" note reaches both reading
surfaces.

``src/semantic/dialect.py::resolve_expression_any`` lets the projector keep a
metric whose only expression is warehouse-flavour SQL (SNOWFLAKE, DATABRICKS,
…) rather than dropping it from the catalog — and stamps
``metric_definitions.notes`` with the caveat that it cannot run through a
local DuckDB query. That caveat only does its job if a reader actually sees
it: a metric that reads as ordinary, runnable SQL is exactly the trap the
note exists to prevent, and there is no ``query_mode`` column on
``metric_definitions`` to carry the fact any other way.

Two reading surfaces, both pinned here from the REAL projector output rather
than a hand-written ``notes`` list, so a change to the note's wording or to
where it is stamped fails here rather than silently rendering nothing:

- ``agnes catalog --metrics --show <id>`` (``cli/commands/catalog.py``) —
  where ``CLAUDE.md``'s agent rails send an agent for the canonical
  definition, under "Never invent metric SQL".
- ``GET /catalog/semantics`` (``app/web/templates/catalog_semantics.html``).
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from cli.commands.catalog import catalog_app
from src.semantic.projection import project_document

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# The only expression is warehouse-flavour: no DUCKDB, no ANSI_SQL.
WAREHOUSE_ONLY_DOC = {
    "semantic_model": [
        {
            "name": "retail",
            "datasets": [{"name": "orders", "source": "db.public.orders"}],
            "metrics": [
                {
                    "name": "wh_only",
                    "description": "Revenue, warehouse dialect only.",
                    "expression": {
                        "dialects": [{"dialect": "SNOWFLAKE", "expression": "SUM(TRY_CAST(amount AS NUMBER))"}]
                    },
                }
            ],
        }
    ]
}


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _project_and_fetch() -> dict:
    """Project the document and hand back the row the readers below get."""
    from src.repositories import metric_repo

    project_document(WAREHOUSE_ONLY_DOC, source="ossie_git", source_ref="repo-a")
    rows = [m for m in metric_repo().list() if (m.get("source") or "") == "ossie_git"]
    assert len(rows) == 1, rows
    return dict(rows[0])


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


class TestTheNoteIsStamped:
    def test_the_projected_row_carries_it(self, e2e_env):
        row = _project_and_fetch()
        notes = list(row.get("notes") or [])
        assert notes, row
        assert any("SNOWFLAKE" in n and "not locally runnable" in n for n in notes)
        # The metric is present, not skipped — that is the whole point of
        # projecting a warehouse-only expression at all.
        assert "TRY_CAST" in row["sql"]


class TestCliShowRendersIt:
    def test_the_note_reaches_agnes_catalog_metrics_show(self, e2e_env, monkeypatch):
        """The row the server would return is fed to the REAL CLI renderer, so
        this fails if either the projector stops stamping the note or the
        renderer stops printing ``notes``."""
        import cli.commands.catalog as catalog_mod

        row = _project_and_fetch()
        monkeypatch.setattr(catalog_mod, "api_get", lambda path: _FakeResponse(row))

        result = CliRunner().invoke(catalog_app, ["--metrics", "--show", row["id"]])

        assert result.exit_code == 0, result.output
        out = _clean(result.output)
        assert "Notes:" in out
        assert "not locally runnable" in out
        assert "SNOWFLAKE" in out

    def test_the_api_hands_the_note_over(self, seeded_app):
        """The CLI test above stubs the transport; this one proves the payload
        it stubs is the payload the endpoint really sends."""
        row = _project_and_fetch()
        c = seeded_app["client"]

        resp = c.get(
            f"/api/metrics/{row['id']}",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        )

        assert resp.status_code == 200, resp.text
        notes = resp.json().get("notes") or []
        assert any("not locally runnable" in n for n in notes), notes


class TestWebCatalogSemanticsRendersIt:
    def test_the_note_reaches_the_catalog_semantics_page(self, seeded_app):
        _project_and_fetch()
        c = seeded_app["client"]

        resp = c.get(
            "/catalog/semantics",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        )

        assert resp.status_code == 200, resp.text
        html = resp.text
        assert "not locally runnable" in html
        assert "SNOWFLAKE" in html
        # Rendered as a Notes list item, not dumped as a repr of the column.
        assert "<strong>Notes:</strong>" in html
        assert "['SNOWFLAKE" not in html
