"""Integration tests for POST /api/store/entities/{id}/rate (#398).

Thumbs up/down ratings: one vote per (entity, user); re-voting flips the
value in place; clear (0) removes the row; the aggregate is surfaced on the
single-entity GET.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient


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
    app = shared_app
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


_OK_DESC = "Use when validating the store rating endpoint across every guardrail tier"
_OK_BODY = (
    "Body explaining when to invoke the component, what inputs it needs, "
    "and the behavior contract. Long enough to clear the 200-char body floor. "
    "Repeated content for length."
) * 2


def _make_skill_zip(skill_name: str = "rate-me") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n",
        )
    return buf.getvalue()


def _upload(client, cookies, name="rate-me"):
    r = client.post(
        "/api/store/entities",
        files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
        data={"type": "skill", "description": _OK_DESC},
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_rate_up_then_down_same_user_keeps_one_row_and_flips(web_client):
    """POST rate up then down by the same user => one row, my_vote flips."""
    _, owner = _create_user(web_client, "owner@x.com")
    eid = _upload(web_client, owner)

    up = web_client.post(
        f"/api/store/entities/{eid}/rate", json={"vote": 1}, cookies=owner,
    )
    assert up.status_code == 200, up.text
    assert up.json() == {"up": 1, "down": 0, "my_vote": 1}

    down = web_client.post(
        f"/api/store/entities/{eid}/rate", json={"vote": -1}, cookies=owner,
    )
    assert down.status_code == 200, down.text
    # Flipped in place — still one row, now a downvote.
    assert down.json() == {"up": 0, "down": 1, "my_vote": -1}


def test_aggregate_counts_across_users_and_surfaced_on_get(web_client):
    _, owner = _create_user(web_client, "o2@x.com")
    eid = _upload(web_client, owner, name="agg-skill")
    _, a = _create_user(web_client, "alpha@x.com")
    _, b = _create_user(web_client, "beta@x.com")

    web_client.post(f"/api/store/entities/{eid}/rate", json={"vote": 1}, cookies=owner)
    web_client.post(f"/api/store/entities/{eid}/rate", json={"vote": 1}, cookies=a)
    web_client.post(f"/api/store/entities/{eid}/rate", json={"vote": -1}, cookies=b)

    # Aggregate surfaced on the single-entity GET, with the caller's my_vote.
    det = web_client.get(f"/api/store/entities/{eid}", cookies=a).json()
    assert det["rating"] == {"up": 2, "down": 1, "my_vote": 1}

    # A user who has not voted sees my_vote = 0.
    _, c = _create_user(web_client, "gamma@x.com")
    det_c = web_client.get(f"/api/store/entities/{eid}", cookies=c).json()
    assert det_c["rating"] == {"up": 2, "down": 1, "my_vote": 0}


def test_clear_vote_removes_the_row(web_client):
    """vote=0 clears the caller's vote (row removed)."""
    _, owner = _create_user(web_client, "o3@x.com")
    eid = _upload(web_client, owner, name="clear-skill")

    web_client.post(f"/api/store/entities/{eid}/rate", json={"vote": 1}, cookies=owner)
    cleared = web_client.post(
        f"/api/store/entities/{eid}/rate", json={"vote": 0}, cookies=owner,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"up": 0, "down": 0, "my_vote": 0}

    det = web_client.get(f"/api/store/entities/{eid}", cookies=owner).json()
    assert det["rating"] == {"up": 0, "down": 0, "my_vote": 0}


def test_invalid_vote_value_rejected(web_client):
    _, owner = _create_user(web_client, "o4@x.com")
    eid = _upload(web_client, owner, name="bad-vote")
    r = web_client.post(
        f"/api/store/entities/{eid}/rate", json={"vote": 2}, cookies=owner,
    )
    assert r.status_code == 422


def test_rate_unknown_entity_404(web_client):
    _, owner = _create_user(web_client, "o5@x.com")
    r = web_client.post(
        "/api/store/entities/does-not-exist/rate", json={"vote": 1}, cookies=owner,
    )
    assert r.status_code == 404


def test_rate_requires_auth(web_client):
    _, owner = _create_user(web_client, "o6@x.com")
    eid = _upload(web_client, owner, name="auth-skill")
    r = web_client.post(f"/api/store/entities/{eid}/rate", json={"vote": 1})
    assert r.status_code in (401, 403)
