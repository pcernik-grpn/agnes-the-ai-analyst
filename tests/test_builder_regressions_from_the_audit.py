"""Four regressions the UX audit caught, pinned so they cannot come back.

Each of these was working code that a later change broke — which is why they
are worth guards rather than comments. They are unrelated to each other except
in provenance: all four shipped in the same fortnight of builder work, and all
four were invisible to the existing tests because each test asserted the
mechanism rather than the outcome.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "app" / "web" / "templates" / "skills.html"
MCP_JS = ROOT / "app" / "web" / "static" / "js" / "components" / "mcp_builder.js"
APPS_PAGE = ROOT / "app" / "web" / "static" / "js" / "components" / "linked_apps_page.js"
ROUTER = ROOT / "app" / "web" / "router.py"


def test_a_published_item_does_not_become_an_unfinished_draft():
    """With guardrails off a save publishes immediately, so `settle()` clears
    `reviewInProgress` and sets `justSaved`. The draft guard keyed only on the
    former, so the still-editable form re-wrote a draft for an item that was
    already live — and the Library then advertised it as "Saved in this
    browser only — not yet in your Library", linking to a create form that
    refused the name.
    """
    page = SKILLS.read_text(encoding="utf-8")
    body = re.search(r"function persist\(\) \{(.*?)\n  \}", page, re.S)
    assert body, "persist() moved — re-point this guard"
    assert "if (justSaved) return 'skipped';" in body.group(1), (
        "a keystroke after a successful publish writes a phantom draft again"
    )


def test_the_page_knows_there_is_no_model_before_the_author_types():
    """`llmUnavailable` was only ever set by a FAILED turn. On the create path
    the opening turn revealed it early; the edit path fires no opening turn, so
    the composer invited a message, took it, and only then withdrew the offer —
    discarding what was typed."""
    page = SKILLS.read_text(encoding="utf-8")
    assert "var llmUnavailable = !BUILDER_LLM_READY;" in page, (
        "the no-model state is discovered by dispatching a turn again"
    )
    assert "var BUILDER_LLM_READY =" in page, "the server no longer tells the page"
    # ...and with no model, the page must not fire an opening turn at all.
    assert "if (!llmUnavailable && !openedFor[k]" in page, (
        "the builder still opens a conversation it knows cannot be answered"
    )

    router = ROUTER.read_text(encoding="utf-8")
    assert "builder_llm_ready=_builder_llm_ready()" in router
    fn = re.search(r"def _builder_llm_ready\(\).*?\n\n\n", router, re.S)
    assert fn, "_builder_llm_ready moved — re-point this guard"
    assert "stub_enabled()" in fn.group(0), (
        "the stub answers every turn, so an instance running it is READY — "
        "reporting otherwise would hide the conversation from every local dev"
    )


def test_the_no_model_predicate_agrees_with_the_thing_it_predicts():
    """`llm_configured()` asks `create_extractor_from_env_or_config` rather
    than restating its resolution order — a first version restated it and was
    wrong in the ordinary case (a config naming a provider with no key present
    reads as configured, then raises when the turn builds the client). This
    pins the agreement across the cases that separate the two readings."""
    import os
    from unittest import mock

    from connectors.llm import create_extractor_from_env_or_config, llm_configured

    cases = [
        ({}, {}),  # nothing configured
        ({"ANTHROPIC_API_KEY": "sk-test"}, {}),
        ({"LLM_API_KEY": "sk-test"}, {}),
        ({"ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "us-east5"}, {}),
        # The case that broke the first implementation: a block that NAMES a
        # provider while no credential is present anywhere.
        ({}, {"provider": "anthropic", "model": "claude-sonnet-5"}),
    ]
    for env, ai_config in cases:
        clean = {k: "" for k in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID")}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=False):
            predicted = llm_configured(ai_config)
            try:
                create_extractor_from_env_or_config(ai_config)
                actual = True
            except ValueError:
                actual = False
            except Exception:  # noqa: BLE001 - a provider that builds but errors is still configured
                actual = True
            assert predicted is actual, f"predicate and factory disagree for env={env}"


def test_turning_a_tool_off_disables_it_and_the_panel_reads_the_stored_flag():
    """Two halves of one regression in the MCP edit builder: the loader marked
    every returned tool enabled, and the toggle deleted rather than disabled."""
    js = MCP_JS.read_text(encoding="utf-8")
    assert "draft.enabled[String(t.original_name || t.exposed_name || '')] = t.enabled !== false;" in js, (
        "the edit panel reports every tool as on again, whatever the admin set"
    )
    body = re.search(r"function saveEdit\(\) \{(.*?)\n  \}", js, re.S)
    assert body, "saveEdit moved — re-point this guard"
    assert "method: 'DELETE'" not in body.group(1).split("grants/")[0], (
        "turning a tool off destroys its registration again"
    )
    assert "{ enabled: false }" in body.group(1) and "{ enabled: true }" in body.group(1), (
        "the toggle no longer maps to enable/disable"
    )


def test_a_partial_grant_is_shown_rather_than_reported_as_nobody():
    """Groups holding SOME of a source's tools were excluded from the Access
    list, so the section printed "Nobody yet … registered and unreachable" over
    a source a group could already reach — directly under a progress line that
    said so."""
    js = MCP_JS.read_text(encoding="utf-8")
    assert "function partialRows()" in js, "partial grants are invisible again"
    body = re.search(r"function accessBody\(\) \{(.*?)\n  \}", js, re.S)
    assert body and "partialRows()" in body.group(1), "the Access section does not render them"
    assert "editing && editing.partialGroups.length" in body.group(1), (
        "the categorical 'Nobody yet' can print over a live partial grant again"
    )


def test_publishing_apps_lands_on_a_parameter_the_library_reads():
    """`?kind=` was read by nothing — the Library validates `?section=` against
    its own keys. The sole confirmation for publishing was a page identical to
    not having done it."""
    js = APPS_PAGE.read_text(encoding="utf-8")
    assert "/library?section=data_app" in js
    assert "kind=data_app" not in js
    router = ROUTER.read_text(encoding="utf-8")
    section_map = re.search(r"_SECTION_TAB = \{(.*?)\n    \}", router, re.S)
    assert section_map and '"data_app"' in section_map.group(1), (
        "data_app is no longer a section key the Library accepts"
    )
