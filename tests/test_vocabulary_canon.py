"""D5 vocabulary v1 — the same concept must read the same name across UI,
CLI and MCP.

Mirrors the static-guard style of `tests/test_ui_layout_theme.py` /
`tests/test_data_source_vocabulary.py`: cheap regressions on retired
synonyms, plus a functional check that every renamed CLI verb kept its old
spelling working as a deprecated alias (breaking-with-alias, same pattern as
`agnes admin telemetry` / `agnes admin usage`).

Canon (see `.claude/skills/agnes-conventions/references/command-ux.md` for
the broader command-UX contract this rides on top of):

- **Collection** is the file-corpus noun. `agnes stack artefacts` /
  `stack_artefact_*` MCP tools still exist (wire-level names are unchanged —
  a deeper rename than this cut takes on) but their user-facing help text
  says "collection", not "artefact"/"corpus".
- **Agent**, not "agent profile", in `agnes agent` CLI help.
- **Semantic model document**, not "Ossie document", in the semantic-model
  CLI help (the literal "Apache Ossie JSON Schema" — the actual external
  standard `agnes semantic-model schema` prints — is a different, legitimate
  usage and stays).
- **`agnes tools`**, not `agnes connectors`/`agnes connector` — the CLI
  verb collided with "Connector" = data source used throughout the docs.
- Deprecated aliases (`connectors`, `connector`, `artefacts`) keep resolving.

Two items from the original brief turned out to already be resolved, or to
conflict with a newer, already-partially-landed decision, once checked
against the current tree — not re-litigated here:

- the admin "Library" section is already relabeled "Content" (2026-08-18);
- "Store moderation"/"Store lint" are NOT renamed to "Flea …": see
  `docs/superpowers/specs/2026-08-18-admin-authoring-seam-design.md`
  decision 8, which is already retiring the word "flea" from admin-nav
  labels in the other direction (`"Flea submissions"` -> `"Submissions"`).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Retired synonyms must not reappear.
# ---------------------------------------------------------------------------


def test_stack_artefacts_help_no_longer_leads_with_corpus_wording():
    text = _read("cli/commands/stack.py")
    assert "Artefact (collection) id" not in text
    assert "Add an artefact to your Stack" not in text
    assert "Remove an artefact from your Stack" not in text
    # The command GROUP is still named `artefacts` (deprecated alias) —
    # only the user-facing wording retired the synonym.
    assert 'name="collections"' in text
    assert 'name="artefacts"' in text


def _cli_help_text(*args: str) -> str:
    from cli.main import app

    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def test_semantic_model_cli_help_retires_ossie_document_wording():
    """What a CLI user actually sees (rendered `--help`, not raw source —
    the module docstrings that never reach `--help` may still explain the
    Apache Ossie standard the format is based on)."""
    for args in (
        ("admin", "semantic-model"),
        ("admin", "semantic-model", "import"),
        ("admin", "semantic-model", "validate"),
        ("semantic-model", "apply"),
    ):
        out = _cli_help_text(*args)
        assert "Ossie document" not in out, args
        assert "Ossie YAML document" not in out, args
        assert "Ossie spec" not in out, args


def test_semantic_model_schema_command_may_still_name_the_real_standard():
    """`agnes semantic-model schema` prints the actual vendored Apache Ossie
    JSON Schema — that is a legitimate reference to the external standard,
    not the retired "my document is an Ossie document" framing."""
    out = _cli_help_text("semantic-model", "schema")
    assert "Apache Ossie JSON Schema" in out


def test_agent_cli_help_drops_the_word_profile():
    text = _read("cli/commands/agent.py")
    assert "agent profile" not in text.lower()


def test_agents_page_disabled_notice_says_agents_not_agent_profiles():
    text = _read("app/web/templates/agents.html")
    assert "agent profiles are disabled" not in text
    assert "agents are disabled" in text


def test_data_sources_page_title_is_connections():
    text = _read("app/web/templates/admin_data_sources.html")
    assert "{% block title %}Connections — " in text


# ---------------------------------------------------------------------------
# Renamed CLI verbs resolve, and their deprecated aliases keep working.
# ---------------------------------------------------------------------------


def _invoke_ok(*args: str) -> None:
    from cli.main import app

    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, f"agnes {' '.join(args)} --help failed:\n{result.output}"


def test_tools_is_canonical_connectors_and_connector_still_resolve():
    _invoke_ok("tools")
    _invoke_ok("connectors")
    _invoke_ok("connector")


def test_stack_collections_is_canonical_artefacts_still_resolves():
    _invoke_ok("stack", "collections")
    _invoke_ok("stack", "artefacts")


def test_connectors_cli_and_hint_point_at_the_new_verb():
    """The module's own help text and its "not found" hint were written
    when `connectors` was primary — both now lead with `tools`."""
    cli_text = _read("cli/commands/connectors.py")
    assert "agnes tools show <slug>" in cli_text

    api_text = _read("app/api/connectors.py")
    assert "agnes tools list" in api_text
