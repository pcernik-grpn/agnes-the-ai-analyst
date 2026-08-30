"""Read-only browse UI for the semantic layer (wave 4.2 of the 2026-08-14
UI/agent-parity design) — three levels: model list (`/semantic-layer`),
model detail (`/semantic-layer/{slug}?tab=`), object detail
(`/semantic-layer/{slug}/{object_id}`).

RBAC tier mirrors the rest of the read surface in
``app/api/semantic_models.py`` (``tests/test_semantic_models_api.py``): any
authenticated user, filtered through ``_can_read_model`` (a Data Package
grant or a direct ``semantic_model`` grant) — never admin-only, and never a
write affordance anywhere on these three pages (editing is a later
increment).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.db import get_system_db

_SLUG = "retail"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _document_json(slug: str = _SLUG) -> dict:
    return {
        "semantic_model": [
            {
                "name": slug,
                "description": "Retail domain: orders and customers.",
                "datasets": [
                    {
                        "name": "orders",
                        "source": "db.public.orders",
                        "primary_key": ["order_id"],
                        "description": "Order lines.",
                        "ai_context": {
                            "instructions": "Prefer this dataset for post-checkout analysis.",
                            "synonyms": ["sales orders"],
                            "keywords": ["orders", "revenue"],
                            "anti_keywords": ["refund", "return"],
                            "hints": ["Join via customer_id"],
                            "warnings": ["Excludes refunds"],
                        },
                        "fields": [
                            {"name": "order_id", "datatype": "String", "description": "Primary key of orders."},
                            {
                                "name": "order_date",
                                "datatype": "Date",
                                "dimension": {"is_time": True},
                                "description": "Order date.",
                            },
                            {"name": "region", "datatype": "String", "description": "Sales region."},
                        ],
                    },
                    {"name": "customers", "source": "db.public.customers", "fields": []},
                ],
                "metrics": [
                    {
                        "name": "revenue",
                        "description": "Total order revenue.",
                        "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(amount)"}]},
                        "custom_extensions": [{"vendor_name": "agnes", "data": json.dumps({"dataset": "orders"})}],
                    }
                ],
                "relationships": [
                    {
                        "name": "orders_to_customers",
                        "from": "orders",
                        "to": "customers",
                        "from_columns": ["customer_id"],
                        "to_columns": ["customer_id"],
                    }
                ],
                "custom_extensions": [
                    {
                        "vendor_name": "agnes",
                        "data": json.dumps(
                            {
                                "constraints": [
                                    {
                                        "name": "region_filter_required",
                                        "constraint_type": "required_filter",
                                        "rule": "region = 'EU'",
                                        "severity": "error",
                                        "metrics": ["revenue"],
                                    }
                                ],
                                "glossary": [
                                    {
                                        "term": "ARR",
                                        "definition": "Annual recurring revenue.",
                                        "see_also": ["MRR"],
                                    }
                                ],
                            }
                        ),
                    }
                ],
            }
        ]
    }


def _seed_model(
    *,
    id: str = f"manual/_/{_SLUG}",
    slug: str = _SLUG,
    source: str = "manual",
    source_ref: str | None = None,
    status: str = "valid",
    validation_errors=None,
) -> dict:
    """Written straight through the repo (like ``test_semantic_models_api.py``
    ``_upsert_model_with_constraints``) so the fixture can carry
    ``custom_extensions``/``ai_context`` extras without fighting the vendored
    Ossie schema's exact upload shape for those provisional fields."""
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=id,
        slug=slug,
        name=slug,
        description="Retail domain: orders and customers.",
        document="# fixture, not schema-authored",
        document_json=_document_json(slug) if status != "invalid" else None,
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source=source,
        source_ref=source_ref,
        status=status,
        validation_errors=validation_errors,
        validated_at=None,
    )


def _grant_model(model_id: str, group_name: str = "Semantic Model Readers") -> None:
    """Mirrors ``test_semantic_models_api.py``'s ``_grant_model`` — a direct
    grant on the model, the narrowest RBAC path this UI's tests need."""
    from src.repositories import resource_grants_repo, user_groups_repo
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    group = user_groups_repo().create(name=group_name, description="", created_by="test")
    gid = group["id"] if isinstance(group, dict) else group
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    resource_grants_repo().create(
        group_id=gid,
        resource_type="semantic_model",
        resource_id=model_id,
        assigned_by="test",
    )


def _seed_metric(name: str = "arr", *, table_name=None) -> None:
    """A flat metric visible to everyone (no table gate when ``table_name`` is
    None) — enough to make the /library Definitions footer render."""
    from src.repositories import metric_repo

    metric_repo().create(
        id=name,
        name=name,
        display_name=name.upper(),
        category="revenue",
        sql="SELECT 1",
        table_name=table_name,
    )


def _seed_document(slug: str, doc: dict, *, source: str = "manual") -> dict:
    """Seed a stored row carrying an arbitrary ``document_json`` (for the
    multi-model / imported-binding cases the shared fixture doesn't cover)."""
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=f"{source}/_/{slug}",
        slug=slug,
        name=slug,
        description="",
        document="# fixture, not schema-authored",
        document_json=doc,
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source=source,
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


class TestModelList:
    def test_list_shows_counts_and_source_badge(self, seeded_app):
        _seed_model(source="manual")
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        assert "retail" in body
        # Object counts per type: 2 datasets, 1 metric, 1 constraint,
        # 1 relationship, 1 glossary term — rendered through fbar_card()'s
        # `tags` slot, which (like every other tag list in the product)
        # shows the first 3 and collapses the rest to "+N".
        assert "2 datasets" in body
        assert "1 metric<" in body or "1 metric " in body
        assert "1 constraint" in body
        assert "+2" in body  # relationships + glossary terms collapse
        # Native (source='manual') carries no "Imported from" badge.
        assert "Imported from" not in body

    def test_imported_model_carries_the_source_badge(self, seeded_app):
        _seed_model(id="manual/_/kb", slug="kb_retail", source="keboola_metastore")
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Imported from Keboola" in r.text

    def test_a_model_with_no_source_is_native_on_the_list_too(self, seeded_app):
        """`is_imported` treats a falsy source as NATIVE (`(source or "manual")
        != "manual"`) and the detail/object pages route the badge through it.
        The list card compared `m.source != 'manual'` directly, so an empty
        source read as imported and `source_label(None)` rendered "Native" —
        the same row said "Imported from Native" on the list and plain "Native"
        on its own page."""
        _seed_model(id="manual/_/nosrc", slug="nosrc_retail", source="")
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert "nosrc_retail" in r.text
        assert "Imported from" not in r.text, "a model with no source is badged as imported"
        # ...and the detail page it contradicted still agrees.
        d = c.get("/semantic-layer/nosrc_retail", headers=_auth(seeded_app["admin_token"]))
        assert d.status_code == 200, d.text
        assert "Imported from" not in d.text

    def test_invalid_model_renders_stored_errors_not_silently(self, seeded_app):
        _seed_model(
            id="manual/_/broken",
            slug="broken_model",
            status="invalid",
            validation_errors=["datasets: this document has no datasets"],
        )
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "broken_model" in r.text
        assert "Invalid" in r.text
        assert "this document has no datasets" in r.text

    def test_non_admin_without_a_grant_does_not_see_the_model(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert "retail" not in r.text

    def test_empty_list_offers_a_creation_cta_only_to_an_admin(self, seeded_app):
        """A3 follow-up (issue #1707): the empty-list panel's primary CTA must
        be a path that can actually succeed (spec §3, "never a CTA that
        can't succeed") — only an admin can act on "no semantic model
        available" (import/register a source), so only an admin gets the
        primary CTA; anyone else gets the neutral ask-an-admin copy and the
        `Metrics & glossary` cross-link stays a plain body link either way,
        never the CTA itself (it can just as easily be empty)."""
        c = seeded_app["client"]
        admin_r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert admin_r.status_code == 200
        assert 'href="/admin/semantic-sources"' in admin_r.text
        assert "Add a semantic source" in admin_r.text
        assert 'href="/catalog/semantics"' in admin_r.text

        # A non-admin with nothing readable sees the same empty panel, but
        # the primary CTA (an admin-only action) is absent.
        no_grant_r = c.get("/semantic-layer", headers=_auth(seeded_app["analyst_token"]))
        assert no_grant_r.status_code == 200
        assert 'href="/admin/semantic-sources"' not in no_grant_r.text
        assert "Add a semantic source" not in no_grant_r.text
        assert 'href="/catalog/semantics"' in no_grant_r.text

    def test_non_admin_with_a_direct_grant_sees_the_model(self, seeded_app):
        row = _seed_model()
        _grant_model(row["id"])
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert "retail" in r.text

    def test_same_slug_rows_collapse_to_one_reachable_card(self, seeded_app):
        """Devin #1398: two models can share a slug (unique only per source).
        The list shows one card per slug — the newest readable row, the one the
        drill-down resolves to — never a second card that silently opens the
        first."""
        doc_a = {"semantic_model": [{"name": "older", "datasets": [{"name": "alpha_ds", "fields": []}]}]}
        doc_b = {"semantic_model": [{"name": "newer", "datasets": [{"name": "beta_ds", "fields": []}]}]}
        _seed_document("dup", doc_a, source="manual")
        _seed_document("dup", doc_b, source="ossie_git")  # coexists (different source), newer
        c = seeded_app["client"]
        r = c.get("/semantic-layer", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert r.text.count('href="/semantic-layer/dup"') == 1  # exactly one card for the slug
        d = c.get("/semantic-layer/dup", headers=_auth(seeded_app["admin_token"]))
        assert d.status_code == 200  # and it is reachable
        assert "beta_ds" in d.text  # opens the newer row, matching the single card


class TestModelDetail:
    def test_each_tab_renders(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        for tab, needle in [
            ("datasets", "orders"),
            ("metrics", "revenue"),
            ("constraints", "region_filter_required"),
            ("relationships", "orders_to_customers"),
            ("glossary", "ARR"),
        ]:
            r = c.get(f"/semantic-layer/{_SLUG}?tab={tab}", headers=_auth(seeded_app["admin_token"]))
            assert r.status_code == 200, tab
            assert needle in r.text, f"tab={tab} did not render {needle!r}"

    def test_constraints_severity_header_uses_fast_tooltip_not_title(self, seeded_app):
        """A7 (issue #1707): the 257-char severity explanation lived in a
        native `title=` on `<th>Severity</th>` — a 600ms+ OS-controlled show
        delay, no styling, and prone to clipping in a scrollable ancestor.
        Converted to the shared `[data-tip]` fast-tooltip (components.css)
        with `aria-label` carrying the same text (the repo convention —
        `title` is never used alongside `data-tip`) and a short one-sentence
        summary; the fuller nuance moved to a note under the table. No
        `role="note"` on the `<th>` — that would override its implicit
        `columnheader` role and break screen-reader table navigation."""
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}?tab=constraints", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        th_match = re.search(r"<th[^>]*>Severity</th>", body)
        assert th_match, "Severity <th> not found"
        th_tag = th_match.group(0)
        assert "title=" not in th_tag
        assert 'role="note"' not in th_tag, "role=note would override the <th>'s implicit columnheader role"
        tip_match = re.search(r'data-tip="([^"]+)"', th_tag)
        assert tip_match, "Severity <th> is missing data-tip"
        tip_text = tip_match.group(1)
        assert len(tip_text) < 160
        # Soft-enforce: an error never blocks a query, only flips the
        # validate-query verdict — the copy must not claim otherwise.
        assert "enforcement is soft" in tip_text
        aria_match = re.search(r'aria-label="([^"]+)"', th_tag)
        assert aria_match, "Severity <th> is missing aria-label"
        assert aria_match.group(1) == tip_text
        # The fuller nuance (which constraint types are checkable today) now
        # lives in a short note under the table, not squeezed into the tooltip.
        assert "statically checkable today" in body

    def test_default_tab_is_datasets(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "orders" in r.text

    def test_unknown_slug_is_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.get("/semantic-layer/does-not-exist", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_non_admin_without_a_grant_gets_404(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 404

    def test_cross_link_from_dataset_row_to_metrics_tab(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}?tab=datasets", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "tab=metrics&amp;q=orders" in r.text

    def test_q_prefilters_the_active_tab(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}?tab=datasets&q=customers", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "customers" in r.text
        assert ">orders<" not in r.text

    @staticmethod
    def _chip_html(body: str) -> str:
        """Slice out just the filter-chip element, so assertions on its
        href/classes can't be satisfied by an unrelated `?tab=` link
        elsewhere on the page (the tab nav, cross-links, ...)."""
        start = body.index('data-testid="slb-filter-chip"')
        # back up to the start of the enclosing <div ...>
        start = body.rindex("<div", 0, start)
        end = body.index("</div>", start) + len("</div>")
        return body[start:end]

    def test_active_filter_renders_a_removable_chip(self, seeded_app):
        """A8 (issue #1707): tabs drop `q` on click with no indication it was
        ever applied. A removable chip above the table makes the active
        filter visible; its "x" links to the same tab without `q`. It is now
        the page's only removable-filter control — the old `Clear`
        link/search-box combo was redundant with it and was removed."""
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}?tab=metrics&q=Orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert 'data-testid="slb-filter-chip"' in r.text
        chip_html = self._chip_html(r.text)
        assert "Orders" in chip_html
        # The remove link must drop `q`, not merely restate it — scoped to
        # the chip element, not the page (the tab nav also links `?tab=metrics`).
        assert f'href="/semantic-layer/{_SLUG}?tab=metrics"' in chip_html
        assert "q=Orders" not in chip_html
        # The redundant `Clear` control next to the search box is gone.
        assert ">Clear<" not in r.text

    def test_no_filter_chip_when_q_is_absent(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}?tab=metrics", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert 'data-testid="slb-filter-chip"' not in r.text

    def test_filter_chip_escapes_special_characters_in_q(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(
            f"/semantic-layer/{_SLUG}?tab=metrics&q=%3Cscript%3Ealert(1)%3C/script%3E",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text

    def test_filter_chip_caps_width_and_ellipsizes_a_long_value(self, seeded_app):
        """A long `q` must not overflow the chip's fixed-height pill — the
        value span carries the same max-width + ellipsis guard as the
        library page's `.fbar-chip__vals` (filter_toolbar.css)."""
        _seed_model()
        c = seeded_app["client"]
        long_q = "x" * 400
        r = c.get(f"/semantic-layer/{_SLUG}?tab=metrics&q={long_q}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        chip_html = self._chip_html(r.text)
        assert long_q in chip_html
        assert "slb-filter-chip__value" in chip_html
        style_block = r.text[: r.text.index("</style>")]
        assert "max-width: 22rem" in style_block
        assert "text-overflow: ellipsis" in style_block
        assert "white-space: nowrap" in style_block

    def test_constraints_filter_matches_the_constraints_own_name(self, seeded_app):
        """Devin #1398: the constraint's own name (the linked first column) must
        be searchable, not only the metric names it applies to."""
        _seed_model()
        c = seeded_app["client"]
        # `region` appears in the constraint name (region_filter_required) but
        # not in its metrics (["revenue"]); before the fix this returned nothing.
        r = c.get(f"/semantic-layer/{_SLUG}?tab=constraints&q=region", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "region_filter_required" in r.text

    def test_relationships_filter_matches_the_relationships_own_name(self, seeded_app):
        """Devin #1398: the relationship's own name must be searchable too."""
        _seed_model()
        c = seeded_app["client"]
        r = c.get(
            f"/semantic-layer/{_SLUG}?tab=relationships&q=orders_to_customers",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert "orders_to_customers" in r.text

    def test_relationship_tab_does_not_link_an_undeclared_dataset(self, seeded_app):
        """Devin #1398: a relationship side naming a dataset not declared in the
        document renders as plain text, not a link that 404s — the same guard
        the object-detail page already applies."""
        doc = {
            "semantic_model": [
                {
                    "name": "rel",
                    "datasets": [{"name": "orders", "fields": []}],  # 'ghost' is NOT declared
                    "relationships": [
                        {"name": "o2g", "from": "orders", "to": "ghost", "from_columns": ["id"], "to_columns": ["id"]}
                    ],
                }
            ]
        }
        _seed_document("rel", doc)
        c = seeded_app["client"]
        r = c.get("/semantic-layer/rel?tab=relationships", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "/semantic-layer/rel/dataset:orders" in r.text  # declared side links
        assert "dataset:ghost" not in r.text  # undeclared side is plain text, not a link
        assert "ghost" in r.text  # but is still shown
        # the dangling target would 404 if it had been linked and clicked
        assert c.get("/semantic-layer/rel/dataset:ghost", headers=_auth(seeded_app["admin_token"])).status_code == 404

    def test_metrics_cross_link_resolves_dataset_name_to_its_source_id(self, seeded_app):
        """Devin #1398: an imported model binds a metric to a dataset by that
        dataset's source id, not its friendly name. The per-dataset cross-link
        (`?tab=metrics&q=<dataset name>`) must resolve name → source so the
        metric still shows — and the vendor tag is read case-insensitively, so
        BOTH the canonical `AGNES` and the lower-case `agnes` the skill docs
        show resolve (the filter reads via the casefolding
        `agnes_extension_payload`, not the projector's case-sensitive helper)."""
        doc = {
            "semantic_model": [
                {
                    "name": "imported",
                    "datasets": [{"name": "Orders", "source": "in.c-main.orders", "fields": []}],
                    "metrics": [
                        {
                            "name": "gross_revenue",
                            "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(amount)"}]},
                            # canonical upper-case tag the Keboola adapter emits
                            "custom_extensions": [
                                {"vendor_name": "AGNES", "data": json.dumps({"dataset": "in.c-main.orders"})}
                            ],
                        },
                        {
                            "name": "net_revenue",
                            "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(net)"}]},
                            # lower-case tag, exactly as the skill docs show it
                            "custom_extensions": [
                                {"vendor_name": "agnes", "data": json.dumps({"dataset": "in.c-main.orders"})}
                            ],
                        },
                    ],
                }
            ]
        }
        _seed_document("imported", doc, source="keboola_metastore")
        c = seeded_app["client"]
        r = c.get("/semantic-layer/imported?tab=metrics&q=Orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "gross_revenue" in r.text  # AGNES-tagged binding resolves
        assert "net_revenue" in r.text  # agnes-tagged binding resolves too

    def test_multi_model_document_shows_every_models_objects(self, seeded_app):
        """Devin #1398: a row declaring more than one semantic_model must show
        all of their objects (like the read tools flatten), not only the first
        — including constraints riding a later model's custom_extensions."""
        doc = {
            "semantic_model": [
                {
                    "name": "a",
                    "datasets": [{"name": "alpha", "fields": []}],
                    "metrics": [
                        {"name": "m_alpha", "expression": {"dialects": [{"dialect": "duckdb", "expression": "1"}]}}
                    ],
                },
                {
                    "name": "b",
                    "datasets": [{"name": "beta", "fields": []}],
                    "metrics": [
                        {"name": "m_beta", "expression": {"dialects": [{"dialect": "duckdb", "expression": "1"}]}}
                    ],
                    "custom_extensions": [
                        {
                            "vendor_name": "AGNES",
                            "data": json.dumps({"constraints": [{"name": "c_beta", "metrics": ["m_beta"]}]}),
                        }
                    ],
                },
            ]
        }
        _seed_document("multi", doc)
        c = seeded_app["client"]
        ds = c.get("/semantic-layer/multi?tab=datasets", headers=_auth(seeded_app["admin_token"]))
        assert "alpha" in ds.text and "beta" in ds.text
        mx = c.get("/semantic-layer/multi?tab=metrics", headers=_auth(seeded_app["admin_token"]))
        assert "m_alpha" in mx.text and "m_beta" in mx.text
        # The constraint rides the SECOND model's custom_extensions — proves
        # model_constraints aggregates across every model, not just the first.
        cx = c.get("/semantic-layer/multi?tab=constraints", headers=_auth(seeded_app["admin_token"]))
        assert "c_beta" in cx.text

    def test_shared_slug_drills_into_the_row_the_caller_can_read(self, seeded_app):
        """Devin #1398: two models can share a slug (unique only per source).
        The drill-down resolves to the newest row THIS caller can read — not
        the newest overall — so a card the analyst can see never 404s or opens
        a row they lack a grant on."""
        doc_a = {"semantic_model": [{"name": "A", "datasets": [{"name": "alpha_ds", "fields": []}]}]}
        doc_b = {"semantic_model": [{"name": "B", "datasets": [{"name": "beta_ds", "fields": []}]}]}
        a = _seed_document("dup", doc_a, source="manual")
        _seed_document("dup", doc_b, source="ossie_git")  # coexists (different source), newer, no grant
        _grant_model(a["id"])  # the analyst can read only A
        c = seeded_app["client"]
        r = c.get("/semantic-layer/dup", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200  # would 404 if it resolved B (the newest overall) instead
        assert "alpha_ds" in r.text
        assert "beta_ds" not in r.text

    @pytest.mark.parametrize("tab", ["datasets", "metrics", "constraints", "relationships", "glossary"])
    def test_filtered_no_match_renders_nothing_found_not_empty(self, seeded_app, tab):
        """A3 (issue #1707): a `q` that matches nothing IN A NON-EMPTY
        collection is a filter collapse, not a genuinely empty collection —
        the exact distinction the shared `state.panel` vocabulary exists to
        keep visible. Must render `nothing_found`, never `empty`, with the
        filter value itself part of the copy (the removable chip above the
        table also carries it). `_seed_model()` carries one row of every
        object type, so every tab's universe is non-empty here."""
        _seed_model()
        c = seeded_app["client"]
        r = c.get(
            f"/semantic-layer/{_SLUG}?tab={tab}&q=no_such_thing_at_all",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert 'data-state-kind="nothing_found"' in r.text
        assert 'data-state-kind="empty"' not in r.text
        assert "no_such_thing_at_all" in r.text

    @pytest.mark.parametrize("tab", ["datasets", "metrics", "constraints", "relationships", "glossary"])
    def test_unfiltered_empty_collection_renders_empty_not_nothing_found(self, seeded_app, tab):
        """A3 (issue #1707): a tab with no `q` and zero rows is the collection
        itself being empty, never a filter collapse. Must render `empty`."""
        _seed_document(
            "blank", {"semantic_model": [{"name": "blank", "datasets": [], "metrics": [], "relationships": []}]}
        )
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/blank?tab={tab}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert 'data-state-kind="empty"' in r.text
        assert 'data-state-kind="nothing_found"' not in r.text

    def test_empty_collection_with_a_nonblank_filter_still_renders_empty(self, seeded_app):
        """A3 follow-up (issue #1707): NOTHING_FOUND is defined as zero matches
        out of a NON-EMPTY universe. A `q` present against an ALREADY-empty
        collection (zero datasets regardless of any filter) must still render
        `empty`, not `nothing_found` — the filter isn't what's to blame here,
        the collection is. Guards the `counts.<type>` half of the predicate."""
        _seed_document(
            "blank", {"semantic_model": [{"name": "blank", "datasets": [], "metrics": [], "relationships": []}]}
        )
        c = seeded_app["client"]
        r = c.get(
            "/semantic-layer/blank?tab=datasets&q=anything",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert 'data-state-kind="empty"' in r.text
        assert 'data-state-kind="nothing_found"' not in r.text

    def test_whitespace_only_filter_on_empty_collection_still_renders_empty(self, seeded_app):
        """A3 follow-up (issue #1707): the router only filters on `q.strip()`
        (a whitespace-only `q` filters nothing), but the template used to
        branch on the raw, unstripped `q` — so a whitespace `q` against an
        empty collection rendered `nothing_found` naming a "filter" that
        never actually ran. Guards the `q.strip()` half of the predicate."""
        _seed_document(
            "blank", {"semantic_model": [{"name": "blank", "datasets": [], "metrics": [], "relationships": []}]}
        )
        c = seeded_app["client"]
        r = c.get(
            "/semantic-layer/blank?tab=datasets&q=%20%20%20",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert 'data-state-kind="empty"' in r.text
        assert 'data-state-kind="nothing_found"' not in r.text


class TestObjectDetail:
    def test_dataset_object_renders_fields_table_and_all_five_ai_groups(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        # Fields table: name, type, role, description.
        assert "order_id" in body
        assert "order_date" in body
        assert "Primary key" in body
        assert "Time dimension" in body
        assert "Primary key of orders." in body
        # All five AI groups, including the negative signal.
        assert "Keywords" in body
        assert "Synonyms" in body
        assert "Anti-keywords" in body
        assert "Hints" in body
        assert "Warnings" in body
        assert "refund" in body  # an anti_keywords value actually rendered
        assert "sales orders" in body  # a synonyms value
        assert "Join via customer_id" in body  # a hints value
        assert "Excludes refunds" in body  # a warnings value

    def test_anti_keywords_group_renders_even_when_empty(self, seeded_app):
        """The negative signal must render as an empty group, not vanish,
        when a document declares no anti_keywords."""
        from src.repositories import semantic_model_repo

        doc = _document_json("no_anti")
        del doc["semantic_model"][0]["datasets"][0]["ai_context"]["anti_keywords"]
        semantic_model_repo().upsert(
            id="manual/_/no_anti",
            slug="no_anti",
            name="no_anti",
            description=None,
            document="# fixture",
            document_json=doc,
            spec_version="0.2.0.dev0",
            content_hash="hash-no-anti",
            source="manual",
            source_ref=None,
            status="valid",
            validation_errors=None,
            validated_at=None,
        )
        c = seeded_app["client"]
        r = c.get("/semantic-layer/no_anti/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Anti-keywords" in r.text
        assert "None declared." in r.text

    def test_metric_object_renders_sql_and_dialect(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/metric:revenue", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "SUM(amount)" in r.text
        assert "duckdb" in r.text

    def test_relationship_object_links_both_sides(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/relationship:orders_to_customers", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        assert 'href="/semantic-layer/retail/dataset:orders"' in body
        assert 'href="/semantic-layer/retail/dataset:customers"' in body

    def test_relationship_ai_context_instructions_are_rendered(self, seeded_app):
        """Devin #1398: a relationship renders the AI-context panel, so its
        `ai_context.instructions`/examples must show — not be dropped while the
        panel says 'None declared.'"""
        doc = {
            "semantic_model": [
                {
                    "name": "rels",
                    "datasets": [{"name": "orders", "fields": []}, {"name": "customers", "fields": []}],
                    "relationships": [
                        {
                            "name": "o2c",
                            "from": "orders",
                            "to": "customers",
                            "from_columns": ["customer_id"],
                            "to_columns": ["customer_id"],
                            "ai_context": {
                                "instructions": "Join orders to customers on customer_id, never on email.",
                                "examples": ["orders.customer_id = customers.customer_id"],
                            },
                        }
                    ],
                }
            ]
        }
        _seed_document("rels", doc)
        c = seeded_app["client"]
        r = c.get("/semantic-layer/rels/relationship:o2c", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "never on email" in r.text
        assert "orders.customer_id = customers.customer_id" in r.text

    def test_constraint_object_severity_uses_fast_tooltip_not_title(self, seeded_app):
        """A7 (issue #1707): same fix as the constraints tab header, applied to
        the badge on the constraint object page — no `title=`, a `[data-tip]`
        + `aria-label` pair (same text, the repo convention) and a short
        one-sentence summary. No `role="note"` here either, for consistency
        with the other `[data-tip]` sites in the repo, none of which use it."""
        _seed_model()
        c = seeded_app["client"]
        r = c.get(
            f"/semantic-layer/{_SLUG}/constraint:region_filter_required", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200
        body = r.text
        span_match = re.search(r'<span class="badge[^"]*"[^>]*>error</span>', body)
        assert span_match, "severity badge not found"
        span_tag = span_match.group(0)
        assert "title=" not in span_tag
        assert 'role="note"' not in span_tag
        tip_match = re.search(r'data-tip="([^"]+)"', span_tag)
        assert tip_match, "severity badge is missing data-tip"
        tip_text = tip_match.group(1)
        assert len(tip_text) < 160
        assert "enforcement is soft" in tip_text
        aria_match = re.search(r'aria-label="([^"]+)"', span_tag)
        assert aria_match, "severity badge is missing aria-label"
        assert aria_match.group(1) == tip_text
        # The fuller nuance moved to a short note under the panel.
        assert "statically checkable today" in body

    def test_object_name_with_a_slash_is_reachable(self, seeded_app):
        """Devin #1398: an object name/term carrying a `/` (a glossary phrase
        like "ARR/MRR") must open its detail page — the object_id is a `:path`
        parameter split on the first colon, so the slash no longer 404s."""
        doc = {
            "semantic_model": [
                {
                    "name": "gl",
                    "datasets": [{"name": "orders", "fields": []}],
                    "custom_extensions": [
                        {
                            "vendor_name": "AGNES",
                            "data": json.dumps(
                                {
                                    "glossary": [
                                        {"term": "ARR/MRR", "definition": "Recurring revenue, annual or monthly."}
                                    ]
                                }
                            ),
                        }
                    ],
                }
            ]
        }
        _seed_document("gl", doc)
        c = seeded_app["client"]
        r = c.get("/semantic-layer/gl/glossary:ARR/MRR", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Recurring revenue, annual or monthly." in r.text

    def test_imported_source_shows_readonly_badge_and_no_edit_controls(self, seeded_app):
        _seed_model(id="manual/_/kb2", slug="kb_retail2", source="keboola_metastore")
        c = seeded_app["client"]
        r = c.get("/semantic-layer/kb_retail2/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        body = r.text
        assert "Imported from Keboola" in body
        assert "read-only" in body
        # No write affordance anywhere on the page — this increment is
        # rendering-only, no edit controls exist for any model, imported or
        # native.
        for marker in ('method="post"', ">Edit<", ">Save<", ">Delete<", "/api/admin/semantic-models"):
            assert marker not in body, f"unexpected edit affordance: {marker!r}"

    def test_native_source_has_no_imported_badge(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Imported from" not in r.text
        assert ">Native<" in r.text

    def test_unknown_object_type_is_404(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/bogus:orders", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_unknown_object_name_is_404(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:does-not-exist", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404

    def test_non_admin_without_a_grant_gets_404_on_direct_object_access(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}/dataset:orders", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 404


def _side_row(body: str, key: str) -> str | None:
    """The value of one `detail.side_rows` row in the right rail, or None when
    the row was not rendered (the macro drops a row with an empty value)."""
    m = re.search(
        r'<span class="detail-side__key">' + re.escape(key) + r"</span>\s*"
        r'<span class="detail-side__val">(.*?)</span>',
        body,
        re.DOTALL,
    )
    return m.group(1).strip() if m else None


class TestObjectDetailScaffold:
    """A5/N6 (issue #1707): the object page renders through the SHARED detail
    scaffold (`macros/_detail.html`) like the dozen other detail pages, instead
    of hand-building `.slb-panel` cards.

    Two consequences a reader can see: the model's provenance is in the right
    rail (`detail.side_rows`), which is where every other detail page keeps its
    facts; and an admin gets a `detail.manage` cluster with the doors to the
    pages that own the model's source, health and sync — previously URLs they
    had to type by hand.
    """

    _MANAGE = re.compile(r'<section class="detail-side detail-manage" data-manage>.*?</section>', re.DOTALL)

    def _object(self, seeded_app, token: str, path: str = f"/semantic-layer/{_SLUG}/dataset:orders"):
        return seeded_app["client"].get(path, headers=_auth(token))

    def test_object_sections_render_through_the_shared_section_macro(self, seeded_app):
        _seed_model()
        r = self._object(seeded_app, seeded_app["admin_token"])
        assert r.status_code == 200, r.text
        body = r.text
        assert 'class="ds-card detail-section"' in body
        assert '<h2 class="detail-section__title">' in body
        # The hand-rolled panel container is gone — that was the finding.
        assert "slb-panel" not in body

    def test_every_object_type_uses_the_section_macro(self, seeded_app):
        _seed_model()
        for object_id in (
            "dataset:orders",
            "metric:revenue",
            "relationship:orders_to_customers",
            "constraint:region_filter_required",
            "glossary:ARR",
        ):
            r = self._object(seeded_app, seeded_app["admin_token"], f"/semantic-layer/{_SLUG}/{object_id}")
            assert r.status_code == 200, object_id
            assert '<h2 class="detail-section__title">' in r.text, object_id
            assert "slb-panel" not in r.text, object_id

    def test_rail_carries_the_models_provenance(self, seeded_app):
        _seed_model(
            id="manual/_/kb3",
            slug="kb_retail3",
            source="keboola_metastore",
            source_ref="workspace-1",
        )
        r = self._object(seeded_app, seeded_app["admin_token"], "/semantic-layer/kb_retail3/dataset:orders")
        assert r.status_code == 200, r.text
        body = r.text
        assert _side_row(body, "Source") == "Keboola"
        assert _side_row(body, "Source ref") == "workspace-1"
        model_row = _side_row(body, "Model") or ""
        assert 'href="/semantic-layer/kb_retail3"' in model_row
        assert "kb_retail3" in model_row
        # `sync_mode` is a Postgres-only column, so a DuckDB-backed instance
        # reads back as the synced default rather than blowing up.
        assert "Synced" in (_side_row(body, "Sync") or "")

    def test_source_ref_row_is_dropped_when_the_model_has_none(self, seeded_app):
        _seed_model()
        r = self._object(seeded_app, seeded_app["admin_token"])
        assert r.status_code == 200, r.text
        assert _side_row(r.text, "Source ref") is None

    def test_native_model_has_no_source_row(self, seeded_app):
        """The hero badge already says Native; a rail row repeating it teaches
        the reader that the rail restates the header."""
        _seed_model()
        r = self._object(seeded_app, seeded_app["admin_token"])
        assert r.status_code == 200, r.text
        assert ">Native<" in r.text  # the header still states it
        assert _side_row(r.text, "Source") is None

    def test_detached_model_says_so_in_the_rail(self, seeded_app, monkeypatch):
        """A model an admin took off sync is the case this row exists for. The
        `sync_mode` column is Postgres-only (A3 ratchet), so the detached row is
        injected here rather than seeded — what is under test is that the
        template renders the state, which is where it was missing."""
        import app.web.router as web_router

        _seed_model()
        real = web_router._readable_model_by_slug

        def _detached(slug, user, conn):
            row = real(slug, user, conn)
            return None if row is None else {**row, "sync_mode": "detached"}

        monkeypatch.setattr(web_router, "_readable_model_by_slug", _detached)
        r = self._object(seeded_app, seeded_app["admin_token"])
        assert r.status_code == 200, r.text
        sync = _side_row(r.text, "Sync") or ""
        assert "Detached" in sync
        assert "Synced" not in sync

    def test_admin_gets_the_manage_cluster_with_one_door(self, seeded_app):
        """`manage()`'s contract: instance-scoped work goes behind ONE
        `admin_href`, and `actions` are reserved for actions on the object
        itself — which this read-only page has none of."""
        _seed_model()
        r = self._object(seeded_app, seeded_app["admin_token"])
        assert r.status_code == 200, r.text
        block = self._MANAGE.search(r.text)
        assert block, "admin is missing the manage cluster"
        manage = block.group(0)
        assert 'href="/admin/semantic-layer"' in manage
        assert "Semantic layer health" in manage
        assert "detail-manage__body" not in manage, "the cluster grew an action list again"

    def test_non_admin_gets_no_manage_cluster(self, seeded_app):
        row = _seed_model()
        _grant_model(row["id"])
        r = self._object(seeded_app, seeded_app["analyst_token"])
        assert r.status_code == 200, r.text
        assert not self._MANAGE.search(r.text), "non-admin was offered admin management links"
        assert "/admin/semantic-layer" not in r.text


class TestObjectDetailLegacyTheme:
    """The scaffold's gating rule: the rail and the redesign-only header
    affordances do not exist on a non-paper instance, so anything they carry
    must still reach the legacy page through an ungated slot. The object type
    is the one such fact here — the header this page replaced printed it
    ungated as part of "<model> · <type>"."""

    @pytest.fixture(autouse=True)
    def _legacy_theme(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "blue")

    def test_legacy_header_still_names_the_object_type(self, seeded_app):
        _seed_model()
        r = seeded_app["client"].get(
            f"/semantic-layer/{_SLUG}/constraint:region_filter_required",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.text
        assert "detail-cols" not in body, "the rail must not render on the legacy path"
        # The type, in the hero's ungated meta line...
        meta = re.search(r'<div class="detail-hero__meta">(.*?)</div>', body, re.DOTALL)
        assert meta, "legacy hero has no meta line"
        assert "Constraint" in meta.group(1)
        # ...and the model name, still in the ungated back link.
        assert f'class="detail-back" href="/semantic-layer/{_SLUG}?tab=constraints"' in body
        assert _SLUG in body

    def test_legacy_dataset_source_heading_is_not_reworded(self, seeded_app):
        """ "Source table" disambiguates the heading from the rail's provenance
        row; with no rail there is no collision, so the legacy heading keeps
        the word it has always had."""
        _seed_model()
        r = seeded_app["client"].get(
            f"/semantic-layer/{_SLUG}/dataset:orders",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert "Source table" not in r.text
        assert re.search(r'<h2 class="detail-section__title">.*?Source</h2>', r.text, re.DOTALL)


class TestLibraryEntryPoint:
    """Inbound-link guard, same bug class ``tests/test_web_nav_agents.py``
    guards against: a route is not a shipped page until something links to
    it. `/semantic-layer` is not a rail row (design-system.md's rail is
    fixed rows; a new content surface reaches the caller through an existing
    destination) — the Library page's "Definitions" footer, which already
    opens `/catalog/semantics`, is where it hangs, for both admin and
    non-admin (this is a read-tier page, not admin-only) — but only when the
    caller can actually read a model, so the link never dead-ends on the
    "No semantic model available" empty state (Devin #1398)."""

    def test_semantic_layer_linked_from_library_for_non_admin_with_a_grant(self, seeded_app):
        row = _seed_model()
        _grant_model(row["id"])
        c = seeded_app["client"]
        r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert 'href="/semantic-layer"' in r.text

    def test_no_link_for_a_non_admin_who_can_read_no_model(self, seeded_app):
        """Devin #1398: the footer link is gated on readability, so a caller
        with no grant is not sent to an empty browse page."""
        _seed_model()  # instance has a model, but this analyst has no grant
        c = seeded_app["client"]
        r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert 'href="/semantic-layer"' not in r.text

    def test_browse_link_gated_independently_of_the_footer_rendering(self, seeded_app):
        """Devin #1398 (template): the browse link is gated on
        `has_semantic_models` alone, not on whether the footer renders. A
        caller with visible metrics but no readable model gets the footer (the
        metric link) WITHOUT the /semantic-layer link — the earlier negative
        test passed only because the seeded instance had zero metrics."""
        _seed_metric()  # a visible metric forces the footer to render
        _seed_model()  # a model exists, but this analyst has no grant on it
        c = seeded_app["client"]
        r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        assert "/catalog/semantics#metrics" in r.text  # the footer did render
        assert 'href="/semantic-layer"' not in r.text  # but not the browse link

    def test_semantic_layer_linked_from_library_for_admin(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert 'href="/semantic-layer"' in r.text


class TestRegistryBackLink:
    """The other half of the #1707 N4 deep link: a metric object page points
    back at the same metric's row in the flat registry (`/catalog/semantics`),
    which carries the SQL Agnes composed, the synonyms and the source badge
    this page does not show.

    Offered only when the metric actually projected — a metric whose only
    expression is in an unusable dialect, or one the projector skipped, has no
    registry row to return to."""

    _BACK = 'href="/catalog/semantics?q=revenue#metrics"'

    def _seed_projected_metric(self) -> None:
        from src.repositories import metric_repo

        # The id `src/semantic/projection.py::projected_metric_id` writes for
        # the fixture document's `revenue` metric.
        metric_repo().create(
            id=f"manual/_/{_SLUG}/revenue",
            name="revenue",
            display_name="Revenue",
            category=_SLUG,
            sql="SELECT SUM(amount) FROM orders",
            source="manual",
        )

    def _object(self, seeded_app, path: str = f"/semantic-layer/{_SLUG}/metric:revenue"):
        return seeded_app["client"].get(path, headers=_auth(seeded_app["admin_token"]))

    def test_metric_object_page_links_back_to_its_registry_row(self, seeded_app):
        _seed_model()
        self._seed_projected_metric()
        r = self._object(seeded_app)
        assert r.status_code == 200, r.text
        assert self._BACK in r.text

    def test_no_back_link_when_the_metric_never_projected(self, seeded_app):
        _seed_model()
        r = self._object(seeded_app)
        assert r.status_code == 200, r.text
        assert "/catalog/semantics?q=" not in r.text

    def test_no_back_link_when_the_registry_row_is_rbac_hidden(self, seeded_app):
        """`/catalog/semantics` drops a metric whose table is outside the
        caller's Data Package stack (#953), so an analyst who can read the
        model but not the table would land on an empty filter."""
        from src.repositories import metric_repo, table_registry_repo

        row = _seed_model()
        _grant_model(row["id"])
        table_registry_repo().register(
            id="orders_tbl",
            name="orders_tbl",
            description="test table",
            source_type="keboola",
            query_mode="materialized",
        )
        metric_repo().create(
            id=f"manual/_/{_SLUG}/revenue",
            name="revenue",
            display_name="Revenue",
            category=_SLUG,
            sql="SELECT SUM(amount) FROM orders_tbl",
            table_name="orders_tbl",
            source="manual",
        )
        r = seeded_app["client"].get(
            f"/semantic-layer/{_SLUG}/metric:revenue",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        assert "/catalog/semantics?q=" not in r.text

    def test_non_metric_object_carries_no_registry_link(self, seeded_app):
        _seed_model()
        self._seed_projected_metric()
        r = self._object(seeded_app, f"/semantic-layer/{_SLUG}/dataset:orders")
        assert r.status_code == 200, r.text
        assert "/catalog/semantics?q=" not in r.text


class TestEmptyStateVocabulary:
    """A3 (issue #1707): the legacy `.empty-state`/`.empty-state__*` markup
    across the three browse templates is retired in favor of the shared
    `macros/_state.html` → `state.panel(kind, ...)` vocabulary, which alone
    can tell a filter collapse (`nothing_found`) apart from a genuinely empty
    collection (`empty`) — the distinction the legacy markup could not
    express."""

    def test_no_legacy_empty_state_class_in_semantic_layer_templates(self):
        templates_dir = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
        offenders = {}
        for path in sorted(templates_dir.glob("semantic_layer_*.html")):
            text = path.read_text(encoding="utf-8")
            if "empty-state" in text:
                offenders[path.name] = text.count("empty-state")
        assert not offenders, f"legacy .empty-state markup still present: {offenders}"
