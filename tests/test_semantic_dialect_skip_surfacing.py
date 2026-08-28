"""A metric whose only declared expression dialect Agnes cannot run locally
(e.g. SNOWFLAKE-only) still projects into ``metric_definitions``
(``src/semantic/projection.py``, via
``src/semantic/dialect.py::resolve_expression_any`` — see the D6 dialect
projection fix) — it is not dropped. The model browse UI must still surface
which metrics are warehouse-only, so an admin knows those need server-side
execution rather than assuming every listed metric runs through a plain
local query.

B3 (remediation program) acceptance test, updated for D6: the counter and
the pill used to mean "silently dropped from the catalog"; after D6 they
mean "in the catalog, but not locally runnable".
"""

from __future__ import annotations

_SLUG = "retail_dialects"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _document_json() -> dict:
    return {
        "semantic_model": [
            {
                "name": _SLUG,
                "datasets": [{"name": "orders", "source": "db.public.orders", "fields": []}],
                "metrics": [
                    {
                        "name": "revenue",
                        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
                    },
                    {
                        "name": "arr_snowflake_only",
                        "expression": {"dialects": [{"dialect": "SNOWFLAKE", "expression": "TRY_CAST(x AS NUMBER)"}]},
                    },
                ],
            }
        ]
    }


def _seed_model() -> dict:
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=f"manual/_/{_SLUG}",
        slug=_SLUG,
        name=_SLUG,
        description=None,
        document="# fixture, not schema-authored",
        document_json=_document_json(),
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{_SLUG}",
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


class TestWarehouseOnlyMetricCount:
    def test_counts_only_the_warehouse_only_dialect_metric(self):
        from src.semantic.dialect import count_warehouse_only_metrics

        metrics = _document_json()["semantic_model"][0]["metrics"]
        assert count_warehouse_only_metrics(metrics) == 1

    def test_a_metric_with_no_expression_at_all_is_not_counted(self):
        """A different, pre-existing problem (an incomplete document, a
        genuine skip — see `ProjectionReport.skipped`) — not the
        "projects, but not locally runnable" case this counts."""
        from src.semantic.dialect import count_warehouse_only_metrics

        assert count_warehouse_only_metrics([{"name": "no_expr", "expression": {"dialects": []}}]) == 0

    def test_zero_when_every_metric_is_locally_runnable(self):
        from src.semantic.dialect import count_warehouse_only_metrics

        metrics = [
            {"name": "a", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(a)"}]}},
        ]
        assert count_warehouse_only_metrics(metrics) == 0


class TestWarehouseOnlyMetricPill:
    def test_model_detail_page_shows_the_warehouse_only_count(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert "1 metric run server-side only (warehouse dialect)" in r.text

    def test_no_pill_when_every_metric_is_locally_runnable(self, seeded_app):
        from src.repositories import semantic_model_repo

        semantic_model_repo().upsert(
            id="manual/_/clean_model",
            slug="clean_model",
            name="clean_model",
            description=None,
            document="# fixture",
            document_json={
                "semantic_model": [
                    {
                        "name": "clean_model",
                        "datasets": [{"name": "orders", "source": "db.public.orders", "fields": []}],
                        "metrics": [
                            {
                                "name": "revenue",
                                "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
                            }
                        ],
                    }
                ]
            },
            spec_version="0.2.0.dev0",
            content_hash="hash-clean",
            source="manual",
            source_ref=None,
            status="valid",
            validation_errors=None,
            validated_at=None,
        )
        c = seeded_app["client"]
        r = c.get("/semantic-layer/clean_model", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert "run server-side only (warehouse dialect)" not in r.text
