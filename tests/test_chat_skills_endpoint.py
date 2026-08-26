"""Tests for GET /api/chat/skills — the composer slash-menu catalog endpoint.

Reuses the lightweight router-only harness from ``test_chat_api.py`` /
``test_chat_requires_rbac_grant`` (real ``require_chat_access`` gate) rather
than the full ``create_app()``. The connection comes from ``get_system_db()``
(not a bare ``duckdb.connect(":memory:")``) because ``resolve_user_marketplace``
reads marketplace/store state through the repository factory, which opens its
own connection against ``DATA_DIR`` — same rationale as
``tests/test_marketplace_filter_store.py``'s ``db_conn`` fixture.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import _get_db, get_current_user
from app.chat.persistence import ChatRepository

from tests.test_chat_api import TEST_USER, _make_mock_manager  # noqa: F401


def _make_app(conn: duckdb.DuckDBPyConnection) -> FastAPI:
    """Router-only app with the REAL ``require_chat_access`` gate (not
    overridden) against ``conn`` — so RBAC-grant tests exercise the actual
    dependency, not a bypass."""
    from app.api.chat import router as chat_router

    app = FastAPI()
    app.include_router(chat_router)
    repo = ChatRepository(conn)
    app.state.chat_repo = repo
    app.state.chat_manager = _make_mock_manager(repo)
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[_get_db] = lambda: conn
    return TestClient(app)


def _grant_chat_access(conn: duckdb.DuckDBPyConnection, *, user_id: str) -> None:
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    gid = UserGroupsRepository(conn).create(name="chat-users")["id"]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
    ResourceGrantsRepository(conn).create(group_id=gid, resource_type="chat", resource_id="chat")


def _make_user(conn, *, user_id: str, email: str) -> None:
    from src.repositories.users import UserRepository

    UserRepository(conn).create(id=user_id, email=email, name=email.split("@")[0])


def _register_marketplace(conn, *, id: str, plugins: list[dict]) -> None:
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [id, id.upper(), f"https://example.test/{id}.git", datetime.now(timezone.utc)],
    )
    for p in plugins:
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name, version, raw, updated_at) VALUES (?, ?, ?, ?, ?)",
            [id, p["name"], p.get("version"), json.dumps(p), datetime.now(timezone.utc)],
        )


def _grant_and_subscribe_marketplace(conn, *, user_id: str, marketplace: str, plugin: str) -> None:
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    gid = UserGroupsRepository(conn).create(name=f"mkt-{marketplace}-{plugin}")["id"]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
    ResourceGrantsRepository(conn).create(
        group_id=gid,
        resource_type="marketplace_plugin",
        resource_id=f"{marketplace}/{plugin}",
    )
    UserCuratedSubscriptionsRepository(conn).subscribe(user_id, marketplace, plugin)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db

    c = get_system_db()
    yield c
    c.close()


def test_requires_rbac_grant(conn):
    """Default-deny: no chat grant -> 403, same gate as sibling endpoints."""
    _make_user(conn, user_id=TEST_USER["id"], email=TEST_USER["email"])
    client = _make_app(conn)
    r = client.get("/api/chat/skills")
    assert r.status_code == 403


def test_empty_when_nothing_bundled_or_granted(conn, tmp_path, monkeypatch):
    """Isolated from the real bundled template — which, since the
    ``agnes-data-apps-extras`` skill landed, is no longer empty — via an
    empty ``BUNDLED_TEMPLATE_DIR`` override, same pattern as
    ``test_bundled_skill_shadowed_by_same_name_marketplace_skill``."""
    import app.api.chat as chat_module

    monkeypatch.setattr(chat_module, "BUNDLED_TEMPLATE_DIR", tmp_path / "empty-bundled-template")

    _make_user(conn, user_id=TEST_USER["id"], email=TEST_USER["email"])
    _grant_chat_access(conn, user_id=TEST_USER["id"])
    client = _make_app(conn)
    r = client.get("/api/chat/skills")
    assert r.status_code == 200
    assert r.json() == {"skills": [], "commands": []}


def test_returns_marketplace_skill_the_caller_is_granted(conn, tmp_path, monkeypatch):
    """Isolated from the real bundled template (see the empty-list test
    above) so this only exercises the marketplace source."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.api.chat as chat_module

    monkeypatch.setattr(chat_module, "BUNDLED_TEMPLATE_DIR", tmp_path / "empty-bundled-template")

    _make_user(conn, user_id=TEST_USER["id"], email=TEST_USER["email"])
    _grant_chat_access(conn, user_id=TEST_USER["id"])
    _register_marketplace(conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
    _grant_and_subscribe_marketplace(conn, user_id=TEST_USER["id"], marketplace="mkt", plugin="p1")

    from app.utils import get_marketplaces_dir

    skill_md = get_marketplaces_dir() / "mkt" / "plugins" / "p1" / "skills" / "howto" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        "---\nname: howto\ndescription: How to do the thing.\n---\n\nBody.\n",
        encoding="utf-8",
    )

    client = _make_app(conn)
    r = client.get("/api/chat/skills")
    assert r.status_code == 200
    body = r.json()
    assert body["commands"] == []
    assert body["skills"] == [{"name": "howto", "description": "How to do the thing.", "source": "marketplace"}]


def test_bundled_skill_shadowed_by_same_name_marketplace_skill(conn, tmp_path, monkeypatch):
    """Contract: marketplace wins when both sources ship the same skill name."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _make_user(conn, user_id=TEST_USER["id"], email=TEST_USER["email"])
    _grant_chat_access(conn, user_id=TEST_USER["id"])
    _register_marketplace(conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
    _grant_and_subscribe_marketplace(conn, user_id=TEST_USER["id"], marketplace="mkt", plugin="p1")

    from app.utils import get_marketplaces_dir

    market_skill = get_marketplaces_dir() / "mkt" / "plugins" / "p1" / "skills" / "shared" / "SKILL.md"
    market_skill.parent.mkdir(parents=True, exist_ok=True)
    market_skill.write_text("---\nname: shared\ndescription: Marketplace wins.\n---\n\nBody.\n", encoding="utf-8")

    bundled_dir = tmp_path / "bundled-template"
    bundled_skill = bundled_dir / ".claude" / "skills" / "shared" / "SKILL.md"
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.write_text("---\nname: shared\ndescription: Bundled loses.\n---\n\nBody.\n", encoding="utf-8")

    import app.api.chat as chat_module

    monkeypatch.setattr(chat_module, "BUNDLED_TEMPLATE_DIR", bundled_dir)

    client = _make_app(conn)
    r = client.get("/api/chat/skills")
    assert r.status_code == 200
    assert r.json()["skills"] == [{"name": "shared", "description": "Marketplace wins.", "source": "marketplace"}]


def test_the_menu_stops_offering_marketplace_skills_when_delivery_is_off(conn, tmp_path, monkeypatch):
    """#1552: the menu must never advertise a skill nothing delivers into the
    session — picking it inserted `/name` and the agent answered "Unknown
    command". Bundled skills are unaffected (they ship in the workspace tree)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_CHAT_BOOTSTRAP_MARKETPLACE", "0")

    _make_user(conn, user_id=TEST_USER["id"], email=TEST_USER["email"])
    _grant_chat_access(conn, user_id=TEST_USER["id"])
    _register_marketplace(conn, id="mkt", plugins=[{"name": "p1", "version": "1.0"}])
    _grant_and_subscribe_marketplace(conn, user_id=TEST_USER["id"], marketplace="mkt", plugin="p1")

    from app.utils import get_marketplaces_dir

    skill_md = get_marketplaces_dir() / "mkt" / "plugins" / "p1" / "skills" / "keboola-cli" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text("---\nname: keboola-cli\n---\n\nBody.\n", encoding="utf-8")

    bundled_dir = tmp_path / "bundled-template"
    bundled_skill = bundled_dir / ".claude" / "skills" / "bundled-one" / "SKILL.md"
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.write_text("---\nname: bundled-one\n---\n\nBody.\n", encoding="utf-8")

    import app.api.chat as chat_module

    monkeypatch.setattr(chat_module, "BUNDLED_TEMPLATE_DIR", bundled_dir)

    r = _make_app(conn).get("/api/chat/skills")

    assert r.status_code == 200
    assert [s["name"] for s in r.json()["skills"]] == ["bundled-one"]


def test_a_marketplace_skill_is_offered_under_its_bare_name(conn, tmp_path, monkeypatch):
    """The composer inserts `/<name>` verbatim. Claude Code exposes a plugin's
    skill as the bare `keboola-cli` (the plugin shows up in the DESCRIPTION),
    so a `<plugin>:<skill>` name here would be an unknown command."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.api.chat as chat_module

    monkeypatch.setattr(chat_module, "BUNDLED_TEMPLATE_DIR", tmp_path / "empty-bundled-template")

    _make_user(conn, user_id=TEST_USER["id"], email=TEST_USER["email"])
    _grant_chat_access(conn, user_id=TEST_USER["id"])
    _register_marketplace(conn, id="mkt", plugins=[{"name": "demo-plugin", "version": "1.0"}])
    _grant_and_subscribe_marketplace(conn, user_id=TEST_USER["id"], marketplace="mkt", plugin="demo-plugin")

    from app.utils import get_marketplaces_dir

    skill_md = get_marketplaces_dir() / "mkt" / "plugins" / "demo-plugin" / "skills" / "keboola-cli" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text("---\nname: keboola-cli\n---\n\nBody.\n", encoding="utf-8")

    r = _make_app(conn).get("/api/chat/skills")

    assert [s["name"] for s in r.json()["skills"]] == ["keboola-cli"]
