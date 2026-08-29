"""Shared state.panel Jinja macro — render contract.

TCRD-207: one vocabulary for "nothing matched" / "exists but you can't see
it" / "ran successfully, produced nothing" / "failed" — see
docs/superpowers/specs/2026-08-29-empty-blocked-forbidden-vocabulary-design.md.

Renders standalone (no FastAPI context) so a macro regression is caught
without dragging the app into the failure mode, mirroring
test_web_stack_card_macro.py.
"""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader


def _env():
    return Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=True,
    )


def _render(*args, **kwargs) -> str:
    env = _env()
    tmpl = env.from_string(
        """{% import "macros/_state.html" as state %}"""
        """{{ state.panel(*args, **kwargs) }}"""
    )
    return tmpl.render(args=args, kwargs=kwargs)


def test_nothing_found_and_empty_share_the_neutral_tone():
    """Both are "nothing went wrong" states — same tone, different copy —
    per the spec's precedence rule (only failed/blocked get a tinted card)."""
    nothing_found = _render("nothing_found", "Nothing matches these filters")
    empty = _render("empty", "Your library is empty")
    assert "state-panel--neutral" in nothing_found
    assert "state-panel--neutral" in empty
    assert "state-panel--warn" not in nothing_found
    assert "state-panel--danger" not in empty


def test_blocked_is_the_only_warn_tone():
    html = _render("blocked", "Not shared with you yet")
    assert "state-panel--warn" in html
    assert "state-panel--neutral" not in html
    assert "state-panel--danger" not in html


def test_failed_is_the_only_danger_tone():
    html = _render("failed", "Couldn't load your conversations")
    assert "state-panel--danger" in html
    assert "state-panel--neutral" not in html
    assert "state-panel--warn" not in html


def test_body_and_cta_render_when_provided():
    html = _render(
        "empty",
        "Your library is empty",
        body="Use + Add to upload a file.",
        cta_label="Upload a file",
        cta_attrs="data-new-upload",
    )
    assert "Use + Add to upload a file." in html
    assert ">Upload a file<" in html
    assert "data-new-upload" in html
    assert "<a " not in html  # no cta_href given -> a <button>, never an <a>


def test_cta_href_renders_an_anchor():
    html = _render("blocked", "Not shared", cta_label="Ask an admin", cta_href="/admin/access")
    assert '<a class="state-panel__cta cc-btn" href="/admin/access">Ask an admin</a>' in html


def test_body_is_omitted_when_not_given():
    html = _render("empty", "Nothing here")
    assert "state-panel__text" not in html


def test_id_and_hidden_pass_through_for_js_toggling():
    html = _render("failed", "Couldn't load", id="cloud-chat-failed-state", hidden=True)
    assert 'id="cloud-chat-failed-state"' in html
    assert "hidden" in html


def test_compact_variant_carries_the_modifier_class():
    html = _render("failed", "Couldn't load", compact=True)
    assert "state-panel--compact" in html
    assert "state-panel--danger" in html


def test_each_kind_gets_a_distinct_icon():
    """Visual distinctness is icon + tone together — two neutral-tone states
    (nothing_found, empty) must still render different glyphs so DES-111's
    "same quiet grey row" complaint doesn't reappear inside one tone."""
    icons = {
        kind: _render(kind, "x").split("state-panel__icon", 2)[1]
        for kind in ("nothing_found", "empty", "blocked", "failed")
    }
    assert len(set(icons.values())) == 4, "each kind must render a distinct icon body"
