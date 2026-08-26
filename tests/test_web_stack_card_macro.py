"""Shared stack_card Jinja macro — render contract.

The macro powers /catalog + /memory Browse/My Stack cards (Task 8.1 of
the v49 unified-stack plan). It mirrors the marketplace.html .mp-card
look but with dual-encoded states (color + text badge) so the cards
read at a11y minimum 3:1 contrast even with color removed.

These tests render the macro standalone (no Flask/FastAPI context) so a
breakage in the macro itself is caught without dragging the full app
into the failure mode.
"""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader


def _env():
    return Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=True,
    )


def _render(entry: dict) -> str:
    env = _env()
    tmpl = env.from_string(
        """{% from "macros/_stack_card.html" import card %}{{ card(entry) }}"""
    )
    return tmpl.render(entry=entry)


def test_default_available_card_renders_add_button():
    html = _render({
        "id": "p1",
        "name": "Sales bundle",
        "icon": "📦",
        "color": "#fce7f3",
        "requirement": "available",
        "in_stack": False,
        "description": "Orders + line items",
    })
    assert 'class="stack-card"' in html
    assert "Sales bundle" in html
    assert "+ Add to stack" in html
    assert "Automatic" not in html
    assert "Remove" not in html


def test_required_card_renders_badge_and_disabled_button():
    html = _render({
        "id": "p1",
        "name": "Sales bundle",
        "icon": "📦",
        "color": "#fce7f3",
        "requirement": "required",
        "in_stack": True,
    })
    assert "is-required" in html
    assert "is-in-stack" in html
    # Tier badge in the top-right corner — "Automatic", the human word for the required tier.
    assert ">Automatic<" in html
    # No Remove or Add buttons — required is non-removable.
    assert 'data-action="remove"' not in html
    assert 'data-action="add"' not in html
    assert "disabled" in html


def test_available_subscribed_card_renders_remove_button():
    html = _render({
        "id": "d1",
        "name": "Sales Playbook",
        "icon": "🎯",
        "color": "#dcfce7",
        "requirement": "available",
        "in_stack": True,
        "meta": "18 items · 4 required",
    })
    assert "is-in-stack" in html
    assert "is-required" not in html
    assert 'data-action="remove"' in html
    assert ">Remove<" in html
    # "In stack" badge variant present.
    assert "stack-card__req-badge--instack" in html


def test_drilldown_link_renders_when_provided():
    html = _render({
        "id": "d1",
        "name": "Engineering",
        "requirement": "available",
        "in_stack": False,
        "drilldown_url": "/memory/d/engineering",
        "footer_left": "View 18 items →",
    })
    assert 'href="/memory/d/engineering"' in html
    assert "View 18 items →" in html


def test_tags_render_as_pills():
    html = _render({
        "id": "p1",
        "name": "X",
        "requirement": "available",
        "in_stack": False,
        "tags": ["keboola", "bigquery"],
    })
    assert "stack-card__tag" in html
    assert ">keboola<" in html
    assert ">bigquery<" in html


def test_data_attributes_carry_state():
    html = _render({
        "id": "abc123",
        "name": "X",
        "requirement": "required",
        "in_stack": True,
    })
    assert 'data-id="abc123"' in html
    assert 'data-requirement="required"' in html
    assert 'data-in-stack="1"' in html
