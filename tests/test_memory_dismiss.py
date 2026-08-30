"""Per-user opt-out (dismiss) for curated memory items — v46 feature.

Covers the four contract surfaces:

1. ``POST /api/memory/{item_id}/dismiss`` — idempotent dismiss; mandatory
   items get a 400 with the governance message; missing items 404.
2. ``DELETE /api/memory/{item_id}/dismiss`` — idempotent un-dismiss.
3. ``GET /api/memory?hide_dismissed=true`` — excludes the user's dismissed
   non-mandatory items but never hides mandatory ones (governance).
4. ``GET /api/memory/bundle`` — always excludes dismissed items for the
   caller, except mandatory ones (the always-on opt-out for AI agents).

Plus the listing carries ``dismissed_by_me`` per item so the frontend can
render the gray-out state without a separate roundtrip.
"""

from src.repositories.knowledge import KnowledgeRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_item(conn, item_id: str, title: str, status: str, *, confidence: float | None = None):
    """Insert a knowledge item directly through the repo + force its status.

    v49: Required tier is no longer encoded in ``status`` — passing
    ``status="mandatory"`` here is read as "this item should be marked
    required" and routed to ``is_required=TRUE`` while the actual lifecycle
    status is set to ``"approved"``. Other status values pass through.
    """
    repo = KnowledgeRepository(conn)
    if status == "mandatory":
        repo.create(
            id=item_id,
            title=title,
            content=f"Content for {title}",
            category="engineering",
            status="approved",
            confidence=confidence,
            is_required=True,
        )
    else:
        repo.create(
            id=item_id,
            title=title,
            content=f"Content for {title}",
            category="engineering",
            status=status,
            confidence=confidence,
        )
        # ``create`` honors the passed status, but bumping it again keeps
        # updated_at fresh — matches the pre-v49 helper behavior.
        repo.update_status(item_id, status)


class TestDismissPost:
    def test_dismiss_writes_row(self, seeded_app):
        """Non-admin dismissing an approved item lands a row in the table."""
        from src.db import get_system_db

        conn = get_system_db()
        _seed_item(conn, "dm_a1", "Approved Fact", "approved")
        conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/memory/dm_a1/dismiss",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"id": "dm_a1", "dismissed": True}

        conn = get_system_db()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item_user_dismissed WHERE user_id = 'analyst1' AND item_id = 'dm_a1'"
        ).fetchone()[0]
        assert cnt == 1
        conn.close()

    def test_dismiss_is_idempotent(self, seeded_app):
        """Re-dismissing the same item returns 200 and doesn't duplicate the row."""
        from src.db import get_system_db

        conn = get_system_db()
        _seed_item(conn, "dm_idem", "Approved Fact", "approved")
        conn.close()

        c = seeded_app["client"]
        for _ in range(3):
            r = c.post(
                "/api/memory/dm_idem/dismiss",
                headers=_auth(seeded_app["analyst_token"]),
            )
            assert r.status_code == 200

        conn = get_system_db()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item_user_dismissed WHERE user_id = 'analyst1' AND item_id = 'dm_idem'"
        ).fetchone()[0]
        assert cnt == 1
        conn.close()

    def test_dismiss_mandatory_item_rejected(self, seeded_app):
        """Mandatory items can never be dismissed — governance hard rule."""
        from src.db import get_system_db

        conn = get_system_db()
        _seed_item(conn, "dm_m1", "Mandatory Fact", "mandatory")
        conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/memory/dm_m1/dismiss",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "Cannot dismiss a mandatory item"

        # And nothing landed in the table.
        conn = get_system_db()
        cnt = conn.execute("SELECT COUNT(*) FROM knowledge_item_user_dismissed WHERE item_id = 'dm_m1'").fetchone()[0]
        assert cnt == 0
        conn.close()

    def test_dismiss_missing_item_returns_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.post(
            "/api/memory/nope-does-not-exist/dismiss",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 404

    def test_dismiss_requires_auth(self, seeded_app):
        r = seeded_app["client"].post("/api/memory/anything/dismiss")
        assert r.status_code == 401


class TestUndismissDelete:
    def test_delete_undismisses(self, seeded_app):
        """DELETE removes the dismissal row; subsequent DELETE is still 204."""
        from src.db import get_system_db

        conn = get_system_db()
        _seed_item(conn, "dm_u1", "Approved Fact", "approved")
        conn.close()

        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        c.post("/api/memory/dm_u1/dismiss", headers=_auth(token))

        r = c.delete("/api/memory/dm_u1/dismiss", headers=_auth(token))
        assert r.status_code == 204

        # Idempotent: a second DELETE still succeeds — absence of the row
        # is the success state.
        r2 = c.delete("/api/memory/dm_u1/dismiss", headers=_auth(token))
        assert r2.status_code == 204

        conn = get_system_db()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item_user_dismissed WHERE user_id = 'analyst1' AND item_id = 'dm_u1'"
        ).fetchone()[0]
        assert cnt == 0
        conn.close()

    def test_delete_missing_item_returns_404(self, seeded_app):
        r = seeded_app["client"].delete(
            "/api/memory/nope-still-no/dismiss",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 404


class TestListingHidesDismissed:
    def test_hide_dismissed_excludes_approved_but_keeps_mandatory(self, seeded_app):
        """``hide_dismissed=true`` filters dismissed approved items but
        leaves mandatory items visible even if a stale dismissal row
        exists for them — the governance hard rule reinforced at the SQL
        layer.
        """
        from src.db import get_system_db

        conn = get_system_db()
        _seed_item(conn, "dm_l_app", "Approved To Hide", "approved")
        _seed_item(conn, "dm_l_keep", "Approved To Keep", "approved")
        _seed_item(conn, "dm_l_mand", "Mandatory Survivor", "mandatory")
        # Hand-insert a dismissal row for the mandatory item too — simulates
        # the case where an item was approved + dismissed and later mandated.
        conn.execute(
            "INSERT INTO knowledge_item_user_dismissed (user_id, item_id) VALUES (?, ?)",
            ["analyst1", "dm_l_mand"],
        )
        conn.close()

        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        # Dismiss the approved item via the API.
        c.post("/api/memory/dm_l_app/dismiss", headers=_auth(token))

        # Without hide_dismissed the dismissed item still appears.
        r = c.get("/api/memory?per_page=100", headers=_auth(token))
        assert r.status_code == 200
        ids = {it["id"] for it in r.json()["items"]}
        assert {"dm_l_app", "dm_l_keep", "dm_l_mand"} <= ids

        # With hide_dismissed the approved item disappears, mandatory stays.
        r2 = c.get(
            "/api/memory?per_page=100&hide_dismissed=true",
            headers=_auth(token),
        )
        assert r2.status_code == 200
        ids2 = {it["id"] for it in r2.json()["items"]}
        assert "dm_l_app" not in ids2, "dismissed approved item must be excluded with hide_dismissed=true"
        assert "dm_l_keep" in ids2
        assert "dm_l_mand" in ids2, "mandatory item must remain visible even when a dismissal row exists"

    def test_listing_carries_dismissed_by_me_flag(self, seeded_app):
        """Each item in the listing carries ``dismissed_by_me``."""
        from src.db import get_system_db

        conn = get_system_db()
        _seed_item(conn, "dm_f_yes", "Will be dismissed", "approved")
        _seed_item(conn, "dm_f_no", "Will stay", "approved")
        conn.close()

        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        c.post("/api/memory/dm_f_yes/dismiss", headers=_auth(token))

        r = c.get("/api/memory?per_page=100", headers=_auth(token))
        assert r.status_code == 200
        items_by_id = {it["id"]: it for it in r.json()["items"]}
        assert items_by_id["dm_f_yes"]["dismissed_by_me"] is True
        assert items_by_id["dm_f_no"]["dismissed_by_me"] is False


class TestBundleAlwaysHidesDismissed:
    def test_bundle_excludes_dismissed_approved_but_keeps_mandatory(self, seeded_app):
        """The bundle endpoint is the always-on opt-out for AI agents —
        no query param needed; dismissed approved items are gone, but
        mandatory items stay regardless of any stale dismissal row.
        """
        from src.db import get_system_db

        conn = get_system_db()
        _seed_item(conn, "dm_b_app", "Approved For Bundle", "approved", confidence=0.8)
        _seed_item(conn, "dm_b_keep", "Approved Survivor", "approved", confidence=0.7)
        _seed_item(conn, "dm_b_mand", "Mandatory Bundle Item", "mandatory")
        # Stale dismissal for the mandatory item — must NOT hide it.
        conn.execute(
            "INSERT INTO knowledge_item_user_dismissed (user_id, item_id) VALUES (?, ?)",
            ["analyst1", "dm_b_mand"],
        )
        # #1573: default distribution_mode (hybrid) gates approved items
        # behind a personal upvote — vote both so dismissal, not the
        # distribution gate, is what this test exercises.
        repo = KnowledgeRepository(conn)
        repo.vote("dm_b_app", "analyst1", 1)
        repo.vote("dm_b_keep", "analyst1", 1)
        conn.close()

        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        # Dismiss the approved item normally.
        c.post("/api/memory/dm_b_app/dismiss", headers=_auth(token))

        r = c.get("/api/memory/bundle", headers=_auth(token))
        assert r.status_code == 200
        body = r.json()
        mandatory_ids = {i["id"] for i in body["mandatory"]}
        approved_ids = {i["id"] for i in body["approved"]}

        assert "dm_b_app" not in approved_ids, "dismissed approved item must be excluded from the bundle"
        assert "dm_b_keep" in approved_ids
        assert "dm_b_mand" in mandatory_ids, "mandatory item must remain in the bundle even with a stale dismissal row"
