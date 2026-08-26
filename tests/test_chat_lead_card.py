"""The chat dashboard's LEAD door — the first of the three cards.

That slot is audience-dependent: an admin gets "Set up {brand}", everyone
else gets "See what {brand} knows". They are one card rendered for two
readers, not two cards that share a position, and before this they had
drifted — the admin's carried an icon and an action, the member's was two
lines of text. The member's landing read as the admin's with the interesting
part taken out.

What these guard is the drift, not the pixels: same anatomy in both
variants, content centred rather than pooled at the top, and one specific
regression (the banner's gradient ink) that is tempting to reintroduce and
fails contrast at this size.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app" / "web" / "static" / "style-custom.css"


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    for sub in ("state", "analytics", "extracts"):
        (tmp_path / sub).mkdir()
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
def member_cookie(web_client):
    """A signed-in user with no admin grant — the real member render.

    Deliberately not `?preview=member`: that switch is gated on
    LOCAL_DEV_MODE, which also bypasses the whole auth layer, so a test built
    on it would prove the preview works rather than that a member sees this.
    """
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    password = "MemberPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="member1",
        email="member@test.com",
        name="Member",
        password_hash=PasswordHasher().hash(password),
    )
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "member@test.com", "password": password})
    assert resp.status_code == 200, f"Member bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


def _enable_chat(web_client, monkeypatch):
    import app.auth.access as access

    monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
    # A member holds no CHAT grant from these fixtures, so /chat would 307 to
    # home and every assertion below would run against an empty body — passing
    # while proving nothing. Open the gate so the render is actually exercised.
    monkeypatch.setattr(access, "can_access", lambda *a, **k: True)
    web_client.app.state.chat_config = SimpleNamespace(enabled=True)


def _doors(web_client, cookie, monkeypatch):
    """The `.cld-doors` nav only — the page says "Set up" elsewhere too."""
    _enable_chat(web_client, monkeypatch)
    resp = web_client.get("/chat", cookies=cookie, follow_redirects=False)
    assert resp.status_code == 200, f"never reached /chat: {resp.status_code}"
    text = resp.text
    assert 'class="cld-doors"' in text, "the doors nav is missing from the dashboard"
    return text.split('class="cld-doors"', 1)[1].split("</nav>", 1)[0]


class TestBothVariantsAreTheSameCard:
    def test_the_admin_variant_is_a_lead_card(self, web_client, admin_cookie, monkeypatch):
        doors = _doors(web_client, admin_cookie, monkeypatch)
        assert "cld-door--setup" in doors
        assert "cld-door--lead" in doors, "the admin's card dropped the shared lead treatment"

    def test_the_member_variant_has_the_same_anatomy(self, web_client, member_cookie, monkeypatch):
        """Icon, title, one line, one action — the four parts the admin's has."""
        doors = _doors(web_client, member_cookie, monkeypatch)
        assert "cld-door--know" in doors, "the member's card is not the lead card"
        assert "cld-door--lead" in doors
        for part in ("cld-door-ico", "cld-door-t", "cld-door-d", "cld-door-btn"):
            assert part in doors, f"the member's lead card has no {part}"

    def test_the_member_card_holds_no_nested_anchor(self, web_client, member_cookie, monkeypatch):
        """Its action is a <span>: the whole card is already the link, and an
        <a> inside an <a> is neither valid nor operable."""
        doors = _doors(web_client, member_cookie, monkeypatch)
        card = doors.split("cld-door--know", 1)[1].split("</a>", 1)[0]
        assert "<a " not in card, "an anchor nested inside the card link"
        assert 'class="cld-door-btn cld-door-btn--quiet">Browse' in card


class TestTheDoorsDoNotPoolSpaceAtTheTop:
    def test_doors_centre_their_content(self):
        """The grid stretches all three cards to the tallest, so top-aligned
        content left a two-line card trailing a third of a card of nothing —
        which reads as something that failed to load."""
        css = CSS.read_text(encoding="utf-8")
        rule = css.split(".cld-door {", 1)[1].split("}", 1)[0]
        assert "justify-content: center" in rule, "the doors are top-aligned again"


class TestTheLeadTitleIsNotTheBannerGradient:
    """`.klb-headline-grad` runs `--ds-assistant` → `--ds-kind-agent`. It is
    the obvious thing to reach for here — this card deliberately borrows the
    banner's surface — and it fails contrast at 14.5px: `--ds-assistant` is
    4.1:1 on paper and `--ds-kind-agent` is 2.4:1 under `data-theme="dark"`,
    against a 4.5:1 floor. The banner clears it only because its headline is
    30px, where 3:1 applies.
    """

    def test_the_title_is_solid_accent_ink(self):
        css = CSS.read_text(encoding="utf-8")
        rule = css.split(".cld-door--lead .cld-door-t {", 1)[1].split("}", 1)[0]
        assert "background-clip" not in rule, "gradient ink is back on a 14.5px title"
        assert "--ds-primary-dark" in rule
