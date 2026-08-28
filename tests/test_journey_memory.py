"""J6 — Corporate memory lifecycle journey tests.

Full cycle: upload local-md → create knowledge item → list → vote → admin approve.
"""

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.journey
class TestMemoryJourney:
    def test_create_list_vote_approve(self, seeded_app, monkeypatch):
        """Full corporate memory lifecycle from creation to approval.

        Exercises the review-queue path specifically, so the instance is
        explicitly configured for it (#1573: with no corporate_memory
        config at all, items auto-approve in the documented legacy mode —
        see TestMemoryCreateRespectsApprovalMode in test_memory_api.py)."""
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_corporate_memory_config", lambda: {"approval_mode": "review_queue"})

        c = seeded_app["client"]
        admin_h = _auth(seeded_app["admin_token"])
        analyst_h = _auth(seeded_app["analyst_token"])

        # Step 1: Create knowledge item as analyst
        resp = c.post(
            "/api/memory",
            json={
                "title": "DuckDB query best practices",
                "content": "Always use parameterised queries to avoid SQL injection.",
                "category": "engineering",
                "tags": ["duckdb", "security"],
            },
            headers=analyst_h,
        )
        assert resp.status_code == 201
        item_id = resp.json()["id"]
        assert resp.json()["status"] == "pending"

        # Step 2: List items — should appear
        resp = c.get("/api/memory", headers=analyst_h)
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert item_id in ids

        # Step 3: Analyst upvotes the item
        resp = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=analyst_h,
        )
        assert resp.status_code == 200
        assert resp.json()["upvotes"] >= 1

        # Step 4: Admin approves
        resp = c.post(
            f"/api/memory/admin/approve?item_id={item_id}",
            headers=admin_h,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Step 5: Verify status in listing
        resp = c.get("/api/memory?status_filter=approved", headers=analyst_h)
        assert resp.status_code == 200
        approved_ids = [i["id"] for i in resp.json()["items"]]
        assert item_id in approved_ids

    def test_upload_local_md_creates_file(self, seeded_app):
        """Uploading CLAUDE.local.md content is stored correctly."""
        c = seeded_app["client"]
        analyst_h = _auth(seeded_app["analyst_token"])

        content = "# Local knowledge\n\nThis is my personal insight."
        resp = c.post(
            "/api/upload/local-md",
            json={"content": content},
            headers=analyst_h,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["size"] == len(content)

    def test_admin_can_reject_item(self, seeded_app):
        """Admin can reject a pending knowledge item."""
        c = seeded_app["client"]
        admin_h = _auth(seeded_app["admin_token"])

        resp = c.post(
            "/api/memory",
            json={"title": "Bad info", "content": "Wrong thing", "category": "misc"},
            headers=admin_h,
        )
        assert resp.status_code == 201
        item_id = resp.json()["id"]

        resp = c.post(
            f"/api/memory/admin/reject?item_id={item_id}",
            json={"reason": "Inaccurate"},
            headers=admin_h,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_vote_invalid_value_rejected(self, seeded_app):
        """Vote values other than 1 and -1 are rejected."""
        c = seeded_app["client"]
        analyst_h = _auth(seeded_app["analyst_token"])

        resp = c.post(
            "/api/memory",
            json={"title": "Test item", "content": "Some content", "category": "test"},
            headers=analyst_h,
        )
        item_id = resp.json()["id"]

        resp = c.post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 5},
            headers=analyst_h,
        )
        assert resp.status_code == 400

    def test_memory_stats_endpoint(self, seeded_app, monkeypatch):
        """Memory stats reflect created items.

        Configures review_queue explicitly (#1573) so the created item
        lands in the "pending" bucket this test asserts on, rather than
        relying on the no-config legacy default (auto-approve)."""
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_corporate_memory_config", lambda: {"approval_mode": "review_queue"})

        c = seeded_app["client"]
        admin_h = _auth(seeded_app["admin_token"])

        # Create an item
        c.post(
            "/api/memory",
            json={"title": "Stats test", "content": "Content", "category": "engineering"},
            headers=admin_h,
        )

        resp = c.get("/api/memory/stats", headers=admin_h)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert "by_status" in body
        assert "pending" in body["by_status"]
