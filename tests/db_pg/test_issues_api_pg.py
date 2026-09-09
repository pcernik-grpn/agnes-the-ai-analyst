"""Issue reports — REST endpoints, on Postgres.

PG-side by necessity, not by preference: ``issue_reports``/``issue_comments``
are Postgres-only tables (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend
discipline"), so Postgres is the only backend on which any of this can run at
all. Pattern follows ``tests/db_pg/test_semantic_feedback_pg.py``'s
``TestTheEndpointsOnPostgres`` shape: ``state_backend``/``seeded_app_both``
parametrize both backends and every test skips on ``duckdb`` — the DuckDB
side's contract (RBAC + typed 501) is pinned in
``tests/test_issues_endpoint.py``.
"""

from __future__ import annotations

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BAD_PNG = b"\xff\xd8\xff" + b"\x00" * 16  # JPEG magic, not PNG


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_report_then_mine_then_show(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])

    r = c.post(
        "/api/issues",
        json={
            "title": "Tables render raw",
            "body": "while streaming",
            "kind": "bug",
            "page_url": "/chat?session=abc",
            "context": {"user_agent": "UA", "recent_errors": []},
        },
        headers=tok,
    )
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["status"] == "open" and row["source_surface"] == "web"
    assert row["context_json"]["app_version"] and "request_id" in row["context_json"]
    assert row["context_json"]["user_agent"] == "UA"

    mine = c.get("/api/issues/mine", headers=tok).json()
    assert mine["count"] == 1 and mine["data"][0]["id"] == row["id"] and mine["truncated"] is None

    show = c.get(f"/api/issues/{row['number']}", headers=tok).json()
    assert show["id"] == row["id"] and show["comments"] == []


def test_validation_errors_are_typed(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])

    assert c.post("/api/issues", json={"title": "   "}, headers=tok).json()["detail"]["error"] == "missing_title"

    bad_kind = c.post("/api/issues", json={"title": "x", "kind": "nope"}, headers=tok)
    assert bad_kind.status_code == 422

    # Every string LEAF is capped to 300 chars server-side, so a single huge
    # string does not trip the 32 KB ceiling — the volume has to survive
    # truncation: 64 keys (the dict cap) x a 50-item list (the list cap) of
    # 300-char strings each is ~960 KB even after every leaf is capped.
    big = {"title": "x", "context": {f"k{i}": ["a" * 300 for _ in range(50)] for i in range(64)}}
    assert c.post("/api/issues", json=big, headers=tok).json()["detail"]["error"] == "context_too_large"


def test_owner_boundary_is_404_not_403(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    from src.repositories import issue_reports_repo

    c = seeded_app_both["client"]
    theirs = issue_reports_repo().create(
        title="theirs",
        body=None,
        kind="bug",
        created_by="other-user",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )
    assert c.get(f"/api/issues/{theirs['id']}", headers=_auth(seeded_app_both["analyst_token"])).status_code == 404
    assert c.get(f"/api/issues/{theirs['id']}", headers=_auth(seeded_app_both["admin_token"])).status_code == 200
    put = c.put(
        f"/api/issues/{theirs['id']}/screenshot",
        content=PNG,
        headers={**_auth(seeded_app_both["analyst_token"]), "Content-Type": "image/png"},
    )
    assert put.status_code == 404


def test_screenshot_roundtrip_and_png_check(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])
    tmp_path = seeded_app_both["data_dir"]

    row = c.post("/api/issues", json={"title": "x"}, headers=tok).json()

    bad = c.put(f"/api/issues/{row['id']}/screenshot", content=BAD_PNG, headers={**tok, "Content-Type": "image/png"})
    assert bad.status_code == 400 and bad.json()["detail"]["error"] == "screenshot_not_png"

    ok = c.put(f"/api/issues/{row['id']}/screenshot", content=PNG, headers={**tok, "Content-Type": "image/png"})
    assert ok.status_code == 204
    assert (tmp_path / "issues" / row["id"] / "screenshot.png").read_bytes() == PNG

    got = c.get(f"/api/issues/{row['id']}/screenshot", headers=tok)
    assert got.status_code == 200 and got.headers["content-type"].startswith("image/png")
    assert "frame-ancestors" in got.headers.get("content-security-policy", "")

    assert (
        c.get(f"/api/issues/{row['id']}/screenshot", headers=_auth(seeded_app_both["admin_token"])).status_code == 200
    )


def test_screenshot_too_large_is_refused(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])
    row = c.post("/api/issues", json={"title": "x"}, headers=tok).json()

    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (3 * 1024 * 1024 + 1)
    resp = c.put(f"/api/issues/{row['id']}/screenshot", content=oversized, headers={**tok, "Content-Type": "image/png"})
    assert resp.status_code == 413 and resp.json()["detail"]["error"] == "screenshot_too_large"


def test_comments_and_resolve(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    c, tok, adm = (
        seeded_app_both["client"],
        _auth(seeded_app_both["analyst_token"]),
        _auth(seeded_app_both["admin_token"]),
    )
    row = c.post("/api/issues", json={"title": "x"}, headers=tok).json()

    mine = c.post(f"/api/issues/{row['id']}/comments", json={"body": "more detail"}, headers=tok).json()
    theirs = c.post(f"/api/issues/{row['id']}/comments", json={"body": "on it"}, headers=adm).json()
    assert (mine["author_kind"], theirs["author_kind"]) == ("reporter", "admin")

    assert c.get("/api/admin/issues", headers=adm).json()["count"] >= 1

    done = c.post(f"/api/admin/issues/{row['id']}/resolve", json={"resolution_note": "fixed"}, headers=adm)
    assert done.status_code == 200 and done.json()["status"] == "resolved"

    again = c.post(f"/api/admin/issues/{row['id']}/resolve", json={}, headers=adm)
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_resolved"

    assert [x["body"] for x in c.get(f"/api/issues/{row['id']}", headers=tok).json()["comments"]] == [
        "more detail",
        "on it",
    ]


def test_resolve_of_an_unknown_issue_is_a_404(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    resp = seeded_app_both["client"].post(
        "/api/admin/issues/iss_nope/resolve", json={}, headers=_auth(seeded_app_both["admin_token"])
    )
    assert resp.status_code == 404 and resp.json()["detail"]["error"] == "issue_not_found"


def test_queue_filters_on_status_and_refuses_unknown_status(state_backend, seeded_app_both):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    c, tok, adm = (
        seeded_app_both["client"],
        _auth(seeded_app_both["analyst_token"]),
        _auth(seeded_app_both["admin_token"]),
    )

    first = c.post("/api/issues", json={"title": "one"}, headers=tok).json()
    c.post("/api/issues", json={"title": "two"}, headers=tok)
    c.post(f"/api/admin/issues/{first['id']}/resolve", json={}, headers=adm)

    open_items = c.get("/api/admin/issues?status=open", headers=adm).json()["data"]
    assert [i["title"] for i in open_items] == ["two"]

    resolved_items = c.get("/api/admin/issues?status=resolved", headers=adm).json()["data"]
    assert [i["title"] for i in resolved_items] == ["one"]

    bad_status = c.get("/api/admin/issues?status=nope", headers=adm)
    assert bad_status.status_code == 400 and bad_status.json()["detail"]["error"] == "invalid_status"


def test_webhook_is_posted_in_the_background_and_marked(state_backend, seeded_app_both, monkeypatch):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    from app.services import issue_notifier

    seen = {}
    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setattr(issue_notifier, "post_webhook", lambda url, payload, **kw: seen.update(payload) or True)

    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])
    row = c.post("/api/issues", json={"title": "Tables render raw", "kind": "bug"}, headers=tok).json()

    assert f"#{row['number']} (bug)" in seen["text"]
    assert c.get(f"/api/issues/{row['id']}", headers=tok).json()["webhook_delivered_at"] is not None


def test_unconfigured_webhook_leaves_delivery_null(state_backend, seeded_app_both, monkeypatch):
    if state_backend != "pg":
        pytest.skip("PG-only feature")
    monkeypatch.delenv("AGNES_ISSUES_WEBHOOK_URL", raising=False)
    from app.services import issue_notifier

    monkeypatch.setattr(issue_notifier, "_config_value", lambda: "")

    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])
    row = c.post("/api/issues", json={"title": "x"}, headers=tok).json()
    assert row["webhook_delivered_at"] is None
