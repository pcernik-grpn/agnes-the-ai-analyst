"""The five surfaces the admin cleanup retired: Studio (with its suggestions
queue), news, the maintained-digests admin page, contribute-a-skill, and the
Moderation & Trust hub.

All five are HIDDEN, not deleted. The pages, templates and APIs are intact and
each surface comes back with one flag, so what this module pins is the two
halves of "hidden":

  1. no ENTRY POINT is drawn — the admin sidebar rows, the rail's News item,
     the command-palette rows;
  2. the ROUTES do not answer — every one redirects home, including the two
     `/admin/contribute-skill` POSTs, because an external "Load skill to
     Agnes" button that still points at a hidden page must not be able to
     publish past it.

Both halves matter separately: hiding only the row leaves a bookmarked URL
working, and gating only the route leaves the column advertising a redirect.

The flags' resolution (env spellings, yaml, precedence) is covered in
tests/test_feature_flags.py and tests/test_switches.py; this module is about
what the product shows.
"""

from __future__ import annotations

import pytest

# label in the admin sidebar → (env var that re-exposes it, a page whose GET
# must stop answering while it is hidden)
SURFACES = {
    # "News editor" is the SIDEBAR label (the page authors news; /news reads it
    # — see admin_nav.py). The key here is the row text this module asserts on.
    "News editor": ("AGNES_NEWS_ENABLED", "/admin/news"),
    "Knowledge digests": ("AGNES_KNOWLEDGE_DIGESTS_ENABLED", "/admin/knowledge-digests"),
    "Contribute a skill": ("AGNES_CONTRIBUTE_SKILL_ENABLED", "/admin/contribute-skill"),
    "Studio": ("AGNES_STUDIO_ENABLED", "/admin/studio"),
    "Store moderation": ("AGNES_STORE_MODERATION_ENABLED", "/admin/store"),
}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _row(label: str) -> str:
    """The served markup for a sidebar row's LABEL.

    Rows carrying a one-line gloss wrap the label in its own span, so the
    older `>{label}</a>` shape no longer matches them — it would report every
    glossed row as absent and quietly pass the hidden-by-default assertions
    for the wrong reason. Matching the label span covers both shapes."""
    return f">{label}</span>"


# --- 1. no entry point ------------------------------------------------------


@pytest.mark.parametrize("label", sorted(SURFACES))
def test_sidebar_row_is_absent_by_default(seeded_app, label):
    """The rows stay in `admin_nav.py`'s inventory (it is what
    test_web_admin_nav.py walks to prove every admin page has a home in the
    column) — they just must not RENDER. So this asserts on the served HTML,
    not on the inventory."""
    resp = seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert _row(label) not in resp.text


@pytest.mark.parametrize("label", sorted(SURFACES))
def test_sidebar_row_comes_back_with_its_flag(seeded_app, monkeypatch, label):
    """The other direction, which is what makes the test above mean anything:
    each row is one flag away, and it is the flag named here that brings back
    that row and no other."""
    env_var, _ = SURFACES[label]
    monkeypatch.setenv(env_var, "1")
    resp = seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert _row(label) in resp.text

    # ...and only that row: the four are independent switches, not one.
    for other, (_, _page) in SURFACES.items():
        if other != label:
            assert _row(other) not in resp.text, f"{env_var} also exposed {other!r}"


def test_studio_suggestions_row_follows_the_studio_flag(seeded_app, monkeypatch):
    """The moderation queue reads `get_studio_enabled()` on its own route, so
    an unconditional row would have become a link to a redirect the moment
    Studio's default flipped."""
    c = seeded_app["client"]
    resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
    assert "Studio suggestions" not in resp.text

    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")
    resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
    assert "Studio suggestions" in resp.text


def test_news_has_no_user_facing_entry_point(seeded_app, monkeypatch):
    """The rail's account-menu item and the command-palette row were /news's
    only two non-admin entry points; both are behind `can_news` now."""
    c = seeded_app["client"]
    page, headers = "/library", _auth(seeded_app["analyst_token"])

    resp = c.get(page, headers=headers)
    assert resp.status_code == 200
    assert 'href="/news"' not in resp.text
    assert "href: '/news'" not in resp.text

    monkeypatch.setenv("AGNES_NEWS_ENABLED", "1")
    resp = c.get(page, headers=headers)
    assert 'href="/news"' in resp.text
    assert "href: '/news'" in resp.text


def test_store_moderation_has_no_palette_entry(seeded_app, monkeypatch):
    """The hub's two non-sidebar doors — the palette row and its `g v`
    shortcut — ride the same flag. A shortcut left mapped would be a keystroke
    onto a redirect, which the sidebar work would not catch."""
    c, headers = seeded_app["client"], _auth(seeded_app["admin_token"])

    resp = c.get("/admin", headers=headers)
    assert resp.status_code == 200
    assert "href: '/admin/store' }" not in resp.text
    assert "'v': '/admin/store'" not in resp.text

    monkeypatch.setenv("AGNES_STORE_MODERATION_ENABLED", "1")
    resp = c.get("/admin", headers=headers)
    assert "href: '/admin/store' }" in resp.text
    assert "'v': '/admin/store'" in resp.text


def test_admin_dashboard_signals_do_not_point_at_the_hidden_hub(seeded_app):
    """Both `/admin` dashboard cards that link to the hub — store verification
    and the C6 agent-share queue — must stop resolving while it is hidden.
    A count is an invitation to act, and the page it sends you to redirects."""
    from app.web import admin_signals

    assert admin_signals._resolve_store_verification() is None
    assert admin_signals._resolve_agent_share_requests() is None


# --- 2. the routes do not answer -------------------------------------------


@pytest.mark.parametrize("label", sorted(SURFACES))
def test_page_redirects_home_by_default(seeded_app, label):
    _, page = SURFACES[label]
    resp = seeded_app["client"].get(page, headers=_auth(seeded_app["admin_token"]), follow_redirects=False)
    assert resp.status_code in (302, 307), page
    assert resp.headers.get("location") == "/", page


@pytest.mark.parametrize("label", sorted(SURFACES))
def test_page_answers_again_with_its_flag(seeded_app, monkeypatch, label):
    env_var, page = SURFACES[label]
    monkeypatch.setenv(env_var, "1")
    resp = seeded_app["client"].get(page, headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200, f"{page}: {resp.status_code}"


def test_news_reader_redirects_too(seeded_app):
    """Not just the admin editor: /news is the reader half of the same
    surface, and its content source is the editor."""
    resp = seeded_app["client"].get("/news", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers.get("location") == "/"


@pytest.mark.parametrize(
    ("path", "form"),
    [
        # `skill_md` is a required Form field, so it has to be present: FastAPI
        # validates the body while resolving dependencies, i.e. BEFORE the
        # handler runs, and a 422 would prove nothing about the gate inside it.
        ("/admin/contribute-skill", {"skill_md": "---\nname: x\n---\n# x\n"}),
        ("/admin/contribute-skill/some-plugin/delete", {}),
    ],
)
def test_contribute_skill_posts_are_gated_not_only_the_page(seeded_app, path, form):
    """The publish and delete handlers, not just the GET. A stale external
    "Load skill to Agnes" button posts straight here; a hidden page whose POST
    still worked would publish into the contributed marketplace with nothing on
    screen to explain where the skill came from.

    Sent with NO csrf_token on purpose: that check lives inside the handler and
    answers 400, so a redirect here also pins the gate as sitting ahead of it —
    the flag decides, not the form token.
    """
    resp = seeded_app["client"].post(path, headers=_auth(seeded_app["admin_token"]), data=form, follow_redirects=False)
    assert resp.status_code in (302, 307), f"{path}: {resp.status_code}"
    assert resp.headers.get("location") == "/", path
