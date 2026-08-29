"""Every admin sidebar row and the page it opens call the place the same thing.

The drift this catches is invisible one page at a time and obvious in a column:
the sidebar said "Store lint" and the page said "Skill Lint"; the sidebar said
"Corporate memory" and the page said "Memory Review"; "Marketplaces" opened
"Curated Marketplaces"; "Linked apps" opened "Link Keboola apps". Eleven rows
had drifted before this guard existed, because nothing compares the two — the
design-system contract tests police colour and layout, not language.

Asserted against the RENDERED page, not the templates, because that is what a
reader sees and because resolving href → template statically is exactly the
fragile step that lets a page slip through (a route registered outside
`app/web/router.py`, like `/admin/chat`, resolves to nothing).

Scope — sidebar ITEM rows only:

  * DESTINATION sections (People, Data, Access) are one page with a tab strip,
    so the heading is correctly the SECTION name and the tabs are lenses
    within it. Comparing a tab label to the page heading would demand
    "Tokens" as the title of the People page. See `admin_nav.py`'s two-tier
    docstring.
  * Rows behind a `when` flag are included — the flags are all turned on here,
    which is the only state where those pages render at all.

The comparison ignores case and whitespace but not words: this checks that both
places name the same thing, it does not enforce a house style.
"""

from __future__ import annotations

import re

import pytest

from app.web.admin_nav import ADMIN_NAV_SECTIONS

# Every `when` flag in the inventory, so a gated row's page actually renders.
_FLAGS = (
    "AGNES_STUDIO_ENABLED",
    "AGNES_NEWS_ENABLED",
    "AGNES_KNOWLEDGE_DIGESTS_ENABLED",
    "AGNES_CONTRIBUTE_SKILL_ENABLED",
    "AGNES_STORE_MODERATION_ENABLED",
)

# Item rows from the legacy GROUP sections only — see the module docstring for
# why the tabbed destinations are out of scope.
_ROWS = [
    (entry["label"], entry["href"])
    for section in ADMIN_NAV_SECTIONS
    if not section.get("tabs")
    for entry in section["items"]
]


def _auth(token: str) -> dict:
    # `Accept: text/html` is not decoration — `/admin/chat` content-negotiates
    # and answers JSON (`{"sessions": []}`) without it, which is what a caller
    # of the API gets and NOT what the nav row opens. Sending what a browser
    # sends is the only way this guard reads the page a reader sees.
    return {"Authorization": f"Bearer {token}", "Accept": "text/html"}


def _headings(html: str) -> list[str]:
    """Every candidate name the page gives itself: the design-system hero
    title, any `<h1>`, and the `<title>`. The row's label must match ONE of
    them — a page is free to carry more than one heading, it just may not
    call itself something else entirely."""
    out: list[str] = []
    for pattern in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        out.extend(re.findall(pattern, html, re.S | re.I))
    # The hero renders through a macro, so also accept the raw set value that
    # `base_page.html` consumes — present in the served HTML as the heading.
    return [" ".join(re.sub(r"<[^>]+>", " ", h).split()) for h in out]


def _normalize(text: str) -> str:
    # Collapse case, whitespace and the "— <instance name>" tail that every
    # <title> carries, so "Store lint — Acme" matches the row "Store lint".
    text = text.replace("&amp;", "&").split("—")[0].split(" - ")[0]
    return " ".join(text.lower().split())


@pytest.mark.parametrize("label,href", _ROWS, ids=[r[1] for r in _ROWS])
def test_sidebar_row_and_its_page_agree_on_the_name(seeded_app, monkeypatch, label, href):
    for flag in _FLAGS:
        monkeypatch.setenv(flag, "1")

    resp = seeded_app["client"].get(href, headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200, f"{href} did not render: {resp.status_code}"

    wanted = _normalize(label)
    found = [_normalize(h) for h in _headings(resp.text)]
    assert wanted in found, (
        f'sidebar row "{label}" ({href}) opens a page that calls itself '
        f"{found[:4]!r}. Rename one so the column and the page agree — the row "
        f"is usually the name to keep (admin_nav.py is the inventory)."
    )
