"""Tests for `agnes catalog --metrics`."""

import re

from typer.testing import CliRunner

from cli.commands.catalog import catalog_app

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_catalog_metrics_help():
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--help"])
    assert result.exit_code == 0
    assert "--metrics" in _clean(result.output)
    assert "--show" in _clean(result.output)


def test_catalog_default_still_works():
    """Existing `agnes catalog` (no flags) behavior unchanged."""
    runner = CliRunner()
    # Help should still mention the default tables view
    result = runner.invoke(catalog_app, ["--help"])
    assert result.exit_code == 0
    # No traceback
    assert "Traceback" not in _clean(result.output)


def test_catalog_show_without_metrics_implies_metrics(monkeypatch):
    """`agnes catalog --show <id>` (no --metrics) runs the metric-detail path."""
    import cli.commands.catalog as catalog_mod

    calls: list = []
    monkeypatch.setattr(
        catalog_mod,
        "_show_metrics",
        lambda metric_ids, as_json: calls.append((metric_ids, as_json)),
    )

    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", "revenue/mrr"])
    assert result.exit_code == 0, result.output
    assert calls == [(["revenue/mrr"], False)]


def test_catalog_show_is_repeatable_and_batches_into_one_call(monkeypatch):
    """Several `--show` ids reach the detail path in ONE call.

    The point of the flag being repeatable: an agent reading twenty metric
    definitions should spend one command and leave one tool result in its
    context, not twenty of each.
    """
    import cli.commands.catalog as catalog_mod

    calls: list = []
    monkeypatch.setattr(
        catalog_mod,
        "_show_metrics",
        lambda metric_ids, as_json: calls.append((metric_ids, as_json)),
    )

    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", "revenue/mrr", "--show", "revenue/net", "--show", "orders/aov"])
    assert result.exit_code == 0, result.output
    assert calls == [(["revenue/mrr", "revenue/net", "orders/aov"], False)]


# ---------------------------------------------------------------------------
# `--show` renders the description, so it needs the server's plain-text
# projection: the column also holds descriptions imported verbatim from an
# external catalog, which are routinely rich HTML. This is the surface
# CLAUDE.md's agent rails send agents to for the canonical business
# definition, so tags here are read as part of the definition.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


def test_show_prefers_the_servers_plain_text_projection(monkeypatch):
    import cli.commands.catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "api_get",
        lambda path: _FakeResponse(
            {
                "id": "keboola/live_deals",
                "name": "live_deals",
                "description": "<p><strong>Live Deals</strong> - deals currently live.</p>",
                "description_text": "Live Deals - deals currently live.",
            }
        ),
    )

    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", "keboola/live_deals"])
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert "Description:  Live Deals - deals currently live." in out
    assert "<strong>" not in out
    assert "<p>" not in out


def test_show_falls_back_to_the_raw_column(monkeypatch):
    """An older server does not send `description_text`. Printing nothing
    would be a worse regression than printing the stored value."""
    import cli.commands.catalog as catalog_mod

    monkeypatch.setattr(
        catalog_mod,
        "api_get",
        lambda path: _FakeResponse({"id": "revenue/mrr", "name": "mrr", "description": "Total MRR."}),
    )

    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", "revenue/mrr"])
    assert result.exit_code == 0, result.output
    assert "Description:  Total MRR." in _clean(result.output)


# ---------------------------------------------------------------------------
# Constraints. `cli/templates/global_rails.md` sends agents to this exact
# command for the canonical definition, under the instruction "Never invent
# metric SQL". Handing over the SQL while silently dropping the rule that
# governs it is the worst of both: the agent acts on an authoritative-looking
# answer that is missing its own caveat.
# ---------------------------------------------------------------------------


def _show(monkeypatch, payload: dict) -> str:
    import cli.commands.catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "api_get", lambda path: _FakeResponse(payload))
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--show", payload.get("id", "revenue/mrr")])
    assert result.exit_code == 0, result.output
    return _clean(result.output)


def test_show_prints_imported_constraints(monkeypatch):
    out = _show(
        monkeypatch,
        {
            "id": "revenue/mrr",
            "name": "mrr",
            "sql": "SELECT SUM(mrr_amount) FROM subscriptions",
            "validation": {
                "rules": [
                    {
                        "name": "non_negative",
                        "constraint_type": "range",
                        "rule": "value >= 0",
                        "severity": "error",
                    }
                ]
            },
        },
    )

    assert "Constraints:" in out
    assert "non_negative" in out
    assert "value >= 0" in out
    assert "error" in out


def test_show_prints_every_rule(monkeypatch):
    out = _show(
        monkeypatch,
        {
            "id": "revenue/mrr",
            "name": "mrr",
            "validation": {
                "rules": [
                    {"name": "non_negative", "rule": "value >= 0", "severity": "error"},
                    {"name": "sane_ceiling", "rule": "value < 1e9", "severity": "warning"},
                ]
            },
        },
    )

    assert "non_negative" in out
    assert "sane_ceiling" in out
    assert "value < 1e9" in out


def test_show_without_constraints_prints_no_header(monkeypatch):
    out = _show(monkeypatch, {"id": "revenue/mrr", "name": "mrr", "sql": "SELECT 1"})
    assert "Constraints:" not in out


def test_show_does_not_swallow_an_unrecognized_validation_shape(monkeypatch):
    """`validation` is a free-form JSON column — the Keboola importer writes
    `{"rules": [...]}`, but a hand-authored or scaffolded metric may carry
    anything. An unknown shape must still reach the reader rather than be
    dropped for not matching the one shape this renderer knows."""
    out = _show(
        monkeypatch,
        {"id": "revenue/mrr", "name": "mrr", "validation": {"expected_range": [0, 100]}},
    )

    assert "Constraints:" in out
    assert "expected_range" in out
