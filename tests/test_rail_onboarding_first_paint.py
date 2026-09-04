"""The rail's onboarding card paints in its RESOLVED state, not a placeholder.

Regression, measured on a 1440x900 viewport: navigating between rail pages
(Library → Agents → …) made Library · Agents · Admin flash 62px too HIGH and
then drop into place. The analyst onboarding card (`#railGetStarted`) sits in
the rail foot, whose height positions every row above it, and the server
rendered it VISIBLE on every page while `chat_onboarding.js` hid it only after
`/api/chat/journey` resolved (`.is-complete`, rail.css `display: none`). So a
caller who had finished onboarding got the card for one paint on every page
load, and the bottom zone jumped up and back down with it. A caller mid-way
got a smaller version of the same thing (6px): the "N of 6 steps complete"
line rendered empty, then filled in, and the row grew.

The admin setup chain beside it never had the problem because it is rendered
server-side — count, arc and steps — for exactly this reason ("rendering it
blank first would invent a flash that has no cause"). This card now gets the
same treatment: the server already knows the caller's journey, so the first
paint carries the class, the title, the count and the arc that the script
would otherwise write a few milliseconds later. The script still owns every
later change (a step landing, "Start over onboarding"); it just has nothing to
change on load.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")

ALL_STEPS = {
    "first_asked": True,
    "explored_stack": True,
    "stack_setup_done": True,
    "catalog_discovered": True,
    "use_anywhere": True,
    "agent_created": True,
}


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()
    yield TestClient(shared_app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


@pytest.fixture
def chat_rail(web_client, monkeypatch):
    """`can_chat` true (chat enabled + an explicit grant — admin god-mode does
    not short-circuit `has_explicit_grant`), and the admin setup chain stood
    down so the analyst card is the slot's only occupant, the state every
    member is in and every admin reaches once the instance is set up."""
    import app.auth.access as access
    import app.web.router as _router

    monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
    web_client.app.state.chat_config = SimpleNamespace(enabled=True)
    monkeypatch.setitem(_router.templates.env.globals, "admin_setup_rail", lambda: None)
    return web_client


def _seed_journey(**flags):
    from src.repositories import user_journey_repo

    user_journey_repo().update("admin1", **flags)


def _card(client, cookie, path="/library"):
    """The analyst card's markup — from its opening tag to the profile row."""
    resp = client.get(path, cookies=cookie)
    assert resp.status_code == 200, resp.text[:500]
    rail = resp.text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
    m = re.search(r'<div class="rail-getstarted[^"]*"\s+id="railGetStarted"', rail)
    assert m, "the analyst onboarding card did not render"
    return rail[m.start() : rail.index('id="userMenu"')]


def _opening_tag(card):
    return card.split(">", 1)[0]


def _js_step_keys():
    js = ONBOARDING_JS.read_text(encoding="utf-8")
    block = js.split("const STEP_KEYS = [", 1)[1].split("];", 1)[0]
    return re.findall(r'"([a-z_]+)"', block)


def test_the_server_counts_the_same_steps_the_script_does():
    """`complete` is "every step done", and which flags are steps is the
    script's `STEP_KEYS` — six of the journey's seven booleans (`onboarded`
    is the greeting, not a step). The server has to agree exactly, in order,
    or it retires a card the script would bring straight back (or the
    reverse), which is the flash this exists to remove, in a new place."""
    from app.services.journey import JOURNEY_STEP_KEYS

    assert list(JOURNEY_STEP_KEYS) == _js_step_keys()


def test_a_finished_journey_paints_the_card_already_retired(chat_rail, admin_cookie):
    _seed_journey(**ALL_STEPS)
    card = _card(chat_rail, admin_cookie)
    assert "is-complete" in _opening_tag(card), (
        "a caller who has finished onboarding must not see the card for one paint on every page"
    )
    assert ">6 of 6 steps complete<" in card


def test_a_journey_in_progress_paints_its_count_title_and_arc(chat_rail, admin_cookie):
    _seed_journey(first_asked=True, explored_stack=True)
    card = _card(chat_rail, admin_cookie)
    assert "is-complete" not in _opening_tag(card)
    assert ">Continue setup<" in card
    assert ">2 of 6 steps complete<" in card
    # Same 2πr the script draws (r=15 → 94.248), same fraction.
    assert 'style="stroke-dasharray: 31.42 94.248"' in card
    # The row's accessible name is the same sentence the script writes.
    assert 'aria-label="Continue setup — 2 of 6 steps complete"' in card


def test_an_untouched_journey_paints_zero_of_six(chat_rail, admin_cookie):
    # /agents rather than /library: opening the Library IS a step ("Explore
    # your Library" lands on the visit — see the test below), so the one page
    # that leaves a fresh journey untouched is the honest probe here.
    card = _card(chat_rail, admin_cookie, path="/agents")
    assert "is-complete" not in _opening_tag(card)
    assert ">Set up Agnes<" in card
    assert ">0 of 6 steps complete<" in card
    assert 'style="stroke-dasharray: 0.00 94.248"' in card


def test_the_library_counts_the_step_its_own_visit_lands(chat_rail, admin_cookie):
    """`/library` marks `explored_stack` BEFORE it renders (the Library page
    is the "Explore your Library" step), so its own response already says
    "1 of 6" for a fresh caller — and so does the script's fetch a moment
    later, which is the whole point: the two must agree on the same page, or
    the row would still grow between the two answers."""
    card = _card(chat_rail, admin_cookie, path="/library")
    assert "is-complete" not in _opening_tag(card)
    assert ">Continue setup<" in card
    assert ">1 of 6 steps complete<" in card
    assert 'style="stroke-dasharray: 15.71 94.248"' in card


def test_an_unreadable_journey_falls_back_to_the_blank_the_script_fills(chat_rail, admin_cookie, monkeypatch):
    """A failed read is "we could not check", never "you are done" — the card
    renders exactly as it did before this change (empty count, zero arc, not
    retired) and the script resolves it, the honest fallback direction."""
    import app.web.router as _router

    monkeypatch.setitem(_router.templates.env.globals, "journey_rail", lambda user_id: None)
    _seed_journey(**ALL_STEPS)
    card = _card(chat_rail, admin_cookie)
    assert "is-complete" not in _opening_tag(card)
    assert '<span class="rail-getstarted-sub" id="rail-getstarted-count"></span>' in card
    assert "stroke-dasharray" not in card


def test_the_journey_is_read_once_per_page_and_never_on_an_admin_page(chat_rail, admin_cookie, monkeypatch):
    """The card is absent on /admin/* (the analyst journey is not the admin's
    job there), so the read must be skipped rather than spent on a card that
    does not render. And one read per page: the rail is included once."""
    import app.web.router as _router

    calls = []

    def spy(user_id):
        calls.append(user_id)
        return {"done": 0, "total": 6, "complete": False}

    monkeypatch.setitem(_router.templates.env.globals, "journey_rail", spy)
    _card(chat_rail, admin_cookie)
    assert calls == ["admin1"]
    calls.clear()
    resp = chat_rail.get("/admin", cookies=admin_cookie)
    assert resp.status_code == 200
    assert calls == []
