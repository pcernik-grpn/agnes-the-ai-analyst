"""`pseudonymize_keyed` -- the keyed (HMAC) column mask, end to end.

The builder's `hash` mask is an unsalted md5: a pseudonym whose whole point is
that it still joins, and which a dictionary over a low-entropy domain (an
email, a short id) reverses in minutes. `pseudonymize_keyed` keeps the join and
takes the dictionary away by keying the digest with the per-instance
anonymization HMAC key (`src/anonymization_key.py`) through a DuckDB-only
scalar function, `agnes_hmac`.

"DuckDB-only" is a hard boundary, not a v1 shortcut: the key exists so
pseudonyms cannot correlate across instances, and shipping the function name to
BigQuery/Databricks would either fail there or (worse) resolve to some
same-named remote function under a key Agnes does not control. So:

* the save-time validator ACCEPTS `agnes_hmac` for a table that never leaves
  the server and REFUSES it for a `query_mode='remote'` one
  (`policy_function_duckdb_only`);
* the resolver's remote-transpile helpers refuse it too, so a body hand-edited
  straight into the database still fails closed rather than leaking a policy
  Agnes cannot enforce;
* caller-authored SQL may not call it at all -- otherwise any analyst could
  compute `agnes_hmac('alice@example.com')` on the same connection and match it
  against the masked column, which is precisely the dictionary attack the key
  was bought to prevent.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from src.access_policy_compile import compile_policy
from src.access_policy_validate import PolicyValidationError, validate_policy_sql

KEY = "0123456789abcdef0123456789abcdef"

COLS = [
    {"name": "invoice_id", "type": "BIGINT"},
    {"name": "cost_center", "type": "VARCHAR"},
    {"name": "email", "type": "VARCHAR"},
    {"name": "amount_eur", "type": "DOUBLE"},
]


def _expected(value: str, key: str = KEY) -> str:
    return hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _instance_key(monkeypatch):
    from src.access_policy_udf import reset_key_cache

    reset_key_cache()
    monkeypatch.setenv("AGNES_ANONYMIZATION_HMAC_KEY", KEY)
    yield
    reset_key_cache()


# ── compiler ───────────────────────────────────────────────────────────────


class TestCompiler:
    def test_sql_snapshot(self):
        spec = {
            "table": "invoices",
            "row_rules": [],
            "row_combine": "and",
            "column_masks": {"email": "pseudonymize_keyed"},
        }
        out = compile_policy(spec, COLS)
        assert 'agnes_hmac("email") AS "email"' in out.sql
        assert out.excluded == ["email"]
        assert out.derived == ["email"]
        # Exactly one output column named email -- never a plaintext sibling.
        assert out.sql.count('"email"') == 2
        assert "SELECT *" not in out.sql

    def test_projection_keeps_the_table_column_order(self):
        spec = {
            "table": "invoices",
            "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
            "row_combine": "and",
            "column_masks": {"email": "pseudonymize_keyed"},
        }
        out = compile_policy(spec, COLS)
        assert out.sql == (
            'SELECT "invoice_id", "cost_center", agnes_hmac("email") AS "email", "amount_eur" '
            'FROM "invoices" WHERE list_contains($user_groups, "cost_center")'
        )

    def test_refused_on_a_non_text_column(self):
        """Text-only, like the other string-surgery masks: the UDF takes and
        returns VARCHAR, so a DOUBLE column would silently change type."""
        spec = {
            "table": "invoices",
            "row_rules": [],
            "row_combine": "and",
            "column_masks": {"amount_eur": "pseudonymize_keyed"},
        }
        with pytest.raises(ValueError) as exc:
            compile_policy(spec, COLS)
        assert "pseudonymize_keyed" in str(exc.value)

    def test_md5_hash_mask_is_unchanged(self):
        """The older mask stays exactly as it was -- this is an addition, not
        a replacement, and existing saved policies keep meaning what they said."""
        spec = {
            "table": "invoices",
            "row_rules": [],
            "row_combine": "and",
            "column_masks": {"email": "hash"},
        }
        assert 'md5("email") AS "email"' in compile_policy(spec, COLS).sql


# ── save-time validator ────────────────────────────────────────────────────


class TestValidator:
    def test_accepted_for_a_server_only_table(self):
        validate_policy_sql(
            'SELECT "invoice_id", agnes_hmac("email") AS "email" FROM "invoices"',
            table_id="invoices",
            table_name="invoices",
            mapping_table_names=set(),
            for_remote=False,
        )

    def test_compiled_output_passes_the_validator(self):
        out = compile_policy(
            {
                "table": "invoices",
                "row_rules": [{"column": "cost_center", "op": "in_caller_groups"}],
                "row_combine": "and",
                "column_masks": {"email": "pseudonymize_keyed"},
            },
            COLS,
        )
        validate_policy_sql(
            out.sql,
            table_id="invoices",
            table_name="invoices",
            mapping_table_names=set(),
            for_remote=False,
        )

    def test_refused_for_a_remote_table(self):
        with pytest.raises(PolicyValidationError) as exc:
            validate_policy_sql(
                'SELECT "invoice_id", agnes_hmac("email") AS "email" FROM "invoices"',
                table_id="invoices",
                table_name="invoices",
                mapping_table_names=set(),
                for_remote=True,
            )
        assert exc.value.reason == "policy_function_duckdb_only"
        # The refusal names the function and the way out, so a retry -- human
        # or agent -- has something concrete to change.
        assert "agnes_hmac" in exc.value.detail
        assert "md5" in exc.value.detail

    def test_the_transpile_check_alone_would_not_have_caught_it(self):
        """Pinned because it is the reason the explicit refusal exists:
        sqlglot happily emits `AGNES_HMAC(...)` for any dialect, so a remote
        policy would have saved clean and then run under a function Agnes
        does not control (or none at all)."""
        import sqlglot

        for engine in ("bigquery", "databricks"):
            out = sqlglot.transpile(
                'SELECT agnes_hmac("email") AS "email" FROM "invoices"', read="duckdb", write=engine
            )
            assert "AGNES_HMAC" in out[0].upper()

    def test_md5_is_still_accepted_for_a_remote_table(self):
        validate_policy_sql(
            'SELECT "invoice_id", md5("email") AS "email" FROM "invoices"',
            table_id="invoices",
            table_name="invoices",
            mapping_table_names=set(),
            for_remote=True,
        )


# ── resolver: defence in depth for a hand-edited row ───────────────────────


class TestRemoteTranspileRefusal:
    def test_bigquery_transpile_refuses_the_function(self):
        from src.access_policy import PolicyError, _transpile_policy_to_bigquery

        with pytest.raises(PolicyError):
            _transpile_policy_to_bigquery('SELECT agnes_hmac("email") AS "email" FROM "invoices"', table_id="invoices")

    def test_databricks_transpile_refuses_the_function(self):
        from src.access_policy import PolicyError, _transpile_policy_to_databricks

        with pytest.raises(PolicyError):
            _transpile_policy_to_databricks(
                'SELECT agnes_hmac("email") AS "email" FROM "invoices"', table_id="invoices"
            )

    def test_an_ordinary_body_still_transpiles(self):
        from src.access_policy import _transpile_policy_to_bigquery

        assert _transpile_policy_to_bigquery('SELECT md5("email") AS "email" FROM "invoices"', table_id="invoices")
