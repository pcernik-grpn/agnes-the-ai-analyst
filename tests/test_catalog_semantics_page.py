"""GET /catalog/semantics — read-only browser for the semantic layer

(business metrics from `metric_definitions` + the glossary from
`glossary_terms`, both already shipped via `GET /api/metrics` and
`GET /api/glossary*`). Analyst-facing tier (get_current_user, no admin
gate) — mirrors the RBAC tier of the underlying REST endpoints and of
/catalog itself. Picks up issue #853 plus the glossary.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_metric(**overrides) -> dict:
    from src.repositories import metric_repo

    defaults = {
        "id": "revenue/mrr",
        "name": "mrr",
        "display_name": "Monthly Recurring Revenue",
        "category": "revenue",
        "sql": "SELECT SUM(mrr_amount) AS mrr FROM subscriptions",
        "description": "Total MRR from active subscriptions.",
    }
    defaults.update(overrides)
    return metric_repo().create(**defaults)


def _make_term(**overrides) -> dict:
    from src.repositories import glossary_repo

    defaults = {
        "id": "kb/m/churn",
        "term": "Churn Rate",
        "definition": "Percent of customers lost in a period.",
    }
    defaults.update(overrides)
    return glossary_repo().create(**defaults)


class TestCatalogSemanticsAuth:
    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/catalog/semantics", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)

    def test_analyst_can_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200

    def test_admin_can_also_load_page(self, seeded_app):
        """Not admin-gated (matches GET /api/metrics / GET /api/glossary — both
        get_current_user-only), but an admin should be able to load it too."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200


class TestCatalogSemanticsContent:
    def test_tabs_and_key_content_present(self, seeded_app):
        _make_metric()
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        # Tab strip — page-local .sl-tab buttons (redesign replaced .tab-strip).
        assert 'class="sl-tab' in body
        assert 'data-tab="metrics"' in body
        assert 'data-tab="glossary"' in body
        assert "Metrics" in body
        assert "Glossary" in body

        # Server-rendered metrics list: category grouping + row content.
        assert "revenue" in body
        assert "Monthly Recurring Revenue" in body
        assert "Total MRR from active subscriptions." in body

        # Client-side filter input for metrics (no new search endpoint).
        assert 'id="metric-filter"' in body

        # Glossary search input, wired to the existing search endpoint.
        assert 'id="glossary-search"' in body
        assert "/api/glossary/search" in body
        assert "/api/glossary" in body

    def test_metrics_grouped_by_category(self, seeded_app):
        _make_metric(id="revenue/mrr", name="mrr", category="revenue")
        _make_metric(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            sql="SELECT COUNT(DISTINCT user_id) FROM events",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        assert "revenue" in body
        assert "engagement" in body
        assert "Daily Active Users" in body

    def test_join_tag_shown_for_relationship_metrics(self, seeded_app):
        from src.db import get_system_db
        from src.repositories import table_registry_repo
        from tests.conftest import grant_table_via_package

        conn = get_system_db()
        for tid in ("orders", "order_items"):
            table_registry_repo().register(
                id=tid,
                name=tid,
                description="test table",
                source_type="keboola",
                query_mode="materialized",
            )
            grant_table_via_package(conn, tid, "analyst1")
        conn.close()

        _make_metric(
            id="sales/attach_rate",
            name="attach_rate",
            display_name="Attach Rate",
            category="sales",
            tables=["orders", "order_items"],
            sql="SELECT * FROM orders JOIN order_items USING (order_id)",
        )
        _make_metric(
            id="sales/order_count",
            name="order_count",
            display_name="Order Count",
            category="sales",
            table_name="orders",
            sql="SELECT COUNT(*) FROM orders",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        assert ">JOIN<" in body

    def test_accordion_detail_has_full_sql_and_extras(self, seeded_app):
        full_sql = "SELECT DATE_TRUNC('month', billing_date) AS m, SUM(mrr_amount) AS mrr FROM subscriptions GROUP BY 1"
        _make_metric(
            sql=full_sql,
            synonyms=["monthly_revenue"],
            notes=["Excludes one-time fees"],
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        # Jinja HTML-escapes the SQL (correct — it renders inside <code>);
        # single quotes come out as &#39;, everything else round-trips as-is.
        assert full_sql.replace("'", "&#39;") in body
        assert "monthly_revenue" in body
        assert "Excludes one-time fees" in body
        # No modal JS/CSS reused — this page builds its own accordion.
        assert "metric_modal.css" not in body
        assert "metric_modal.js" not in body

    def test_source_badge_mapping(self, seeded_app):
        _make_metric(id="a/1", name="a1", category="a", source="manual")
        _make_metric(id="a/2", name="a2", category="a", source="yaml_import")
        _make_metric(id="a/3", name="a3", category="a", source="openmetadata")
        _make_metric(id="a/4", name="a4", category="a", source="keboola_semantic_layer")
        _make_metric(id="a/5", name="a5", category="a", source="some_future_source")
        # The post-flat-table-cutover Keboola writer source must badge exactly
        # like the retired one (src.semantic.keboola_sources).
        _make_metric(id="a/6", name="a6", category="a", source="keboola_metastore")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        # 4-slot vocabulary: keboola_semantic_layer/keboola_metastore ->
        # success, yaml_import -> info, openmetadata -> warn, manual + unknown
        # -> neutral (no accent). One extra "badge--success" occurrence comes
        # from the client-side `sourceBadge()` JS literal, not a rendered row.
        assert body.count("badge--success") == 3
        assert "badge--info" in body
        assert "badge--warn" in body

    def test_glossary_client_fetch_limit_matches_server_count_limit(self, seeded_app):
        """The tab label's initial count comes from glossary_repo().list(limit=500)
        (app/web/router.py); the client re-fetch on tab-open must use the same
        limit, or the displayed count silently shrinks from up to 500 to
        whatever the client asked for once the user opens the Glossary tab."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        body = resp.text
        assert "/api/glossary?limit=500" in body
        assert "/api/glossary?limit=200" not in body


class TestCatalogSemanticsRBAC:
    """Metric visibility on this page must match `GET /api/metrics` — a
    metric whose table(s) the analyst can't access via their Data Package
    stack must not be server-rendered here either (#953 security fix)."""

    def _register_table(self, table_id: str, table_name: str | None = None):
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id=table_id,
            name=table_name or table_id,
            description="test table",
            source_type="keboola",
            query_mode="materialized",
        )

    def _grant(self, table_id: str, user_id: str = "analyst1"):
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        conn = get_system_db()
        grant_table_via_package(conn, table_id, user_id)
        conn.close()

    def test_analyst_without_grant_does_not_see_metric_or_category(self, seeded_app):
        self._register_table("orders_tbl")
        _make_metric(
            id="finance/orders_total",
            name="orders_total",
            category="finance_only",
            table_name="orders_tbl",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "orders_total" not in body
        # The category has zero visible metrics — its header must not render.
        assert "finance_only" not in body

    def test_analyst_with_grant_sees_metric(self, seeded_app):
        self._register_table("orders_tbl2")
        self._grant("orders_tbl2")
        _make_metric(
            id="finance/orders_total2",
            name="orders_total2",
            category="finance_only2",
            table_name="orders_tbl2",
        )
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "orders_total2" in body
        assert "finance_only2" in body

    def test_admin_sees_metrics_regardless_of_stack(self, seeded_app):
        self._register_table("orders_tbl3")
        _make_metric(
            id="finance/orders_total3",
            name="orders_total3",
            category="finance_only3",
            table_name="orders_tbl3",
        )
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "orders_total3" in body


class TestCatalogSemanticsLinkFromCatalog:
    def test_library_carries_the_door_to_semantics(self):
        """The Definitions block in the Library is the way in.

        This asked /catalog for a rendered link, which the classic template's
        Semantic layer card supplied. The unified Catalog offers only what the
        caller does not already have, and the semantic layer is not one of
        those — the rail's own IA note names the Library's Definitions block as
        the door instead.

        Asserted on the template rather than on a render because the block is
        conditional on the instance having definitions to show, and seeding
        metrics + glossary entries here would be testing the Library's empty
        state rather than the presence of the link."""
        from pathlib import Path

        library = Path("app/web/templates/library.html").read_text(encoding="utf-8")
        assert "/catalog/semantics" in library, (
            "library.html no longer links /catalog/semantics — under the rail that "
            "block is the page's entry point (see the IA note in _app_rail.html)"
        )


class TestCatalogSemanticsWayOut:
    """The page is link-only — reached from the Library's Definitions block,
    the Catalog's Semantic layer card, a chat citation or global search — and
    is a nav destination in neither chrome. Without a back link the browser's
    Back button was the only way out, and under the rail no nav item lit up
    either, so the chrome read as "nowhere"."""

    def _body(self, seeded_app) -> str:
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        return resp.text

    def test_back_link_returns_to_the_library(self, seeded_app):
        """The way out. It pointed at /catalog while the classic Catalog page
        carried the card that linked here; the Library's Definitions block is
        that door now, so the back link follows it — a back link to a page that
        no longer offers the surface is the "nowhere" this class exists about."""
        body = self._body(seeded_app)
        assert 'class="sl-back"' in body
        assert 'href="/library' in body

    def test_rail_back_link_returns_to_the_definitions_block(self, seeded_app, monkeypatch):
        # /library is the rail's one browse surface and carries the block the
        # reader clicked; /catalog is not in the rail nav at all. The ANCHOR is
        # the point: the Definitions block closes /library below the whole
        # inventory, so a bare /library returns them to the top of the page
        # with everything they own between them and where they were.
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        body = self._body(seeded_app)
        assert '<a class="sl-back" href="/library#lib-defs">' in body
        assert '<a class="sl-back" href="/catalog">' not in body

    def test_the_anchor_the_back_link_targets_exists_on_the_library(self, seeded_app, monkeypatch):
        # A back link into an id no page emits is a link to the top of that
        # page — indistinguishable from the bare /library it replaced, and
        # silently so. Pin the two ends together.
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        # The block renders under `if definitions_footer` — set only when the
        # caller can see at least one metric or term, which is also the only
        # state in which they could have clicked through from it.
        _make_metric()
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        lib = c.get("/library", headers=_auth(token))
        assert lib.status_code == 200
        assert 'id="lib-defs"' in lib.text

    def test_rail_highlights_library_while_on_this_page(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        body = self._body(seeded_app)
        # The Library rail item carries `on` — the same active class the rail
        # gives /library itself.
        assert 'class="rail-i on" href="/library" id="nav-artefacts"' in body


class TestCatalogSemanticsDetailRendering:
    """The expanded detail renders the full definition (description as
    sanitized markdown, a type/unit/grain meta line, and dimensions), and
    the row preview / filter index are plain-text projections (no literal
    markdown markup, synonyms searchable)."""

    def _page(self, seeded_app) -> str:
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        return resp.text

    def test_description_markdown_rendered_in_detail(self, seeded_app):
        _make_metric(
            description="**Bold definition** with `inline_code` term.",
        )
        body = self._page(seeded_app)
        assert "<strong>Bold definition</strong>" in body
        assert "<code>inline_code</code>" in body
        # Raw markdown markup must not appear anywhere (preview or detail).
        assert "**Bold definition**" not in body

    def test_preview_is_plain_text_with_block_boundaries(self, seeded_app):
        import re

        _make_metric(
            description="## Heading Alpha\n\nFirst paragraph beta.",
        )
        body = self._page(seeded_app)
        m = re.search(r'<div class="sl-row__desc">([^<]*)</div>', body)
        assert m, "plain-text preview div missing"
        preview = m.group(1)
        assert "Heading Alpha" in preview
        assert "First paragraph beta." in preview
        # Adjacent blocks must not fuse into "AlphaFirst".
        assert "AlphaFirst" not in preview
        assert "#" not in preview

    def test_description_is_sanitized(self, seeded_app):
        _make_metric(
            description="[click](javascript:alert(1)) <script>alert(2)</script>",
        )
        body = self._page(seeded_app)
        assert 'href="javascript:' not in body
        assert "<script>alert(2)</script>" not in body

    def test_html_blob_description_does_not_leak_tags_into_the_preview(self, seeded_app):
        """A metric imported from OpenMetadata stores rich HTML in the same
        column a hand-authored one uses for markdown. Rendered as pure
        markdown, the blob was escaped into entities — leaving the tag-strip
        nothing to remove — and then unescaped back, so the analyst read the
        characters `<p><strong>` in the preview."""
        import re

        _make_metric(
            description="<p><strong>Live Deals</strong> - deals currently live.</p>",
            source="keboola_semantic_layer",
        )
        body = self._page(seeded_app)
        m = re.search(r'<div class="sl-row__desc">([^<]*)</div>', body)
        assert m, "plain-text preview div missing"
        preview = m.group(1)
        assert "Live Deals - deals currently live." in preview
        assert "&lt;" not in preview and "&gt;" not in preview

    def test_html_blob_description_renders_as_markup_in_the_detail(self, seeded_app):
        """Same input, other projection: the detail shows bold text rather
        than the literal characters of the tag."""
        _make_metric(
            description="<p><strong>Live Deals</strong> - deals currently live.</p>",
            source="keboola_semantic_layer",
        )
        body = self._page(seeded_app)
        assert "<strong>Live Deals</strong>" in body
        assert "&lt;strong&gt;" not in body

    def test_html_blob_description_is_still_sanitized(self, seeded_app):
        """Accepting HTML from the source widens what is displayed, never
        what is allowed — the nh3 allowlist is the same one."""
        _make_metric(
            description='<p onclick="steal()">hi</p><script>alert(3)</script>',
            source="keboola_semantic_layer",
        )
        body = self._page(seeded_app)
        assert "onclick" not in body
        assert "alert(3)" not in body

    def test_meta_line_shows_type_unit_grain_and_dimensions(self, seeded_app):
        _make_metric(
            type="ratio",
            unit="percentage",
            grain="session-week",
            dimensions=["Country", "Traffic Source"],
        )
        body = self._page(seeded_app)
        assert "ratio" in body
        assert "percentage" in body
        assert "session-week" in body
        assert "Country, Traffic Source" in body

    def test_filter_index_includes_synonyms(self, seeded_app):
        import re

        _make_metric(
            synonyms=["average order value", "AOV"],
        )
        body = self._page(seeded_app)
        m = re.search(r'data-ft="([^"]*)"', body)
        assert m, "filter index attribute (data-ft) missing"
        idx = m.group(1)
        assert "average order value" in idx
        assert "aov" in idx


class TestCatalogSemanticsSidebarLayout:
    """#1207: a bare `nav { display: flex; … }` in style-custom.css was
    written for the header's primary nav but applied to every `<nav>` in the
    app, including `.sl-cat-nav` here — turning the category list into a
    horizontal row that got clipped by `.sl-sidebar-body`'s `overflow:
    hidden`, so a populated semantic layer's sidebar rendered blank. Static
    CSS check (no `seeded_app`) so it stays independent of the page's actual
    render."""

    def test_sl_cat_nav_declares_block_layout(self):
        import re
        from pathlib import Path

        css = (Path("app/web/templates/catalog_semantics.html")).read_text(encoding="utf-8")
        m = re.search(r"\.sl-cat-nav\s*\{([^}]*)\}", css)
        assert m, ".sl-cat-nav rule not found in catalog_semantics.html"
        body = m.group(1)
        assert re.search(r"display\s*:\s*block\b", body), (
            ".sl-cat-nav must declare `display: block` so its category buttons "
            "stack vertically instead of inheriting the global `nav` flex-row layout"
        )


def test_every_heading_the_allowlist_admits_is_styled_in_the_detail():
    """A preserved tag with no rule falls back to the browser default.

    The `html_source` allowlist keeps `h1`/`h5`/`h6` because dropping them
    fused the sections they separated. But `.sl-detail__desc` styled only
    `h2, h3`, so an imported `<h1>` rendered at ~2em with large margins inside
    a compact metric row — and out-shouted the page's own `<h1>Semantic
    layer</h1>` in the document outline. Preserving structure and sizing it
    are two halves of the same change.
    """
    from pathlib import Path

    import app.markdown_render as mr

    tpl = Path("app/web/templates/catalog_semantics.html").read_text(encoding="utf-8")
    admitted = {
        t for t in (mr._ALLOWED_TAGS | mr._HTML_SOURCE_EXTRA_TAGS) if len(t) == 2 and t[0] == "h" and t[1].isdigit()
    }
    assert admitted, "expected the allowlists to admit heading tags"
    missing = [h for h in sorted(admitted) if f".sl-detail__desc {h}" not in tpl]
    assert not missing, (
        f"headings admitted by the allowlist but unstyled in .sl-detail__desc: {missing} — "
        "they will render at browser-default size inside a compact metric row"
    )


class TestCatalogSemanticsDetailCompleteness:
    """Every stored field of a metric definition reaches the detail.

    The four below were carried by `metric_definitions` and by the importer but
    never rendered, so the page showed a metric's *generated* SQL while hiding
    the upstream `expression` it was composed from — the field an analyst opens
    the detail to read.
    """

    def _page(self, seeded_app) -> str:
        c = seeded_app["client"]
        resp = c.get("/catalog/semantics", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        return resp.text

    def test_expression_is_shown(self, seeded_app):
        """The Keboola semantic-layer import stores it on every metric it
        writes (connectors/keboola/semantic_layer.py), and eleven of the
        bundled YAML metrics carry one."""
        _make_metric(expression="SUM(mrr_amount) / COUNT(DISTINCT account_id)")
        body = self._page(seeded_app)
        assert "SUM(mrr_amount) / COUNT(DISTINCT account_id)" in body

    def test_time_column_is_shown(self, seeded_app):
        _make_metric(time_column="billing_date")
        assert "billing_date" in self._page(seeded_app)

    def test_filters_are_shown(self, seeded_app):
        _make_metric(filters=["status = 'active'", "region IS NOT NULL"])
        body = self._page(seeded_app)
        assert "status = &#39;active&#39;" in body
        assert "region IS NOT NULL" in body

    def test_sql_variants_are_shown(self, seeded_app):
        """Stored as a dict of variant name -> SQL; each needs its own labelled
        block, not a dumped repr."""
        _make_metric(sql_variants={"quarter": "SELECT 1 AS quarterly", "region": "SELECT 2 AS by_region"})
        body = self._page(seeded_app)
        assert "quarter" in body and "SELECT 1 AS quarterly" in body
        assert "region" in body and "SELECT 2 AS by_region" in body
        assert "{&#39;quarter&#39;:" not in body, "rendered as a python repr rather than per-variant blocks"

    def test_a_metric_without_them_renders_no_empty_labels(self, seeded_app):
        """Every one is optional — absent fields must not leave dangling
        headings behind."""
        _make_metric()
        body = self._page(seeded_app)
        for label in ("Expression", "Time column", "Filters", "Variants"):
            assert f"<strong>{label}</strong>" not in body


def test_web_uploaded_metrics_are_a_distinct_writer(seeded_app):
    """`POST /api/admin/metrics/import` must not stamp the same `source` the
    CLI import uses: `agnes admin metrics import --prune` deletes rows in that
    scope which its directory no longer lists, and an uploaded metric is in no
    directory. Sharing the value made hand-uploaded metrics collateral."""
    import io

    c, token = seeded_app["client"], seeded_app["admin_token"]
    resp = c.post(
        "/api/admin/metrics/import",
        files={"file": ("m.yml", io.BytesIO(b"name: uploaded\ncategory: ops\nsql: SELECT 1\n"), "text/yaml")},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    from src.repositories import metric_repo

    row = metric_repo().get("ops/uploaded")
    assert row is not None
    assert row["source"] == "web_upload", "an upload must not claim to be the CLI's yaml_import"


#: The door itself, asserted as a LINK — the page's own CSS legitimately
#: names the path in a comment, so a bare substring check would pass on
#: styling alone and never notice a missing anchor.
_DOOR = 'href="/semantic-layer"'


class TestCatalogSemanticsDoorToTheDocument:
    """This page renders the FLAT projection (`metric_definitions` +
    `glossary_terms`); the stored Ossie document itself is browsed at
    `/semantic-layer`. Both are titled "Semantic layer", and this is the more
    reachable of the two, so a model with datasets and relationships but no
    metrics rendered "No metrics registered yet" here with nothing pointing at
    the document — the page read as "there is no semantic layer" while there
    was one. `/library`'s Definitions block already offers the door on exactly
    this condition (`has_semantic_models`); the standalone page did not.

    Gated on the same `_can_read_model` tier as the browse pages, NOT on the
    projection's counts: a caller who can read no model must not be sent to an
    empty page.
    """

    def _seed_model(self, *, slug: str = "retail", status: str = "valid") -> dict:
        from src.repositories import semantic_model_repo

        return semantic_model_repo().upsert(
            id=f"manual/_/{slug}",
            slug=slug,
            name=slug,
            description="Retail domain: orders and customers.",
            document="# fixture",
            document_json={
                "semantic_model": [
                    {
                        "name": slug,
                        "datasets": [{"name": "orders", "source": "db.public.orders", "fields": []}],
                    }
                ]
            },
            spec_version="0.2.0.dev0",
            content_hash=f"hash-{slug}",
            source="manual",
            source_ref=None,
            status=status,
            validation_errors=None,
            validated_at=None,
        )

    def _body(self, seeded_app, token_key: str) -> str:
        resp = seeded_app["client"].get("/catalog/semantics", headers=_auth(seeded_app[token_key]))
        assert resp.status_code == 200
        return resp.text

    def test_no_door_when_the_instance_has_no_model(self, seeded_app):
        body = self._body(seeded_app, "admin_token")
        assert _DOOR not in body, (
            "offered the browse page with no model to browse — the link must be "
            "gated on a readable document, not rendered unconditionally"
        )

    def test_door_is_offered_when_a_readable_model_exists(self, seeded_app):
        self._seed_model()
        body = self._body(seeded_app, "admin_token")
        assert _DOOR in body

    def test_empty_state_names_the_document_instead_of_only_the_import_command(self, seeded_app):
        """The exact confusion this fixes: zero metrics is the DEFAULT state of
        a converted document (an upstream model may declare datasets and
        relationships and no aggregations at all), so the empty state must
        explain the split rather than imply nothing was imported."""
        self._seed_model()
        body = self._body(seeded_app, "admin_token")
        assert "No metrics registered yet" in body
        assert _DOOR in body
        assert "agnes admin metrics import docs/metrics/" in body, (
            "the import hint is still the right next step for an instance that "
            "wants metrics — the document link is added beside it, not instead of it"
        )

    def test_analyst_without_a_grant_is_not_sent_to_a_page_they_cannot_read(self, seeded_app):
        """`_can_read_model` is not admin-only, but it is not open either: an
        analyst with neither a direct model grant nor a Data Package grant
        would get a 404/empty browse page, so they must not see the door."""
        self._seed_model()
        body = self._body(seeded_app, "analyst_token")
        assert _DOOR not in body
