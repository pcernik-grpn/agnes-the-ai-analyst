"""Render tests for the fact-graph type map macro (TCRD-250).

These RENDER the macro through Jinja rather than grepping its source, so
they fail on a broken template rather than on a reworded comment. The macro
is deliberately free of app globals (no `static_url`, no request), which is
what lets it be imported into the Knowledge tab with one line — a bare
`Environment` over the templates directory is enough to exercise it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"


@pytest.fixture(scope="module")
def render():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)

    def _render(**kwargs) -> str:
        tpl = env.from_string(
            "{% from 'macros/_type_map.html' import type_map %}{{ type_map(**kwargs) }}"
        )
        return tpl.render(kwargs=kwargs)

    return _render


def test_every_type_renders_with_its_count(render):
    html = render(types=[{"type": "client", "count": 9}, {"type": "engagement", "count": 12}], total=21)
    assert "client" in html
    assert "engagement" in html
    assert ">9<" in html
    assert ">12<" in html


def test_counts_are_labelled_as_caller_scoped(render):
    """The evidence rule has to be visible in the same glance as the number
    — a reader who knows the real total is bigger must be able to see why
    this one is smaller, without hovering anything."""
    html = render(types=[{"type": "client", "count": 9}], total=9)
    assert "evidence you can read" in html
    assert "no access" in html


def test_a_type_links_to_its_filtered_view_when_a_base_url_is_given(render):
    html = render(types=[{"type": "service_offering", "count": 3}], total=3, base_url="/library")
    assert 'href="/library?type=service_offering"' in html


def test_without_a_base_url_chips_are_not_links(render):
    html = render(types=[{"type": "client", "count": 1}], total=1)
    assert "<a " not in html
    assert "tmap-chip--static" in html


def test_underscored_type_names_read_as_words(render):
    html = render(types=[{"type": "service_offering", "count": 3}], total=3)
    assert "service offering" in html


def test_empty_map_says_which_of_the_two_reasons_applies(render):
    """No types is either "nothing extracted" or "nothing you can read" —
    the response cannot distinguish them (that is the non-disclosure), so
    the copy names both rather than asserting the wrong one."""
    html = render(types=[], total=0)
    assert "extracted into the graph" in html
    assert "collection you can read" in html


def test_singular_and_plural_agree(render):
    one = render(types=[{"type": "client", "count": 1}], total=1)
    assert "1 thing across" in one
    assert "1 type" in one
    many = render(types=[{"type": "client", "count": 2}, {"type": "person", "count": 3}], total=5)
    assert "5 things across" in many
    assert "2 types" in many


def test_a_type_name_cannot_inject_markup(render):
    """Type names come from producer-controlled ontology data, so the macro
    must escape them like any other untrusted string."""
    html = render(types=[{"type": "<img src=x onerror=alert(1)>", "count": 1}], total=1)
    assert "<img" not in html
    assert "&lt;img" in html
