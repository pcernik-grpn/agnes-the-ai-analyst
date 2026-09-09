"""`features.onboarding_enabled` — the operator's kill switch for the analyst
onboarding layer.

Why a switch exists at all: the guided tour narrates over the product on a
first visit, and an instance whose users already know Agnes (or whose operator
would rather introduce it their own way) had no way to stop it. Turning it off
is an operator decision, so it goes through the switch registry — one entry,
one `/admin/server-config` row, one env var — rather than a per-page opt-out.

What "off" covers, and the seam that makes it enforceable: every path into a
tour goes through one of `tour.js`'s three exported entry points, and every
paint of the checklist through one of `chat_onboarding.js`'s two. Gating those
five is exhaustive by construction, which is what these tests pin — a sixth
entry point added later without the gate is the failure mode a call-site-by-
call-site check would miss.

What "off" deliberately does NOT cover: the ADMIN setup chain (a different rail
card — an operator silencing the walkthrough keeps their own instance-setup
progress), the chat's greeting, and the empty-Stack question that recommends
data packages. Those answer a user's question rather than narrating over it.
"""

import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TOUR_JS = Path("app/web/static/js/tour.js")
ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")

#: Every exported way into a tour. `tour.js` has no other one: `_startTour` is
#: module-private and reached only from these.
TOUR_ENTRY_POINTS = ("launchTour", "autoLaunchTour", "resumePendingTour")

#: Every way the checklist gets painted or mounted.
CHECKLIST_ENTRY_POINTS = ("renderJourneyPanel", "mountJourneyPanel")


# ── fixtures ────────────────────────────────────────────────────────────────
# Modelled on tests/test_rail_onboarding_first_paint.py: the card under test is
# the same one, and it needs the same `can_chat` + stood-down-admin-chain state
# to be the rail foot's only occupant.


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
    """`can_chat` true and the admin setup chain stood down, so the analyst
    onboarding card is the rail foot's only occupant."""
    import app.web.router as _router
    from app.auth import access

    monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
    web_client.app.state.chat_config = SimpleNamespace(enabled=True)
    monkeypatch.setitem(_router.templates.env.globals, "admin_setup_rail", lambda: None)
    return web_client


def _rail(client, cookie, path="/library"):
    resp = client.get(path, cookies=cookie)
    assert resp.status_code == 200, resp.text[:500]
    return resp.text


def _js_body(source: str, name: str) -> str:
    """The body of a top-level JS function, from its signature to the closing
    brace in column 0. Good enough because every function here is top-level
    and the file is formatted that way."""
    m = re.search(rf"^(?:export )?(?:async )?function {re.escape(name)}\(", source, re.MULTILINE)
    assert m, f"{name} not found — did it get renamed?"
    end = source.index("\n}", m.start())
    return source[m.start() : end]


# ── the registry entry ──────────────────────────────────────────────────────


def test_the_switch_is_registered_and_defaults_to_on():
    """A kill switch for an EXISTING surface defaults to the surface being
    there: `docs/feature-flags.md`'s "new features default OFF" rule is about
    new features, and defaulting this one off would silently retire onboarding
    on every instance that upgrades."""
    from app.switches import get_switch

    sw = get_switch("onboarding")
    assert sw.config_keys == ("features", "onboarding_enabled")
    assert sw.env_var == "AGNES_ONBOARDING_ENABLED"
    assert sw.kind == "bool"
    assert sw.default is True, "an upgrade must not turn onboarding off"
    assert sw.effect == "live", "flipping it must not need a restart — it is read per request"
    assert sw.editable is True, "an operator has to be able to flip it in /admin/server-config"


def test_the_switch_is_offered_in_the_admin_editor():
    """`editable=True` is only half the write path: `POST /api/admin/server-config`
    validates the SECTION, so the `features` section has to be writable for the
    flag to be settable at all."""
    from app.api.admin import _EDITABLE_SECTIONS

    assert "features" in _EDITABLE_SECTIONS


# ── resolution ──────────────────────────────────────────────────────────────


def test_it_resolves_on_by_default_and_off_from_the_env(monkeypatch):
    from app.web.router import _onboarding_enabled

    monkeypatch.delenv("AGNES_ONBOARDING_ENABLED", raising=False)
    assert _onboarding_enabled() is True

    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "0")
    assert _onboarding_enabled() is False

    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "false")
    assert _onboarding_enabled() is False

    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "1")
    assert _onboarding_enabled() is True


def test_an_unreadable_switch_leaves_onboarding_on(monkeypatch):
    """Fail OPEN. Onboarding is the pre-switch behaviour, so a config read that
    throws must not retire a surface every instance has — the honest direction
    for a guard whose absence is invisible."""
    from app import switches

    def boom(_name):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(switches, "switch_value", boom)
    from app.web.router import _onboarding_enabled

    assert _onboarding_enabled() is True


# ── what the server renders ─────────────────────────────────────────────────


def test_on_the_page_stamps_the_flag_true_and_renders_the_card(chat_rail, admin_cookie, monkeypatch):
    monkeypatch.delenv("AGNES_ONBOARDING_ENABLED", raising=False)
    html = _rail(chat_rail, admin_cookie)
    assert "window._agOnboardingEnabled = true;" in html
    assert 'id="railGetStarted"' in html
    assert 'id="rail-restart-onboarding"' in html


def test_off_the_card_is_not_rendered_at_all(chat_rail, admin_cookie, monkeypatch):
    """Not hidden with a class, not left for the script to retire — absent. The
    rail foot's height positions every row above it, so an empty-but-present
    card would reserve space for a surface the operator turned off."""
    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "0")
    html = _rail(chat_rail, admin_cookie)
    assert "window._agOnboardingEnabled = false;" in html
    assert 'id="railGetStarted"' not in html
    assert 'id="chat-journey"' not in html


def test_off_the_restart_onboarding_menu_entry_goes_too(chat_rail, admin_cookie, monkeypatch):
    """With no checklist there is nothing to start over: the entry would reset
    journey state and show nothing, which is the dead control its existing
    `can_chat and not _admin_page` gate exists to avoid."""
    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "0")
    html = _rail(chat_rail, admin_cookie)
    assert 'id="rail-restart-onboarding"' not in html
    assert "Start over onboarding" not in html


def test_off_the_checklist_module_is_not_even_imported(chat_rail, admin_cookie, monkeypatch):
    """The card it mounts into is gone, so loading the module would fetch
    /api/chat/journey to paint nothing."""
    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "0")
    html = _rail(chat_rail, admin_cookie)
    assert "mountJourneyPanel" not in html


def test_off_the_admin_setup_chain_is_untouched(web_client, admin_cookie, monkeypatch):
    """The scope line that matters most: an operator silencing the ANALYST
    walkthrough keeps their own instance-setup progress. Two different cards,
    two different audiences, one switch that only owns the first."""
    import app.web.router as _router
    from app.auth import access

    monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
    web_client.app.state.chat_config = SimpleNamespace(enabled=True)
    monkeypatch.setitem(
        _router.templates.env.globals,
        "admin_setup_rail",
        lambda: SimpleNamespace(done=1, total=4, complete=False, steps=[]),
    )
    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "0")
    html = _rail(web_client, admin_cookie)
    assert 'id="railSetupChain"' in html, "the admin setup chain is not part of this switch"


def test_the_journey_api_keeps_recording_while_the_ui_is_off(chat_rail, admin_cookie, monkeypatch):
    """Off gates UI only. The steps keep landing, so flipping the switch back on
    resumes every user exactly where they were instead of restarting them."""
    monkeypatch.setenv("AGNES_ONBOARDING_ENABLED", "0")
    resp = chat_rail.put("/api/chat/journey", json={"first_asked": True}, cookies=admin_cookie)
    assert resp.status_code == 200, resp.text[:300]
    read = chat_rail.get("/api/chat/journey", cookies=admin_cookie)
    assert read.status_code == 200, read.text[:300]
    assert read.json().get("first_asked") is True


# ── the client-side seam ────────────────────────────────────────────────────


@pytest.mark.parametrize("name", TOUR_ENTRY_POINTS)
def test_every_tour_entry_point_consults_the_switch(name):
    """Exhaustive by construction, and this is the test that keeps it that way:
    a fourth exported entry point added without the gate leaves that path
    launching tours on an instance whose operator turned onboarding off."""
    body = _js_body(TOUR_JS.read_text(encoding="utf-8"), name)
    assert "_onboardingEnabled()" in body, f"{name} does not check the onboarding switch"


@pytest.mark.parametrize("name", CHECKLIST_ENTRY_POINTS)
def test_every_checklist_entry_point_consults_the_switch(name):
    body = _js_body(ONBOARDING_JS.read_text(encoding="utf-8"), name)
    assert "onboardingEnabled()" in body, f"{name} does not check the onboarding switch"


def test_a_refused_resume_clears_the_stashed_record():
    """A cross-page hop stashed mid-tour outlives the switch being flipped.
    Refusing without clearing would re-enter `resumePendingTour` on every page
    load of that tab for the record's whole freshness window — and re-suppress
    an unrelated coach-mark through `_pendingResumesHere` if the switch went
    back on inside it."""
    body = _js_body(TOUR_JS.read_text(encoding="utf-8"), "resumePendingTour")
    gate = body[: body.index("\n", body.index("_onboardingEnabled()"))]
    assert "clearPending()" in gate, "a refused resume must drop the stashed record, not just return"


@pytest.mark.parametrize(
    ("path", "helper"),
    [(TOUR_JS, "_onboardingEnabled"), (ONBOARDING_JS, "onboardingEnabled")],
)
def test_undefined_means_on(path, helper):
    """Only an explicit `false` disables. The flag is absent on a page rendered
    before this switch existed, in a unit test that imports the module with no
    chrome, and in any embedding that does not include `_app_scripts.html` —
    none of which is an operator turning onboarding off. A truthiness check
    (`if (!window._agOnboardingEnabled)`) would read all three as "off"."""
    body = _js_body(path.read_text(encoding="utf-8"), helper)
    assert "window._agOnboardingEnabled !== false" in body, (
        "the gate must compare against false explicitly, so a missing flag means ON"
    )


def test_the_flag_is_stamped_from_the_shared_partial():
    """`_app_scripts.html`, not a per-page block: `tour.js` is imported from
    four places (chat_onboarding.js, agents.html, skills.html, base_ds.html's
    resume boot), and this partial is the one thing both `base.html` and
    `base_ds.html` include — so every page that can reach a tour carries the
    flag."""
    partial = Path("app/web/templates/_app_scripts.html").read_text(encoding="utf-8")
    assert "window._agOnboardingEnabled" in partial
    assert "onboarding_enabled()" in partial

    for base in ("app/web/templates/base.html", "app/web/templates/base_ds.html"):
        assert "_app_scripts.html" in Path(base).read_text(encoding="utf-8"), (
            f"{base} must include the partial that stamps the flag"
        )


# ── the operator-facing inventory ───────────────────────────────────────────
# A switch that only the web UI can see is half a switch: `agnes admin` and the
# operator MCP tools read `/api/admin/config-surface`, which builds its `knobs`
# array from `_KNOB_CATALOGUE` alone and resolves each row by looking its
# `resolver` up on `app.instance_config`. A switch with no resolver there is
# absent from that inventory, so automation cannot discover its value or where
# the value came from (Devin Review on this PR). Nothing guards this coupling —
# it is not a sync-map row — so these tests are the guard.


def _knob(client, token):
    resp = client.get("/api/admin/config-surface", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    knobs = resp.json()["knobs"]
    return next((k for k in knobs if k["resolver"] == "get_onboarding_enabled"), None)


def test_the_switch_appears_in_the_config_surface(seeded_app):
    knob = _knob(seeded_app["client"], seeded_app["admin_token"])
    assert knob is not None, "the onboarding switch is missing from /api/admin/config-surface"
    assert knob["env_var"] == "AGNES_ONBOARDING_ENABLED"
    assert knob["yaml_path"] == "features.onboarding_enabled"
    assert knob["default"] is True


def test_on_a_clean_instance_it_reports_source_default(seeded_app):
    """The catalogued `default` has to be what the resolver actually returns
    with nothing configured. `_source_for` infers `yaml` from `current_value
    != default`, so a stale default here would report onboarding nobody
    touched as deliberately set."""
    env_clean = {k: v for k, v in os.environ.items() if k != "AGNES_ONBOARDING_ENABLED"}
    with patch.dict("os.environ", env_clean, clear=True):
        knob = _knob(seeded_app["client"], seeded_app["admin_token"])
    assert knob is not None
    assert knob["current_value"] == knob["default"] is True
    assert knob["source"] == "default"


def test_an_env_override_is_reported_as_env_sourced(seeded_app):
    """The case the finding named: with the switch off in the environment, the
    UI hides onboarding, and automation has to be able to see that — the value
    AND that it came from `env` rather than a config file."""
    with patch.dict("os.environ", {"AGNES_ONBOARDING_ENABLED": "0"}):
        knob = _knob(seeded_app["client"], seeded_app["admin_token"])
    assert knob is not None
    assert knob["current_value"] is False
    assert knob["source"] == "env"


def test_the_page_and_the_inventory_share_one_read(seeded_app):
    """One resolver, not two. The Jinja global the rail gates on delegates to
    `get_onboarding_enabled`, which is the function the config surface
    resolves — so the pages and the operator inventory cannot disagree about
    whether onboarding is on."""
    import app.web.router as _router
    from app.instance_config import get_onboarding_enabled

    with patch.object(_router, "get_onboarding_enabled", lambda: False) as _:
        assert _router._onboarding_enabled() is False
    with patch.dict("os.environ", {"AGNES_ONBOARDING_ENABLED": "0"}):
        assert get_onboarding_enabled() is False
        assert _router._onboarding_enabled() is False
