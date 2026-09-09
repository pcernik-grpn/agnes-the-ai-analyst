"""Tests for LLM table auto-documentation (#399).

The pure core (`src.table_autodoc`) is exercised with a fake extractor — no
live LLM. The CLI command is exercised end-to-end against a seeded system
DuckDB with the extractor monkeypatched, proving the repo + profile wiring.
"""

from __future__ import annotations

import typer
import pytest
from typer.testing import CliRunner

from src.table_autodoc import build_prompt, generate_description


class _FakeExtractor:
    """Stands in for connectors.llm StructuredExtractor."""

    def __init__(self, description="Synthetic test description.", error=None):
        self.description = description
        self.error = error
        self.calls = []

    def extract_json(self, *, prompt, max_tokens, json_schema, schema_name):
        self.calls.append({"prompt": prompt, "schema_name": schema_name, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        return {"description": self.description}


# --------------------------------------------------------------------------- #
# Pure core
# --------------------------------------------------------------------------- #


def test_build_prompt_includes_table_columns_and_samples():
    p = build_prompt(
        "orders",
        [{"name": "id", "type": "STRING"}, {"name": "amount", "type": "NUMERIC"}],
        [{"id": "a1", "amount": "9.99"}],
        source="bigquery",
    )
    assert "orders" in p and "bigquery" in p
    assert "id (STRING)" in p and "amount (NUMERIC)" in p
    assert "9.99" in p  # sample value surfaced


def test_build_prompt_tolerates_missing_metadata():
    p = build_prompt("t", None, None)
    assert "(no column metadata)" in p and "(no sample rows)" in p


def test_generate_description_returns_model_text():
    fx = _FakeExtractor("One row per order.")
    out = generate_description(fx, "orders", [{"name": "id", "type": "STRING"}], [{"id": "x"}])
    assert out == "One row per order."
    assert fx.calls and fx.calls[0]["schema_name"] == "table_description"


def test_generate_description_strips_and_handles_empty():
    assert generate_description(_FakeExtractor("  padded.  "), "t", [], []) == "padded."
    assert generate_description(_FakeExtractor(""), "t", [], []) == ""


def test_generate_description_propagates_llm_error():
    from connectors.llm.exceptions import LLMError

    with pytest.raises(LLMError):
        generate_description(_FakeExtractor(error=LLMError("boom")), "t", [], [])


# --------------------------------------------------------------------------- #
# CLI — agnes admin autodoc-tables
# --------------------------------------------------------------------------- #


def _cli_app():
    """Throwaway Typer hosting just the command (callback prevents Typer from
    hoisting the single command and dropping the verb)."""
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        pass

    from cli.commands.admin_autodoc import autodoc_tables

    app.command("autodoc-tables")(autodoc_tables)
    return app


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from src.db import get_system_db
    from src.repositories.profiles import ProfileRepository
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    reg = TableRegistryRepository(conn)
    prof = ProfileRepository(conn)

    # A: empty description + profile -> a target.
    reg.register(id="orders", name="Orders", source_type="bigquery")
    prof.save(
        "orders",
        {
            "columns": [{"name": "id", "type": "STRING"}, {"name": "amount", "type": "NUMERIC"}],
            "sample_rows": [{"id": "a1", "amount": "9.99"}],
        },
    )
    # B: already described + profile -> must be left untouched.
    reg.register(id="users", name="Users", source_type="bigquery", description="Already described.")
    prof.save("users", {"columns": [{"name": "uid", "type": "STRING"}], "sample_rows": [{"uid": "u1"}]})
    # C: empty description, NO profile -> skipped.
    reg.register(id="events", name="Events", source_type="bigquery")
    return tmp_path


def _reg():
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    return TableRegistryRepository(get_system_db())


def test_cli_fills_empty_description_and_respects_existing(seeded, monkeypatch):
    import cli.commands.admin_autodoc as m

    monkeypatch.setattr(m, "_build_extractor", lambda: _FakeExtractor("One row per order."))
    r = CliRunner().invoke(_cli_app(), ["autodoc-tables"])
    assert r.exit_code == 0, r.output

    reg = _reg()
    assert reg.get("orders")["description"] == "One row per order."  # filled
    assert reg.get("users")["description"] == "Already described."  # not clobbered
    assert (reg.get("events")["description"] or "") == ""  # skipped (no profile)


def test_cli_dry_run_saves_nothing(seeded, monkeypatch):
    import cli.commands.admin_autodoc as m

    monkeypatch.setattr(m, "_build_extractor", lambda: _FakeExtractor("Would be saved."))
    r = CliRunner().invoke(_cli_app(), ["autodoc-tables", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "Would be saved." in r.output
    assert (_reg().get("orders")["description"] or "") == ""  # nothing persisted


def test_cli_table_filter_targets_one(seeded, monkeypatch):
    import cli.commands.admin_autodoc as m

    monkeypatch.setattr(m, "_build_extractor", lambda: _FakeExtractor("Just orders."))
    r = CliRunner().invoke(_cli_app(), ["autodoc-tables", "--table", "orders"])
    assert r.exit_code == 0, r.output
    assert _reg().get("orders")["description"] == "Just orders."


def test_cli_unknown_table_errors(seeded, monkeypatch):
    import cli.commands.admin_autodoc as m

    monkeypatch.setattr(m, "_build_extractor", lambda: _FakeExtractor())
    r = CliRunner().invoke(_cli_app(), ["autodoc-tables", "--table", "nope"])
    assert r.exit_code == 1


def test_the_description_call_names_the_table_it_documents():
    """An autodoc sweep is one call per table — the record says which."""
    from src.observability.llm_context import current_llm_context

    seen = {}

    class _RecordingExtractor(_FakeExtractor):
        def extract_json(self, **kwargs):
            seen["context"] = current_llm_context()
            return super().extract_json(**kwargs)

    generate_description(_RecordingExtractor(), "orders", None, None)

    ctx = seen["context"]
    assert (ctx.workload, ctx.purpose) == ("semantic_layer", "table_autodoc")
    assert ctx.subject_id == "orders"
