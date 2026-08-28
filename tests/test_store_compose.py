"""POST /api/store/entities/from-components — composing a plugin from Library items.

The Library already bakes every entity into a one-plugin tree, so publishing a
plugin is not a step on the way to distributing a skill. This endpoint covers
the one thing that shape cannot express: one install handing someone several
skills and agent templates at once, assembled from items already in the
Library rather than from a .zip the author packaged elsewhere.
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
    yield TestClient(shared_app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(id=user_id, email=email, name=user_id, password_hash=ph.hash(password))
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


_OK_DESC = "Use when validating the store compose pipeline across every guardrail tier"
_OK_BODY = (
    "Body explaining when to invoke the component, what inputs it needs, "
    "and the behavior contract. Long enough to clear the 200-char body floor. "
    "Repeated content for length."
) * 2


def _skill_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n",
        )
    return buf.getvalue()


def _agent_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{name}.md",
            f"---\nname: {name}\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n",
        )
    return buf.getvalue()


def _plugin_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            ".claude-plugin/plugin.json",
            json.dumps({"name": name, "description": _OK_DESC, "version": "0.1"}),
        )
        zf.writestr(
            "skills/dummy/SKILL.md",
            f"---\nname: dummy\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n",
        )
    return buf.getvalue()


def _upload(client, cookies, type_: str, payload: bytes, **extra) -> str:
    data = {"type": type_, "description": _OK_DESC}
    data.update(extra)
    r = client.post(
        "/api/store/entities",
        files={"file": ("b.zip", payload, "application/zip")},
        data=data,
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _compose(client, cookies, **body):
    payload = {"name": "team-toolkit", "description": _OK_DESC}
    payload.update(body)
    return client.post("/api/store/entities/from-components", json=payload, cookies=cookies)


def _baked_paths(entity_id: str) -> set:
    from app.api.store import _plugin_dir

    root = _plugin_dir(entity_id)
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


class TestComposeHappyPath:
    def test_composite_carries_every_selected_component(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        s2 = _upload(web_client, cookies, "skill", _skill_zip("release-notes"))
        a1 = _upload(web_client, cookies, "agent", _agent_zip("triager"))

        r = _compose(web_client, cookies, components=[s1, s2, a1])
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["type"] == "plugin"

        paths = _baked_paths(created["id"])
        assert "skills/code-review-by-alice/SKILL.md" in paths
        assert "skills/release-notes-by-alice/SKILL.md" in paths
        assert "agents/triager-by-alice.md" in paths
        # Exactly one manifest — the composite's own. Each component's
        # standalone manifest describes it as a plugin in its own right and
        # must not survive the merge.
        assert [p for p in paths if p.endswith("plugin.json")] == [".claude-plugin/plugin.json"]

    def test_manifest_name_is_the_suffixed_composite_name(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))

        r = _compose(web_client, cookies, components=[s1])
        assert r.status_code == 201, r.text
        from app.api.store import _plugin_dir

        manifest = json.loads((_plugin_dir(r.json()["id"]) / ".claude-plugin" / "plugin.json").read_text())
        assert manifest["name"] == "team-toolkit-by-alice"
        assert manifest["description"] == _OK_DESC

    def test_component_survives_composition_unchanged(self, web_client):
        """Composing copies; it never moves or rewrites the source item."""
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        before = _baked_paths(s1)

        assert _compose(web_client, cookies, components=[s1]).status_code == 201

        assert _baked_paths(s1) == before
        assert web_client.get(f"/api/store/entities/{s1}", cookies=cookies).status_code == 200

    def test_private_component_of_own_is_composable(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"), access="private")

        r = _compose(web_client, cookies, components=[s1])
        assert r.status_code == 201, r.text

    def test_someone_elses_published_skill_is_composable(self, web_client):
        """The Library is community-open: what you can install, you can compose."""
        _, alice = _create_user(web_client, "alice@x.com")
        _, bob = _create_user(web_client, "bob@x.com")
        s1 = _upload(web_client, alice, "skill", _skill_zip("code-review"))

        r = _compose(web_client, bob, components=[s1])
        assert r.status_code == 201, r.text
        assert "skills/code-review-by-alice/SKILL.md" in _baked_paths(r.json()["id"])


class TestComposeDryRun:
    def test_dry_run_lists_components_and_writes_nothing(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        a1 = _upload(web_client, cookies, "agent", _agent_zip("triager"))
        before = web_client.get("/api/store/entities?owner=alice", cookies=cookies).json()

        r = _compose(web_client, cookies, components=[s1, a1], dry_run=True)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "plugin"
        assert body["name"] == "team-toolkit"
        kinds = {c["type"] for c in body["components"]}
        assert "skill" in kinds and "agent" in kinds

        after = web_client.get("/api/store/entities?owner=alice", cookies=cookies).json()
        assert after["total"] == before["total"]

    def test_dry_run_reports_a_name_the_save_would_refuse(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        # The composite's name collides with something the author already owns.
        r = _compose(web_client, cookies, components=[s1], name="code-review", dry_run=True)
        assert r.status_code == 200, r.text
        codes = {i["code"] for i in r.json()["field_issues"]}
        assert "conflict_owner_name" in codes


class TestComposeRefusals:
    def test_no_components(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = _compose(web_client, cookies, components=[])
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "no_components"

    def test_duplicate_component(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        r = _compose(web_client, cookies, components=[s1, s1])
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "duplicate_component"

    def test_too_many_components(self, web_client):
        from app.api.store import MAX_COMPONENTS

        _, cookies = _create_user(web_client, "alice@x.com")
        r = _compose(web_client, cookies, components=[f"id{i}" for i in range(MAX_COMPONENTS + 1)])
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "too_many_components"

    def test_unknown_component(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = _compose(web_client, cookies, components=["does-not-exist"])
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "component_not_found"

    def test_invisible_component_is_404_not_403(self, web_client):
        """A composite must not be a probe for someone else's private item."""
        _, alice = _create_user(web_client, "alice@x.com")
        _, bob = _create_user(web_client, "bob@x.com")
        s1 = _upload(web_client, alice, "skill", _skill_zip("code-review"), access="private")

        r = _compose(web_client, bob, components=[s1])
        assert r.status_code == 404, r.text

    def test_plugin_component_is_refused_by_name(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        p1 = _upload(web_client, cookies, "plugin", _plugin_zip("inner"))
        r = _compose(web_client, cookies, components=[p1])
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["code"] == "component_type_unsupported"
        assert detail["given"] == "plugin"

    def test_invalid_composite_name(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        r = _compose(web_client, cookies, components=[s1], name="Not A Handle")
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_name_format"

    def test_missing_component_bundle(self, web_client):
        """A row whose bytes are gone (purged, or still baking) is named, not 500."""
        import shutil

        from app.api.store import _plugin_dir

        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        shutil.rmtree(_plugin_dir(s1))

        r = _compose(web_client, cookies, components=[s1])
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "component_bundle_missing"

    def test_path_conflict_between_two_components(self, web_client):
        """Defense in depth: baked dirs are per-owner-suffixed, so two items
        colliding is not reachable through the upload path today. Seed the
        collision on disk so the guard is exercised rather than assumed."""
        from app.api.store import _plugin_dir

        _, cookies = _create_user(web_client, "alice@x.com")
        s1 = _upload(web_client, cookies, "skill", _skill_zip("code-review"))
        s2 = _upload(web_client, cookies, "skill", _skill_zip("release-notes"))
        for eid in (s1, s2):
            shared = _plugin_dir(eid) / "shared" / "helper.md"
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_text("collides")

        r = _compose(web_client, cookies, components=[s1, s2])
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "component_path_conflict"
        assert detail["path"] == "shared/helper.md"
