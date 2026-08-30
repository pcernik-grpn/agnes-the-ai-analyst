"""Guard tests for ONE page pattern across the admin surfaces.

People, Data and Access are the three places an admin actually works, and
they are meant to read as three views of one product rather than three
products. That is a structural claim, and it decayed twice before this file
existed: each page grew a private copy of the same four objects (page head,
context strip, section head, toolbar placement), a few pixels and one type
size from the others.

What is pinned here:

(a) The HEAD is the shared primitive in its PLAIN variant — the index-page
    head /library, /agents and /chats wear (`page_hero_plain = True` →
    `.page-header--plain`), never the hero card, and never a hand-written
    copy of the markup. The hero variant stays available for pages that
    introduce themselves (/home, install); a workspace an admin stands in
    and filters is not one of those.
(b) The head is SECTION-level: every page of a section carries the section's
    name, and the tab strip below it names the lens. So /admin/users and
    /admin/tokens both say "People", the Access pair both say "Access", and
    the four Data lenses all say "Data".
(c) No eyebrow above it. The sidebar the reader used to get here already
    says "Administration".
(d) The page-local copies of the shared objects stay retired.

None of this needs a request — the templates are read off disk, so the
guard is fast and fails on the diff that reintroduces a copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")

# section name → the templates that render a page of that section. The value
# is what the h1 must say on every one of them.
SECTION_PAGES: dict[str, tuple[str, ...]] = {
    "People": ("admin_users.html", "admin_tokens.html"),
    "Data": (
        "admin_data_sources.html",
        "admin_data_packages.html",
        "admin_semantic_layer.html",
    ),
    # One page: `admin_groups.html` was a second list of the same groups and
    # 308s onto the workspace now.
    "Access": ("admin_access.html",),
}

ALL_PAGES = tuple(name for names in SECTION_PAGES.values() for name in names)


def _src(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ALL_PAGES)
def test_page_head_is_the_shared_plain_variant(name: str) -> None:
    src = _src(name)
    assert "page_hero_plain = True" in src, (
        f"{name} must opt into the plain page head (`page_hero_plain = True`) — "
        "the admin surfaces share one head with /library and /agents."
    )
    # Hand-written `<section class="page-header …">` is how a title drifts: the
    # tokens page carried "Users & Access" for a whole section split.
    assert 'class="page-header' not in src, f"{name} hand-writes page-header markup — include _page_hero.html instead."


@pytest.mark.parametrize("name", ALL_PAGES)
def test_page_head_carries_no_hero_card_and_no_eyebrow(name: str) -> None:
    src = _src(name)
    assert "page_hero_eyebrow" not in src, f"{name} sets a hero eyebrow — the sidebar already names the section."
    assert "page-header--hero" not in src, f"{name} still asks for the hero card."


@pytest.mark.parametrize(
    ("section", "name"),
    [(section, name) for section, names in SECTION_PAGES.items() for name in names],
)
def test_head_title_is_the_section_not_the_lens(section: str, name: str) -> None:
    """Every page of a section introduces the section; the tabs name the lens."""
    src = _src(name)
    assert f'page_hero_title = "{section}"' in src, (
        f"{name} is a {section} page, so its h1 must say {section!r} — the tab strip below it is what names the lens."
    )
    # The guarantee is that SOMETHING on the page names the lens, not that it
    # is the section-tab include. Access reads one set of grants three ways
    # (`?by=group|bundle|person`) and its switch is a `.tab-strip` on the list
    # itself, because switching changes what the list is of rather than which
    # page you are on — so the nav has no sub-tabs for it to include.
    has_section_tabs = '{% include "_admin_tabs.html" %}' in src
    has_own_tabs = 'class="tab-strip' in src and 'role="tablist"' in src
    assert has_section_tabs or has_own_tabs, (
        f"{name} carries a section-level head but nothing that names the lens — "
        "no section tab strip, and no tab strip of its own."
    )


def test_shared_page_vocabulary_is_loaded_by_both_admin_bases() -> None:
    for base in ("base_admin.html", "base_admin_page.html"):
        assert "css/admin_page.css" in _src(base), (
            f"{base} must load admin_page.css — it owns the section head, the "
            "context strip and the toolbar placement every admin page uses."
        )


def test_plain_head_variant_is_declared() -> None:
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    assert ".page-header--plain" in css


def test_an_admin_page_opens_at_the_same_height_as_the_index_pages() -> None:
    """An admin page begins where /library and /agents begin.

    Those open on 32px above the title (`.idx-head`); admin pages had the
    container's 16px gutter and nothing else, because `.admin-main` is a grid
    cell rather than a padded head band, so no rule had ever set one. The
    clearance is stated as the DIFFERENCE — `--space-4` here on top of the
    container's `--space-4` — so the two cannot drift apart the next time the
    container's padding is touched.

    Pinned as tokens, not as pixels: the point is that both surfaces resolve to
    the same value from the same scale, which a hard-coded `32px` on either
    side would quietly stop guaranteeing.
    """
    index_head = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    assert "padding: 32px 32px 0;" in index_head, (
        "`.idx-head`'s 32px top clearance is what the admin column matches — if "
        "it moved, move `.admin-main`'s padding-top with it."
    )

    admin_page = (STATIC / "css" / "admin_page.css").read_text(encoding="utf-8")
    assert ".admin-main { padding-top: var(--space-4); }" in admin_page, (
        "the admin content column must carry the clearance that brings it level "
        "with `.idx-head` — without it an admin page starts 16px higher than "
        "every other inventory surface in the app."
    )


# Each retired class, and the shared object that replaced it. A page that
# reintroduces one has forked the vocabulary again.
RETIRED: dict[str, str] = {
    "pp-sechead": "apg-sechead",
    "pp-count": "apg-sechead__count",
    "pp-bar": "apg-bar",
    "gp-sechead": "apg-sechead",
    "gp-count": "apg-sechead__count",
    "gp-bar": "apg-bar",
    "idp-strip": "apg-strip",
    "ds-section-header": "apg-sechead",
    "ds-section-title": "apg-sechead__title",
    "ds-section-desc": "apg-sec-desc",
    "ds-semantic-summary": "apg-strip",
    "ds-vault-banner": "apg-strip apg-strip--warn",
    "ds-footnote": "apg-foot",
    "sl-status": "apg-strip",
    "sl-section-title": "apg-sechead__title",
    "sl-section-desc": "apg-sec-desc",
    "adp-count": "apg-sechead__count",
    "adp-tray": "apg-strip apg-strip--warn",
}


@pytest.mark.parametrize("name", ALL_PAGES)
def test_no_page_local_copy_of_a_shared_object(name: str) -> None:
    src = _src(name)
    offenders = sorted(cls for cls in RETIRED if cls in src)
    assert not offenders, "\n".join(
        f"{name}: .{cls} is a page-local copy — use .{RETIRED[cls]} (admin_page.css)" for cls in offenders
    )
