"""Case-table tests for the storage-independent semantic-layer query validator
(``src/semantic_validation.py``).

Design: docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md
(section 3, "Query validator") + the companion contract spec (section
"Background: Apache Ossie") for the document shape.

The engine is a pure transformation over a plain Python ``document`` dict (the
future ``semantic_models.document_json``), so these tests need no database —
they build one representative fixture document by hand and assert on the
validator's vendor-shape output. See ``_fixture_document`` below.
"""

from __future__ import annotations

import json

from src.semantic_validation import (
    AGNES_VENDOR_NAME,
    check_dialects,
    detect_used_objects,
    evaluate_constraints,
    extract_constraints,
    validate_query,
)

# --------------------------------------------------------------------------- #
# Fixture -- pending the storage slice (contract spec) landing. Shaped like a
# single entry of an Ossie ``semantic_model`` list, i.e. what will become one
# ``semantic_models.document_json`` row: 2 datasets (one with fields[] +
# ai_context incl. anti_keywords), 2 metrics (one DUCKDB expression, one
# SNOWFLAKE-only), 1 relationship, 1 glossary term, and constraints riding
# `custom_extensions` under the Agnes vendor name.
# --------------------------------------------------------------------------- #


def _agnes_extension(constraints: list[dict]) -> dict:
    return {"vendor_name": AGNES_VENDOR_NAME, "data": json.dumps({"constraints": constraints})}


def _fixture_document() -> dict:
    return {
        "name": "retail_model",
        "description": "Retail orders and customers.",
        "datasets": [
            {
                "name": "orders",
                "source": "analytics.orders",
                "primary_key": ["order_id"],
                "ai_context": {
                    "keywords": ["orders", "purchases"],
                    "synonyms": ["sales orders"],
                    "anti_keywords": ["draft_orders", "cart_items"],
                    "hints": ["Always filter by order_date for performance."],
                    "warnings": ["Do not use for refunds; see the refunds dataset."],
                },
                "fields": [
                    {"name": "order_id", "datatype": "String", "description": "Primary key."},
                    {"name": "order_date", "datatype": "Date", "description": "Order date."},
                    {"name": "customer_id", "datatype": "String", "description": "FK to customers."},
                ],
            },
            {
                "name": "customers",
                "source": "analytics.customers",
                "primary_key": ["customer_id"],
                "fields": [
                    {"name": "customer_id", "datatype": "String"},
                    {"name": "email", "datatype": "String"},
                ],
            },
        ],
        "relationships": [
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            },
        ],
        "metrics": [
            {
                "name": "revenue",
                "dataset": "orders",
                "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "SUM(orders.amount)"}]},
                "datatype": "Decimal",
            },
            {
                "name": "customer_lifetime_value",
                "dataset": "customers",
                "expression": {
                    "dialects": [
                        {
                            "dialect": "SNOWFLAKE",
                            "expression": "SUM(amount) OVER (PARTITION BY customer_id)",
                        }
                    ]
                },
                "datatype": "Decimal",
            },
        ],
        "glossary": [
            {"term": "MRR", "definition": "Monthly recurring revenue.", "seeAlso": ["revenue"]},
        ],
        "custom_extensions": [
            _agnes_extension(
                [
                    {
                        "name": "revenue_requires_date_filter",
                        "constraint_type": "required_filter",
                        "rule": "order_date",
                        "severity": "error",
                        "metrics": ["revenue"],
                    },
                    {
                        "name": "revenue_non_negative",
                        "constraint_type": "value_range",
                        "rule": "value >= 0",
                        "severity": "warning",
                        "metrics": ["revenue"],
                    },
                ]
            )
        ],
    }


# --------------------------------------------------------------------------- #
# detect_used_objects
# --------------------------------------------------------------------------- #


class TestDetectUsedObjects:
    def test_matches_dataset_metric_and_relationship_by_name(self):
        sql = "SELECT revenue FROM orders o JOIN customers c ON o.customer_id = c.customer_id"
        result = detect_used_objects(sql, _fixture_document())
        assert result["used_datasets"] == ["orders", "customers"]
        assert result["used_metrics"] == ["revenue"]
        assert result["matched_relationships"] == ["orders_to_customers"]

    def test_matches_dataset_referenced_by_qualified_source_path(self):
        result = detect_used_objects("SELECT * FROM analytics.orders", _fixture_document())
        assert result["used_datasets"] == ["orders"]

    def test_matches_dataset_via_column_name_evidence_without_dataset_name(self):
        result = detect_used_objects("SELECT order_date FROM some_view", _fixture_document())
        assert result["used_datasets"] == ["orders"]

    def test_relationship_not_matched_when_only_one_side_used(self):
        result = detect_used_objects("SELECT revenue FROM orders", _fixture_document())
        assert result["used_datasets"] == ["orders"]
        assert result["matched_relationships"] == []

    def test_word_boundary_avoids_substring_false_positive(self):
        result = detect_used_objects("SELECT * FROM historical_orders_v2", _fixture_document())
        assert result["used_datasets"] == []

    def test_matching_is_case_insensitive(self):
        result = detect_used_objects("select REVENUE from ORDERS", _fixture_document())
        assert result["used_datasets"] == ["orders"]
        assert result["used_metrics"] == ["revenue"]

    def test_empty_sql_matches_nothing(self):
        result = detect_used_objects("", _fixture_document())
        assert result == {"used_datasets": [], "used_metrics": [], "matched_relationships": []}

    def test_document_with_no_objects_returns_empty(self):
        result = detect_used_objects("SELECT 1", {})
        assert result == {"used_datasets": [], "used_metrics": [], "matched_relationships": []}


# --------------------------------------------------------------------------- #
# extract_constraints
# --------------------------------------------------------------------------- #


class TestExtractConstraints:
    def test_extracts_constraints_from_agnes_vendor_extension(self):
        constraints = extract_constraints(_fixture_document())
        assert len(constraints) == 2
        by_name = {c["name"]: c for c in constraints}
        assert by_name["revenue_requires_date_filter"]["severity"] == "error"
        assert by_name["revenue_requires_date_filter"]["metrics"] == ["revenue"]
        assert by_name["revenue_non_negative"]["severity"] == "warning"

    def test_ignores_extensions_under_other_vendor_names(self):
        document = {
            "custom_extensions": [
                {
                    "vendor_name": "some_other_vendor",
                    "data": json.dumps({"constraints": [{"name": "x", "metrics": ["revenue"]}]}),
                }
            ]
        }
        assert extract_constraints(document) == []

    def test_tolerates_missing_custom_extensions(self):
        assert extract_constraints({}) == []

    def test_tolerates_custom_extensions_not_a_list(self):
        assert extract_constraints({"custom_extensions": "not-a-list"}) == []

    def test_tolerates_malformed_json_in_data(self):
        document = {"custom_extensions": [{"vendor_name": AGNES_VENDOR_NAME, "data": "{not json"}]}
        assert extract_constraints(document) == []

    def test_tolerates_data_of_unreadable_type(self):
        document = {"custom_extensions": [{"vendor_name": AGNES_VENDOR_NAME, "data": 42}]}
        assert extract_constraints(document) == []

    def test_accepts_data_as_parsed_json_object(self):
        # Devin Review on PR #1319: storage may hand ``data`` back as parsed
        # JSON rather than a stringified blob -- same payload, must be read.
        document = {
            "custom_extensions": [
                {
                    "vendor_name": AGNES_VENDOR_NAME,
                    "data": {"constraints": [{"name": "c1", "metrics": ["revenue"], "severity": "error"}]},
                }
            ]
        }
        constraints = extract_constraints(document)
        assert len(constraints) == 1
        assert constraints[0]["name"] == "c1"
        assert constraints[0]["severity"] == "error"

    def test_accepts_data_as_parsed_json_list(self):
        document = {
            "custom_extensions": [{"vendor_name": AGNES_VENDOR_NAME, "data": [{"name": "c1", "metrics": ["revenue"]}]}]
        }
        assert len(extract_constraints(document)) == 1

    def test_tolerates_constraints_key_missing_or_wrong_type(self):
        document = {
            "custom_extensions": [{"vendor_name": AGNES_VENDOR_NAME, "data": json.dumps({"constraints": "nope"})}]
        }
        assert extract_constraints(document) == []

    def test_defaults_severity_when_absent_or_invalid(self):
        document = {
            "custom_extensions": [
                _agnes_extension(
                    [
                        {"name": "c1", "metrics": ["revenue"]},
                        {"name": "c2", "severity": "critical", "metrics": ["revenue"]},
                    ]
                )
            ]
        }
        constraints = extract_constraints(document)
        assert {c["severity"] for c in constraints} == {"warning"}

    def test_accepts_constraints_as_bare_list_payload(self):
        document = {
            "custom_extensions": [
                {
                    "vendor_name": AGNES_VENDOR_NAME,
                    "data": json.dumps([{"name": "c1", "metrics": ["revenue"], "severity": "error"}]),
                }
            ]
        }
        constraints = extract_constraints(document)
        assert len(constraints) == 1
        assert constraints[0]["severity"] == "error"

    def test_document_not_a_dict_returns_empty(self):
        assert extract_constraints(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# evaluate_constraints
# --------------------------------------------------------------------------- #


class TestEvaluateConstraints:
    def test_violation_when_required_filter_absent(self):
        constraints = extract_constraints(_fixture_document())
        violations, _post_checks = evaluate_constraints(constraints, ["revenue"], "SELECT SUM(amount) FROM orders")
        by_name = {v["name"]: v for v in violations}
        assert "revenue_requires_date_filter" in by_name
        assert by_name["revenue_requires_date_filter"]["severity"] == "error"

    def test_no_violation_when_required_filter_present(self):
        constraints = extract_constraints(_fixture_document())
        violations, _post_checks = evaluate_constraints(
            constraints,
            ["revenue"],
            "SELECT SUM(amount) FROM orders WHERE order_date >= '2026-01-01'",
        )
        assert "revenue_requires_date_filter" not in {v["name"] for v in violations}

    def test_statically_uncheckable_rule_degrades_to_post_execution(self):
        constraints = extract_constraints(_fixture_document())
        _violations, post_checks = evaluate_constraints(
            constraints,
            ["revenue"],
            "SELECT SUM(amount) FROM orders WHERE order_date >= '2026-01-01'",
        )
        assert "revenue_non_negative" in {c["name"] for c in post_checks}

    def test_constraint_skipped_when_its_metric_is_not_used(self):
        constraints = extract_constraints(_fixture_document())
        violations, post_checks = evaluate_constraints(constraints, ["customer_lifetime_value"], "SELECT 1")
        assert violations == []
        assert post_checks == []

    def test_empty_constraints_list(self):
        assert evaluate_constraints([], ["revenue"], "SELECT 1") == ([], [])


# --------------------------------------------------------------------------- #
# check_dialects
# --------------------------------------------------------------------------- #


class TestCheckDialects:
    def test_locally_executable_true_for_duckdb_only_metric(self):
        result = check_dialects(_fixture_document(), ["revenue"], target_engine="duckdb")
        assert result["sql_dialects"] == ["DUCKDB"]
        assert result["locally_executable"] is True
        assert result["mixed_dialect_warning"] is None

    def test_locally_executable_false_for_snowflake_only_metric(self):
        result = check_dialects(_fixture_document(), ["customer_lifetime_value"], target_engine="duckdb")
        assert result["sql_dialects"] == ["SNOWFLAKE"]
        assert result["locally_executable"] is False

    def test_mixed_dialect_warning_when_multiple_engines_used(self):
        result = check_dialects(_fixture_document(), ["revenue", "customer_lifetime_value"], target_engine="duckdb")
        assert set(result["sql_dialects"]) == {"DUCKDB", "SNOWFLAKE"}
        assert result["mixed_dialect_warning"] is not None
        assert result["locally_executable"] is False

    def test_no_used_metrics_means_no_dialects_and_locally_executable(self):
        result = check_dialects(_fixture_document(), [], target_engine="duckdb")
        assert result == {
            "sql_dialects": [],
            "mixed_dialect_warning": None,
            "locally_executable": True,
            "not_executable_metrics": [],
        }

    def test_no_model_document_defaults_safe(self):
        result = check_dialects({}, ["revenue"], target_engine="duckdb")
        assert result == {
            "sql_dialects": [],
            "mixed_dialect_warning": None,
            "locally_executable": True,
            "not_executable_metrics": [],
        }

    def test_ansi_only_metric_is_executable_on_any_target_engine(self):
        # Devin Review on PR #1319: ANSI_SQL is the universally accepted
        # baseline -- executability must honour the same universality the
        # mixed-dialect check composes by, whatever the target engine.
        document = {
            "metrics": [
                {
                    "name": "revenue",
                    "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
                }
            ]
        }
        for engine in ("duckdb", "snowflake", "bigquery"):
            result = check_dialects(document, ["revenue"], target_engine=engine)
            assert result["locally_executable"] is True, engine

    def test_no_mixed_warning_when_one_metric_declares_alternative_dialects(self):
        # Devin Review on PR #1319: a metric's dialects are ALTERNATIVES for
        # one expression, not requirements -- offering both engines is not a mix.
        document = {
            "metrics": [
                {
                    "name": "revenue",
                    "expression": {
                        "dialects": [
                            {"dialect": "DUCKDB", "expression": "SUM(amount)"},
                            {"dialect": "SNOWFLAKE", "expression": "SUM(amount)"},
                        ]
                    },
                }
            ]
        }
        result = check_dialects(document, ["revenue"], target_engine="duckdb")
        assert set(result["sql_dialects"]) == {"DUCKDB", "SNOWFLAKE"}
        assert result["mixed_dialect_warning"] is None
        assert result["locally_executable"] is True

    def test_no_mixed_warning_when_used_metrics_share_a_common_dialect(self):
        document = {
            "metrics": [
                {
                    "name": "revenue",
                    "expression": {
                        "dialects": [
                            {"dialect": "DUCKDB", "expression": "SUM(amount)"},
                            {"dialect": "SNOWFLAKE", "expression": "SUM(amount)"},
                        ]
                    },
                },
                {
                    "name": "orders_count",
                    "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "COUNT(*)"}]},
                },
            ]
        }
        result = check_dialects(document, ["revenue", "orders_count"], target_engine="duckdb")
        assert result["mixed_dialect_warning"] is None

    def test_not_executable_metrics_names_only_the_offenders(self):
        """`locally_executable` is one bool for the whole statement, so a
        caller that wants to say WHICH metric is unusable had to name every
        used metric — turning one bad metric into a warning against all of
        them. The names come back alongside the bool."""
        result = check_dialects(_fixture_document(), ["revenue", "customer_lifetime_value"], target_engine="duckdb")
        assert result["locally_executable"] is False
        assert result["not_executable_metrics"] == ["customer_lifetime_value"]

    def test_not_executable_metrics_is_empty_when_everything_composes(self):
        result = check_dialects(_fixture_document(), ["revenue"], target_engine="duckdb")
        assert result["locally_executable"] is True
        assert result["not_executable_metrics"] == []

    def test_a_metric_whose_dialect_entries_carry_no_body_is_named_too(self):
        """The label-only case (PR #1327) sets `locally_executable` False from
        a second loop — it must contribute its name like any other offender,
        and must not stop at the first one."""
        document = {
            "metrics": [
                {"name": "revenue", "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "  "}]}},
                {"name": "orders_count", "expression": {"dialects": [{"dialect": "DUCKDB"}]}},
                {"name": "clean", "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "COUNT(*)"}]}},
            ]
        }
        result = check_dialects(document, ["revenue", "orders_count", "clean"], target_engine="duckdb")
        assert result["locally_executable"] is False
        assert result["not_executable_metrics"] == ["revenue", "orders_count"]

    def test_case_variant_dialect_labels_count_as_one_dialect(self):
        # Devin Review on PR #1319: labels come from untrusted imported text --
        # "DUCKDB" and "duckdb" are one dialect, in the list and in the warning.
        document = {
            "metrics": [
                {"name": "revenue", "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "SUM(a)"}]}},
                {"name": "orders_count", "expression": {"dialects": [{"dialect": "duckdb", "expression": "COUNT(*)"}]}},
            ]
        }
        result = check_dialects(document, ["revenue", "orders_count"], target_engine="duckdb")
        assert result["sql_dialects"] == ["DUCKDB"]
        assert result["mixed_dialect_warning"] is None


# --------------------------------------------------------------------------- #
# validate_query (vendor-shape composition)
# --------------------------------------------------------------------------- #


class TestValidateQuery:
    def test_constraint_violation_drives_valid_false(self):
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [_fixture_document()])
        assert result["valid"] is False
        assert any(v["name"] == "revenue_requires_date_filter" for v in result["violations"])
        assert result["used_datasets"] == ["orders"]
        assert result["used_metrics"] == ["revenue"]

    def test_query_with_required_filter_present_is_valid_but_has_post_execution_check(self):
        sql = "SELECT SUM(amount) AS revenue FROM orders WHERE order_date >= '2026-01-01'"
        result = validate_query(sql, [_fixture_document()])
        assert result["valid"] is True
        assert result["violations"] == []
        assert any(c["name"] == "revenue_non_negative" for c in result["post_execution_checks"])

    def test_locally_executable_false_for_snowflake_only_metric_query(self):
        sql = "SELECT customer_lifetime_value FROM customers"
        result = validate_query(sql, [_fixture_document()], target_engine="duckdb")
        assert result["locally_executable"] is False
        assert result["sql_dialects"] == ["SNOWFLAKE"]
        # Advisory only -- a dialect trap never flips validity by itself.
        assert result["valid"] is True

    def test_not_executable_metrics_unions_across_documents(self):
        """The per-document offender lists become one de-duplicated union, in
        first-seen order, like every other list this function composes."""
        sql = "SELECT revenue, customer_lifetime_value FROM orders JOIN customers ON 1=1"
        result = validate_query(sql, [_fixture_document(), _fixture_document()], target_engine="duckdb")
        assert result["locally_executable"] is False
        assert result["not_executable_metrics"] == ["customer_lifetime_value"]

    def test_not_executable_metrics_is_empty_on_a_clean_query(self):
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [_fixture_document()])
        assert result["not_executable_metrics"] == []

    def test_dialect_mix_warning_surfaces_in_summary(self):
        sql = (
            "SELECT revenue, customer_lifetime_value FROM orders "
            "JOIN customers ON orders.customer_id = customers.customer_id"
        )
        result = validate_query(sql, [_fixture_document()])
        assert set(result["sql_dialects"]) == {"DUCKDB", "SNOWFLAKE"}
        assert "mixed" in result["summary"].lower()

    def test_empty_documents_list_is_valid_and_empty(self):
        result = validate_query("SELECT 1", [])
        assert result["valid"] is True
        assert result["used_datasets"] == []
        assert result["used_metrics"] == []
        assert result["matched_relationships"] == []
        assert result["violations"] == []
        assert result["post_execution_checks"] == []
        assert result["sql_dialects"] == []
        assert result["locally_executable"] is True
        assert isinstance(result["summary"], str) and result["summary"]

    def test_empty_sql_is_valid_and_detects_nothing(self):
        result = validate_query("", [_fixture_document()])
        assert result["valid"] is True
        assert result["used_datasets"] == []
        assert result["used_metrics"] == []

    def test_malformed_custom_extensions_document_does_not_raise(self):
        document = dict(_fixture_document())
        document["custom_extensions"] = "not-a-list"
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [document])
        assert result["violations"] == []
        assert result["post_execution_checks"] == []
        assert result["valid"] is True

    def test_expected_objects_diff(self):
        sql = "SELECT revenue FROM orders"
        expected = [
            {"type": "dataset", "name": "orders"},
            {"type": "metric", "name": "customer_lifetime_value"},
        ]
        result = validate_query(sql, [_fixture_document()], expected=expected)
        assert result["matched_expected_objects"] == [{"type": "dataset", "name": "orders"}]
        assert result["missing_expected_objects"] == [{"type": "metric", "name": "customer_lifetime_value"}]
        assert {"type": "metric", "name": "revenue"} in result["unexpected_detected_objects"]

    def test_expected_omitted_means_no_expected_keys_in_result(self):
        result = validate_query("SELECT 1", [_fixture_document()])
        assert "matched_expected_objects" not in result
        assert "missing_expected_objects" not in result
        assert "unexpected_detected_objects" not in result

    def test_summary_reports_advisory_violations_instead_of_claiming_none(self):
        # Devin Review on PR #1319: a warning-severity violation keeps
        # valid=True, but the summary must not claim "no constraint
        # violations detected" while 'violations' lists one.
        document = _fixture_document()
        document["custom_extensions"] = [
            _agnes_extension(
                [
                    {
                        "name": "prefer_date_filter",
                        "constraint_type": "required_filter",
                        "rule": "order_date",
                        "severity": "warning",
                        "metrics": ["revenue"],
                    }
                ]
            )
        ]
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [document])
        assert result["valid"] is True
        assert [v["name"] for v in result["violations"]] == ["prefer_date_filter"]
        assert "no constraint violations" not in result["summary"]
        assert "advisory" in result["summary"]

    def test_summary_with_an_error_still_names_advisory_count_and_readout(self):
        # Devin Review on PR #1319 (round 8): with a blocking violation
        # present, the summary previously reported only the error count --
        # hiding the advisory violation and dropping the dataset/metric
        # read-out entirely. The counts must agree with len(violations).
        document = _fixture_document()
        document["custom_extensions"] = [
            _agnes_extension(
                [
                    {
                        "name": "eu_only",
                        "constraint_type": "required_filter",
                        "rule": "region = 'EU'",
                        "severity": "error",
                        "metrics": ["revenue"],
                    },
                    {
                        "name": "prefer_date_filter",
                        "constraint_type": "required_filter",
                        "rule": "order_date",
                        "severity": "warning",
                        "metrics": ["revenue"],
                    },
                ]
            )
        ]
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [document])
        assert result["valid"] is False
        assert len(result["violations"]) == 2
        assert "1 blocking and 1 advisory" in result["summary"]
        assert "Query references" in result["summary"]

    def test_no_cross_document_mixed_warning_when_a_common_dialect_exists(self):
        # One metric per document; both offer DUCKDB (one alongside a
        # SNOWFLAKE alternative) -- composable, so no mixed-dialect warning.
        doc_a = {
            "datasets": [{"name": "orders", "source": "analytics.orders"}],
            "metrics": [
                {
                    "name": "revenue",
                    "expression": {
                        "dialects": [
                            {"dialect": "DUCKDB", "expression": "SUM(amount)"},
                            {"dialect": "SNOWFLAKE", "expression": "SUM(amount)"},
                        ]
                    },
                }
            ],
        }
        doc_b = {
            "datasets": [{"name": "customers", "source": "analytics.customers"}],
            "metrics": [
                {
                    "name": "customers_count",
                    "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "COUNT(*)"}]},
                }
            ],
        }
        sql = "SELECT revenue, customers_count FROM orders JOIN customers USING (customer_id)"
        result = validate_query(sql, [doc_a, doc_b])
        assert set(result["sql_dialects"]) == {"DUCKDB", "SNOWFLAKE"}
        assert "mixed" not in result["summary"].lower()
        assert result["locally_executable"] is True


class TestOffShapeDocuments:
    """Devin Review on PR #1319: imported documents are untrusted — an
    off-shape metric ``expression`` (a plain string instead of the dialect
    object) must degrade to "no declared dialects", never raise."""

    def _document_with_string_expression(self) -> dict:
        return {
            "name": "off_shape",
            "metrics": [{"name": "revenue", "expression": 'SUM("amount")'}],
            "datasets": [{"name": "orders", "source": "analytics.orders"}],
        }

    def test_check_dialects_tolerates_string_expression(self):
        result = check_dialects(self._document_with_string_expression(), ["revenue"], target_engine="duckdb")
        assert result["sql_dialects"] == []
        assert result["locally_executable"] is True

    def test_validate_query_tolerates_string_expression(self):
        result = validate_query(
            "SELECT SUM(amount) AS revenue FROM orders",
            [self._document_with_string_expression()],
        )
        assert result["valid"] is True


class TestCrossModelConstraintScoping:
    """Devin Review on PR #1319: constraints must be evaluated against the
    metrics detected in THEIR OWN model, not a name-keyed pool across all
    models — a constraint in model A must not fire on a same-named metric
    that only model B defines."""

    def _model_b_with_revenue(self) -> dict:
        return {
            "name": "model_b",
            "datasets": [{"name": "orders_b", "source": "analytics.orders_b"}],
            "metrics": [
                {"name": "revenue", "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "SUM(amount)"}]}}
            ],
        }

    def _model_a_constraint_only(self) -> dict:
        # Model A carries a constraint naming "revenue" but defines no such
        # metric itself (plausible in imported data after a partial edit).
        return {
            "name": "model_a",
            "datasets": [{"name": "unrelated", "source": "analytics.unrelated"}],
            "metrics": [],
            "custom_extensions": [
                {
                    "vendor_name": AGNES_VENDOR_NAME,
                    "data": json.dumps(
                        {
                            "constraints": [
                                {
                                    "name": "must_filter_region",
                                    "constraint_type": "required_filter",
                                    "rule": "region = 'EU'",
                                    "severity": "error",
                                    "metrics": ["revenue"],
                                }
                            ]
                        }
                    ),
                }
            ],
        }

    def test_constraint_from_other_model_does_not_fire(self):
        sql = "SELECT SUM(amount) AS revenue FROM orders_b"
        result = validate_query(sql, [self._model_a_constraint_only(), self._model_b_with_revenue()])
        assert result["violations"] == []
        assert result["valid"] is True

    def test_constraint_still_fires_inside_its_own_model(self):
        model_b = self._model_b_with_revenue()
        model_b["custom_extensions"] = self._model_a_constraint_only()["custom_extensions"]
        sql = "SELECT SUM(amount) AS revenue FROM orders_b"
        result = validate_query(sql, [model_b])
        assert [v["name"] for v in result["violations"]] == ["must_filter_region"]
        assert result["valid"] is False


class TestUniversalDialectDoesNotConstrain:
    """Devin Review on PR #1319 (round 3): a metric offering the universal
    ANSI_SQL variant ALONGSIDE an engine-specific one is composable with
    anything -- it must not constrain the mix at all, and the warning must be
    a machine-readable result field, not just summary prose."""

    def _two_metric_document(self, dialects_a: list[str], dialects_b: list[str]) -> dict:
        def expr(dialects: list[str]) -> dict:
            return {"dialects": [{"dialect": d, "expression": "SUM(x)"} for d in dialects]}

        return {
            "name": "m",
            "datasets": [{"name": "orders", "source": "analytics.orders"}],
            "metrics": [
                {"name": "revenue", "expression": expr(dialects_a)},
                {"name": "margin", "expression": expr(dialects_b)},
            ],
        }

    def test_universal_alongside_specific_never_warns(self):
        document = self._two_metric_document(["SNOWFLAKE", "ANSI_SQL"], ["DUCKDB"])
        result = validate_query("SELECT revenue, margin FROM orders", [document])
        assert result["mixed_dialect_warning"] is None
        assert "mixed SQL dialects" not in result["summary"]

    def test_genuine_mix_warns_in_a_result_field(self):
        document = self._two_metric_document(["SNOWFLAKE"], ["BIGQUERY"])
        result = validate_query("SELECT revenue, margin FROM orders", [document])
        assert result["mixed_dialect_warning"] is not None
        assert "BIGQUERY" in result["mixed_dialect_warning"]
        assert "SNOWFLAKE" in result["mixed_dialect_warning"]


class TestCaseInsensitiveNameJoins:
    """Devin Review on PR #1319 (round 4): document-internal name references
    are untrusted imported text, so the constraint->metric and
    relationship->dataset joins must match names case-insensitively, like
    every other comparison in the module -- an exact-case join silently
    drops an error-severity constraint (fail-open)."""

    def test_constraint_fires_across_case_difference(self):
        constraints = [
            {
                "name": "must_filter",
                "constraint_type": "required_filter",
                "rule": "region = 'EU'",
                "severity": "error",
                "metrics": ["Revenue"],
            }
        ]
        violations, checks = evaluate_constraints(constraints, ["revenue"], "SELECT revenue FROM orders")
        assert [v["name"] for v in violations] == ["must_filter"]
        assert checks == []

    def test_relationship_matches_across_case_difference(self):
        document = {
            "name": "m",
            "datasets": [
                {"name": "Orders", "source": "analytics.orders"},
                {"name": "Customers", "source": "analytics.customers"},
            ],
            "relationships": [{"name": "orders_customers", "from": "orders", "to": "CUSTOMERS"}],
        }
        detected = detect_used_objects("SELECT * FROM orders JOIN customers USING (id)", document)
        assert detected["matched_relationships"] == ["orders_customers"]


class TestExpectedObjectsCaseInsensitive:
    """Devin Review on PR #1319 (round 5): expected-object names come from an
    agent's tool call and are compared against imported document names -- the
    join must ignore capitalisation like every other name join, or one object
    is reported both missing and unexpected."""

    def test_expected_matches_across_case_difference(self):
        document = {
            "name": "m",
            "datasets": [{"name": "orders", "source": "analytics.orders"}],
            "metrics": [
                {"name": "revenue", "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "SUM(amount)"}]}}
            ],
        }
        result = validate_query(
            "SELECT revenue FROM orders",
            [document],
            expected=[{"type": "metric", "name": "Revenue"}],
        )
        assert result["matched_expected_objects"] == [{"type": "metric", "name": "Revenue"}]
        assert result["missing_expected_objects"] == []
        assert {"type": "metric", "name": "revenue"} not in result["unexpected_detected_objects"]


class TestRequiredFilterTolerantMatch:
    """Devin Review on PR #1319 (rounds 5-6): the required_filter presence
    check must tolerate spacing and identifier-quoting differences -- a query
    that genuinely applies the filter must not be flagged as an error-severity
    violation over `region='EU'` vs `region = 'EU'`, or over `"region"` vs
    `region` (double quotes are SQL identifier quoting, so they are stripped;
    a double-quoted *string literal* -- nonstandard SQL -- loses its quotes
    too, accepted under the best-effort contract)."""

    def _constraints(self, rule: str) -> list[dict]:
        return [
            {
                "name": "eu_only",
                "constraint_type": "required_filter",
                "rule": rule,
                "severity": "error",
                "metrics": ["revenue"],
            }
        ]

    def test_spacing_difference_is_not_a_violation(self):
        violations, _ = evaluate_constraints(
            self._constraints("region = 'EU'"),
            ["revenue"],
            "SELECT revenue FROM orders WHERE region='EU'",
        )
        assert violations == []

    def test_quoted_identifier_in_query_is_not_a_violation(self):
        # `"region"` and `region` are the same identifier in SQL -- quoting
        # the column must not turn an applied filter into a violation.
        violations, _ = evaluate_constraints(
            self._constraints("region = 'EU'"),
            ["revenue"],
            "SELECT revenue FROM orders WHERE \"region\" = 'EU'",
        )
        assert violations == []

    def test_double_quoted_rule_matches_double_quoted_query(self):
        # A double-quoted "literal" in a rule is nonstandard SQL; both sides
        # lose the quotes (identifier treatment), so the rule still matches
        # its double-quoted twin.
        violations, _ = evaluate_constraints(
            self._constraints('region = "EU"'),
            ["revenue"],
            'SELECT revenue FROM orders WHERE region = "EU"',
        )
        assert violations == []

    def test_genuinely_absent_filter_still_violates(self):
        violations, _ = evaluate_constraints(
            self._constraints("region = 'EU'"),
            ["revenue"],
            "SELECT revenue FROM orders",
        )
        assert [v["name"] for v in violations] == ["eu_only"]


class TestConstraintCaseNormalization:
    """Devin Review on PR #1319 (round 6): ``severity`` and ``type`` on an
    imported constraint are document text and must be compared casefolded like
    every other comparison in this module -- an exact-case test silently
    downgrades an ``"ERROR"`` constraint to a warning (it can then never set
    ``valid=False``) and hides a ``"Required_Filter"`` from the static
    presence check."""

    def test_severity_case_variants_normalize_to_lowercase(self):
        document = {
            "custom_extensions": [
                _agnes_extension(
                    [
                        {"name": "c1", "severity": "ERROR", "metrics": ["revenue"]},
                        {"name": "c2", "severity": "Warning", "metrics": ["revenue"]},
                    ]
                )
            ]
        }
        constraints = extract_constraints(document)
        assert [c["severity"] for c in constraints] == ["error", "warning"]

    def test_cased_vendor_name_still_yields_constraints(self):
        # Devin Review on PR #1319 (round 7): a model tagged "Agnes"/"AGNES"
        # must not silently drop every constraint (fail-open).
        ext = _agnes_extension(
            [{"name": "c1", "constraint_type": "required_filter", "rule": "x", "metrics": ["revenue"]}]
        )
        for vendor in ("Agnes", "AGNES"):
            document = {"custom_extensions": [{**ext, "vendor_name": vendor}]}
            assert [c["name"] for c in extract_constraints(document)] == ["c1"], vendor

    def test_non_string_rule_degrades_to_post_execution_not_violation(self):
        # Devin Review on PR #1319 (round 7): a structured (dict/list) rule
        # can never appear verbatim in SQL text -- stringifying it would
        # manufacture an error-severity violation on a query that genuinely
        # applies the filter. "Never a guess in either direction."
        constraints = [
            {
                "name": "date_floor",
                "constraint_type": "required_filter",
                "rule": {"column": "order_date", "op": ">="},
                "severity": "error",
                "metrics": ["revenue"],
            }
        ]
        violations, post_checks = evaluate_constraints(
            constraints, ["revenue"], "SELECT revenue FROM orders WHERE order_date >= '2026-01-01'"
        )
        assert violations == []
        assert [c["name"] for c in post_checks] == ["date_floor"]

    def test_uppercase_error_severity_drives_valid_false(self):
        document = _fixture_document()
        document["custom_extensions"] = [
            _agnes_extension(
                [
                    {
                        "name": "revenue_requires_date_filter",
                        "constraint_type": "required_filter",
                        "rule": "order_date",
                        "severity": "ERROR",
                        "metrics": ["revenue"],
                    }
                ]
            )
        ]
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [document])
        assert [v["severity"] for v in result["violations"]] == ["error"]
        assert result["valid"] is False

    def test_keboola_shaped_constraint_type_key_drives_valid_false(self):
        """`connectors/keboola/semantic_ossie.py::_compose_constraint` writes
        the constraint's type under `constraint_type`, not this module's own
        `type` key -- without accepting that key, every constraint composed
        from a real Keboola project degrades to `post_execution_checks` and
        `validate_query` can never return `valid=False` for it (wave-3 finding).
        Shaped exactly like the adapter's own composed dict."""
        document = _fixture_document()
        document["custom_extensions"] = [
            _agnes_extension(
                [
                    {
                        "name": "revenue_requires_date_filter",
                        "constraint_type": "required_filter",
                        "rule": "order_date",
                        "severity": "error",
                        "metrics": ["revenue"],
                    }
                ]
            )
        ]
        constraints = extract_constraints(document)
        assert constraints[0]["constraint_type"] == "required_filter"

        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [document])
        assert result["post_execution_checks"] == []
        assert [v["severity"] for v in result["violations"]] == ["error"]
        assert result["valid"] is False

    def test_type_case_variant_stored_normalized(self):
        document = {
            "custom_extensions": [
                _agnes_extension(
                    [{"name": "c1", "constraint_type": "Required_Filter", "rule": "x", "metrics": ["revenue"]}]
                )
            ]
        }
        constraints = extract_constraints(document)
        assert constraints[0]["constraint_type"] == "required_filter"

    def test_type_case_variant_is_statically_checked(self):
        # Hand-built constraint (not via extract_constraints): the
        # checkability comparison itself must casefold too.
        constraints = [
            {
                "name": "eu_only",
                "constraint_type": "Required_Filter",
                "rule": "region = 'EU'",
                "severity": "error",
                "metrics": ["revenue"],
            }
        ]
        violations, post_checks = evaluate_constraints(constraints, ["revenue"], "SELECT revenue FROM orders")
        assert [v["name"] for v in violations] == ["eu_only"]
        assert post_checks == []

    def test_severity_case_variant_still_blocks_when_hand_built(self):
        # Hand-built constraint with "ERROR": the violation entry must carry
        # the casefolded severity, or ``valid`` (which keys off exactly
        # "error") silently downgrades a blocking rule to advisory.
        constraints = [
            {
                "name": "eu_only",
                "constraint_type": "required_filter",
                "rule": "region = 'EU'",
                "severity": "ERROR",
                "metrics": ["revenue"],
            }
        ]
        violations, _ = evaluate_constraints(constraints, ["revenue"], "SELECT revenue FROM orders")
        assert [v["severity"] for v in violations] == ["error"]

    def test_severity_unknown_value_defaults_to_warning_when_hand_built(self):
        constraints = [
            {
                "name": "eu_only",
                "constraint_type": "required_filter",
                "rule": "region = 'EU'",
                "severity": "critical",
                "metrics": ["revenue"],
            }
        ]
        violations, _ = evaluate_constraints(constraints, ["revenue"], "SELECT revenue FROM orders")
        assert [v["severity"] for v in violations] == ["warning"]


class TestDialectJoinCallerCase:
    """Devin Review on PR #1319 (round 6): ``check_dialects`` is public API
    and its ``used_metrics`` may be caller-supplied -- a case difference
    against the document's metric declaration must not yield "no declared
    dialects", which reads as ``locally_executable=True`` (fail-open)."""

    def test_caller_cased_metric_name_finds_dialects(self):
        result = check_dialects(_fixture_document(), ["REVENUE"], target_engine="duckdb")
        assert result["sql_dialects"] == ["DUCKDB"]
        assert result["locally_executable"] is True

    def test_caller_cased_metric_name_does_not_fail_open_on_executability(self):
        result = check_dialects(_fixture_document(), ["Customer_Lifetime_Value"], target_engine="duckdb")
        assert result["sql_dialects"] == ["SNOWFLAKE"]
        assert result["locally_executable"] is False


class TestUnexpectedDetectedScope:
    """Devin Review on PR #1319 (round 6): ``expected`` listing only one
    object type (e.g. a single dataset) must not report every detected
    metric/relationship as unexpected -- a type absent from ``expected``
    means the caller expressed no expectation for it."""

    def test_type_not_enumerated_in_expected_is_not_reported_unexpected(self):
        result = validate_query(
            "SELECT revenue FROM orders",
            [_fixture_document()],
            expected=[{"type": "dataset", "name": "orders"}],
        )
        assert result["matched_expected_objects"] == [{"type": "dataset", "name": "orders"}]
        assert result["missing_expected_objects"] == []
        assert result["unexpected_detected_objects"] == []

    def test_wrong_metric_reported_when_expected_enumerates_metrics(self):
        result = validate_query(
            "SELECT revenue FROM orders",
            [_fixture_document()],
            expected=[
                {"type": "dataset", "name": "orders"},
                {"type": "metric", "name": "customer_lifetime_value"},
            ],
        )
        assert {"type": "metric", "name": "revenue"} in result["unexpected_detected_objects"]
        assert {"type": "metric", "name": "customer_lifetime_value"} in result["missing_expected_objects"]

    def test_cased_expected_type_is_checked_not_silently_dropped(self):
        # Devin Review on PR #1319 (round 7): {"type": "Dataset"} must be the
        # same expectation as {"type": "dataset"} -- a silent drop reports
        # "nothing missing" without ever checking. Caller's spelling is kept
        # in the output entry.
        result = validate_query(
            "SELECT revenue FROM orders",
            [_fixture_document()],
            expected=[{"type": "Dataset", "name": "orders"}, {"type": "Metric", "name": "nope"}],
        )
        assert result["matched_expected_objects"] == [{"type": "Dataset", "name": "orders"}]
        assert result["missing_expected_objects"] == [{"type": "Metric", "name": "nope"}]


class TestLabelOnlyDialectEntries:
    """Devin Review on PR #1319 (round 7): a dialects[] entry that carries a
    label but no expression body has nothing to compose -- it must not count
    as a declared dialect (and so cannot flip locally_executable either way)."""

    def test_label_only_entry_is_not_declared(self):
        document = {
            "name": "m",
            "datasets": [{"name": "orders", "source": "analytics.orders"}],
            "metrics": [{"name": "revenue", "expression": {"dialects": [{"dialect": "SNOWFLAKE"}]}}],
        }
        result = check_dialects(document, ["revenue"], target_engine="duckdb")
        assert result["sql_dialects"] == []
        # Not executable: the entry is dropped from the declared labels (there
        # is no fragment behind it), but a metric whose ONLY entries are
        # label-only composes on no engine at all -- see
        # TestUnusableDialectEntriesAreNotSilence. The first version of this
        # test asserted True here, which encoded exactly that hole (Devin
        # Review on PR #1327).
        assert result["locally_executable"] is False

    def test_expression_bearing_entry_still_counts(self):
        document = {
            "name": "m",
            "datasets": [{"name": "orders", "source": "analytics.orders"}],
            "metrics": [
                {
                    "name": "revenue",
                    "expression": {
                        "dialects": [
                            {"dialect": "SNOWFLAKE", "expression": 'SUM("amount")'},
                            {"dialect": "DUCKDB"},
                        ]
                    },
                }
            ],
        }
        result = check_dialects(document, ["revenue"], target_engine="duckdb")
        assert result["sql_dialects"] == ["SNOWFLAKE"]
        assert result["locally_executable"] is False


class TestUnusableDialectEntriesAreNotSilence:
    """Devin Review on PR #1327: dropping label-only entries must not make a
    metric that declares ONLY such entries read as "declares nothing".
    Nothing can be composed for it on any engine, so it is not locally
    executable -- while a metric with no expression block at all stays
    unflagged, as documented."""

    def _metric_document(self, metric: dict) -> dict:
        return {
            "name": "m",
            "datasets": [{"name": "orders", "source": "analytics.orders"}],
            "metrics": [metric],
        }

    def test_only_label_only_entries_is_not_locally_executable(self):
        document = self._metric_document(
            {"name": "revenue", "expression": {"dialects": [{"dialect": "SNOWFLAKE"}, {"dialect": "BIGQUERY"}]}}
        )
        result = check_dialects(document, ["revenue"], target_engine="duckdb")
        assert result["sql_dialects"] == []
        assert result["locally_executable"] is False

    def test_no_expression_block_at_all_stays_unflagged(self):
        document = self._metric_document({"name": "revenue"})
        result = check_dialects(document, ["revenue"], target_engine="duckdb")
        assert result["locally_executable"] is True

    def test_empty_dialects_list_stays_unflagged(self):
        document = self._metric_document({"name": "revenue", "expression": {"dialects": []}})
        result = check_dialects(document, ["revenue"], target_engine="duckdb")
        assert result["locally_executable"] is True


# --------------------------------------------------------------------------- #
# The constraint payload key, exercised through the only importer that writes one
# --------------------------------------------------------------------------- #


class TestKeboolaComposedConstraints:
    """The hand-built fixtures above pin the key; this pins that the key is
    the one the real adapter emits. Constraints have no home in the Ossie
    schema — they ride Agnes's own ``custom_extensions`` payload, so its shape
    is Agnes's to fix, and the name is ``constraint_type`` across the whole
    chain (adapter → document → ``metric_definitions.validation.rules[]`` →
    ``agnes catalog --metrics --show``). Reading anything else here made every
    imported constraint degrade to ``post_execution_checks``."""

    def _document(self, *, rule: str = "order_date", constraint_type: str = "required_filter") -> dict:
        import yaml

        from connectors.keboola.semantic_ossie import compose_document

        model_item = {"type": "semantic-model", "id": "model-1", "attributes": {"name": "core"}}
        model_items = {
            "semantic-dataset": [
                {
                    "type": "semantic-dataset",
                    "id": "ds-1",
                    "attributes": {"name": "orders", "tableId": "in.c-shop.orders", "modelUUID": "model-1"},
                }
            ],
            "semantic-metric": [
                {
                    "type": "semantic-metric",
                    "id": "m-1",
                    "attributes": {
                        "name": "revenue",
                        "sql": 'SUM("amount")',
                        "dataset": "in.c-shop.orders",
                        "modelUUID": "model-1",
                    },
                }
            ],
            "semantic-constraint": [
                {
                    "type": "semantic-constraint",
                    "id": "c-1",
                    "attributes": {
                        "name": "revenue_requires_date_filter",
                        "constraintType": constraint_type,
                        "rule": rule,
                        "metrics": ["revenue"],
                        "severity": "error",
                    },
                }
            ],
            "semantic-relationship": [],
            "semantic-glossary": [],
        }
        text = compose_document(model_item, model_items)
        assert text is not None
        return yaml.safe_load(text)["semantic_model"][0]

    def test_composed_constraint_reaches_extract_constraints(self):
        constraints = extract_constraints(self._document(constraint_type="requiredFilter"))
        assert [c["name"] for c in constraints] == ["revenue_requires_date_filter"]
        # Keboola's own camelCase value normalizes to the checkable key.
        assert constraints[0]["constraint_type"] == "requiredfilter"

    def test_composed_required_filter_is_statically_checked(self):
        result = validate_query("SELECT revenue FROM orders", [self._document()])
        assert [v["name"] for v in result["violations"]] == ["revenue_requires_date_filter"]
        assert result["valid"] is False

    def test_composed_required_filter_is_satisfied_when_present(self):
        result = validate_query("SELECT revenue FROM orders WHERE order_date >= '2026-01-01'", [self._document()])
        assert result["violations"] == []
        assert result["valid"] is True

    def test_an_uncheckable_composed_constraint_still_degrades(self):
        document = self._document(constraint_type="value_range", rule="value >= 0")
        result = validate_query("SELECT revenue FROM orders", [document])
        assert result["violations"] == []
        assert [c["name"] for c in result["post_execution_checks"]] == ["revenue_requires_date_filter"]
