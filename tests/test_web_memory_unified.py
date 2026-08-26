"""GET /corporate-memory — unified Browse / My Stack with domain cards.

Task 8.4 of the v49 plan. The top-level page is now a Browse of memory
domains; the per-item richness moves to /memory/d/<slug> (Task 8.5).
"""

from __future__ import annotations

import uuid


import pytest


@pytest.fixture(autouse=True)
def _auto_membership_mode(monkeypatch):
    """This suite pins the AUTO-membership semantics, which are opt-in since
    the classic subscribe model became the default again (spec
    2026-08-07-default-chrome-ux-parity). Classic-mode contracts live in
    tests/test_stack_membership_modes.py (and the classic fan-out siblings in
    tests/test_e2e_stack_rbac.py / tests/test_cli_api_parity.py)."""
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_domain(slug: str = "qa", name: str = "QA", *, with_item: bool = True) -> str:
    """Create a memory domain and (by default) attach one approved item to
    it. Empty domains are hidden from /corporate-memory by design — a
    domain with no items has nothing for an analyst to opt-into — so tests
    asserting visibility must seed at least one item. Pass
    ``with_item=False`` to test the empty-hidden contract explicitly."""
    from src.db import get_system_db
    from src.repositories.memory_domains import MemoryDomainsRepository
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    try:
        domain_id = MemoryDomainsRepository(conn).create(
            slug=slug,
            name=name,
            description=f"{name} desc",
            icon="🎯",
            color="#dcfce7",
            created_by="test",
        )
        if with_item:
            kr = KnowledgeRepository(conn)
            item_id = str(uuid.uuid4())
            kr.create(
                id=item_id,
                title=f"{name} starter item",
                content="seeded for visibility test",
                category="convention",
                domain=slug,
                source_type="manual",
                source_user="test",
            )
            kr.update_status(item_id, "approved")
        return domain_id
    finally:
        conn.close()


def _grant(group_name: str, resource_id: str, requirement: str = "available", users: list[str] | None = None):
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid_row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid_row:
            return
        group_id = gid_row[0]
        if users:
            for u in users:
                try:
                    UserGroupMembersRepository(conn).add_member(u, group_id, source="test")
                except Exception:
                    pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'memory_domain', ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, resource_id, requirement],
        )
    finally:
        conn.close()


class TestMemoryUnifiedPage:
    def test_admin_sees_browse_and_my_stack_tabs(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Browse" in body
        assert "My Stack" in body

    def test_admin_without_grant_does_not_see_ungranted_domain(self, seeded_app):
        """Admin god-mode (``browse_admin``) was removed from the
        user-facing /corporate-memory — an ungranted domain must not
        appear even for an admin (moved to /admin/data-packages)."""
        _make_domain("qa-domain-1", "QA")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        assert "QA" not in resp.text

    def test_analyst_with_required_domain_grant_sees_card(self, seeded_app):
        """The granted domain reaches the caller.

        Read off /library. /corporate-memory 302s into the Library's Memory
        band whenever that band would show the caller a row — which a grant
        always does — so the page this used to assert on is not what a granted
        caller gets any more. The `is-required` class was corporate_memory.html's;
        the band's own locked-membership pill is guarded by
        tests/test_web_library_memory_band.py::test_required_membership_stays_locked.
        """
        dom_id = _make_domain("eng", "Engineering Memory")
        _grant("Everyone", dom_id, requirement="required", users=["analyst1"])
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/library", headers=_auth(token))
        assert resp.status_code == 200
        assert "Engineering Memory" in resp.text

    def test_analyst_no_grants_sees_empty_state(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # Either explicit "ask your admin" or "no memory domains" empty banner.
        assert "ask your admin" in body.lower() or "no memory" in body.lower()


class TestTopnavClassicMemoryCardState:
    """The restored pre-redesign /corporate-memory page, measured rather than
    reasoned about.

    A review round argued the frozen `corporate_memory_legacy.html` renders
    every domain as "not in stack", on the grounds that
    `_memory_domain_entry_dict` no longer emits `in_stack`. It does emit it
    (`app/web/router.py`, mirroring `entry.materialized`), and in classic mode
    `browse()` sets `in_stack = materialized`, so the two agree. But this PR
    is what makes that page the default surface again, so the claim is worth a
    rendered assertion instead of another round of grepping (Devin on #1199).
    """

    @pytest.fixture(autouse=True)
    def _classic_membership(self, monkeypatch):
        """Classic (subscribe) membership, stated rather than assumed.

        This used to `delenv` the three knobs on the grounds that the defaults
        WERE topnav + classic. Wave 0 (2026-08) flipped both — the rail is the
        only chrome and auto-membership is the default — so deleting them now
        selects the opposite mode from the one the class is named for. Classic
        remains a fully supported explicit opt-out."""
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")

    # `test_a_required_domain_card_reads_as_in_stack` was here. It read the
    # card off /corporate-memory, which 302s into the Library's Memory band
    # whenever that band would have a row for the caller — and a REQUIRED
    # domain always gives it one, so this case can no longer reach the page it
    # asserted on. The same fact is covered on the surface that renders it:
    # tests/test_web_library_memory_band.py::test_required_membership_stays_locked
    # pins the locked pill and the absence of a remove control.
    def _retired_test_a_required_domain_card_reads_as_in_stack(self, seeded_app):
        dom = _make_domain(slug="req-dom", name="Required Domain")
        _grant("Everyone", dom, requirement="required", users=["analyst1"])

        body = seeded_app["client"].get(
            "/corporate-memory", headers=_auth(seeded_app["analyst_token"])
        ).text

        assert "Required Domain" in body, "precondition: the granted domain renders at all"
        card = body[body.index("Required Domain") - 3000 : body.index("Required Domain") + 500]
        assert 'data-in-stack="1"' in card, (
            "a required domain rendered as NOT in stack — the card would offer "
            "'+ Add to stack' for something the user cannot remove"
        )

    # test_an_unsubscribed_available_domain_reads_as_addable was retired with
    # the Catalog browse shell it read from: /catalog now 302s into
    # /library?scope=available (the Catalog/Marketplace fold), so there is no
    # Memory grid there to carry `data-state="add"`. The addable/in-stack
    # split it guarded lives on the Library's own Memory band, pinned by
    # tests/test_web_library_memory_band.py.

