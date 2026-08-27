"""Tests for ``corporate_memory.distribution_mode`` enforcement (#1573).

``distribution_mode`` decides which OPTIONAL (approved, non-required) items
reach a caller across three surfaces that must never disagree:

- ``GET /api/memory/bundle`` (JSON, AI-agent injection)
- ``GET /api/memory/bundle?domain=<slug>`` (markdown, what ``agnes pull``
  writes to ``~/.claude/memory/<slug>/bundle.md``)
- ``GET /api/sync/manifest``'s ``memory_domains[].md5`` (the change-detection
  token the CLI trusts to decide whether to re-fetch the markdown above)

The md5-agreement tests matter operationally: if the manifest and the
markdown route ever disagree about which items are "distributable", an
analyst's ``agnes pull`` either converges on a permanently stale bundle.md
(md5 unchanged while content should have) or thrashes on pointless refetches
(md5 changed, content identical) — see
``app/api/memory.py::select_distributable_items``.
"""

from __future__ import annotations

import uuid

from src.db import get_system_db
from src.repositories.knowledge import KnowledgeRepository
from src.repositories.memory_domains import MemoryDomainsRepository
from src.repositories.resource_grants import ResourceGrantsRepository
from src.repositories.user_group_members import UserGroupMembersRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_domain(slug: str, name: str = "D") -> str:
    conn = get_system_db()
    try:
        existing = MemoryDomainsRepository(conn).get_by_slug(slug)
        if existing:
            return existing["id"]
        return MemoryDomainsRepository(conn).create(
            name=name,
            slug=slug,
            description="Test domain",
            icon=None,
            color=None,
            created_by="test",
        )
    finally:
        conn.close()


def _create_item(domain_id: str, title: str, *, status: str = "approved", is_required: bool = False) -> str:
    conn = get_system_db()
    try:
        item_id = "ki_" + uuid.uuid4().hex[:8]
        KnowledgeRepository(conn).create(
            id=item_id,
            title=title,
            content=f"Content for {title}",
            category="engineering",
            status=status,
            is_required=is_required,
        )
        MemoryDomainsRepository(conn).add_item(domain_id, item_id, added_by="test")
    finally:
        conn.close()
    return item_id


def _grant_domain_access(domain_id: str, *, user_id: str = "analyst1", group_name: str = "Everyone") -> None:
    conn = get_system_db()
    try:
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()[0]
        ResourceGrantsRepository(conn).create(
            group_id=gid,
            resource_type="memory_domain",
            resource_id=domain_id,
            assigned_by="test",
        )
        UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    finally:
        conn.close()


def _upvote(item_id: str, user_id: str) -> None:
    conn = get_system_db()
    try:
        KnowledgeRepository(conn).vote(item_id, user_id, 1)
    finally:
        conn.close()


def _set_distribution_mode(monkeypatch, mode) -> None:
    """Patch ``get_corporate_memory_config`` — every call site imports it
    inline at call time, so patching the module attribute is enough."""
    import app.instance_config as instance_config

    cfg = {"distribution_mode": mode} if mode is not None else {}
    monkeypatch.setattr(instance_config, "get_corporate_memory_config", lambda: cfg)


def _manifest_domain_entry(client, token, slug):
    body = client.get("/api/sync/manifest", headers=_auth(token)).json()
    return next(d for d in body["memory_domains"] if d["slug"] == slug)


def _markdown(client, token, slug):
    return client.get(f"/api/memory/bundle?domain={slug}", headers=_auth(token)).text


class TestHybridDefault:
    """Mode 3 (default): approved items are opt-in via personal upvote."""

    def test_not_upvoted_approved_item_excluded_from_all_three_surfaces(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-hybrid-1")
        req_id = _create_item(dom_id, "Required Fact", is_required=True)
        opt_id = _create_item(dom_id, "Optional Fact")
        _grant_domain_access(dom_id)

        md = _markdown(c, token, "dm-hybrid-1")
        assert "Required Fact" in md
        assert "Optional Fact" not in md

        entry = _manifest_domain_entry(c, token, "dm-hybrid-1")
        # md5 must correspond to a "required only" render — reproduce the
        # exact hash formula _build_memory_domains_section uses.
        import hashlib

        h = hashlib.md5()
        h.update(f"{req_id}|Required Fact|approved|True|Content for Required Fact|".encode())
        assert entry["md5"] == h.hexdigest()

        r = c.get("/api/memory/bundle", headers=_auth(token))
        approved_ids = {i["id"] for i in r.json()["approved"]}
        assert opt_id not in approved_ids

    def test_upvoted_approved_item_included_on_all_three_surfaces_and_md5_matches(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-hybrid-2")
        req_id = _create_item(dom_id, "Required Fact 2", is_required=True)
        opt_id = _create_item(dom_id, "Optional Fact 2")
        _grant_domain_access(dom_id)

        md5_before = _manifest_domain_entry(c, token, "dm-hybrid-2")["md5"]
        md_before = _markdown(c, token, "dm-hybrid-2")
        assert "Optional Fact 2" not in md_before

        _upvote(opt_id, "analyst1")

        md_after = _markdown(c, token, "dm-hybrid-2")
        assert "Optional Fact 2" in md_after
        assert "Required Fact 2" in md_after

        entry_after = _manifest_domain_entry(c, token, "dm-hybrid-2")
        assert entry_after["md5"] != md5_before

        import hashlib

        h = hashlib.md5()
        for iid, title, is_req, content in sorted(
            [
                (req_id, "Required Fact 2", True, "Content for Required Fact 2"),
                (opt_id, "Optional Fact 2", False, "Content for Optional Fact 2"),
            ]
        ):
            h.update(f"{iid}|{title}|approved|{is_req}|{content}|".encode())
        assert entry_after["md5"] == h.hexdigest()

        r = c.get("/api/memory/bundle", headers=_auth(token))
        approved_ids = {i["id"] for i in r.json()["approved"]}
        assert opt_id in approved_ids

    def test_md5_stable_when_distributable_set_unchanged(self, seeded_app):
        """A no-op refetch (nothing voted, nothing edited) must not flip
        the md5 — otherwise the CLI would thrash on every pull."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-hybrid-3")
        _create_item(dom_id, "Required Fact 3", is_required=True)
        _create_item(dom_id, "Optional Fact 3")
        _grant_domain_access(dom_id)

        md5_1 = _manifest_domain_entry(c, token, "dm-hybrid-3")["md5"]
        md5_2 = _manifest_domain_entry(c, token, "dm-hybrid-3")["md5"]
        assert md5_1 == md5_2

    def test_no_config_defaults_to_hybrid(self, seeded_app):
        """No ``corporate_memory:`` block at all → documented default."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-hybrid-nocfg")
        opt_id = _create_item(dom_id, "Optional NoCfg")
        _grant_domain_access(dom_id)

        md = _markdown(c, token, "dm-hybrid-nocfg")
        assert "Optional NoCfg" not in md

        _upvote(opt_id, "analyst1")
        md = _markdown(c, token, "dm-hybrid-nocfg")
        assert "Optional NoCfg" in md


class TestMandatoryOnly:
    """Mode 1: no optional channel at all, voting or not."""

    def test_upvoted_approved_item_still_excluded(self, seeded_app, monkeypatch):
        _set_distribution_mode(monkeypatch, "mandatory_only")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-mand-1")
        req_id = _create_item(dom_id, "Required Mand", is_required=True)
        opt_id = _create_item(dom_id, "Optional Mand")
        _grant_domain_access(dom_id)
        _upvote(opt_id, "analyst1")  # even upvoted — must not matter

        md = _markdown(c, token, "dm-mand-1")
        assert "Required Mand" in md
        assert "Optional Mand" not in md

        r = c.get("/api/memory/bundle", headers=_auth(token))
        data = r.json()
        assert opt_id not in {i["id"] for i in data["approved"]}
        assert req_id in {i["id"] for i in data["mandatory"]}

        entry = _manifest_domain_entry(c, token, "dm-mand-1")
        import hashlib

        h = hashlib.md5()
        h.update(f"{req_id}|Required Mand|approved|True|Content for Required Mand|".encode())
        assert entry["md5"] == h.hexdigest()


class TestAdminCurated:
    """Mode 2: voting is feedback only — distribution is admin-driven."""

    def test_upvoted_approved_item_excluded_from_distribution_but_visible_in_catalog(self, seeded_app, monkeypatch):
        _set_distribution_mode(monkeypatch, "admin_curated")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-curated-1")
        _create_item(dom_id, "Required Curated", is_required=True)
        opt_id = _create_item(dom_id, "Optional Curated")
        _grant_domain_access(dom_id)
        _upvote(opt_id, "analyst1")

        md = _markdown(c, token, "dm-curated-1")
        assert "Optional Curated" not in md

        r = c.get("/api/memory/bundle", headers=_auth(token))
        assert opt_id not in {i["id"] for i in r.json()["approved"]}

        # Distribution narrows; the browsable catalog is untouched — the
        # item is still an "approved" item admins/users can see and vote
        # on as feedback, just never auto-distributed.
        catalog = c.get("/api/memory", headers=_auth(token))
        assert catalog.status_code == 200
        assert opt_id in {i["id"] for i in catalog.json()["items"]}


class TestRequiredAlwaysDistributed:
    """Required items are unaffected by distribution_mode in every mode."""

    def test_required_item_present_in_every_mode(self, seeded_app, monkeypatch):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        for mode in ("mandatory_only", "admin_curated", "hybrid"):
            _set_distribution_mode(monkeypatch, mode)
            dom_id = _create_domain(f"dm-req-{mode}")
            _create_item(dom_id, f"Required {mode}", is_required=True)
            _grant_domain_access(dom_id)

            md = _markdown(c, token, f"dm-req-{mode}")
            assert f"Required {mode}" in md, f"mode={mode}"


class TestUnrecognizedMode:
    def test_unrecognized_mode_falls_back_to_hybrid_and_warns(self, seeded_app, monkeypatch, caplog):
        _set_distribution_mode(monkeypatch, "bogus_mode")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-unknown")
        opt_id = _create_item(dom_id, "Optional Unknown")
        _grant_domain_access(dom_id)
        _upvote(opt_id, "analyst1")

        import logging

        with caplog.at_level(logging.WARNING, logger="app.api.memory"):
            md = _markdown(c, token, "dm-unknown")
        # hybrid fallback: the upvoted item shows up.
        assert "Optional Unknown" in md
        assert any("distribution_mode" in rec.message for rec in caplog.records)


class TestRBACNarrowOnly:
    """distribution_mode can only narrow what a grant already allows — a
    vote never substitutes for a missing domain grant."""

    def test_no_grant_still_403_even_when_upvoted(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        dom_id = _create_domain("dm-no-grant")
        opt_id = _create_item(dom_id, "Ungranted Optional")
        _upvote(opt_id, "analyst1")  # no grant on the domain at all

        resp = c.get("/api/memory/bundle?domain=dm-no-grant", headers=_auth(token))
        assert resp.status_code == 403
