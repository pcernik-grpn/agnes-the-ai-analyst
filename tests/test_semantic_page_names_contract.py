"""One name per semantic-layer page — a naming contract, not a styling one.

Agnes shipped FOUR semantic-layer surfaces, and three of them shipped under
the same name: ``/catalog/semantics`` and ``/semantic-layer`` rendered the
byte-identical ``<title>Semantic layer — …</title>``, and the admin lens at
``/admin/semantic-layer`` used it a third time. A reader could not tell from a
browser tab, a bookmark, a history entry or a link label which of the three
they were looking at — and the templates' own comments said so out loud
("Both pages are titled 'Semantic layer'").

Three LIVE pages remain, each named for what it is FOR:

===========================  ==========================================
``/semantic-layer``          **Semantic models** — the stored documents,
                             plus the flat metric and glossary registries
                             as its "All metrics" / "All glossary" tabs
``/admin/semantic-layer``    **Semantic layer health** — is it complete
``/admin/semantic-sources``  **Semantic sources** — where documents come from
===========================  ==========================================

``/catalog/semantics`` is the fourth, and it is no longer a page: #1707 N5
folded the flat projection into the model list and left a 308 behind. Two
pages over one semantic layer asked the reader to know, before arriving,
whether they wanted "a metric" or "the model a metric came from" — a
distinction the naming above could make legible but never remove.

What is pinned here:

(a) No two LIVE pages share a ``<title>``. This is the guard proper — a
    future page rename can move a name, but it can never re-collide. A
    redirect has no title to collide with, which is why the retired URL is
    pinned as a redirect instead.
(b) Each page's title is the decided name above, so a link label written
    against this table stays true.
(c) The health page's door points at ``/semantic-layer`` and is LABELLED
    "Semantic models". It used to point at ``/catalog/semantics`` while its
    neighbour ``/admin/semantic-sources`` pointed at ``/semantic-layer`` —
    two adjacent admin pages disagreeing about where "the layer" is. Health
    reports on documents, so it links to documents.

Live URLs are deliberately NOT renamed: every bookmark, skill reference and
deep link keeps working. Only the human-readable names changed.
"""

from __future__ import annotations

import html
import re

import pytest

#: URL → the one name that page may carry. Update this table and the
#: templates together; nothing else in the suite encodes these names.
PAGE_NAMES: dict[str, str] = {
    "/semantic-layer": "Semantic models",
    "/admin/semantic-layer": "Semantic layer health",
    "/admin/semantic-sources": "Semantic sources",
}

#: The retired URL and where it now lands. A page that folded into another
#: has no name of its own to guard — what has to hold instead is that it
#: still ANSWERS, permanently and without rewriting the request.
RETIRED_PAGES: dict[str, str] = {
    "/catalog/semantics": "/semantic-layer?tab=all_metrics",
}

#: The door on /admin/semantic-layer, asserted as a full anchor: a bare
#: substring check would pass on a stray href in a CSS comment.
HEALTH_DOOR = '<a href="/semantic-layer">Semantic models →</a>'


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _render(seeded_app, path: str) -> str:
    resp = seeded_app["client"].get(path, headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200, f"{path} → {resp.status_code}"
    return resp.text


def _page_name(body: str, path: str) -> str:
    """The page's own name — the ``<title>`` minus the instance suffix."""
    match = re.search(r"<title>(.*?)</title>", body, re.DOTALL)
    assert match, f"{path} renders no <title>"
    return html.unescape(match.group(1)).strip().split(" — ")[0].strip()


def test_no_two_semantic_pages_share_a_title(seeded_app) -> None:
    """The collision guard. Three of these were "Semantic layer" once."""
    titles: dict[str, str] = {}
    for path in PAGE_NAMES:
        body = _render(seeded_app, path)
        match = re.search(r"<title>(.*?)</title>", body, re.DOTALL)
        assert match, f"{path} renders no <title>"
        titles[path] = html.unescape(match.group(1)).strip()

    duplicates = {t for t in titles.values() if list(titles.values()).count(t) > 1}
    assert not duplicates, (
        "Semantic-layer pages share a <title>: "
        + "; ".join(f"{p} → {t!r}" for p, t in titles.items() if t in duplicates)
        + ". Each page needs a name of its own — see PAGE_NAMES in this file."
    )


@pytest.mark.parametrize(("path", "name"), sorted(PAGE_NAMES.items()))
def test_page_title_is_the_decided_name(seeded_app, path: str, name: str) -> None:
    assert _page_name(_render(seeded_app, path), path) == name


def test_health_page_door_points_at_semantic_models(seeded_app) -> None:
    """/admin/semantic-layer reports on the stored documents, so its "browse"
    link opens the documents — the same target /admin/semantic-sources uses,
    under the target's own name."""
    body = _render(seeded_app, "/admin/semantic-layer")
    assert HEALTH_DOOR in body, "the health page's door must be a labelled link to /semantic-layer"
    assert 'href="/catalog/semantics"' not in body, (
        "/admin/semantic-layer still links to the metric/glossary projection — "
        "its neighbour /admin/semantic-sources points at /semantic-layer, and the two must agree."
    )


@pytest.mark.parametrize(("path", "target"), sorted(RETIRED_PAGES.items()))
def test_retired_page_permanently_redirects(seeded_app, path: str, target: str) -> None:
    """A folded page keeps answering — with a 308, so the method and body are
    preserved and every bookmark, chat citation and skill reference lands on
    the surface that absorbed it."""
    resp = seeded_app["client"].get(path, headers=_auth(seeded_app["admin_token"]), follow_redirects=False)
    assert resp.status_code == 308, f"{path} → {resp.status_code}, expected a permanent redirect"
    assert resp.headers["location"] == target
