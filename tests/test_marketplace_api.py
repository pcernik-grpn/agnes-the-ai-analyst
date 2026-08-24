"""Integration tests for the unified /api/marketplace endpoints.

Covers the v28 Model B browse + install surface: per-tab listing,
categories, curated detail with RBAC guard, and subscribe/unsubscribe.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

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


def _seed_curated_grant(
    *,
    user_id: str,
    marketplace: str,
    plugin: str,
    plugin_meta: dict | None = None,
    group_name: str | None = None,
) -> tuple[str, str]:
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    conn = get_system_db()
    try:
        existing = conn.execute(
            "SELECT 1 FROM marketplace_registry WHERE id = ?", [marketplace],
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO marketplace_registry (id, name, url, registered_at) "
                "VALUES (?, ?, ?, ?)",
                [marketplace, marketplace.upper(),
                 f"https://example.test/{marketplace}.git",
                 datetime.now(timezone.utc)],
            )
        meta = {"name": plugin, "version": "1.0", "description": "desc"}
        if plugin_meta:
            meta.update(plugin_meta)
        conn.execute(
            "INSERT INTO marketplace_plugins "
            "(marketplace_id, name, description, version, category, raw, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                marketplace, plugin, meta.get("description"), meta.get("version"),
                meta.get("category"), json.dumps(meta),
                datetime.now(timezone.utc),
            ],
        )
        gname = group_name or f"G-{user_id}-{marketplace}"
        gid = UserGroupsRepository(conn).create(name=gname)["id"]
        UserGroupMembersRepository(conn).add_member(user_id, gid, source="admin")
        grant_id = ResourceGrantsRepository(conn).create(
            group_id=gid, resource_type="marketplace_plugin",
            resource_id=f"{marketplace}/{plugin}",
        )
        return gid, grant_id
    finally:
        conn.close()


_OK_DESC = "Use when validating marketplace API endpoints across guardrail tiers"
_OK_BODY = (
    "Body explaining the skill, when to invoke it, and the expected outputs. "
    "Long enough to clear the 200-char content guardrail floor. " * 2
)


def _make_skill_zip(skill_name: str = "code-review") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}",
        )
    return buf.getvalue()


def _make_plugin_zip(plugin_name: str, inner_skill: str = "dummy") -> bytes:
    """Mirror of test_store_api._make_plugin_zip — minimal flea plugin
    ZIP with one inner skill, used to drive ``/api/marketplace/flea/{id}
    /skill/{name}`` inner-detail tests."""
    import json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            ".claude-plugin/plugin.json",
            json.dumps({
                "name": plugin_name,
                "description": _OK_DESC,
                "version": "0.1",
            }),
        )
        zf.writestr(
            f"skills/{inner_skill}/SKILL.md",
            f"---\nname: {inner_skill}\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}",
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# /api/marketplace/items
# ---------------------------------------------------------------------------


class TestListItems:
    def test_curated_empty_for_user_without_grants(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.get("/api/marketplace/items?tab=curated", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_curated_lists_granted_plugins(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        r = web_client.get("/api/marketplace/items?tab=curated", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["source"] == "curated"
        assert data["items"][0]["name"] == "alpha"
        assert data["items"][0]["installed"] is False
        assert data["items"][0]["marketplace_slug"] == "mkt-x"

    def test_flea_lists_uploads(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("alpha"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=cookies,
        )
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["source"] == "flea"
        # v49 phase-1: `name` is the suffixed invocation slug — kept as the
        # technical identifier card JS falls back to when display_name is
        # absent. v49 phase-2: `display_name` carries the humanized title
        # (`Alpha`), and JS uses it as the visible card heading.
        assert item["name"] == "alpha-by-alice"
        assert item["display_name"] == "Alpha"

    def test_my_subscriptions_default_empty(self, web_client):
        """Without explicit install, a granted curated plugin doesn't show
        up under tab=my (Model B)."""
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        r = web_client.get("/api/marketplace/items?tab=my", cookies=cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0

    def test_my_stack_carries_marketplace_metadata_enrichment(self, web_client):
        """Once a curated plugin is in the user's stack (subscribed), the
        ``tab=my`` card MUST carry the same marketplace-metadata enrichment
        (cover_photo_url, video_url, category override) the ``tab=curated``
        card shows. Previously the My Stack handler built rows from the
        on-disk ``marketplace.json``, which doesn't carry those columns —
        same plugin → cover photo on Curated, gradient placeholder on
        My Stack.
        """
        from src.db import get_system_db
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )

        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")

        # Backfill the marketplace-metadata enrichment columns on the seeded
        # plugin row — same shape `_refresh_plugin_cache` writes after a
        # nightly sync that picked up a curator's marketplace-metadata.json.
        cover = "/api/marketplace/curated/mkt-x/alpha/asset/cover.png"
        video = "https://www.youtube.com/watch?v=abc123"
        conn = get_system_db()
        try:
            conn.execute(
                "UPDATE marketplace_plugins SET cover_photo_url = ?, "
                "video_url = ?, category = ? "
                "WHERE marketplace_id = 'mkt-x' AND name = 'alpha'",
                [cover, video, "Code & Engineering"],
            )
            UserCuratedSubscriptionsRepository(conn).subscribe(
                user_id=user_id, marketplace_id="mkt-x", plugin_name="alpha",
            )
        finally:
            conn.close()

        r = web_client.get("/api/marketplace/items?tab=my", cookies=cookies)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 1, data
        item = data["items"][0]
        assert item["source"] == "curated"
        assert item["name"] == "alpha"
        # The bug the test guards: ``photo_url`` (mapped from
        # ``marketplace_plugins.cover_photo_url``) used to be hard-coded
        # None on the My Stack path. Now the My Stack handler looks up the
        # enriched marketplace_plugins row and surfaces it — matching the
        # Curated tab. ``MarketplaceItem`` flattens the column name to
        # ``photo_url``; see :func:`_curated_to_item`.
        assert item["photo_url"] == cover, (
            "My Stack must surface marketplace-metadata cover_photo_url, not None"
        )
        assert item["category"] == "Code & Engineering"


# ---------------------------------------------------------------------------
# /api/marketplace/categories
# ---------------------------------------------------------------------------


class TestCategories:
    def test_curated_categories_count(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(
            user_id=user_id, marketplace="mkt-x", plugin="alpha",
            plugin_meta={"category": "Code & Engineering"},
        )
        _seed_curated_grant(
            user_id=user_id, marketplace="mkt-x", plugin="beta",
            plugin_meta={"category": "Code & Engineering"},
            group_name="G-alice-mkt-x-beta",
        )
        r = web_client.get(
            "/api/marketplace/categories?tab=curated", cookies=cookies,
        )
        assert r.status_code == 200
        data = r.json()
        cats = {c["name"]: c["count"] for c in data["items"]}
        assert cats.get("Code & Engineering") == 2

    def test_categories_skip_empty(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.get(
            "/api/marketplace/categories?tab=curated", cookies=cookies,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []  # no plugins in scope → no categories


# ---------------------------------------------------------------------------
# Curated detail + install
# ---------------------------------------------------------------------------


class TestCuratedDetail:
    def test_detail_403_without_grant(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.get(
            "/api/marketplace/curated/some-mp/some-plugin", cookies=cookies,
        )
        assert r.status_code == 403

    def test_detail_200_with_grant(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["plugin_name"] == "alpha"
        assert data["installed"] is False
        # New fields populated for the redesigned plugin detail page.
        assert "files" in data and isinstance(data["files"], list)
        assert "docs" in data and isinstance(data["docs"], list)
        assert data["install_count"] == 0

    def test_detail_rich_content_from_marketplace_metadata(
        self, web_client, tmp_path,
    ):
        """When curator wrote rich content into marketplace-metadata.json, the
        detail endpoint surfaces display_name, tagline, description_long_html
        (server-rendered markdown), use_cases, and sample_interaction. The
        on-demand parser reads from `${DATA_DIR}/marketplaces/<id>/...` —
        this test seeds that file and verifies the API response carries
        the fields through to PluginDetailResponse."""
        import json
        from pathlib import Path

        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")

        # Write a marketplace-metadata.json to the working tree the on-demand
        # parser will read.
        marketplaces_dir = Path(tmp_path) / "marketplaces" / "mkt-x" / ".claude-plugin"
        marketplaces_dir.mkdir(parents=True, exist_ok=True)
        (marketplaces_dir / "marketplace-metadata.json").write_text(json.dumps({
            "plugins": {
                "alpha": {
                    "display_name": "Friendly Alpha",
                    "tagline": "One-line value prop.",
                    "description": "Para 1.\n\nPara 2 with **bold**.",
                    "use_cases": [
                        {"title": "Find owner", "description": "X+Y.", "prompt": "/q"},
                    ],
                    "sample_interaction": {
                        "user": "What?",
                        "assistant": "Here's *the* answer.",
                    },
                },
            },
        }), encoding="utf-8")

        r = web_client.get("/api/marketplace/curated/mkt-x/alpha", cookies=cookies)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["display_name"] == "Friendly Alpha"
        assert data["tagline"] == "One-line value prop."
        # description_long_html is the server-rendered markdown body.
        assert "<strong>bold</strong>" in data["description_long_html"]
        assert "<p>Para 1.</p>" in data["description_long_html"]
        assert len(data["use_cases"]) == 1
        assert data["use_cases"][0]["title"] == "Find owner"
        # sample_interaction carries both the raw assistant text + rendered HTML.
        assert data["sample_interaction"]["user"] == "What?"
        assert "<em>the</em>" in data["sample_interaction"]["assistant_html"]

    def test_detail_falls_back_when_no_rich_content(self, web_client):
        """No marketplace-metadata.json on disk → API returns the historical
        shape with rich fields left null / empty. No 500, no crash."""
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["display_name"] is None
        assert data["tagline"] is None
        assert data["description_long_html"] is None
        assert data["use_cases"] == []
        assert data["sample_interaction"] is None

    def test_detail_tolerates_partial_curator_json(self, web_client, tmp_path):
        """Curator commits a sample_interaction with only ``user`` (forgot
        ``assistant``) and a use_cases entry missing ``prompt``. The endpoint
        must skip the malformed sections instead of 500-ing on Pydantic's
        required-field validation — PR description promises rich content
        renders only when populated, partial population should degrade
        gracefully."""
        import json
        from pathlib import Path

        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")

        marketplaces_dir = Path(tmp_path) / "marketplaces" / "mkt-x" / ".claude-plugin"
        marketplaces_dir.mkdir(parents=True, exist_ok=True)
        (marketplaces_dir / "marketplace-metadata.json").write_text(json.dumps({
            "plugins": {
                "alpha": {
                    "display_name": "Friendly Alpha",
                    "use_cases": [
                        {"title": "Good one", "description": "X.", "prompt": "/q"},
                        {"title": "Missing prompt", "description": "Y."},
                        {"title": "Empty prompt", "description": "Z.", "prompt": ""},
                    ],
                    "sample_interaction": {"user": "Just user, no assistant"},
                },
            },
        }), encoding="utf-8")

        r = web_client.get("/api/marketplace/curated/mkt-x/alpha", cookies=cookies)
        assert r.status_code == 200, r.text
        data = r.json()
        # Good card survives; malformed cards are dropped.
        assert len(data["use_cases"]) == 1
        assert data["use_cases"][0]["title"] == "Good one"
        # Partial sample_interaction is dropped, not crashed.
        assert data["sample_interaction"] is None
        # The well-formed field next to the broken ones still renders.
        assert data["display_name"] == "Friendly Alpha"

    def test_detail_html_is_sanitized(self, web_client, tmp_path):
        """Curator-written `<script>` in description markdown must NOT
        survive into description_long_html — defense-in-depth check."""
        import json
        from pathlib import Path

        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")

        marketplaces_dir = Path(tmp_path) / "marketplaces" / "mkt-x" / ".claude-plugin"
        marketplaces_dir.mkdir(parents=True, exist_ok=True)
        (marketplaces_dir / "marketplace-metadata.json").write_text(json.dumps({
            "plugins": {
                "alpha": {
                    "description": "Hello <script>alert(1)</script> world",
                },
            },
        }), encoding="utf-8")

        r = web_client.get("/api/marketplace/curated/mkt-x/alpha", cookies=cookies)
        assert r.status_code == 200, r.text
        html = r.json()["description_long_html"] or ""
        assert "<script>" not in html
        # `alert(1)` could appear as escaped text inside the rendered HTML;
        # what we MUST not see is unescaped `<script>` tags executing it.
        # Verify the literal `<script` open-tag never reaches the response.
        assert "<script" not in html.lower()

    def test_install_403_without_grant(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        r = web_client.post(
            "/api/marketplace/curated/some-mp/some-plugin/install",
            cookies=cookies,
        )
        assert r.status_code == 403

    def test_install_uninstall_round_trip(self, web_client):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")

        # Install.
        r = web_client.post(
            "/api/marketplace/curated/mkt-x/alpha/install", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        # Verify in DB.
        from src.db import get_system_db
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        conn = get_system_db()
        try:
            assert UserCuratedSubscriptionsRepository(conn).is_subscribed(
                user_id, "mkt-x", "alpha",
            )
        finally:
            conn.close()

        # Detail now reports installed=True.
        d = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha", cookies=cookies,
        ).json()
        assert d["installed"] is True

        # Uninstall.
        r = web_client.delete(
            "/api/marketplace/curated/mkt-x/alpha/install", cookies=cookies,
        )
        assert r.status_code == 204
        conn = get_system_db()
        try:
            assert not UserCuratedSubscriptionsRepository(conn).is_subscribed(
                user_id, "mkt-x", "alpha",
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Curated nested skill / agent detail — extended response shape
# ---------------------------------------------------------------------------


def _seed_curated_skill_on_disk(
    tmp_path, marketplace: str, plugin: str, skill: str,
    *, files: dict[str, str] | None = None,
):
    """Materialize a skill on disk so curated_skill_detail can read it.

    `files` maps relative paths inside the skill dir to file contents.
    SKILL.md is always written; extra files surface in the Files section.
    """
    skill_dir = tmp_path / "marketplaces" / marketplace / "plugins" / plugin / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: Use when validating marketplace skill rows across guardrail tiers and endpoints\n---\nbody",
        encoding="utf-8",
    )
    for rel, content in (files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _seed_curated_agent_on_disk(
    tmp_path, marketplace: str, plugin: str, agent: str,
):
    agents_dir = tmp_path / "marketplaces" / marketplace / "plugins" / plugin / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent}.md").write_text(
        f"---\nname: {agent}\ndescription: Use when validating marketplace agent rows across guardrail tiers and endpoints\n---\nbody",
        encoding="utf-8",
    )


class TestCuratedInnerDetail:
    def test_skill_detail_includes_parent_metadata_and_files(
        self, web_client, tmp_path,
    ):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(
            user_id=user_id, marketplace="mkt-x", plugin="alpha",
            plugin_meta={"category": "Data", "author": {"name": "ops-team"}},
        )
        _seed_curated_skill_on_disk(
            tmp_path, "mkt-x", "alpha", "data-explorer",
            files={"REFERENCE.md": "ref docs"},
        )
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha/skill/data-explorer",
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        # Inner-detail fields.
        assert d["kind"] == "skill"
        assert d["name"] == "data-explorer"
        assert d["description"] == "Use when validating marketplace skill rows across guardrail tiers and endpoints"
        # Parent plugin metadata surfaced for the redesigned hero / sidebar.
        assert d["category"] == "Data"
        assert d["marketplace_name"]  # registry display name
        assert d["parent_updated_at"] is not None
        # Bundle + files.
        assert d["bundle_size"] is not None and d["bundle_size"] > 0
        names = {f["path"] for f in d["files"]}
        assert "SKILL.md" in names
        assert "REFERENCE.md" in names

    def test_agent_detail_single_file(self, web_client, tmp_path):
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        _seed_curated_agent_on_disk(tmp_path, "mkt-x", "alpha", "incident-responder")
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha/agent/incident-responder",
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["kind"] == "agent"
        # Agents are flat single-file .md → exactly one file entry.
        assert len(d["files"]) == 1
        assert d["files"][0]["path"] == "incident-responder.md"
        assert d["bundle_size"] == d["files"][0]["size"]


class TestSafeJoinContainment:
    """Defense-in-depth unit tests for ``_safe_join`` — the helper backing
    ``_read_inner`` / ``curated_skill_detail`` / ``curated_agent_detail``.

    The threat model is a curated marketplace's git mirror containing a
    booby-trapped symlink (or a future regression in Starlette's ``[^/]+``
    path-param regex letting ``..`` slip through). HTTP-level ``..`` tests
    aren't useful — httpx normalizes ``..`` segments before they reach the
    wire — so the guard is verified at the function boundary.
    """

    def _plugin_root(self, tmp_path):
        root = tmp_path / "marketplaces" / "mkt-x" / "plugins" / "alpha"
        (root / "skills").mkdir(parents=True)
        (root / "agents").mkdir(parents=True)
        return root

    def test_resolves_normal_skill_path(self, tmp_path):
        from app.api.marketplace import _safe_join
        root = self._plugin_root(tmp_path)
        skill_dir = root / "skills" / "data-explorer"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
        result = _safe_join(root, "skills", "data-explorer", "SKILL.md")
        assert result is not None
        assert result == (skill_dir / "SKILL.md").resolve()

    def test_dotdot_segment_escaping_root_returns_none(self, tmp_path):
        from app.api.marketplace import _safe_join
        root = self._plugin_root(tmp_path)
        # Plant a sibling plugin's file that `..` traversal would otherwise reach.
        sibling = tmp_path / "marketplaces" / "mkt-x" / "plugins" / "beta"
        sibling.mkdir(parents=True)
        (sibling / "SECRET.md").write_text("cross-plugin secret", encoding="utf-8")
        # /skills/../../beta/SECRET.md would resolve to the sibling's file.
        assert _safe_join(root, "skills", "..", "..", "beta", "SECRET.md") is None

    def test_symlink_outside_plugin_returns_none(self, tmp_path):
        import os, sys
        if sys.platform == "win32":
            pytest.skip("Symlink creation requires elevated permissions on Windows")
        from app.api.marketplace import _safe_join
        root = self._plugin_root(tmp_path)
        outside = tmp_path / "secrets" / "OTHER.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("cross-plugin secret", encoding="utf-8")
        # A curator-planted symlink inside skills/evil/ pointing outside the
        # plugin tree must not resolve through the guard.
        evil_dir = root / "skills" / "evil"
        evil_dir.mkdir()
        os.symlink(outside, evil_dir / "SKILL.md")
        assert _safe_join(root, "skills", "evil", "SKILL.md") is None

    def test_missing_file_returns_none(self, tmp_path):
        from app.api.marketplace import _safe_join
        root = self._plugin_root(tmp_path)
        assert _safe_join(root, "skills", "nope", "SKILL.md") is None

    def test_inner_endpoint_404s_on_symlink_escape(self, web_client, tmp_path):
        """End-to-end: the symlink containment check actually wires through
        the HTTP endpoint to a 404 (not a leaked 200)."""
        import os, sys
        if sys.platform == "win32":
            pytest.skip("Symlink creation requires elevated permissions on Windows")
        user_id, cookies = _create_user(web_client, "alice@x.com")
        _seed_curated_grant(user_id=user_id, marketplace="mkt-x", plugin="alpha")
        outside = tmp_path / "secrets" / "OTHER.md"
        outside.parent.mkdir(parents=True)
        outside.write_text(
            "---\nname: leaked\n---\ncross-plugin secret", encoding="utf-8",
        )
        evil_dir = (
            tmp_path / "marketplaces" / "mkt-x" / "plugins" / "alpha"
            / "skills" / "evil"
        )
        evil_dir.mkdir(parents=True)
        os.symlink(outside, evil_dir / "SKILL.md")
        r = web_client.get(
            "/api/marketplace/curated/mkt-x/alpha/skill/evil",
            cookies=cookies,
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "skill_not_found"


# ---------------------------------------------------------------------------
# Flea standalone detail — extended response shape
# ---------------------------------------------------------------------------


class TestFleaDetail:
    def test_flea_skill_detail_populates_files_owner_install_count(
        self, web_client,
    ):
        _, cookies = _create_user(web_client, "alice@x.com")
        # Upload a skill into the Store.
        up = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("alpha"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC}, cookies=cookies,
        )
        assert up.status_code == 201, up.text
        entity_id = up.json()["id"]

        r = web_client.get(
            f"/api/marketplace/flea/{entity_id}/detail", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["source"] == "flea"
        assert d["entity_id"] == entity_id
        # Files walked from disk.
        assert isinstance(d["files"], list) and len(d["files"]) >= 1
        # Friendly owner_display falls through to users.name (email local-part
        # is the seeded `name` in _create_user → 'alice').
        assert d["owner_display"] == "alice"
        # install_count starts at 0; bumps after install/uninstall toggle.
        assert d["install_count"] == 0
        # docs is always a list (empty when uploader didn't ship any).
        assert isinstance(d["docs"], list)


# ---------------------------------------------------------------------------
# v49 phase-2 — title + tagline + full-name owner on flea presentation
# ---------------------------------------------------------------------------


def _set_user_full_name(user_id: str, full_name: str) -> None:
    """Override the `users.name` field for an existing test user. Used to
    simulate the real-world case where a user has a proper full name
    (e.g. "Carolina Bsolinová Pauerová") distinct from their kebab-case
    `owner_username` derived from email (`c-bsolinovapauerova`)."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", [full_name, user_id])
    finally:
        conn.close()


class TestFleaPhase2Presentation:
    """v49 phase-2 — flea cards and detail pages surface `title` (humanized),
    `tagline`, and the owner's full name (`users.name`) instead of the
    kebab-case slug + bare username they used to render."""

    def test_flea_card_carries_title_tagline_and_full_name_owner(self, web_client):
        user_id, cookies = _create_user(web_client, "c_marustamyan@x.com")
        # Simulate a real account whose users.name is the friendly form;
        # owner_username on the entity will be the sanitized kebab-case
        # local-part ("c-marustamyan").
        _set_user_full_name(user_id, "Minas Arustamyan")

        web_client.post(
            "/api/store/entities",
            files={
                "file": ("s.zip", _make_skill_zip("mcp-builder"), "application/zip"),
            },
            data={
                "type": "skill",
                "description": _OK_DESC,
                "tagline": "Spawns MCP servers from a one-line prompt.",
            },
            cookies=cookies,
        )

        r = web_client.get("/api/marketplace/items?tab=flea", cookies=cookies)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 1
        it = items[0]
        # display_name carries the acronym-aware humanized title from
        # store_entities.title; JS card uses it as the visible heading.
        assert it["display_name"] == "MCP Builder"
        # tagline rides the existing curated chain; JS prefers it over
        # description for the card subtitle.
        assert it["tagline"] == "Spawns MCP servers from a one-line prompt."
        # owner is now the full users.name, not the kebab-case slug.
        assert it["owner"] == "Minas Arustamyan"
        # The technical suffixed slug stays on `name` as the JS-fallback
        # identifier (legacy compat — no card UI surfaces it directly).
        assert it["name"] == "mcp-builder-by-c-marustamyan"

    def test_flea_card_owner_falls_back_to_email_then_username(self, web_client):
        """When users.name is NULL, owner display falls back to users.email;
        when neither is present, to owner_username (defensive bottom)."""
        from src.db import get_system_db
        user_id, cookies = _create_user(web_client, "bob@x.com")
        # Clear the seeded users.name so the fallback chain kicks in.
        conn = get_system_db()
        try:
            conn.execute("UPDATE users SET name = NULL WHERE id = ?", [user_id])
        finally:
            conn.close()
        web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("alpha"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=cookies,
        )
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=cookies)
        assert r.status_code == 200
        it = r.json()["items"][0]
        # Fallback: users.name=NULL → users.email → "bob@x.com".
        assert it["owner"] == "bob@x.com"

    def test_flea_detail_exposes_title_and_tagline(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")
        up = web_client.post(
            "/api/store/entities",
            files={
                "file": ("s.zip", _make_skill_zip("oauth-server"), "application/zip"),
            },
            data={
                "type": "skill",
                "description": _OK_DESC,
                "tagline": "Mock OAuth provider for integration tests.",
            },
            cookies=cookies,
        )
        entity_id = up.json()["id"]

        r = web_client.get(
            f"/api/marketplace/flea/{entity_id}/detail", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        # `display_name` is the curated-style hero title — phase 2 wires
        # it up for flea so the heroTitle JS chain renders the friendly
        # form instead of falling through to plugin_name (= entity name).
        assert d["display_name"] == "OAuth Server"
        assert d["tagline"] == "Mock OAuth provider for integration tests."
        # plugin_name + manifest_name unchanged — the JS chain in templates
        # uses display_name first; these remain for backward compat with
        # paths that have always read the slug.
        assert d["plugin_name"] == "oauth-server"
        assert d["manifest_name"] == "oauth-server-by-alice"

    def test_flea_detail_tagline_null_when_omitted(self, web_client):
        """Tagline is optional — flea entity uploaded without it must
        surface as None on detail so the hero element stays hidden."""
        _, cookies = _create_user(web_client, "alice@x.com")
        up = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("notagline"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=cookies,
        )
        entity_id = up.json()["id"]
        r = web_client.get(
            f"/api/marketplace/flea/{entity_id}/detail", cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["tagline"] is None
        # display_name still set from the humanizer fallback in POST.
        assert d["display_name"] == "Notagline"

    def test_flea_inner_skill_parent_display_name_uses_title(self, web_client):
        """v49 phase-3: inner skill/agent detail of a flea plugin surfaces
        the parent plugin's user-friendly ``title`` (humanized) via
        ``parent_display_name``. JS chains (breadcrumb 3rd segment, hero
        "part of <plugin>", helper "This skill is part of <plugin>",
        sidebar "Parent plugin") all read this field first — single
        source swap drives every surface to the friendly form."""
        _, cookies = _create_user(web_client, "alice@x.com")
        up = web_client.post(
            "/api/store/entities",
            files={
                "file": (
                    "p.zip",
                    _make_plugin_zip("codex-second-opinion", inner_skill="codex-setup"),
                    "application/zip",
                ),
            },
            data={
                "type": "plugin",
                "description": _OK_DESC,
                "title": "Codex Second Opinion",
            },
            cookies=cookies,
        )
        assert up.status_code == 201, up.text
        entity_id = up.json()["id"]

        r = web_client.get(
            f"/api/marketplace/flea/{entity_id}/skill/codex-setup",
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        # Inner skill's own name still comes from frontmatter.
        assert d["name"] == "codex-setup"
        # Parent identification: manifest_name = entity.name (technical
        # slug used by the rename / archive paths); parent_display_name =
        # entity.title (the human form rendered everywhere on the UI).
        assert d["manifest_name"] == "codex-second-opinion"
        assert d["parent_display_name"] == "Codex Second Opinion"

    def test_flea_card_and_detail_read_synthetic_name_from_db(self, web_client):
        """v49 phase-3: ``MarketplaceItem.name`` (card) and
        ``PluginDetailResponse.manifest_name`` (detail) source from the
        stored ``synthetic_name`` column. Manually override the column to
        a non-canonical value — both surfaces must reflect the override,
        proving they read the column instead of recomputing
        ``<name>-by-<owner_username>``."""
        from src.db import get_system_db
        _, cookies = _create_user(web_client, "syntheticread@x.com")
        up = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("orig"), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=cookies,
        )
        eid = up.json()["id"]
        conn = get_system_db()
        try:
            conn.execute(
                "UPDATE store_entities SET synthetic_name = ? WHERE id = ?",
                ["manual-override-mkt", eid],
            )
        finally:
            conn.close()
        # Card
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=cookies)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "manual-override-mkt"
        # Detail
        d = web_client.get(
            f"/api/marketplace/flea/{eid}/detail", cookies=cookies,
        )
        assert d.status_code == 200, d.text
        assert d.json()["manifest_name"] == "manual-override-mkt"

    def test_flea_inner_skill_parent_display_name_humanize_fallback(self, web_client):
        """When title is omitted on upload, the POST endpoint humanizes the
        plugin name as a fallback — phase 3 must thread that humanized form
        into ``parent_display_name`` too, not the kebab-case slug."""
        _, cookies = _create_user(web_client, "alice@x.com")
        up = web_client.post(
            "/api/store/entities",
            files={
                "file": (
                    "p.zip",
                    _make_plugin_zip("mcp-tools", inner_skill="dummy"),
                    "application/zip",
                ),
            },
            data={"type": "plugin", "description": _OK_DESC},
            cookies=cookies,
        )
        entity_id = up.json()["id"]
        r = web_client.get(
            f"/api/marketplace/flea/{entity_id}/skill/dummy",
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        # Humanizer + acronym dict from phase 1 — "mcp-tools" → "MCP Tools".
        assert d["parent_display_name"] == "MCP Tools"
        assert d["manifest_name"] == "mcp-tools"


# ---------------------------------------------------------------------------
# v49 hardening — owner-display N+1 regression guard
# ---------------------------------------------------------------------------


class TestFleaOwnerDisplayBatched:
    """Pre-fix, ``_flea_to_item`` called ``_resolve_owner_display`` for every
    item in the list comprehension — one ``SELECT … FROM users WHERE id = ?``
    per item. v49 hardening batches the prefetch via ``_load_users_display``
    so the listing endpoint runs O(1) user lookups regardless of item count.

    Regression guard: assert ``_resolve_owner_display`` is NEVER invoked
    during the flea listing path. If a future change reintroduces the per-row
    helper inside ``_flea_to_item``, this fails immediately."""

    def test_listing_does_not_call_per_row_owner_resolver(self, web_client, monkeypatch):
        # Three owners + three flea uploads — enough to make any per-row
        # call obviously visible if it sneaks back in.
        for email in ("alice@x.com", "bob@x.com", "carol@x.com"):
            _, cookies = _create_user(web_client, email)
            web_client.post(
                "/api/store/entities",
                files={
                    "file": (
                        "s.zip",
                        _make_skill_zip(f"skill-{email.split('@')[0]}"),
                        "application/zip",
                    ),
                },
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        # Any logged-in user can see the public flea tab.
        _, viewer_cookies = _create_user(web_client, "viewer@x.com")

        # Spy on the per-row resolver. The fixed implementation must not
        # call it inside the listing comprehension.
        from app.api import marketplace as marketplace_module
        calls: list[str] = []
        original = marketplace_module._resolve_owner_display

        def spy(owner_user_id, fallback):
            calls.append(owner_user_id)
            return original(owner_user_id, fallback)

        monkeypatch.setattr(marketplace_module, "_resolve_owner_display", spy)

        r = web_client.get(
            "/api/marketplace/items?tab=flea", cookies=viewer_cookies,
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 3
        assert calls == [], (
            f"_resolve_owner_display called {len(calls)} times inside the "
            f"flea listing path — regression to the N+1 pattern."
        )

        # All three owner display strings still resolved correctly via the
        # batch prefetch (so we're not just sidestepping the assertion by
        # returning blank owners).
        owners = sorted(it["owner"] for it in items)
        assert owners == ["alice", "bob", "carol"]


class TestListMcpsTransport:
    """`_list_mcps` labels the transport shown on the plugin detail page.

    An explicit `type` in `.mcp.json` is authoritative — both Claude Code and
    the Agent Plugins `mcp.json` schema spell the transport that way. Only when
    it is absent do we infer, and a bare `url` is Streamable HTTP: HTTP+SSE was
    deprecated by the MCP spec and labelling every URL server `sse` mislabels
    the common case.
    """

    @staticmethod
    def _plugin_root(tmp_path, servers):
        root = tmp_path / "plug"
        root.mkdir(parents=True, exist_ok=True)
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8"
        )
        return root

    def _one(self, tmp_path, cfg):
        from app.api.marketplace import _list_mcps
        entries = _list_mcps(self._plugin_root(tmp_path, {"srv": cfg}))
        assert len(entries) == 1
        return entries[0]

    def test_explicit_streamable_http_is_honored(self, tmp_path):
        e = self._one(tmp_path, {"type": "streamable-http", "url": "https://x.test/mcp"})
        assert e.type == "streamable-http"

    def test_explicit_sse_is_honored(self, tmp_path):
        e = self._one(tmp_path, {"type": "sse", "url": "https://x.test/sse"})
        assert e.type == "sse"

    def test_explicit_stdio_is_honored(self, tmp_path):
        e = self._one(tmp_path, {"type": "stdio", "command": "srv-bin"})
        assert e.type == "stdio"

    def test_explicit_type_wins_over_inference(self, tmp_path):
        """A server carrying both `type` and `command` must not be re-inferred."""
        e = self._one(tmp_path, {"type": "sse", "command": "legacy-bin", "url": "https://x.test"})
        assert e.type == "sse"

    def test_bare_url_infers_http_not_sse(self, tmp_path):
        e = self._one(tmp_path, {"url": "https://x.test/mcp"})
        assert e.type == "http"

    def test_bare_command_infers_stdio(self, tmp_path):
        e = self._one(tmp_path, {"command": "srv-bin", "args": ["--flag"]})
        assert e.type == "stdio"
        assert e.description == "srv-bin --flag"

    def test_unrecognized_type_is_not_invented(self, tmp_path):
        """An unknown transport string is passed through, not coerced."""
        e = self._one(tmp_path, {"type": "websocket", "url": "wss://x.test"})
        assert e.type == "websocket"

    def test_no_transport_signal_leaves_type_none(self, tmp_path):
        e = self._one(tmp_path, {"description": "nothing actionable"})
        assert e.type is None




class TestDetailFileListingExclusions:
    """The plugin detail page's Files list and `bundle_size` must describe what
    Agnes actually serves. A root-source plugin's `plugin_root` IS the
    marketplace clone, so an unfiltered `rglob` sweeps `.git/**` (every loose
    object, ref and hook sample), `.agnes/**` and `marketplace-metadata.json`
    into the listing and the size — none of which reach the served ZIP / git
    tree (`src.marketplace_filter.is_unserved_path`).
    """

    @staticmethod
    def _clone(tmp_path):
        root = tmp_path / "clone"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "solo"}), encoding="utf-8")
        (root / ".claude-plugin" / "marketplace-metadata.json").write_text(
            json.dumps({"plugins": {}}), encoding="utf-8"
        )
        (root / "CLAUDE.md").write_text("# solo\n", encoding="utf-8")
        (root / ".git" / "objects").mkdir(parents=True)
        (root / ".git" / "config").write_text("[core]\n" * 100, encoding="utf-8")
        (root / ".agnes").mkdir()
        (root / ".agnes" / "cover.png").write_bytes(b"\x89PNG" * 50)
        return root

    def test_walk_files_skips_unserved_paths(self, tmp_path):
        from app.api.marketplace import _walk_files

        paths = {e.path for e in _walk_files(self._clone(tmp_path))}
        assert paths == {"CLAUDE.md", ".claude-plugin/plugin.json"}

    def test_bundle_size_counts_only_served_files(self, tmp_path):
        from app.api.marketplace import _bundle_size

        root = self._clone(tmp_path)
        expected = (root / "CLAUDE.md").stat().st_size + (root / ".claude-plugin" / "plugin.json").stat().st_size
        assert _bundle_size(root) == expected
