"""Every authoring surface opens on what the thing IS, not what it is called.

TCRD-205. The name is the least interesting decision on these forms and the
hardest to make first — you do not know what to call a thing you have not
described yet — and on the data package it was actively harmful, because the
slug derives from the name as you type, so a placeholder name immediately
became a placeholder identifier.

These assert ORDER IN THE EMITTED MARKUP, by offset, rather than checking a
comment: a reorder that puts the name back on top fails here even if every
comment still claims otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS = Path("app/web/templates/agents.html")
SKILLS = Path("app/web/templates/skills.html")
DRAWER = Path("app/web/static/js/components/package_drawer.js")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _identity_body(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i : src.index(end, i)]


# ── agents ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def agent_identity() -> str:
    return _identity_body(_read(AGENTS), "function identityBody", "function toolbar")


def test_the_agent_form_opens_on_what_it_is_for(agent_identity):
    assert agent_identity.index("Role") < agent_identity.index("data-ag-field=\"name\"")


def test_the_agent_name_is_the_last_field(agent_identity):
    name = agent_identity.index('data-ag-field="name"')
    for earlier in ("data-ag-field=\"role\"", "data-ag-field=\"instructions\"", "data-ag-tone", "data-ag-field=\"greeting\""):
        assert agent_identity.index(earlier) < name, f"{earlier} must come before the name"


# ── skills / plugins / agent templates ─────────────────────────────────────


@pytest.fixture(scope="module")
def skill_identity() -> str:
    return _identity_body(_read(SKILLS), "key: 'identity'", "key: 'content'")


def test_the_skill_form_opens_on_the_description(skill_identity):
    assert skill_identity.index("descLabel") < skill_identity.index('data-sk-field="name"')


def test_the_skill_name_is_last_and_keeps_its_hint_and_error(skill_identity):
    """The name's validity hint and field error have to travel WITH it — a
    hint for a field three positions below it is worse than no hint."""
    name = skill_identity.index('data-sk-field="name"')
    assert skill_identity.index('data-sk-field="description"') < name
    assert skill_identity.index('data-sk-field="category"') < name
    assert skill_identity.index("sk-name-hint") > name
    assert skill_identity.index("fieldErrHtml('name')") > name


# ── data package drawer ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def drawer() -> str:
    return _read(DRAWER)


def test_the_package_drawer_opens_on_what_is_in_it(drawer):
    assert drawer.index('for="pdw-desc"') < drawer.index('for="pdw-name"')


def test_the_package_name_and_slug_come_after_the_substance(drawer):
    name = drawer.index('for="pdw-name"')
    for earlier in ('for="pdw-desc"', 'for="pdw-status"', 'for="pdw-category"', 'id="pdw-tables-field"'):
        assert drawer.index(earlier) < name, f"{earlier} must come before the name"
    assert drawer.index('for="pdw-slug"') > name, "the slug follows the name, not the other way round"


def test_the_slug_still_derives_from_the_name(drawer):
    """Deriving was never the bug — deriving from a PLACEHOLDER was. With the
    name asked last it has something real to derive from, so the behaviour
    stays."""
    assert "els.slug.value = slugify(els.name.value)" in drawer
    assert "st.slugTouched" in drawer


def test_the_drawer_lede_no_longer_asks_for_a_name_first(drawer):
    assert "Name it after what it carries" not in drawer
    assert "the name comes last" in drawer
