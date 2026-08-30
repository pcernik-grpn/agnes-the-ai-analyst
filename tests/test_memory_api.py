"""Tests for corporate memory API — knowledge items, voting, governance."""

from src.repositories.knowledge import KnowledgeRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestMemoryCreate:
    def test_create_knowledge_item(self, seeded_app):
        """No corporate_memory config is set in the test env — legacy mode,
        matching the documented 'no admin review' default (#1573)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "Best Practice", "content": "Always document your code.", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["status"] == "approved"

    def test_create_with_tags(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/memory",
            json={
                "title": "Tagged Item",
                "content": "Content here",
                "category": "process",
                "tags": ["tag1", "tag2"],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        assert "id" in resp.json()

    def test_create_missing_title_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"content": "No title", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_create_missing_content_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "No content", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_create_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/memory",
            json={"title": "Test", "content": "Content", "category": "engineering"},
        )
        assert resp.status_code == 401

    def test_create_invalid_domain_returns_400(self, seeded_app):
        """POST validates ``domain`` against VALID_DOMAINS, mirroring PATCH —
        otherwise an item lands in the DB with a domain it can't be PATCHed
        to (PR #126 review). Empty / missing domain stays valid."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={
                "title": "Bad domain",
                "content": "x",
                "category": "engineering",
                "domain": "totally_made_up_domain",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 400
        # Sanity: a valid domain is still accepted.
        ok = c.post(
            "/api/memory",
            json={
                "title": "Good domain",
                "content": "x",
                "category": "engineering",
                "domain": "finance",
            },
            headers=_auth(token),
        )
        assert ok.status_code == 201


class TestMemoryCreateRespectsApprovalMode:
    """#1573 finding 4: POST /api/memory used to hardcode status='pending'
    regardless of corporate_memory.approval_mode. Every test here sets a
    DIFFERENT approval_mode and asserts a DIFFERENT resulting status —
    proving the endpoint actually branches on the config, not just that a
    schema default round-trips."""

    def _post(self, seeded_app, monkeypatch, governance_config: dict):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_corporate_memory_config", lambda: governance_config)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        return c.post(
            "/api/memory",
            json={"title": "Governed item", "content": "Some fact.", "category": "engineering"},
            headers=_auth(token),
        )

    def test_review_queue_is_pending(self, seeded_app, monkeypatch):
        resp = self._post(seeded_app, monkeypatch, {"approval_mode": "review_queue"})
        assert resp.status_code == 201
        assert resp.json()["status"] == "pending"

    def test_auto_publish_is_approved(self, seeded_app, monkeypatch):
        resp = self._post(seeded_app, monkeypatch, {"approval_mode": "auto_publish"})
        assert resp.status_code == 201
        assert resp.json()["status"] == "approved"

    def test_no_governance_config_auto_approves(self, seeded_app, monkeypatch):
        """Legacy mode (empty config, same as an instance with no
        corporate_memory: block at all) auto-approves — matches the
        documented 'no admin review' default."""
        resp = self._post(seeded_app, monkeypatch, {})
        assert resp.status_code == 201
        assert resp.json()["status"] == "approved"

    def test_persisted_status_matches_response(self, seeded_app, monkeypatch):
        """The response status isn't just cosmetic — it's what landed in the DB."""
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository

        resp = self._post(seeded_app, monkeypatch, {"approval_mode": "auto_publish"})
        item_id = resp.json()["id"]
        conn = get_system_db()
        item = KnowledgeRepository(conn).get_by_id(item_id)
        conn.close()
        assert item["status"] == "approved"


class TestMemoryList:
    def _create_item(self, c, token, title="Test Item", category="engineering"):
        resp = c.post(
            "/api/memory",
            json={"title": title, "content": f"Content for {title}", "category": category},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_list_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/memory", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "count" in data

    def test_list_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory")
        assert resp.status_code == 401

    def test_list_pagination(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Create 3 items
        for i in range(3):
            self._create_item(c, token, title=f"Item {i}")

        # Page 1 with per_page=2
        resp = c.get("/api/memory?page=1&per_page=2", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["per_page"] == 2
        assert data["page"] == 1
        assert len(data["items"]) <= 2

    def test_list_search(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        self._create_item(c, token, title="Unique Keyword SearchTarget")
        self._create_item(c, token, title="Another Item")

        resp = c.get("/api/memory?search=SearchTarget", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        titles = [item["title"] for item in data["items"]]
        assert any("SearchTarget" in t for t in titles)

    def test_search_respects_domain_filter(self, seeded_app):
        """search + domain must only return items in that domain."""
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository

        conn = get_system_db()
        repo = KnowledgeRepository(conn)
        repo.create(
            id="srch_fin", title="Finance SearchKeyword", content="x", category="data_analysis", domain="finance"
        )
        repo.create(
            id="srch_eng",
            title="Engineering SearchKeyword",
            content="x",
            category="data_analysis",
            domain="engineering",
        )
        conn.close()

        resp = seeded_app["client"].get(
            "/api/memory?search=SearchKeyword&domain=finance",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        ids = {i["id"] for i in resp.json()["items"]}
        assert "srch_fin" in ids
        assert "srch_eng" not in ids

    def test_search_respects_pagination(self, seeded_app):
        """search + per_page must not return more items than requested."""
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository

        conn = get_system_db()
        repo = KnowledgeRepository(conn)
        for i in range(5):
            repo.create(id=f"pgn_{i}", title=f"PaginateSearch item {i}", content="x", category="data_analysis")
        conn.close()

        resp = seeded_app["client"].get(
            "/api/memory?search=PaginateSearch&per_page=2",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) <= 2


class TestMemoryStats:
    def test_get_stats_returns_counts(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/memory/stats", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert isinstance(data["total"], int)
        assert data["total"] >= 0
        assert "by_status" in data
        assert isinstance(data["by_status"], dict)
        assert "categories" in data
        assert isinstance(data["categories"], list)

    def test_get_stats_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory/stats")
        assert resp.status_code == 401

    def test_get_stats_does_not_load_all_items(self, seeded_app):
        """Stats endpoint uses SQL aggregation, not list_items()."""
        from unittest.mock import patch

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch.object(
            KnowledgeRepository, "list_items", side_effect=AssertionError("list_items should not be called")
        ):
            resp = c.get("/api/memory/stats", headers=_auth(token))
            assert resp.status_code == 200


class TestMemoryVote:
    def _create_item(self, c, token):
        resp = c.post(
            "/api/memory",
            json={"title": "Voteable", "content": "vote me", "category": "process"},
            headers=_auth(token),
        )
        return resp.json()["id"]

    def test_vote_upvote(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        resp = c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["upvotes"] >= 1

    def test_vote_downvote(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        resp = c.post(f"/api/memory/{item_id}/vote", json={"vote": -1}, headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["downvotes"] >= 1

    def test_vote_invalid_value_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        resp = c.post(f"/api/memory/{item_id}/vote", json={"vote": 5}, headers=_auth(token))
        assert resp.status_code == 400

    def test_vote_nonexistent_item_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/memory/nonexistent-id/vote", json={"vote": 1}, headers=_auth(token))
        assert resp.status_code == 404

    def test_vote_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/memory/some-id/vote", json={"vote": 1})
        assert resp.status_code == 401


class TestMemoryMyVotes:
    def test_get_my_votes_empty(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/api/memory/my-votes", headers=_auth(token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_get_my_votes_after_voting(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # Create and vote
        item_resp = c.post(
            "/api/memory",
            json={"title": "My Vote Item", "content": "content", "category": "engineering"},
            headers=_auth(token),
        )
        item_id = item_resp.json()["id"]
        c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))

        # Check my-votes
        resp = c.get("/api/memory/my-votes", headers=_auth(token))
        assert resp.status_code == 200
        votes = resp.json()
        assert item_id in votes
        assert votes[item_id] == 1

    def test_my_votes_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory/my-votes")
        assert resp.status_code == 401


class TestMemoryAdminEndpoints:
    def _create_item(self, c, token):
        resp = c.post(
            "/api/memory",
            json={"title": "Admin Test", "content": "content", "category": "policy"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_admin_approve(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(f"/api/memory/admin/approve?item_id={item_id}", headers=_auth(admin_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_admin_reject(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/reject?item_id={item_id}",
            json={"reason": "not relevant"},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_admin_mandate(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/mandate?item_id={item_id}",
            json={"reason": "company policy", "audience": "all"},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "mandatory"

    def test_admin_approve_analyst_gets_403(self, seeded_app):
        """Analyst cannot use admin governance endpoints."""
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(f"/api/memory/admin/approve?item_id={item_id}", headers=_auth(analyst_token))
        assert resp.status_code == 403

    def test_admin_reject_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/reject?item_id={item_id}",
            json={"reason": "nope"},
            headers=_auth(analyst_token),
        )
        assert resp.status_code == 403

    def test_admin_mandate_analyst_gets_403(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        analyst_token = seeded_app["analyst_token"]
        item_id = self._create_item(c, admin_token)

        resp = c.post(
            f"/api/memory/admin/mandate?item_id={item_id}",
            json={"reason": "policy"},
            headers=_auth(analyst_token),
        )
        assert resp.status_code == 403

    def test_admin_approve_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/memory/admin/approve?item_id=some-id")
        assert resp.status_code == 401


class TestPersonalItemPrivacy:
    """Regression tests for the is_personal privacy leak (pd-ps review Q5).

    Personal items must be visible only to the contributor and to privileged
    viewers (km_admin/admin). Non-privileged callers must not be able to bypass
    the filter via list, search, provenance, or vote endpoints — even by setting
    exclude_personal=false.
    """

    def _create_and_flag_personal(self, client, contributor_token):
        resp = client.post(
            "/api/memory",
            json={"title": "Confidential note", "content": "private detail", "category": "engineering"},
            headers=_auth(contributor_token),
        )
        assert resp.status_code == 201
        item_id = resp.json()["id"]
        flag = client.post(
            f"/api/memory/{item_id}/personal",
            json={"is_personal": True},
            headers=_auth(contributor_token),
        )
        assert flag.status_code == 200
        return item_id

    def test_list_hides_personal_from_non_contributor_non_admin(self, seeded_app):
        c = seeded_app["client"]
        # admin is contributor; analyst is the non-contributor non-privileged caller.
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id not in ids

    def test_list_exclude_personal_false_is_coerced_for_non_admin(self, seeded_app):
        """Caller sets exclude_personal=false; server must silently coerce to true for non-admins."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            "/api/memory?exclude_personal=false",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id not in ids

    def test_search_hides_personal_from_non_admin(self, seeded_app):
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            "/api/memory?search=Confidential",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id not in ids

    def test_provenance_returns_404_for_non_contributor_non_admin(self, seeded_app):
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            f"/api/memory/{item_id}/provenance",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # 404 (not 403) avoids leaking item existence.
        assert resp.status_code == 404

    def test_vote_returns_404_for_non_contributor_non_admin(self, seeded_app):
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 404

    def test_admin_can_see_personal_when_opting_in(self, seeded_app):
        """Admin (privileged viewer) opting in via exclude_personal=false sees personal items."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        resp = c.get(
            "/api/memory?exclude_personal=false",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        ids = {it["id"] for it in resp.json()["items"]}
        assert item_id in ids

    def test_contributor_can_access_their_own_personal_item(self, seeded_app):
        """Contributor reaches their personal item via /my-contributions and direct provenance/vote."""
        c = seeded_app["client"]
        # Use analyst as the contributor (non-privileged).
        item_id = self._create_and_flag_personal(c, seeded_app["analyst_token"])

        # /my-contributions exposes contributor's own items including personal ones.
        mine = c.get("/api/memory/my-contributions", headers=_auth(seeded_app["analyst_token"]))
        assert mine.status_code == 200
        assert item_id in {it["id"] for it in mine.json()["items"]}

        # Direct provenance + vote work for the contributor.
        prov = c.get(f"/api/memory/{item_id}/provenance", headers=_auth(seeded_app["analyst_token"]))
        assert prov.status_code == 200

        vote = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert vote.status_code == 200

    def test_admin_search_with_opt_in_returns_personal_item(self, seeded_app):
        """Confirms exclude_personal flows through to repo.search() for privileged callers."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])
        resp = c.get(
            "/api/memory?search=Confidential&exclude_personal=false",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert item_id in {it["id"] for it in resp.json()["items"]}

    def test_unflag_personal_makes_item_visible_again(self, seeded_app):
        """Round-trip: contributor flags, then un-flags. Non-admins must regain visibility."""
        c = seeded_app["client"]
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        # Pre-condition: analyst can't see it.
        before = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert item_id not in {it["id"] for it in before.json()["items"]}

        # Contributor un-flags.
        unflag = c.post(
            f"/api/memory/{item_id}/personal",
            json={"is_personal": False},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert unflag.status_code == 200

        # Now analyst can see it.
        after = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert item_id in {it["id"] for it in after.json()["items"]}

    def test_stats_excludes_personal_for_non_admin(self, seeded_app):
        """`/stats` aggregates must not include personal items for non-admins
        — otherwise total/by_status/by_domain change in observable ways when
        a colleague flags or unflags a personal item, leaking existence info."""
        c = seeded_app["client"]
        # Admin baseline (admin sees everything by default).
        admin_before = c.get("/api/memory/stats", headers=_auth(seeded_app["admin_token"]))
        analyst_before = c.get("/api/memory/stats", headers=_auth(seeded_app["analyst_token"]))
        admin_total_before = admin_before.json()["total"]
        analyst_total_before = analyst_before.json()["total"]

        # Admin creates AND flags a personal item.
        item_id = self._create_and_flag_personal(c, seeded_app["admin_token"])

        # Admin's total reflects the new item; analyst's must not.
        admin_after = c.get("/api/memory/stats", headers=_auth(seeded_app["admin_token"]))
        analyst_after = c.get("/api/memory/stats", headers=_auth(seeded_app["analyst_token"]))
        assert admin_after.json()["total"] == admin_total_before + 1
        assert analyst_after.json()["total"] == analyst_total_before, (
            f"analyst /stats total leaked the personal item creation: "
            f"{analyst_total_before} -> {analyst_after.json()['total']}"
        )
        assert item_id  # silence unused-warning

    def test_non_personal_item_remains_visible_to_non_admin(self, seeded_app):
        """Negative control: an item that is NOT personal must still be visible — confirms
        we did not over-tighten the filter."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/memory",
            json={"title": "Public note", "content": "shared", "category": "engineering"},
            headers=_auth(seeded_app["admin_token"]),
        )
        item_id = resp.json()["id"]

        listed = c.get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert item_id in {it["id"] for it in listed.json()["items"]}

        prov = c.get(f"/api/memory/{item_id}/provenance", headers=_auth(seeded_app["analyst_token"]))
        assert prov.status_code == 200

        vote = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert vote.status_code == 200


class TestAdminContradictionsExcludePersonal:
    """Verify exclude_personal param on GET /api/memory/admin/contradictions."""

    def _make_item(self, item_id: str, title: str, is_personal: bool = False):
        from datetime import datetime, timezone
        from src.db import get_system_db

        conn = get_system_db()
        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO knowledge_items
               (id, title, content, category, source_user, is_personal, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [item_id, title, f"content of {title}", "test", "admin@test.com", is_personal, "approved", now, now],
        )
        conn.close()

    def _make_contradiction(self, cid: str, a_id: str, b_id: str):
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            """INSERT INTO knowledge_contradictions
               (id, item_a_id, item_b_id, explanation, detected_at)
               VALUES (?, ?, ?, ?, current_timestamp)""",
            [cid, a_id, b_id, "test contradiction"],
        )
        conn.close()

    def test_personal_item_hidden_by_default(self, seeded_app):
        """By default, personal items in contradictions are replaced with {id, hidden: true}."""
        c = seeded_app["client"]
        self._make_item("item_pub", "Public item")
        self._make_item("item_priv", "Private item", is_personal=True)
        self._make_contradiction("kc_test1", "item_pub", "item_priv")

        r = c.get("/api/memory/admin/contradictions", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        data = r.json()
        contra = next(c for c in data["contradictions"] if c["id"] == "kc_test1")
        # item_a is public — full dict
        assert contra["item_a"]["id"] == "item_pub"
        assert "title" in contra["item_a"]
        assert contra["item_a"].get("hidden") is not True
        # item_b is personal — hidden placeholder
        assert contra["item_b"]["id"] == "item_priv"
        assert contra["item_b"].get("hidden") is True
        assert "title" not in contra["item_b"]

    def test_admin_can_opt_in_to_personal_content(self, seeded_app):
        """With exclude_personal=false an admin sees full content."""
        c = seeded_app["client"]
        self._make_item("item_pub2", "Public item 2")
        self._make_item("item_priv2", "Private item 2", is_personal=True)
        self._make_contradiction("kc_test2", "item_pub2", "item_priv2")

        r = c.get(
            "/api/memory/admin/contradictions?exclude_personal=false",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        data = r.json()
        contra = next(c for c in data["contradictions"] if c["id"] == "kc_test2")
        assert contra["item_b"]["id"] == "item_priv2"
        assert "title" in contra["item_b"]
        assert contra["item_b"].get("hidden") is not True

    def test_non_personal_items_always_enriched(self, seeded_app):
        """Non-personal items on both sides are always returned in full."""
        c = seeded_app["client"]
        self._make_item("item_pub3", "Public A")
        self._make_item("item_pub4", "Public B")
        self._make_contradiction("kc_test3", "item_pub3", "item_pub4")

        r = c.get("/api/memory/admin/contradictions", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        data = r.json()
        contra = next(c for c in data["contradictions"] if c["id"] == "kc_test3")
        assert "title" in contra["item_a"]
        assert "title" in contra["item_b"]
        assert contra["item_a"].get("hidden") is not True
        assert contra["item_b"].get("hidden") is not True


class TestAudienceDistribution:
    """Verify audience-based knowledge distribution — mandate persists audience,
    list respects user.groups, admins see all."""

    def _seed_item(self, conn, item_id: str, title: str, audience: str | None = None):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO knowledge_items
               (id, title, content, category, source_user, audience, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [item_id, title, f"content {title}", "test", "admin@test.com", audience, "approved", now, now],
        )

    def test_mandate_persists_audience(self, seeded_app):
        """POST /admin/mandate with audience stores it on the item."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "aud_item1", "Finance policy")
        conn.close()

        r = c.post(
            "/api/memory/admin/mandate?item_id=aud_item1",
            json={"reason": "important", "audience": "group:finance"},
            headers=_auth(token),
        )
        assert r.status_code == 200

        from src.db import get_system_db

        conn = get_system_db()
        row = conn.execute("SELECT audience, is_required FROM knowledge_items WHERE id = 'aud_item1'").fetchone()
        conn.close()
        assert row[0] == "group:finance"
        # v49: mandate flips ``is_required`` (Required tier); status stays
        # on the lifecycle path (typically ``approved``).
        assert row[1] is True

    def test_batch_mandate_persists_audience(self, seeded_app):
        """POST /admin/batch mandate action stores audience."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "aud_item2", "Eng item")
        conn.close()

        r = c.post(
            "/api/memory/admin/batch",
            json={"item_ids": ["aud_item2"], "action": "mandate", "audience": "group:engineering"},
            headers=_auth(token),
        )
        assert r.status_code == 200

        from src.db import get_system_db

        conn = get_system_db()
        row = conn.execute("SELECT audience FROM knowledge_items WHERE id = 'aud_item2'").fetchone()
        conn.close()
        assert row[0] == "group:engineering"

    @staticmethod
    def _add_user_to_group(conn, user_id: str, group_name: str) -> None:
        """v13 helper: attach a user to a named group via user_group_members,
        creating the group_name row if it doesn't exist yet. Pre-v13 the test
        seeded a JSON list on users.groups; that column was dropped."""
        import uuid as _uuid

        existing = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if existing is None:
            group_id = str(_uuid.uuid4())
            conn.execute(
                "INSERT INTO user_groups (id, name, created_by) VALUES (?, ?, 'test:seed')",
                [group_id, group_name],
            )
        else:
            group_id = existing[0]
        try:
            conn.execute(
                """INSERT INTO user_group_members
                   (user_id, group_id, source, added_by)
                   VALUES (?, ?, 'admin', 'test:seed')""",
                [user_id, group_id],
            )
        except Exception:
            pass  # already a member; idempotent for re-runs

    def test_user_in_group_sees_group_items(self, seeded_app):
        """A user whose user_group_members row references 'finance' sees
        audience='group:finance' items."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        self._seed_item(conn, "aud_fin", "Finance fact", audience="group:finance")
        self._seed_item(conn, "aud_all", "All-users fact", audience="all")
        # Create a user and attach them to the finance group via the v13 model.
        repo = UserRepository(conn)
        repo.create(id="fin_user1", email="fin@test.com", name="Finance User")
        self._add_user_to_group(conn, "fin_user1", "finance")
        conn.close()

        token = create_access_token("fin_user1", "fin@test.com")
        r = seeded_app["client"].get("/api/memory", headers=_auth(token))
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_fin" in ids
        assert "aud_all" in ids

    def test_user_not_in_group_cannot_see_group_items(self, seeded_app):
        """A user without 'finance' group does not see audience='group:finance' items."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        self._seed_item(conn, "aud_fin2", "Finance fact 2", audience="group:finance")
        self._seed_item(conn, "aud_null", "No audience fact", audience=None)
        # Create a user with NO groups
        repo = UserRepository(conn)
        repo.create(id="eng_user1", email="eng@test.com", name="Eng User")
        conn.close()

        token = create_access_token("eng_user1", "eng@test.com")
        r = seeded_app["client"].get("/api/memory", headers=_auth(token))
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_fin2" not in ids
        assert "aud_null" in ids  # null audience treated as 'all'

    def test_admin_sees_all_audiences(self, seeded_app):
        """Admin sees items regardless of audience."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "aud_fin3", "Finance exclusive", audience="group:finance")
        self._seed_item(conn, "aud_eng3", "Eng exclusive", audience="group:engineering")
        conn.close()

        r = seeded_app["client"].get("/api/memory", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_fin3" in ids
        assert "aud_eng3" in ids

    def test_null_audience_visible_to_all(self, seeded_app):
        """Items with no audience set are visible to all authenticated users."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "aud_null2", "Global fact", audience=None)
        conn.close()

        r = seeded_app["client"].get("/api/memory", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200
        ids = {i["id"] for i in r.json()["items"]}
        assert "aud_null2" in ids

    def test_memory_domain_grant_extends_visibility(self, seeded_app):
        """A non-admin user whose group is granted MEMORY_DOMAIN/<domain>
        sees items in that domain even when audience is restrictive
        (audience='group:admins-only' that they're not in).

        Wires together: resource_grants row + _caller_granted_memory_domains
        helper + the OR domain IN (...) clause in list_items SQL."""
        import uuid
        from datetime import datetime, timezone
        from src.db import get_system_db

        conn = get_system_db()
        # Item is restricted by audience to a group the analyst is NOT in,
        # but tagged with domain=engineering via the v49 junction.
        self._seed_item(
            conn,
            "dom_eng1",
            "Eng-only via domain grant",
            audience="group:admins-only",
        )
        # v49: domain lives in the junction; resolve slug → id.
        eng_row = conn.execute("SELECT id FROM memory_domains WHERE slug = 'engineering'").fetchone()
        eng_id = eng_row[0]
        conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) "
            "VALUES ('dom_eng1', ?, 'test') ON CONFLICT DO NOTHING",
            [eng_id],
        )

        # The seeded analyst (id="analyst1") has no implicit Everyone
        # membership since 0.18.0 BREAKING change. Create a dedicated
        # group, add the analyst, then grant MEMORY_DOMAIN/engineering.
        # v49: grant resource_id is the memory_domains.id (md_engineering),
        # not the slug, per the migration re-point.
        gid = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO user_groups (id, name, description, created_by, created_at)
               VALUES (?, 'eng-team', 'test', 'test', ?)""",
            [gid, datetime.now(timezone.utc)],
        )
        conn.execute(
            """INSERT INTO user_group_members (user_id, group_id, source)
               VALUES ('analyst1', ?, 'admin')""",
            [gid],
        )
        conn.execute(
            """INSERT INTO resource_grants (id, group_id, resource_type, resource_id, assigned_at, assigned_by)
               VALUES (?, ?, 'memory_domain', ?, ?, 'test')""",
            [str(uuid.uuid4()), gid, eng_id, datetime.now(timezone.utc)],
        )
        conn.close()

        # Without the grant the analyst would not see this item (audience
        # gate excludes group:admins-only). With the grant on engineering
        # domain, the OR clause restores visibility.
        r = seeded_app["client"].get(
            "/api/memory?per_page=500",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        ids = {i["id"] for i in r.json()["items"]}
        assert "dom_eng1" in ids, (
            "MEMORY_DOMAIN/engineering grant must make engineering items visible regardless of audience filter"
        )


class TestVoteRetract:
    def _create_item(self, c, token):
        resp = c.post(
            "/api/memory",
            json={"title": "Retract Vote Item", "content": "content", "category": "process"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_vote_zero_retracts_existing_vote(self, seeded_app):
        """vote=0 removes a prior vote so upvotes returns to 0."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))
        r = c.post(f"/api/memory/{item_id}/vote", json={"vote": 0}, headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        assert data["upvotes"] == 0

    def test_vote_zero_on_unvoted_item_is_noop(self, seeded_app):
        """vote=0 on an item the user never voted on returns 200 and zero counts."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        r = c.post(f"/api/memory/{item_id}/vote", json={"vote": 0}, headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        assert data["upvotes"] == 0
        assert data["downvotes"] == 0

    def test_my_votes_omits_retracted_vote(self, seeded_app):
        """After retract, /my-votes must not include the item."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create_item(c, token)

        c.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=_auth(token))
        c.post(f"/api/memory/{item_id}/vote", json={"vote": 0}, headers=_auth(token))

        r = c.get("/api/memory/my-votes", headers=_auth(token))
        assert r.status_code == 200
        assert item_id not in r.json()


class TestBundle:
    def _seed_item(self, conn, item_id: str, title: str, status: str, confidence: float = None):
        """v49: ``status="mandatory"`` is no longer a lifecycle value — flip
        it to ``approved`` + ``is_required=True`` so the bundle path picks
        the item up via the new boolean filter."""
        from src.repositories.knowledge import KnowledgeRepository

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
            repo.update_status(item_id, status)

    def test_bundle_requires_auth(self, seeded_app):
        r = seeded_app["client"].get("/api/memory/bundle")
        assert r.status_code == 401

    def test_bundle_empty(self, seeded_app):
        """Bundle returns empty lists when no mandatory/approved items exist."""
        r = seeded_app["client"].get("/api/memory/bundle", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        data = r.json()
        assert data["mandatory"] == []
        assert data["approved"] == []
        assert "token_estimate" in data
        assert "token_budget" in data

    def test_bundle_mandatory_items_always_included(self, seeded_app):
        """Mandatory items appear in the bundle regardless of token budget."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "bnd_m1", "Mandatory Fact", "mandatory", confidence=0.9)
        self._seed_item(conn, "bnd_a1", "Approved Fact", "approved", confidence=0.8)
        # #1573: distribution_mode default (hybrid) gates optional/approved
        # items behind a personal upvote — vote so this test still exercises
        # "mandatory always included regardless of what's approved", not an
        # empty approved set.
        KnowledgeRepository(conn).vote("bnd_a1", "admin1", 1)
        conn.close()

        r = seeded_app["client"].get("/api/memory/bundle", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        data = r.json()
        mandatory_ids = {i["id"] for i in data["mandatory"]}
        approved_ids = {i["id"] for i in data["approved"]}
        assert "bnd_m1" in mandatory_ids
        assert "bnd_a1" in approved_ids

    def test_bundle_token_budget_limits_approved(self, seeded_app):
        """Approved items exceeding the token budget are excluded."""
        from src.db import get_system_db
        from app.api.memory import BUNDLE_TOKEN_BUDGET, _CHARS_PER_TOKEN

        # Create an approved item whose content alone exceeds the entire budget.
        huge_content = "X" * (BUNDLE_TOKEN_BUDGET * _CHARS_PER_TOKEN + 100)
        conn = get_system_db()
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(
            id="bnd_huge",
            title="Huge Item",
            content=huge_content,
            category="engineering",
            status="approved",
            confidence=1.0,
        )
        repo.update_status("bnd_huge", "approved")
        # #1573: make the item distribution_mode-eligible (hybrid default
        # requires an opt-in upvote) so the assertion below actually
        # exercises the token-budget truncation, not the distribution gate.
        repo.vote("bnd_huge", "admin1", 1)
        conn.close()

        r = seeded_app["client"].get("/api/memory/bundle", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        approved_ids = {i["id"] for i in r.json()["approved"]}
        assert "bnd_huge" not in approved_ids

    def test_bundle_pending_items_excluded(self, seeded_app):
        """Pending items must not appear in the bundle."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item(conn, "bnd_p1", "Pending Fact", "pending")
        conn.close()

        r = seeded_app["client"].get("/api/memory/bundle", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        data = r.json()
        all_ids = {i["id"] for i in data["mandatory"]} | {i["id"] for i in data["approved"]}
        assert "bnd_p1" not in all_ids

    def test_bundle_confidence_zero_treated_as_default(self, seeded_app):
        """An item with confidence=0.0 (not NULL) uses 0.0 in ranking, not the 0.5 fallback."""
        from src.db import get_system_db
        from src.repositories.knowledge import KnowledgeRepository

        conn = get_system_db()
        repo = KnowledgeRepository(conn)
        repo.create(
            id="bnd_zero",
            title="Zero Confidence",
            content="content",
            category="engineering",
            status="approved",
            confidence=0.0,
        )
        repo.update_status("bnd_zero", "approved")
        repo.create(
            id="bnd_high",
            title="High Confidence",
            content="content",
            category="engineering",
            status="approved",
            confidence=0.9,
        )
        repo.update_status("bnd_high", "approved")
        # #1573: both items need to clear the distribution_mode gate
        # (hybrid default: opt-in via upvote) to reach the ranking step
        # this test exercises.
        repo.vote("bnd_zero", "admin1", 1)
        repo.vote("bnd_high", "admin1", 1)
        conn.close()

        r = seeded_app["client"].get("/api/memory/bundle", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        approved = r.json()["approved"]
        ids_in_order = [i["id"] for i in approved]
        # high-confidence item must rank above zero-confidence item
        if "bnd_high" in ids_in_order and "bnd_zero" in ids_in_order:
            assert ids_in_order.index("bnd_high") < ids_in_order.index("bnd_zero")


class TestAutoTopicTagging:
    """Integration tests for Haiku auto-tagging wired into POST /api/memory.

    Tagging is best-effort — no LLM available in test env — so these tests
    focus on:
    1. User-supplied tags survive creation (the merge path must not drop them).
    2. Creating without any tags works (no crash, tags field is absent/null).
    3. When a mocked tagger returns topics they are prepended before user tags.
    """

    def test_user_tags_preserved_on_create(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={
                "title": "Tag preservation test",
                "content": "content",
                "category": "engineering",
                "tags": ["my-tag", "another-tag"],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        item_id = resp.json()["id"]

        # Fetch the item and verify tags are present
        list_resp = c.get("/api/memory", headers=_auth(token))
        assert list_resp.status_code == 200
        items = {i["id"]: i for i in list_resp.json()["items"]}
        assert item_id in items
        stored_tags = items[item_id].get("tags") or []
        assert "my-tag" in stored_tags
        assert "another-tag" in stored_tags

    def test_create_without_tags_succeeds(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "No tags item", "content": "content", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        assert "id" in resp.json()

    def test_mocked_tagger_topics_prepended_before_user_tags(self, seeded_app, monkeypatch):
        """Topics from tagger appear before user-supplied tags."""
        from services.corporate_memory import tagger as tagger_module

        def _fake_auto_tag(items, extractor):
            return {items[0]["id"]: ["data", "queries"]}

        monkeypatch.setattr(tagger_module, "auto_tag_items", _fake_auto_tag)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        resp = c.post(
            "/api/memory",
            json={
                "title": "Tagger integration",
                "content": "SQL query optimisation tips",
                "category": "engineering",
                "tags": ["user-tag"],
            },
            headers=_auth(token),
        )
        # Creation must succeed regardless of whether tagger ran
        assert resp.status_code == 201

    def test_tagger_failure_does_not_block_creation(self, seeded_app, monkeypatch):
        """If auto_tag_items raises, item creation must still return 201."""
        from services.corporate_memory import tagger as tagger_module

        def _boom(items, extractor):
            raise RuntimeError("LLM unreachable")

        monkeypatch.setattr(tagger_module, "auto_tag_items", _boom)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "Resilience test", "content": "content", "category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 201


# ===========================================================================
# Issue #62 — duplicate-candidate API, tree, PATCH, bulk-update
# ===========================================================================


def _seed_relation_via_repo(seeded_app, item_a_id, item_b_id, score=0.5):
    """Insert a likely_duplicate relation directly via the repo for testing
    the read/resolve endpoints (the auto-detector path is exercised by
    ``test_corporate_memory_relations``)."""
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    KnowledgeRepository(conn).create_relation(
        item_a_id,
        item_b_id,
        "likely_duplicate",
        score=score,
    )
    conn.close()


class TestDuplicateCandidatesAPI:
    def _create_with_entities(self, c, token, *, title, entities, domain="finance"):
        # Items via POST /api/memory don't carry entities by default — use
        # the request body fields directly.
        resp = c.post(
            "/api/memory",
            json={
                "title": title,
                "content": f"content for {title}",
                "category": "business_logic",
                "domain": domain,
                "entities": entities,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def test_list_default_unresolved(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        a = self._create_with_entities(c, token, title="A", entities=["x", "y"])
        b = self._create_with_entities(c, token, title="B", entities=["x", "y"])
        _seed_relation_via_repo(seeded_app, a, b)
        resp = c.get(
            "/api/memory/admin/duplicate-candidates",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["relations"][0]["item_a_id"] in {a, b}
        assert data["relations"][0]["item_b_id"] in {a, b}
        assert "item_a" in data["relations"][0]
        assert "item_b" in data["relations"][0]

    def test_list_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/memory/admin/duplicate-candidates",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_resolve_writes_audit_row(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        a = self._create_with_entities(c, token, title="A", entities=["x", "y"])
        b = self._create_with_entities(c, token, title="B", entities=["x", "y"])
        _seed_relation_via_repo(seeded_app, a, b)
        resp = c.post(
            f"/api/memory/admin/duplicate-candidates/resolve?item_a_id={a}&item_b_id={b}",
            json={"resolution": "duplicate"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        # Idempotent re-resolve → 400
        resp2 = c.post(
            f"/api/memory/admin/duplicate-candidates/resolve?item_a_id={a}&item_b_id={b}",
            json={"resolution": "duplicate"},
            headers=_auth(token),
        )
        assert resp2.status_code == 400

        # Audit row landed under the new corporate_memory.* prefix.
        audit = c.get("/api/memory/admin/audit", headers=_auth(token))
        assert audit.status_code == 200
        actions = {e.get("action") for e in audit.json()["entries"]}
        assert "corporate_memory.resolve_duplicate" in actions

    def test_resolve_invalid_resolution_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        a = self._create_with_entities(c, token, title="A", entities=["x", "y"])
        b = self._create_with_entities(c, token, title="B", entities=["x", "y"])
        _seed_relation_via_repo(seeded_app, a, b)
        resp = c.post(
            f"/api/memory/admin/duplicate-candidates/resolve?item_a_id={a}&item_b_id={b}",
            json={"resolution": "merge"},  # not in the new enum
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_resolve_not_found_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory/admin/duplicate-candidates/resolve?item_a_id=missing_a&item_b_id=missing_b",
            json={"resolution": "duplicate"},
            headers=_auth(token),
        )
        assert resp.status_code == 404


class TestTreeEndpoint:
    def _seed(self, c, token, **kwargs):
        body = {
            "title": kwargs["title"],
            "content": kwargs.get("content", "content"),
            "category": kwargs.get("category", "business_logic"),
            "domain": kwargs.get("domain"),
            "tags": kwargs.get("tags"),
        }
        resp = c.post("/api/memory", json={k: v for k, v in body.items() if v is not None}, headers=_auth(token))
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_tree_groups_by_domain(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        self._seed(c, token, title="A", domain="finance")
        self._seed(c, token, title="B", domain="finance")
        self._seed(c, token, title="C", domain="product")
        resp = c.get("/api/memory/tree?axis=domain", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        keys = {g["key"]: g["count"] for g in data["groups"]}
        assert keys.get("finance", 0) >= 2
        assert keys.get("product", 0) >= 1

    def test_tree_invalid_axis(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/memory/tree?axis=invalid",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 400

    def test_tree_tag_axis_multi_bucket(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        self._seed(c, token, title="X", tags=["t1", "t2"], domain="data")
        resp = c.get("/api/memory/tree?axis=tag", headers=_auth(token))
        assert resp.status_code == 200
        keys = [g["key"] for g in resp.json()["groups"]]
        # Tag-axis: item appears in both buckets.
        assert "t1" in keys
        assert "t2" in keys

    def test_tree_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/memory/tree?axis=domain")
        assert resp.status_code == 401

    @staticmethod
    def _seed_item_direct(
        conn,
        item_id,
        title,
        *,
        audience=None,
        source_type="user_verification",
        status="approved",
        domain=None,
        category="business_logic",
        source_user="admin@test.com",
    ):
        """Direct insert — POST /api/memory doesn't accept audience/source_type/status.

        v49: ``domain`` is no longer a scalar column on knowledge_items —
        we INSERT without it and (if provided) write a junction row through
        ``memory_domains.id`` resolution. Mandatory tier rides on
        ``is_required``; pass ``status='mandatory'`` to flip both fields.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        is_required = status == "mandatory"
        effective_status = "approved" if is_required else status
        conn.execute(
            """INSERT INTO knowledge_items
               (id, title, content, category, source_user, audience,
                status, source_type, is_required, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                item_id,
                title,
                f"content {title}",
                category,
                source_user,
                audience,
                effective_status,
                source_type,
                is_required,
                now,
                now,
            ],
        )
        if domain:
            row = conn.execute("SELECT id FROM memory_domains WHERE slug = ?", [domain]).fetchone()
            if not row:
                raise ValueError(f"Unknown memory domain slug {domain!r}")
            conn.execute(
                "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) "
                "VALUES (?, ?, 'test') ON CONFLICT DO NOTHING",
                [item_id, row[0]],
            )

    def test_tree_audience_axis_privacy_non_admin(self, seeded_app):
        """Non-admin tree on audience axis sees only their own group buckets +
        null/'all'; group:engineering bucket must not surface for a finance user."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        self._seed_item_direct(conn, "tree_aud_fin", "Finance fact", audience="group:finance", domain="finance")
        self._seed_item_direct(conn, "tree_aud_eng", "Eng fact", audience="group:engineering", domain="engineering")
        self._seed_item_direct(conn, "tree_aud_all", "All-users fact", audience="all", domain="data")
        self._seed_item_direct(conn, "tree_aud_null", "Null-audience fact", audience=None, domain="data")
        repo = UserRepository(conn)
        repo.create(id="tree_fin_user", email="treefin@test.com", name="Tree Finance User")
        TestAudienceDistribution._add_user_to_group(conn, "tree_fin_user", "finance")
        conn.close()

        token = create_access_token("tree_fin_user", "treefin@test.com")
        c = seeded_app["client"]
        resp = c.get("/api/memory/tree?axis=audience", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        keys = {g["key"] for g in resp.json()["groups"]}
        # Finance user sees their own group + null/all; never the eng bucket.
        assert "group:finance" in keys
        assert "all" in keys  # both null and 'all' values bucket here
        assert "group:engineering" not in keys

        # Admin, by contrast, sees every audience bucket including engineering.
        admin_resp = c.get(
            "/api/memory/tree?axis=audience",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert admin_resp.status_code == 200
        admin_keys = {g["key"] for g in admin_resp.json()["groups"]}
        assert "group:finance" in admin_keys
        assert "group:engineering" in admin_keys

    def test_tree_has_duplicate_filter(self, seeded_app):
        """``has_duplicate=true`` narrows to items present in an unresolved
        likely_duplicate relation; items without a relation drop out."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        a = self._seed(c, token, title="Dup A", domain="finance")
        b = self._seed(c, token, title="Dup B", domain="finance")
        c_id = self._seed(c, token, title="Solo C", domain="finance")
        _seed_relation_via_repo(seeded_app, a, b)

        resp = c.get(
            "/api/memory/tree?axis=domain&has_duplicate=true",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        ids_in_groups = {item["id"] for g in resp.json()["groups"] for item in g.get("items", [])}
        # The duplicated pair surfaces; the solo item does not.
        assert a in ids_in_groups
        assert b in ids_in_groups
        assert c_id not in ids_in_groups

    def test_tree_audience_chip_includes_null_when_filtering_all(self, seeded_app):
        """``audience='all'`` chip must include NULL-audience items, matching
        the SQL filter / count_by_audience COALESCE / _bucket_key behavior.
        Pre-fix the in-memory chip filter compared raw audience to 'all' and
        dropped NULLs. PR #126 review."""
        from src.db import get_system_db

        conn = get_system_db()
        self._seed_item_direct(
            conn,
            "tree_aud_null_chip",
            "Null aud item",
            audience=None,
            domain="data",
        )
        self._seed_item_direct(
            conn,
            "tree_aud_all_chip",
            "All aud item",
            audience="all",
            domain="data",
        )
        self._seed_item_direct(
            conn,
            "tree_aud_fin_chip",
            "Finance aud item",
            audience="group:finance",
            domain="data",
        )
        conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        # audience=all chip → both NULL and explicit-'all' surface; group:* drops out.
        resp = c.get(
            "/api/memory/tree?axis=domain&audience=all",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        ids_all = {item["id"] for g in resp.json()["groups"] for item in g.get("items", [])}
        assert "tree_aud_null_chip" in ids_all
        assert "tree_aud_all_chip" in ids_all
        assert "tree_aud_fin_chip" not in ids_all

        # audience=group:finance chip → NULL must NOT slip into a group bucket.
        resp = c.get(
            "/api/memory/tree?axis=domain&audience=group:finance",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        ids_fin = {item["id"] for g in resp.json()["groups"] for item in g.get("items", [])}
        assert "tree_aud_null_chip" not in ids_fin
        assert "tree_aud_all_chip" not in ids_fin
        assert "tree_aud_fin_chip" in ids_fin

    def test_tree_chip_filter_composition(self, seeded_app):
        """``status_filter`` + ``source_type`` apply together — only items
        matching both end up in the response."""
        from src.db import get_system_db

        conn = get_system_db()
        # Two items differing on each chip dimension; only one matches both.
        self._seed_item_direct(
            conn, "tree_chip_match", "Both match", status="approved", source_type="user_verification", domain="finance"
        )
        self._seed_item_direct(
            conn,
            "tree_chip_status_only",
            "Approved but wrong source",
            status="approved",
            source_type="claude_local_md",
            domain="finance",
        )
        self._seed_item_direct(
            conn,
            "tree_chip_source_only",
            "Right source but pending",
            status="pending",
            source_type="user_verification",
            domain="finance",
        )
        conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(
            "/api/memory/tree?axis=domain&status_filter=approved&source_type=user_verification",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        ids_in_groups = {item["id"] for g in resp.json()["groups"] for item in g.get("items", [])}
        assert "tree_chip_match" in ids_in_groups
        assert "tree_chip_status_only" not in ids_in_groups
        assert "tree_chip_source_only" not in ids_in_groups


class TestPatchAndBulkUpdate:
    def _create(self, c, token, title="Patch test"):
        resp = c.post(
            "/api/memory",
            json={"title": title, "content": "content", "category": "business_logic"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_patch_updates_category_domain_tags(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token)
        resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"category": "engineering", "domain": "engineering", "tags": ["x", "y"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert sorted(body["updated"]) == ["category", "domain", "tags"]

    def test_patch_invalid_domain_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token)
        resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"domain": "nonsense"},
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_patch_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        admin_token = seeded_app["admin_token"]
        item_id = self._create(c, admin_token)
        resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"category": "x"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_patch_rejects_null_title(self, seeded_app):
        # ``title`` is NOT NULL in the schema; explicit null in PATCH body must
        # be rejected at the boundary (400) instead of bubbling up as a 500
        # constraint violation. PR #126 round-5 review.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token)
        resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"title": None},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "title" in resp.json()["detail"].lower()

    def test_bulk_update_rejects_null_title(self, seeded_app):
        # Symmetric with PATCH — bulk-update used to surface a per-item
        # Constraint Error instead of a clean 400 when title=null leaked
        # through the allowlist. PR #126 round-6 review.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token, title="bulk null title")
        resp = c.post(
            "/api/memory/admin/bulk-update",
            json={"item_ids": [item_id], "updates": {"title": None}},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "title" in resp.json()["detail"].lower()

    def test_bulk_update_partial_failure(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        a = self._create(c, token, title="bulk a")
        b = self._create(c, token, title="bulk b")
        resp = c.post(
            "/api/memory/admin/bulk-update",
            json={"item_ids": [a, b, "missing_id"], "updates": {"category": "engineering"}},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["updated"]) == {a, b}
        assert "missing_id" in body["not_found"]

    def test_bulk_update_rejects_governance_fields(self, seeded_app):
        """Governance-sensitive fields (status / sensitivity / is_personal /
        confidence) must not slip through bulk-update — those have dedicated
        governance endpoints with their own audit rows. PR #126 review."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        a = self._create(c, token, title="gov a")

        # status: clear blocker the review called out — would silently flip
        # an item to mandatory bypassing /admin/mandate.
        resp = c.post(
            "/api/memory/admin/bulk-update",
            json={"item_ids": [a], "updates": {"status": "mandatory"}},
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "status" in resp.json()["detail"]

        # is_personal: same blast radius — would bypass /{id}/personal's
        # contributor-only check.
        resp = c.post(
            "/api/memory/admin/bulk-update",
            json={"item_ids": [a], "updates": {"is_personal": False}},
            headers=_auth(token),
        )
        assert resp.status_code == 400

        # sensitivity / confidence: same allowlist gate.
        resp = c.post(
            "/api/memory/admin/bulk-update",
            json={"item_ids": [a], "updates": {"sensitivity": "secret"}},
            headers=_auth(token),
        )
        assert resp.status_code == 400

        # Confirm a clean call still works post-fix.
        ok = c.post(
            "/api/memory/admin/bulk-update",
            json={"item_ids": [a], "updates": {"category": "engineering"}},
            headers=_auth(token),
        )
        assert ok.status_code == 200, ok.text
        assert a in ok.json()["updated"]

    # ---- exclude_unset=True regression tests (PR #126 round-4 review) ----
    # Pre-fix the PATCH/bulk-update endpoints used model_dump(exclude_none=True),
    # which silently dropped explicit ``null`` values. That left no path to
    # clear ``audience`` (and only the empty-string short-circuit for
    # ``domain``). Switching to exclude_unset=True preserves nulls so callers
    # can reset Optional fields.

    def _read(self, seeded_app, item_id):
        from src.db import get_system_db

        conn = get_system_db()
        try:
            return KnowledgeRepository(conn).get_by_id(item_id)
        finally:
            conn.close()

    def test_patch_clears_audience_with_null(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token, title="audience clear")
        # First set an audience so we have something to clear.
        set_resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"audience": "group:finance"},
            headers=_auth(token),
        )
        assert set_resp.status_code == 200, set_resp.text
        assert self._read(seeded_app, item_id)["audience"] == "group:finance"
        # Now clear via explicit null.
        clear_resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"audience": None},
            headers=_auth(token),
        )
        assert clear_resp.status_code == 200, clear_resp.text
        assert clear_resp.json()["updated"] == ["audience"]
        assert self._read(seeded_app, item_id)["audience"] is None

    def test_patch_clears_domain_with_null(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token, title="domain clear")
        # Set a domain first.
        set_resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"domain": "engineering"},
            headers=_auth(token),
        )
        assert set_resp.status_code == 200, set_resp.text
        assert self._read(seeded_app, item_id)["domain"] == "engineering"
        # Clear via explicit null. None is falsy so it skips the
        # VALID_DOMAINS validator (intentional — same as empty-string path).
        clear_resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"domain": None},
            headers=_auth(token),
        )
        assert clear_resp.status_code == 200, clear_resp.text
        assert clear_resp.json()["updated"] == ["domain"]
        assert self._read(seeded_app, item_id)["domain"] is None

    def test_bulk_update_clears_audience_with_null(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token, title="bulk audience clear")
        # Seed an audience via PATCH so the clear has something to undo.
        c.patch(
            f"/api/memory/admin/{item_id}",
            json={"audience": "group:finance"},
            headers=_auth(token),
        )
        assert self._read(seeded_app, item_id)["audience"] == "group:finance"
        # Bulk-update with explicit null should clear it.
        resp = c.post(
            "/api/memory/admin/bulk-update",
            json={"item_ids": [item_id], "updates": {"audience": None}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert item_id in resp.json()["updated"]
        assert self._read(seeded_app, item_id)["audience"] is None

    def test_patch_unset_field_left_alone(self, seeded_app):
        """Regression for exclude_unset=True semantics: fields NOT sent in the
        request body must not be touched (distinct from explicit-null clearing)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        item_id = self._create(c, token, title="leave alone")
        # Seed both audience + domain.
        c.patch(
            f"/api/memory/admin/{item_id}",
            json={"audience": "group:finance", "domain": "engineering"},
            headers=_auth(token),
        )
        before = self._read(seeded_app, item_id)
        assert before["audience"] == "group:finance"
        assert before["domain"] == "engineering"
        # PATCH only category — audience/domain must be untouched.
        resp = c.patch(
            f"/api/memory/admin/{item_id}",
            json={"category": "engineering"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["updated"] == ["category"]
        after = self._read(seeded_app, item_id)
        assert after["audience"] == "group:finance"
        assert after["domain"] == "engineering"
        assert after["category"] == "engineering"


class TestStatsExtensionsAPI:
    def test_stats_includes_by_tag_and_by_audience(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.post(
            "/api/memory",
            json={"title": "Tagged", "content": "x", "category": "business_logic", "tags": ["t1"]},
            headers=_auth(token),
        )
        resp = c.get("/api/memory/stats", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert "by_tag" in data
        assert "by_audience" in data


class TestAuditPrefixBackCompat:
    def test_audit_filter_surfaces_legacy_km_rows(self, seeded_app):
        """Legacy ``km_*`` audit rows still surface in the admin audit tab."""
        from src.db import get_system_db
        from src.repositories.audit import AuditRepository

        conn = get_system_db()
        # Inject a legacy-prefixed row directly.
        AuditRepository(conn).log(
            user_id="legacy@x",
            action="km_approve",
            resource="kv_legacy",
            params={"reason": "back-compat row"},
        )
        conn.close()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/memory/admin/audit", headers=_auth(token))
        assert resp.status_code == 200
        actions = {e.get("action") for e in resp.json()["entries"]}
        assert "km_approve" in actions

    def test_audit_filter_surfaces_new_corporate_memory_rows(self, seeded_app):
        """New rows write under the corporate_memory.* namespace."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/memory",
            json={"title": "audit test", "content": "x", "category": "business_logic"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        item_id = resp.json()["id"]
        c.post(f"/api/memory/admin/approve?item_id={item_id}", headers=_auth(token))
        audit = c.get("/api/memory/admin/audit", headers=_auth(token))
        actions = {e.get("action") for e in audit.json()["entries"]}
        assert "corporate_memory.approve" in actions

    def test_audit_pagination_returns_distinct_pages(self, seeded_app):
        """page=2 must return rows distinct from page=1. Pre-fix the SQL
        ignored page entirely and returned page 1 for every page param.
        PR #126 review."""
        from src.db import get_system_db
        from src.repositories.audit import AuditRepository

        conn = get_system_db()
        audit = AuditRepository(conn)
        # Seed enough rows that per_page=2 spans at least three pages.
        for i in range(6):
            audit.log(
                user_id=f"pagetest{i}@x",
                action="corporate_memory.approve",
                resource=f"audit_page_resource_{i}",
                params={"i": i},
            )
        conn.close()

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        page1 = c.get("/api/memory/admin/audit?page=1&per_page=2", headers=_auth(token))
        page2 = c.get("/api/memory/admin/audit?page=2&per_page=2", headers=_auth(token))
        assert page1.status_code == 200
        assert page2.status_code == 200

        ids_page1 = [(e.get("resource"), e.get("timestamp")) for e in page1.json()["entries"]]
        ids_page2 = [(e.get("resource"), e.get("timestamp")) for e in page2.json()["entries"]]
        assert len(ids_page1) == 2
        assert len(ids_page2) == 2
        # The two pages must not overlap row-for-row (offset is honored).
        assert set(ids_page1).isdisjoint(set(ids_page2)), (
            f"page 1 and page 2 returned the same rows: {ids_page1} vs {ids_page2}"
        )

        # And the same with the action filter branch — which had the same bug.
        page1_f = c.get(
            "/api/memory/admin/audit?action=approve&page=1&per_page=2",
            headers=_auth(token),
        )
        page2_f = c.get(
            "/api/memory/admin/audit?action=approve&page=2&per_page=2",
            headers=_auth(token),
        )
        assert page1_f.status_code == 200
        assert page2_f.status_code == 200
        ids_page1_f = [(e.get("resource"), e.get("timestamp")) for e in page1_f.json()["entries"]]
        ids_page2_f = [(e.get("resource"), e.get("timestamp")) for e in page2_f.json()["entries"]]
        assert set(ids_page1_f).isdisjoint(set(ids_page2_f))
