"""Tests for `agnes search` (unified knowledge search CLI, K2).

All network calls are monkeypatched — no running server required.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from cli.commands.search import search_app

runner = CliRunner()

_BODY = {
    "query": "invoices",
    "results": [
        {
            "type": "chunk",
            "score": 1.0,
            "filename": "billing.md",
            "ordinal": 0,
            "text": "Invoices are generated monthly.",
        },
        {
            "type": "knowledge",
            "score": 0.8,
            "id": "ki1",
            "title": "Billing policy",
            "snippet": "We invoice monthly.",
        },
        {
            "type": "table",
            "score": 0.5,
            "table_id": "t_orders",
            "name": "orders",
            "pivot_hint": "structured data — query with SQL via `agnes query`, table id: t_orders",
        },
        {
            "type": "metric",
            "score": 0.7,
            "id": "revenue/mrr",
            "name": "mrr",
            "display_name": "Monthly Recurring Revenue",
            "category": "revenue",
            "description": "Total MRR from active subscriptions.",
        },
        {
            "type": "glossary",
            "score": 0.5,
            "id": "kb/m/churn",
            "term": "Churn Rate",
            "definition": "Percent of customers lost in a period.",
        },
    ],
}


def test_search_renders_all_three_types():
    with patch("cli.commands.search.api_get_json", return_value=_BODY) as m:
        r = runner.invoke(search_app, ["invoices"])
    assert r.exit_code == 0, r.output
    m.assert_called_once_with("/api/knowledge/search", q="invoices", k=10)
    assert "billing.md" in r.output
    assert "Billing policy" in r.output
    assert "agnes query" in r.output


def test_search_renders_metric_type():
    with patch("cli.commands.search.api_get_json", return_value=_BODY):
        r = runner.invoke(search_app, ["invoices"])
    assert r.exit_code == 0, r.output
    assert "mtrc" in r.output
    assert "Monthly Recurring Revenue" in r.output
    assert "Total MRR from active subscriptions." in r.output


def test_search_renders_glossary_type():
    with patch("cli.commands.search.api_get_json", return_value=_BODY):
        r = runner.invoke(search_app, ["invoices"])
    assert r.exit_code == 0, r.output
    assert "term" in r.output
    assert "Churn Rate" in r.output
    assert "Percent of customers lost" in r.output


def test_search_json_output():
    with patch("cli.commands.search.api_get_json", return_value=_BODY):
        r = runner.invoke(search_app, ["--json", "invoices"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["query"] == "invoices"


def test_search_no_matches():
    with patch("cli.commands.search.api_get_json", return_value={"query": "x", "results": []}):
        r = runner.invoke(search_app, ["x"])
    assert r.exit_code == 0
    assert "No matches." in r.output


def test_search_api_error_exits_nonzero():
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.search.api_get_json",
        side_effect=V2ClientError(status_code=500, body={"detail": "boom"}),
    ):
        r = runner.invoke(search_app, ["x"])
    assert r.exit_code == 1


def test_search_limit_is_alias_for_k():
    with patch("cli.commands.search.api_get_json", return_value=_BODY) as m:
        r = runner.invoke(search_app, ["--limit", "3", "invoices"])
    assert r.exit_code == 0, r.output
    m.assert_called_once_with("/api/knowledge/search", q="invoices", k=3)


def test_search_server_prints_sources_line():
    with patch("cli.commands.search.api_get_json", return_value=_BODY):
        r = runner.invoke(search_app, ["invoices"])
    assert r.exit_code == 0, r.output
    assert "sources: documents + knowledge + catalog + metrics + glossary + plugins (server)" in r.output


def test_search_no_matches_still_prints_sources_line():
    with patch("cli.commands.search.api_get_json", return_value={"query": "x", "results": []}):
        r = runner.invoke(search_app, ["x"])
    assert r.exit_code == 0
    assert "No matches." in r.output
    assert "sources: documents + knowledge + catalog + metrics + glossary + plugins (server)" in r.output


def test_search_local_and_explicit_scope_server_conflict_exits_1():
    r = runner.invoke(search_app, ["--local", "--scope", "server", "invoices"])
    assert r.exit_code == 1


def test_search_invalid_scope_exits_1():
    r = runner.invoke(search_app, ["--scope", "bogus", "invoices"])
    assert r.exit_code == 1
