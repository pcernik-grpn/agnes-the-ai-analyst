"""A metric whose only declared expression dialect Agnes cannot run (e.g.
SNOWFLAKE-only) is silently dropped at projection (``src/semantic/
projection.py``, via ``src/semantic/dialect.py::resolve_expression``). The
model browse UI must surface how many were dropped, rather than showing no
hint that metrics went missing.

B3 (remediation program) acceptance test. The unit test must fail (function
does not exist) against unfixed code; the endpoint test must fail (no warn
pill rendered) against unfixed code.
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


class TestDialectSkipCount:
    def test_counts_only_the_unusable_dialect_metric(self):
        from src.semantic.dialect import count_dialect_skipped_metrics

        metrics = _document_json()["semantic_model"][0]["metrics"]
        assert count_dialect_skipped_metrics(metrics) == 1

    def test_a_metric_with_no_expression_at_all_is_not_counted(self):
        """A different, pre-existing problem (an incomplete document) — not
        the "silently dropped despite having SQL" case this surfaces."""
        from src.semantic.dialect import count_dialect_skipped_metrics

        assert count_dialect_skipped_metrics([{"name": "no_expr", "expression": {"dialects": []}}]) == 0

    def test_zero_when_every_metric_is_runnable(self):
        from src.semantic.dialect import count_dialect_skipped_metrics

        metrics = [
            {"name": "a", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(a)"}]}},
        ]
        assert count_dialect_skipped_metrics(metrics) == 0


class TestDialectSkipWarnPill:
    def test_model_detail_page_shows_the_skipped_count(self, seeded_app):
        _seed_model()
        c = seeded_app["client"]
        r = c.get(f"/semantic-layer/{_SLUG}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        assert "1 metric skipped (unsupported dialect)" in r.text

    def test_no_pill_when_nothing_was_skipped(self, seeded_app):
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
        assert "skipped (unsupported dialect)" not in r.text
