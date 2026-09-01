"""The flat semantic projection — business metrics from `metric_definitions`
plus the glossary from `glossary_terms` (both already shipped via
`GET /api/metrics` and `GET /api/glossary*`). Analyst-facing tier
(get_current_user, no admin gate) — mirrors the RBAC tier of the underlying
REST endpoints and of /catalog itself. Picks up issue #853 plus the glossary.

It used to be its own page at `GET /catalog/semantics`. Since #1707 N5 it is
two TABS of the model list (`/semantic-layer?tab=all_metrics|all_glossary`)
and the old URL is a 308 onto the first of them. Most of this file still
drives the page through `/catalog/semantics` on purpose: following the
redirect is what proves the fold preserved the behaviour rather than
approximating it. Tests that were about the STANDALONE page's shell — its
back link out, its client-side tab row — are gone with the shell; the fold's
own contract (the redirect, the tab strip, the deep link's URL shape) lives
in tests/test_web_semantic_layer_browse.py::TestFlatProjectionTabsFold.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_model(slug: str = "retail", **doc) -> None:
    """A minimal readable semantic model, written straight through the repo so a
    test can assert on the Semantic models tab without fighting the vendored
    Ossie schema's upload shape."""
    from src.repositories import semantic_model_repo

    semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description="Orders and customers.",
        document="# fixture, not schema-authored",
        document_json={"semantic_model": [dict({"name": slug, "datasets": []}, **doc)]},
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


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

        # A segmented control, not link tabs: the three registries are BUCKETS
        # of one filtered set now, so switching one is a client-side split of
        # rows already on the page rather than a page load. `?tab=` still
        # arrives (the 308 above depends on it) and seeds the opening segment.
        assert 'id="sl-tabs"' in body
        assert 'data-own="all_metrics"' in body
        assert 'data-own="all_glossary"' in body
        assert 'data-seg-count="all_metrics"' in body, "the badge moves as you filter"
        # The keys stay `all_*` — they are in bookmarks and in the 308 above —
        # but the LABELS dropped the "All", which said nothing beside a count
        # badge and read as a filter state on tabs that have none.
        assert ">Metrics<" in body
        assert ">Glossary<" in body

        # Server-rendered metrics list: category grouping + row content.
        assert "revenue" in body
        assert "Monthly Recurring Revenue" in body
        assert "Total MRR from active subscriptions." in body

        # Client-side filter input for metrics (no new search endpoint).
        assert 'id="sl-search"' in body

        # Glossary search input, wired to the existing search endpoint. Its own
        # tab is its own REQUEST now — the two panels no longer share a DOM, so
        # the sidebar that belongs to the glossary renders only there.
        from src.repositories import glossary_repo

        glossary_repo().create(id="gl_bench", term="Bench", definition="Unassigned but available time.")
        glossary = c.get("/semantic-layer?tab=all_glossary", headers=_auth(token))
        assert glossary.status_code == 200
        assert 'id="sl-search"' in glossary.text
        # No fetch: the terms are server-rendered like the metrics beside them,
        # which is what lets this tab's sidebar carry a filter at all (#1956
        # item 1) — the server had nothing to build a nav from while the list
        # arrived after the page did.
        assert "/api/glossary/search" not in glossary.text
        assert "/api/glossary?limit=" not in glossary.text
        assert 'class="sl-item"' in glossary.text, "the same row component the metrics use"

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
        # -> neutral (no accent). Two occurrences, one per Keboola row: the
        # third used to come from the client-side `sourceBadge()` JS literal,
        # a second copy of this vocabulary that went with the fetched glossary
        # cards. One definition now, in the template macro.
        assert body.count("badge--success") == 2
        assert "badge--info" in body
        assert "badge--warn" in body

    def test_the_glossary_is_server_rendered_under_one_bound(self, seeded_app):
        """The two-limits hazard this guarded is gone rather than fixed.

        The tab label's count came from ``glossary_repo().list(limit=500)`` while
        the panel was FETCHED on tab-open, so a client limit smaller than the
        server's silently shrank the number the tab had just promised. The terms
        are rendered server-side now, under one bound, so there is no second
        number to disagree and no fetch to keep in step."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        body = c.get("/semantic-layer?tab=all_glossary", headers=_auth(token)).text
        assert "/api/glossary?limit=" not in body
        from app.web.router import _GLOSSARY_COUNT_LIMIT

        assert _GLOSSARY_COUNT_LIMIT == 500


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
        """The Library's Semantic models section is the way in.

        This asked /catalog for a rendered link, which the classic template's
        (now retired) Semantic layer card supplied. The unified Catalog offers
        only what the caller does not already have, and the semantic layer is
        not one of those — the rail's own IA note names the Library as the
        door instead. Since #1707 N3 that door is a NAMED section rather than
        a footer aside, and since N5 it opens the tabs directly.

        Asserted on the template rather than on a render because the section
        is conditional on the instance having definitions to show, and seeding
        metrics + glossary entries here would be testing the Library's empty
        state rather than the presence of the link."""
        from pathlib import Path

        library = Path("app/web/templates/library.html").read_text(encoding="utf-8")
        assert "/semantic-layer?tab=all_metrics" in library, (
            "library.html no longer links the metric registry — under the rail that "
            "section is the page's entry point (see the IA note in _app_rail.html)"
        )
        assert "/catalog/semantics" not in library, (
            "library.html still emits the retired URL — the 308 is for links Agnes "
            "does not control, not for its own emitters"
        )


class TestCatalogSemanticsWayOut:
    """The projection is reached from the Library's Semantic models section, a
    chat citation or global search (the classic Catalog's card is retired, see
    the class above) — and is a nav destination in neither chrome, so without
    something lighting up the chrome reads as "nowhere".

    The standalone page answered that with its own `.sl-back` link out. Since
    the fold there is no standalone page to leave: the projection is a tab of
    the model list, and the tab strip beside it is the way to the rest of the
    surface. What survives from the original bug is the RAIL half — the
    Library item must still light up, or every semantic surface is a
    navigation blank spot."""

    def _body(self, seeded_app) -> str:
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/semantics", headers=_auth(token))
        assert resp.status_code == 200
        return resp.text

    def test_the_definitions_anchor_the_library_script_binds_to_exists(self, seeded_app, monkeypatch):
        # `#lib-defs` is what the Library's own "that word is a definition"
        # search hint binds to (library.html). It was also the back link's
        # target until the fold removed the back link; the id outlives it
        # because the script still needs it, and a silently-renamed id turns
        # the hint off with nothing failing.
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        # The section renders under `if definitions_footer` — set only when the
        # caller can see at least one metric or term.
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
    """#1207 is now structurally impossible here, and this records why.

    A bare `nav { display: flex; … }` in style-custom.css was written for the
    header's primary nav but applied to every `<nav>` in the app, including
    this page's `.sl-cat-nav` — turning the category list into a horizontal row
    that `.sl-sidebar-body`'s `overflow: hidden` then clipped, so a populated
    semantic layer's sidebar rendered blank.

    The sidebar is gone: filtering moved to the shared toolbar, whose menu is a
    `<div role="menu">`. So rather than pin `display: block` on a class that no
    longer exists, this pins the reason the bug cannot return — the page owns
    no `<nav>` for that global rule to reach."""

    def test_the_page_owns_no_nav_for_the_global_rule_to_reach(self):
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert "<nav" not in tpl, (
            "a <nav> here inherits the global `nav { display: flex }` (#1207) — "
            "the filter menu is a <div role='menu'>, which does not"
        )
        assert ".sl-cat-nav" not in tpl, "the sidebar's nav is retired, not merely unstyled"


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

    tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
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
    `/semantic-layer`. Both were once titled "Semantic layer" (this page is
    now "Metrics & glossary", that one "Semantic models" — see
    tests/test_semantic_page_names_contract.py), and this is the more
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

    def test_empty_state_claims_no_document_when_there_is_none(self, seeded_app):
        """Since the fold the Models tab is structural — the strip links to it
        from every tab, and it renders its own honest empty panel, so the
        old "is the door offered at all" gate has nothing left to protect.
        What still needs protecting is the SENTENCE: the metrics tab may only
        say "but this instance has a semantic model" when this caller has
        one."""
        body = self._body(seeded_app, "admin_token")
        assert "No metrics registered yet" in body
        assert "but this instance has a semantic model" not in body

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

    def test_analyst_without_a_grant_is_not_told_a_document_awaits_them(self, seeded_app):
        """`_can_read_model` is not admin-only, but it is not open either: an
        analyst with neither a direct model grant nor a Data Package grant
        would find the Models tab empty, so the metrics tab must not promise
        them a document sitting next door."""
        self._seed_model()
        body = self._body(seeded_app, "analyst_token")
        assert "but this instance has a semantic model" not in body


class TestCatalogSemanticsDeepLinkIntoTheDocument:
    """Per-row door into the document browser (#1707, N4).

    The two views of one metric did not know about each other: this flat page
    listed a projected metric with no way to reach the object it came from,
    and the object page had no way back. A metric row that resolves to a
    document object now carries an "in the model" link; a metric this
    instance authored by hand or imported from YAML has no such object and
    must carry none — a link that 404s is worse than no link.

    The mapping is the projector's own id formula
    (`src/semantic/projection.py::projected_metric_id`), not a guess at
    parsing the stored id apart.
    """

    _SLUG = "retail"
    _LINK = 'href="/semantic-layer/retail/metric:revenue"'

    def _seed_model(self, *, slug: str | None = None, metric_name: str = "revenue") -> dict:
        from src.repositories import semantic_model_repo

        slug = slug or self._SLUG
        return semantic_model_repo().upsert(
            id=f"manual/_/{slug}",
            slug=slug,
            name=slug,
            description="Retail domain.",
            document="# fixture",
            document_json={
                "semantic_model": [
                    {
                        "name": slug,
                        "datasets": [{"name": "orders", "source": "db.public.orders", "fields": []}],
                        "metrics": [
                            {
                                "name": metric_name,
                                "description": "Total order revenue.",
                                "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(amount)"}]},
                            }
                        ],
                    }
                ]
            },
            spec_version="0.2.0.dev0",
            content_hash=f"hash-{slug}",
            source="manual",
            source_ref=None,
            status="valid",
            validation_errors=None,
            validated_at=None,
        )

    def _body(self, seeded_app, token_key: str = "admin_token") -> str:
        resp = seeded_app["client"].get("/catalog/semantics", headers=_auth(seeded_app[token_key]))
        assert resp.status_code == 200
        return resp.text

    def test_document_owned_metric_row_links_to_its_object_page(self, seeded_app):
        self._seed_model()
        _make_metric(
            id="manual/_/retail/revenue",
            name="revenue",
            display_name="Revenue",
            category="retail",
            source="manual",
            source_ref=None,
        )
        assert self._LINK in self._body(seeded_app)

    def test_metric_with_no_document_object_gets_no_link(self, seeded_app):
        """A yaml_import metric is a registry row with no document behind it —
        it must render bare even on an instance that HAS a readable model."""
        self._seed_model()
        _make_metric(id="revenue/mrr", name="mrr", source="yaml_import")
        body = self._body(seeded_app)
        assert "Monthly Recurring Revenue" in body
        assert "/semantic-layer/retail/metric:" not in body

    def test_link_is_not_offered_to_a_caller_who_cannot_read_the_model(self, seeded_app):
        """Same `_can_read_model` tier as the browse pages: an analyst without
        a grant would 404 on the object page, so the row stays bare for them."""
        self._seed_model()
        _make_metric(
            id="manual/_/retail/revenue",
            name="revenue",
            display_name="Revenue",
            category="retail",
            source="manual",
            source_ref=None,
        )
        assert self._LINK not in self._body(seeded_app, "analyst_token")

    def test_the_readable_model_sweep_runs_once_per_request(self, seeded_app, monkeypatch):
        """The deep-link map and the page header's browse-link gate are two
        answers off ONE `_can_read_model` sweep.

        The check resolves a model's Data Packages per row, so a second sweep
        doubles this page's semantic-layer cost for an answer it already had —
        invisible in output, which is why it is asserted on the call count."""
        import app.api.semantic_models as semantic_models

        self._seed_model(slug="retail")
        self._seed_model(slug="finance")

        real = semantic_models._can_read_model
        seen: list[str] = []

        def counting(user, row, conn):
            seen.append(str(row.get("slug")))
            return real(user, row, conn)

        monkeypatch.setattr(semantic_models, "_can_read_model", counting)
        self._body(seeded_app)
        assert len(seen) == 2, f"expected one sweep over the two models, got {len(seen)} checks: {seen}"

    def test_the_back_links_q_value_matches_what_this_page_filters_on(self, seeded_app):
        """The round trip's data agreement, end to end.

        The object page PRODUCES `?q=<term>`; this page's filter CONSUMES it
        against each row's `data-ft` index (lowercased on both sides). Asserted
        as one dataflow rather than by grepping for the JS: a `q` the index
        does not contain lands the reader on an empty list, and neither half
        can see that on its own."""
        import re
        from urllib.parse import unquote

        self._seed_model()
        _make_metric(
            id="manual/_/retail/revenue",
            name="revenue",
            display_name="Revenue",
            category="retail",
            source="manual",
            source_ref=None,
        )
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])

        obj = c.get("/semantic-layer/retail/metric:revenue", headers=headers)
        assert obj.status_code == 200, obj.text
        produced = re.search(r'href="/semantic-layer\?tab=all_metrics&amp;q=([^"]*)"', obj.text)
        assert produced, "the metric object page emitted no registry back link"
        term = unquote(produced.group(1)).lower()

        body = self._body(seeded_app)
        rows = re.findall(r'data-ft="([^"]*)"', body)
        assert any(term in row for row in rows), (
            f"no metric row indexes {term!r} — the back link would land on an empty filter"
        )
        # ...and the consumer end is wired at all: without this read the term
        # arrives in the URL and the list renders unfiltered.
        assert "URLSearchParams(window.location.search).get('q')" in body


class TestSemanticPagesAreWhiteSheets:
    """The semantic layer pages are white, like the app around
    them (#1898).

    Both are reached from the Library's Definitions block, and the Library — like
    every index page (`.idx`) and every admin page (`.container--full`) — paints
    its shell `--ds-surface`. A semantic page that extends a base which does
    not paint turned the canvas grey on the way in: two products, one journey.
    Each of the three now lands on a white sheet through the shell it is
    actually on — the index shell's own `.idx` rule, the shared
    `body.page-sheet` modifier, or the `body.detail-page` family on the same
    CSS rule — rather than by carrying a page background of its own, which is
    what the design-system contract forbids and what would drift the moment one
    of them was edited."""

    # One assertion per page, naming the mechanism that actually paints it —
    # the three pages sit on three different shells since the #1707 rebuild,
    # and asserting a single `page-sheet` opt-in across all three would pass
    # only by adding a redundant class to two pages that are already white.
    def test_every_semantic_page_lands_on_a_white_sheet(self):
        from pathlib import Path

        tpl = Path("app/web/templates")

        # The model list is on the index shell, whose `.idx` rule paints
        # --ds-surface for every index page (My Stack, Catalog, Library).
        lst = (tpl / "semantic_layer_list.html").read_text(encoding="utf-8")
        assert '{% extends "base_index.html" %}' in lst
        idx = Path("app/web/static/style-custom.css").read_text(encoding="utf-8")
        assert "background: var(--ds-surface)" in idx.split(".idx {", 1)[1].split("}", 1)[0], (
            "the index shell stopped painting its own surface — the model list "
            "would fall back onto the grey canvas with nothing to say so"
        )

        # The model detail page is on `base_page.html`, which paints nothing of
        # its own, so it is the one page that opts in by name.
        detail = (tpl / "semantic_layer_detail.html").read_text(encoding="utf-8")
        assert '{% extends "base_page.html" %}' in detail
        assert '{% block body_attrs %}class="page-sheet"{% endblock %}' in detail

        # The object page carries `detail-page`, which the same CSS rule covers.
        obj = (tpl / "semantic_layer_object.html").read_text(encoding="utf-8")
        assert '{% block body_attrs %}class="detail-page"{% endblock %}' in obj

    def test_the_modifier_paints_the_body_and_outranks_the_theme(self):
        """On the BODY, so it is full bleed — painting the padded `.container`
        would leave grey gutters beside a white column. And with the extra
        element in the selector, so it beats `[data-theme="paper"] body` (which
        is what paints the grey) without depending on sheet order."""
        from pathlib import Path

        css = Path("app/web/static/style-custom.css").read_text(encoding="utf-8")
        assert "html body.page-sheet,\nhtml body.detail-page { background: var(--ds-surface); }" in css, (
            "the sheet rule must paint both the opt-in class and the detail-page family"
        )
        paper = Path("app/web/static/css/paper-skin.css").read_text(encoding="utf-8")
        assert '[data-theme="paper"] body {' in paper, (
            "the rule this one has to outrank has moved — re-check the specificity note"
        )

    # The app-side detail pages, named rather than globbed: `*_detail.html` also
    # matches the admin ones (already white via `.container--full`) and the
    # semantic-layer document page (an opt-in above, since it is not on the
    # shared detail shell).
    DETAIL_FAMILY = (
        "catalog_package_detail.html",
        "catalog_table_detail.html",
        "catalog_recipe_detail.html",
        "marketplace_plugin_detail.html",
        "marketplace_item_detail.html",
        "data_app_detail.html",
        "library_detail.html",
        "library_file_detail.html",
        "memory_domain_detail.html",
    )

    def test_the_detail_page_family_is_on_the_same_rule(self):
        """These nine already carry `body.detail-page` for their footer treatment,
        so they join the sheet by that marker rather than by nine separate
        opt-ins — one selector, and nothing to forget when a tenth is written.

        The assertion is that the marker is still what they all carry: the day one
        of them drops it, it silently drops back onto the grey canvas."""
        from pathlib import Path

        tpl = Path("app/web/templates")
        for name in self.DETAIL_FAMILY:
            text = (tpl / name).read_text(encoding="utf-8")
            assert 'class="detail-page"' in text, f"{name} lost the class that paints its sheet"


class TestGlossaryRowExpansion:
    """A term row opens only when opening shows something.

    A metric always has SQL behind it, so its expansion always pays. A term
    carries a definition and `see_also` and nothing else — so on the common
    row, expanding repeated the line above it verbatim, which teaches a reader
    to stop clicking.
    """

    def test_a_term_with_see_also_is_expandable(self, seeded_app):
        from src.repositories import glossary_repo

        glossary_repo().create(
            id="gl_credit",
            term="Credit note",
            definition="A reduction against an invoice.",
            see_also=["Net revenue"],
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        row = body.split('id="glossary-list"', 1)[1]
        assert 'data-has-more="1"' in row

    def test_a_plain_short_term_is_not_marked_expandable(self, seeded_app):
        """Nothing behind it, so the page gives it no chevron and no click."""
        from src.repositories import glossary_repo

        glossary_repo().create(id="gl_bench", term="Bench", definition="Unassigned but available time.")
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        row = body.split('id="glossary-list"', 1)[1].split("</div>", 1)[0]
        assert "data-has-more" not in row

    def test_a_definition_with_markup_is_expandable(self, seeded_app):
        """A definition imported verbatim from an external catalog is often rich
        HTML — the preview can only show its text, so opening it pays."""
        from src.repositories import glossary_repo

        glossary_repo().create(
            id="gl_rich",
            term="Recognition",
            definition="See the [policy](https://example.com/policy) for the full rule.",
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        assert 'data-has-more="1"' in body

    def test_an_expansion_does_not_repeat_the_definition(self, seeded_app):
        """The bug this whole rule exists for, in its last hiding place.

        Flattening the plain rows left the expandable ones — and a row opened
        for its `see_also` still rendered the definition again above it, word
        for word, under a sentence the closed row had already shown in full.
        The panel carries the definition only when the markdown renders
        something the preview could not.
        """
        from src.repositories import glossary_repo

        glossary_repo().create(
            id="gl_credit",
            term="Credit note",
            definition="A reduction against an issued invoice.",
            see_also=["Net revenue"],
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        detail = body.split('id="glossary-list"', 1)[1].split('<div class="sl-detail">', 1)[1]
        detail = detail.split("</div>\n        </div>", 1)[0]
        assert "sl-seealso" in detail, "the reference is what earned this row its chevron"
        assert "A reduction against an issued invoice." not in detail, (
            "the closed row already shows this sentence in full"
        )

    def test_an_expansion_does_carry_a_definition_the_preview_flattened(self, seeded_app):
        """The other side of the same rule: markdown the text preview lost."""
        from src.repositories import glossary_repo

        glossary_repo().create(
            id="gl_rich",
            term="Recognition",
            definition="See the [policy](https://example.com/policy) for the rule.",
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        assert 'href="https://example.com/policy"' in body

    def test_a_long_plain_definition_is_still_not_expandable(self, seeded_app):
        """Length is not a reason, and this is the regression it replaces.

        A definition that overran the one-line clamp by a couple of words used
        to earn a chevron, and opening it showed the same sentence again. The
        row wraps to two lines instead, so the only thing length can now do is
        take a second line.
        """
        from src.repositories import glossary_repo

        glossary_repo().create(
            id="gl_long",
            term="Utilization",
            definition=(
                "The share of a delivery person's available hours that were booked to "
                "client work in the period, excluding internal projects and holiday."
            ),
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        assert "data-has-more" not in body

    def test_the_flattening_rule_does_not_measure_the_row(self):
        """The whole rule is the server's flag — no width, no character count."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert "scrollWidth" not in tpl, "length is not a reason to expand a term row"
        assert "if (!item.dataset.hasMore) item.classList.add('is-flat');" in tpl
        assert ".sl-item.is-flat .sl-row__chev { visibility: hidden; }" in tpl

    def test_a_row_definition_wraps_instead_of_clipping(self):
        """Every row, not only a term's. One line meant a description a few
        words too long was cut mid-sentence and then restated in full inside
        the panel — the same duplication, one registry over."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        block = tpl.split("\n.sl-row__desc {", 1)[1].split("}", 1)[0]
        assert "white-space: normal" in block
        assert "-webkit-line-clamp: 2" in block
        assert "white-space: nowrap" not in block
        assert ".sl-item[data-gcat] .sl-row__desc {" not in tpl, (
            "the glossary-only override is the base rule now"
        )


class TestGlossaryCrossReferences:
    """`see_also` names another definition — and a definition is as often a
    metric as a term, so a reference is resolved against both registries
    before it is drawn."""

    def test_a_reference_to_a_term_filters_this_list(self, seeded_app):
        from src.repositories import glossary_repo

        glossary_repo().create(id="gl_a", term="Bench", definition="Unassigned but available time.")
        glossary_repo().create(
            id="gl_b",
            term="Utilization",
            definition="Share of available hours booked to client work.",
            see_also=["Bench"],
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        assert 'data-seealso="Bench"' in body

    def test_a_reference_to_a_metric_links_to_the_metrics_tab(self, seeded_app):
        """The reference that motivated resolving them at all: "Credit note →
        Net revenue" points at a metric, which lives on the other tab. Filtering
        the glossary for it found nothing."""
        from src.repositories import glossary_repo, metric_repo

        metric_repo().create(
            id="rev/net",
            name="net_revenue",
            display_name="Net revenue",
            category="revenue",
            sql="SELECT 1",
        )
        glossary_repo().create(
            id="gl_credit",
            term="Credit note",
            definition="A reduction against an invoice.",
            see_also=["Net revenue"],
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        assert "/semantic-layer?tab=all_metrics&amp;q=Net%20revenue" in body
        assert 'data-seealso="Net revenue"' not in body, "a metric is not filtered out of the glossary"

    def test_a_reference_to_neither_is_not_a_link(self, seeded_app):
        """A definition can be renamed or deleted out from under a reference.
        Stating a dead name is honest; offering it as a link is not."""
        from src.repositories import glossary_repo

        glossary_repo().create(
            id="gl_ghost",
            term="Backlog",
            definition="Work committed but not started.",
            see_also=["Deleted concept"],
        )
        body = (
            seeded_app["client"].get("/semantic-layer?tab=all_glossary", headers=_auth(seeded_app["admin_token"])).text
        )
        assert "Deleted concept" in body
        assert 'data-seealso="Deleted concept"' not in body
        assert "q=Deleted%20concept" not in body
        assert 'class="sl-seealso--dead"' in body


class TestSemanticPageChrome:
    """The two semantic pages had drifted into looking like a different product
    from the Library one click away."""

    def test_the_semantic_pages_use_the_standard_page_header(self):
        """`/semantic-layer` hand-rolled its own `.sl-head` / `.sl-kicker` /
        `.page-title` trio — a heavier eyebrow, a different title size, its own
        spacing. It renders the same `page-header--plain` block every other
        content page does (see `_page_hero.html`); the markup is copied rather
        than included only because this page extends `base_index.html` and
        cannot take `base_page.html`'s hero variables."""
        from pathlib import Path

        lst = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert 'class="page-header page-header--plain"' in lst
        assert 'class="page-header__title"' in lst
        assert 'class="sl-kicker"' not in lst, "the bespoke eyebrow is gone, not just unused"
        # …and no eyebrow at all: it read "LIBRARY" directly under a back link
        # already saying "← Library" — the same word twice, two lines apart,
        # one of them in caps. The back link IS the relation.
        assert 'class="page-header__eyebrow"' not in lst
        det = Path("app/web/templates/semantic_layer_detail.html").read_text(encoding="utf-8")
        assert "page_hero_eyebrow" not in det, "same on the model page, under '← Definitions'"

    def test_the_semantic_back_link_matches_every_other_way_back(self):
        """It diverged from `.apg-back` on weight, spacing and hover colour —
        three near-misses that make one page feel like a different product."""
        from pathlib import Path

        for name in ("semantic_layer_list.html", "semantic_layer_detail.html"):
            css = Path("app/web/templates/%s" % name).read_text(encoding="utf-8")
            css = css.split(".slb-back {")[1].split("}")[0]
            assert "font-weight: 600" in css, name
            assert "margin: 0 0 10px" in css, name


class TestSemanticPageDetails:
    """Four near-misses that each made this corner read as a different product
    from the rest of the app."""

    def test_a_metric_panel_does_not_restate_the_row(self):
        """The row wraps to two lines and carries the description; the panel
        drops its copy unless the row is actually clipping it. Which it is can
        only be answered at a width, so the page asks the browser — and asks
        again on resize, or a description that fit wide would vanish narrow."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert "function trimRedundantDescriptions()" in tpl
        assert "row.scrollHeight > row.clientHeight + 1" in tpl
        assert "trimRedundantDescriptions();" in tpl
        assert "addEventListener('resize'" in tpl, "a width-dependent rule has to re-run"

    def test_a_metric_panel_keeps_a_description_that_is_more_than_the_row(self):
        """The guard against over-trimming: only an exact text match is
        redundant. A description whose markdown renders a link or a list says
        more than its plain preview and must survive."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        fn = tpl.split("function trimRedundantDescriptions()", 1)[1].split("\n    }", 1)[0]
        assert "norm(panel.textContent) !== norm(row.textContent)" in fn
        assert "return;" in fn

    def test_a_model_card_never_advertises_a_zero(self, seeded_app):
        """A zero spent one of the card's three visible chips saying a thing is
        absent, which pushed the real counts behind a "+2" and truncated the
        survivors mid-word ("0 constrai…")."""
        body = seeded_app["client"].get("/semantic-layer", headers=_auth(seeded_app["admin_token"])).text
        for noun in ("constraint", "relationship", "glossary term", "dataset", "metric"):
            assert "0 %s" % noun not in body

    def test_both_semantic_pages_draw_the_same_way_back(self):
        """An arrow on one page and a chevron on the other, one click apart."""
        from pathlib import Path

        for name in ("semantic_layer_list.html", "semantic_layer_detail.html"):
            src = Path("app/web/templates/%s" % name).read_text(encoding="utf-8")
            back = src.split('class="slb-back"', 1)[1].split("</a>", 1)[0]
            assert '<span aria-hidden="true">&larr;</span>' in back, name
            assert "<svg" not in back, name

    def test_the_provenance_badge_says_what_it_means(self):
        """"Native" was the badge's own jargon for "nobody imported this". Its
        opposite says "Imported from X", so the pair reads as a sentence now."""
        from pathlib import Path

        for name in ("semantic_layer_detail.html", "semantic_layer_object.html"):
            src = Path("app/web/templates/%s" % name).read_text(encoding="utf-8")
            assert ">Created in Agnes<" in src, name
            assert ">Native<" not in src, name


class TestOneToolbarOverThreeBuckets:
    """One filtering idiom for the whole app, and one for the whole page.

    This page carried three: Semantic models had none, Metrics had a left
    sidebar of categories plus its own search box, Glossary had a different
    sidebar plus another search box — while the Library, /chats and three admin
    pages all run on static/js/filter_toolbar.js. Then it carried three
    toolbars, one per tab, which was consistent but still meant a search saw
    only the tab it was typed on.

    Now: one toolbar over the page, the tabs UNDER it as buckets of what
    survives. Narrow above, and every tab's count moves.
    """

    def _body(self, seeded_app, tab=""):
        url = f"/semantic-layer?tab={tab}" if tab else "/semantic-layer"
        return seeded_app["client"].get(url, headers=_auth(seeded_app["admin_token"])).text

    def test_one_toolbar_sits_above_the_tabs(self, seeded_app):
        """The ORDER is the claim: everything above the tabs narrows the whole
        page, and the tabs below split what survives. Tabs above the bar would
        say the opposite — that you pick a tab and then search within it."""
        _make_metric()
        _seed_model()
        body = self._body(seeded_app)
        for one in ('id="sl-search"', 'id="sl-chips"', 'id="sl-count"', 'id="sl-tabs"'):
            assert body.count(one) == 1, f"{one} must exist exactly once on the page"
        assert body.index('id="sl-search"') < body.index('id="sl-chips"') < body.index('id="sl-tabs"')

    def test_all_three_buckets_render_on_every_tab(self, seeded_app):
        """What makes one search able to see all three. The rows have to BE on
        the page; the tab only decides which block is shown."""
        _make_metric()
        _seed_model()
        from src.repositories import glossary_repo

        glossary_repo().create(id="gl_b", term="Bench", definition="Unassigned.")
        for tab in ("", "all_metrics", "all_glossary"):
            body = self._body(seeded_app, tab)
            for bucket in ("models", "all_metrics", "all_glossary"):
                assert f'data-bucket="{bucket}"' in body, (tab, bucket)
            assert 'data-tab="all_metrics"' in body, tab
            assert 'data-tab="all_glossary"' in body, tab

    def test_the_page_local_filtering_is_gone(self):
        """Deleted, not merely bypassed — two implementations of one behaviour
        is how they drift apart in the first place."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert "function applyMetrics" not in tpl
        assert "function applyGlossary" not in tpl
        assert "sl-cat-btn" not in tpl and "sl-gcat-btn" not in tpl
        assert "window.FilterToolbar.init" in tpl
        assert tpl.count("FilterToolbar.init") == 1, "one engine for the page, not one per tab"
        assert "js/filter_toolbar.js" in tpl, "the engine has to be on the page to drive it"

    def test_model_and_domain_are_two_facets_not_one(self, seeded_app):
        """`category` means the MODEL's name on a projected metric and a
        business DOMAIN on a hand-authored one, so one control offering both
        mixed two kinds of thing under one heading and a reader could not tell
        which was which.

        Seeded so BOTH axes can actually split the list — a facet with one
        value is dropped, so a thinner fixture would prove only that rule."""
        from src.repositories import metric_repo, semantic_model_repo
        from src.semantic.projection import projected_metric_id

        doc_model = {"name": "commercial", "datasets": []}
        semantic_model_repo().upsert(
            id="manual/_/commercial", slug="commercial", name="commercial", description="",
            document="# fixture",
            document_json={"semantic_model": [dict(doc_model, metrics=[{"name": "win_rate"}])]},
            spec_version="0.2.0.dev0", content_hash="h1", source="manual", source_ref=None,
            status="valid", validation_errors=None, validated_at=None,
        )
        metric_repo().create(
            id=projected_metric_id("manual", None, doc_model, "win_rate"),
            name="win_rate", display_name="win_rate", category="commercial",
            sql="SELECT 1", description="Projected from the commercial model.",
        )
        _make_metric(id="finance/dso", name="dso", display_name="Days sales outstanding",
                     category="finance", sql="SELECT 1", description="Invoice to cash.")
        _make_metric(id="sales/pipeline", name="pipeline", display_name="Pipeline value",
                     category="sales", sql="SELECT 1", description="Open pursuits.")

        body = self._body(seeded_app, "all_metrics")
        assert 'data-cat="model"' in body, "which document declares it"
        assert 'data-cat="domain"' in body, "what its author filed it under"
        assert 'value="commercial"' in body
        assert 'value="finance"' in body and 'value="sales"' in body
        # The projected row carries no domain: its `category` IS the model name,
        # which is exactly the conflation the split undoes.
        row = body.split('data-model="commercial"', 1)[1].split(">", 1)[0]
        assert 'data-domain=""' in row, row

    def test_a_facet_with_one_value_does_not_render(self, seeded_app):
        """P7, and the rule that kills it for good. The glossary's provenance
        nav used to render "All 12 / Defined directly 12" — a control that
        could not change what was on screen."""
        from src.repositories import glossary_repo

        for i, term in enumerate(("Bench", "Ramp", "Pursuit")):
            glossary_repo().create(id=f"gl_{i}", term=term, definition=f"{term} means something.")
        body = self._body(seeded_app, "all_glossary")
        assert 'id="sl-search"' in body, "the search box always renders"
        assert 'id="sl-filter-btn"' not in body, (
            "every facet has one value here, so the Filter button has nothing to open"
        )
        assert "Defined directly" in body, "provenance is still STATED on each row"


class TestSearchSpansTheTabs:
    """A search used to see only the tab it was typed on: asking for "margin"
    on Glossary found nothing while two metrics named "…margin" sat one tab
    away, and nothing on screen suggested looking.

    An earlier fix shipped each tab a text index of the other two. That is gone:
    with all three buckets on the page the rows themselves answer, the tab
    badges move as you type, and there is no second copy of the data to drift."""

    def _body(self, seeded_app, tab=""):
        url = f"/semantic-layer?tab={tab}" if tab else "/semantic-layer"
        return seeded_app["client"].get(url, headers=_auth(seeded_app["admin_token"])).text

    def test_one_search_box_for_the_whole_page(self, seeded_app):
        _make_metric()
        _seed_model()
        body = self._body(seeded_app, "all_glossary")
        assert 'id="sl-search"' in body
        # Not `count('type="search"')`: the app rail carries its own global
        # search box, which is not this page's. The claim is that the PAGE has
        # one, so it is made against the per-tab ids that used to exist.
        for retired in ('id="metrics-search"', 'id="glossary-search"', 'id="models-search"'):
            assert retired not in body, f"{retired} — one box for the page, not one per tab"

    def test_a_model_card_carries_the_search_index(self, seeded_app):
        """The cards had no `data-ft`, so the box matched nothing and reported
        "0 of 2" for a model that was on screen."""
        _seed_model()
        body = self._body(seeded_app)
        # The ARTICLE, not the first mention of the class — the page's own
        # stylesheet talks about `.fbar-card` well above the markup.
        card = body.split('<article class="fbar-card"', 1)[1].split(">", 1)[0]
        assert "data-ft=" in card, card

    def test_the_cross_tab_index_is_gone(self):
        """It existed only because the other tabs' rows were not on the page.
        Shipping it now would be a second copy of data already in the DOM."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert "const CROSS" not in tpl
        assert "syncCrossTab" not in tpl

    def test_a_tab_badge_reports_what_it_would_hold(self):
        """Counted with the tab itself ignored, or every badge but the active
        one reads zero — the bug the Library hit with the same mechanism."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        fn = tpl.split("function refreshTabCounts()", 1)[1].split("\n    }", 1)[0]
        assert "data-seg-count" in fn
        assert "data-ft" in fn and "data-tab" in fn
        assert ".is-active" not in fn, (
            "the badge must not be scoped to the ACTIVE tab — that is what makes "
            "the other tabs' counts readable while you search"
        )


class TestTheWrongBucketIsNeverBlank:
    """The state a page-wide search over three buckets creates constantly.

    Search "margin" from Semantic models and that bucket has nothing, while
    Metrics has three and Glossary has one. Left alone the reader gets a blank
    area under a tab strip whose other badges are plainly non-zero, which reads
    as a bug rather than as an answer.
    """

    def test_the_page_distinguishes_empty_here_from_empty_everywhere(self):
        """Two states, and telling them apart is the point. `noResults` is the
        engine's — nothing on the whole page matched. `sl-elsewhere` is ours —
        this bucket is empty and another is not."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert 'id="sl-noresults"' in tpl
        assert 'id="sl-elsewhere"' in tpl
        fn = tpl.split("function syncBucketState()", 1)[1].split("\n    }", 1)[0]
        assert "const elsewhere = here === 0 && others.length > 0;" in fn, (
            "shown only when THIS bucket is empty and another is not"
        )
        assert "none.hidden = !(here === 0 && !elsewhere)" in fn, (
            "and only ever ONE of the two speaks — both were firing at once"
        )

    def test_the_jump_keeps_the_query(self):
        """It clicks the tab rather than navigating, so the search box, the
        filters and the chips all survive — the reader is moved to where their
        answer is, not returned to an unfiltered page."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        fn = tpl.split("function syncBucketState()", 1)[1].split("\n    }", 1)[0]
        assert "o.btn.click()" in fn
        assert "href" not in fn, "a page load would drop the filters the reader set"

    def test_no_inline_handler_property_anywhere(self):
        """The metric-description sanitizer's guard is a page-wide substring
        check for inline handlers. Honouring it in our own script keeps that
        guard strict instead of scoping it down to suit us."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert "onclick" not in tpl
        assert "addEventListener('click'" in tpl


class TestTheViewRidesTheUrl:
    """These tabs used to be real links, so the address named the tab you were
    on, Back returned to the previous one and a copied link opened where you
    were. Turning them into client-side buckets took all three away silently:
    you could click Glossary and hand someone a link that opened on Metrics."""

    def test_the_tab_and_query_are_written_back_to_the_url(self):
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        fn = tpl.split("function syncUrl()", 1)[1].split("\n    }", 1)[0]
        assert "history.pushState" in fn and "history.replaceState" in fn, (
            "push for the tab, replace for the query"
        )
        assert "if (tab !== 'models') p.set('tab', tab)" in fn, (
            "the default tab keeps the bare URL — one canonical address"
        )
        assert "if (q) p.set('q', q)" in fn

    def test_switching_a_tab_pushes_and_typing_replaces(self):
        """pushState for the tab, per /admin/access's note on the same move:
        switching view is exactly what Back should undo. replaceState for the
        query, or a search would bury the page in history one keystroke at a
        time."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        fn = tpl.split("function syncUrl()", 1)[1].split("\n    }", 1)[0]
        assert "if (tab !== urlTab) {" in fn
        assert fn.index("pushState") < fn.index("} else {") < fn.index("replaceState")

    def test_back_restores_the_tab(self):
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert "addEventListener('popstate'" in tpl


class TestARowSaysWhereItCameFrom:
    """You could narrow by Model and still not see the answer without opening
    something — and the pairs that share a display name (the same concept
    defined once in a document and once by hand, a real state) had nothing on
    the row to tell them apart."""

    def test_a_metric_row_states_its_model_like_a_glossary_row_does(self, seeded_app):
        _make_metric()
        body = seeded_app["client"].get(
            "/semantic-layer?tab=all_metrics", headers=_auth(seeded_app["admin_token"])
        ).text
        row = body.split('id="metrics-list"', 1)[1].split("</button>", 1)[0]
        assert 'class="sl-row__prov"' in row, "the metric row hid what the glossary row states"
        assert "Defined directly" in row


class TestTheAnswerIsAnnounced:
    """A screen-reader user types into the search box and the only thing that
    changes is a number and a state neither of which was announced."""

    def test_the_count_and_the_empty_state_are_live_regions(self):
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert '<div class="sl-countline" aria-live="polite">' in tpl
        assert 'id="sl-elsewhere" aria-live="polite"' in tpl

    def test_a_row_says_whether_it_is_open(self):
        """It is a real button toggling real content; without this the click
        appears to do nothing."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        assert tpl.count('class="sl-row" aria-expanded="false"') == 2, "both row kinds"
        assert "btn.setAttribute('aria-expanded'" in tpl
        # …and a row the filter closed underneath must stop claiming it is open.
        assert "b.setAttribute('aria-expanded', 'false')" in tpl

    def test_the_tab_strip_scrolls_rather_than_clipping_on_a_phone(self):
        """Three tabs with counts do not fit 375px."""
        from pathlib import Path

        tpl = Path("app/web/templates/semantic_layer_list.html").read_text(encoding="utf-8")
        block = tpl.split("@media (max-width: 560px) {", 1)[1].split("}", 1)[0]
        assert "overflow-x: auto" in block
        assert "padding-right" in block, "a sliver of the next tab is the only cue it scrolls"

