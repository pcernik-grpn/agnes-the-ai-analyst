"""Admin "Moderation & Trust" hub — GET /admin/store.

The consolidated surface that lists Store entities awaiting verification
(``verification_state='requested'``) with a DEEP LINK to each entity's detail
page — where the Verify / Request changes / Archive / Override actions live —
plus jump-offs to the submission review queue and marketplace curation.

Covers:
  * require_admin gate (non-admin → 403)
  * renders the queue + marketplace jump-offs
  * lists a requested entity and links to /marketplace/flea/<id>
  * empty state when nothing is awaiting verification

The page is HIDDEN by default (``features.store_moderation_enabled``, off —
see tests/test_retired_admin_surfaces.py, which owns the hidden-by-default
behavior and the redirect). This module is about what the page RENDERS, so
`web_client` turns the flag on; without it every test here would assert
against a 302 and pass for the wrong reason.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from src.db import close_system_db, get_system_db
from src.repositories.users import UserRepository


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    # The hub is off by default; these tests are about its contents.
    monkeypatch.setenv("AGNES_STORE_MODERATION_ENABLED", "1")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id,
        email=email,
        name=user_id,
        password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _create_admin(client, email="admin@x.com"):
    from tests.helpers.auth import grant_admin

    user_id, cookies = _create_user(client, email, password="AdminPass1!")
    conn = get_system_db()
    grant_admin(conn, user_id)
    conn.close()
    return user_id, cookies


def _seed_requested_entity(owner_id: str, entity_id: str, name: str) -> None:
    """A user-published entity whose author has asked for org verification."""
    from src.repositories.store_entities import StoreEntitiesRepository

    conn = get_system_db()
    repo = StoreEntitiesRepository(conn)
    repo.create(
        id=entity_id,
        owner_user_id=owner_id,
        owner_username=owner_id,
        type="skill",
        name=name,
        title="Needs Review Skill",
        description="Use when validating the moderation hub verification list",
        category=None,
        version="1.0.0",
        file_size=256,
        visibility_status="approved",
    )
    repo.set_verification(entity_id, "requested", by_user_id=owner_id)
    conn.close()


def _enable_verification(monkeypatch) -> None:
    # The verify vocabulary is opt-in per instance (default off). The route
    # imports get_store_verification_enabled at call time, so patching the
    # module attribute flips it on for the request.
    monkeypatch.setattr("app.instance_config.get_store_verification_enabled", lambda: True)


def test_requires_admin(web_client):
    _, user_cookies = _create_user(web_client, "user@x.com")
    r = web_client.get("/admin/store", cookies=user_cookies)
    assert r.status_code == 403


def test_renders_queue_and_marketplace_jumpoffs(web_client):
    _, admin_cookies = _create_admin(web_client)
    r = web_client.get("/admin/store", cookies=admin_cookies)
    assert r.status_code == 200
    body = r.text
    assert 'href="/admin/store/submissions"' in body
    assert 'href="/admin/marketplaces"' in body


def test_lists_requested_entity_with_detail_deeplink(web_client, monkeypatch):
    _enable_verification(monkeypatch)
    _, admin_cookies = _create_admin(web_client)
    _seed_requested_entity("author1", "ent-req-1", "needs-review-skill")

    r = web_client.get("/admin/store", cookies=admin_cookies)
    assert r.status_code == 200
    body = r.text
    # The row deep-links to the entity detail page, where the admin's
    # Verify / Archive / Override actions live.
    assert 'href="/marketplace/flea/ent-req-1?from=admin-moderation"' in body
    assert "Needs Review Skill" in body


def test_empty_state_when_nothing_awaiting(web_client, monkeypatch):
    _enable_verification(monkeypatch)
    _, admin_cookies = _create_admin(web_client)
    r = web_client.get("/admin/store", cookies=admin_cookies)
    assert r.status_code == 200
    assert "No verification requests waiting." in r.text


def test_empty_queue_uses_the_shared_empty_state(web_client, monkeypatch):
    """TCRD-207: an empty moderation queue is EMPTY on the shared vocabulary
    (macros/_state.html) — distinct from the feature-disabled banner above it,
    which is a config gate, not one of the four states, and keeps its own
    plain `.empty-state` copy (see the Postgres-backend assertion elsewhere
    in this file)."""
    _enable_verification(monkeypatch)
    _, admin_cookies = _create_admin(web_client)
    r = web_client.get("/admin/store", cookies=admin_cookies)
    body = r.text
    block = body[body.index("No verification requests waiting.") - 600 :]
    assert 'data-state-kind="empty"' in block
    assert "state-panel--neutral" in block


def test_agent_share_requests_zone_disabled_on_duckdb_backend(web_client):
    """Track C6's approval queue is PG-only (A3 ratchet) — on this DuckDB-
    backed fixture the zone must say so rather than 501ing the whole page
    or silently pretending there's nothing waiting."""
    _, admin_cookies = _create_admin(web_client)
    r = web_client.get("/admin/store", cookies=admin_cookies)
    assert r.status_code == 200
    assert "Pending agent shares" in r.text
    assert "requires a Postgres app-state backend" in r.text
