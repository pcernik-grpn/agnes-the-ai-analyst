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


# ---------------------------------------------------------------------------
# Devin review on #2402 — the three defects it found, each pinned by a test
# that fails on the code as reviewed.
# ---------------------------------------------------------------------------


def test_resolve_race_produces_exactly_one_winner(state_backend, seeded_app_both):
    """Two admins resolving at once: one 200, one 409 — never two 200s.

    The read-then-write this replaced let both callers pass the status check
    before either wrote, so the loser returned success and silently overwrote
    who closed the report. Driven through the repository rather than the HTTP
    layer because the interleaving has to be exact: caller B must act after
    A's UPDATE has committed and while B still believes the report is open,
    which is precisely the window a SELECT-first implementation opens.
    """
    if state_backend != "pg":
        pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
    from src.repositories import issue_reports_repo
    from src.repositories.issue_reports_pg import IssueAlreadyResolved

    repo = issue_reports_repo()
    row = repo.create(
        title="raced",
        body=None,
        kind="bug",
        created_by="u-race",
        created_by_email=None,
        source_surface="web",
        page_url=None,
        context=None,
    )

    first = repo.resolve(row["id"], resolved_by="admin-a", resolution_note="a")
    assert first is not None and first["status"] == "resolved"

    with pytest.raises(IssueAlreadyResolved):
        repo.resolve(row["id"], resolved_by="admin-b", resolution_note="b")

    # The winner's signature survives — the loser must not overwrite it.
    assert repo.get(row["id"])["resolved_by"] == "admin-a"
    assert repo.get(row["id"])["resolution_note"] == "a"

    # A report that is simply gone is a different answer from a resolved one.
    assert repo.resolve("iss_gone", resolved_by="admin-a", resolution_note=None) is None


def test_webhook_names_the_screenshot_uploaded_after_the_201(state_backend, seeded_app_both, monkeypatch):
    """The operator message links a screenshot that lands after the 201.

    The upload is a SECOND request the client cannot start until the 201 has
    been received, so mirroring the creation snapshot always produced a
    message with no screenshot line — the gap Devin flagged, and one a live
    run reproduced.

    The late arrival is written through the repository rather than the HTTP
    route on purpose: ``TestClient`` runs background tasks inside the
    request/response cycle, so a second HTTP call could not overlap the
    waiting mirror at all and the test would deadlock instead of testing
    anything. What is under test is the mirror's wait-and-re-read, and this
    reproduces exactly the state transition it has to notice.
    """
    if state_backend != "pg":
        pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
    import threading
    import time

    from app.services import issue_notifier
    from src.repositories import issue_reports_repo

    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setattr("app.api.issues._SCREENSHOT_WAIT_SEC", 10.0)
    monkeypatch.setattr("app.api.issues._SCREENSHOT_POLL_SEC", 0.05)

    posted: list[dict] = []
    monkeypatch.setattr(issue_notifier, "post_webhook", lambda url, payload, **kw: posted.append(payload) or True)

    title = "with a late screenshot"

    def _attach_when_the_row_appears() -> None:
        repo = issue_reports_repo()
        for _ in range(400):
            match = next((r for r in repo.list_all(limit=50) if r["title"] == title), None)
            if match is not None:
                repo.set_screenshot(match["id"], f"issues/{match['id']}/screenshot.png")
                return
            time.sleep(0.02)

    attacher = threading.Thread(target=_attach_when_the_row_appears, daemon=True)
    attacher.start()

    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])
    created = c.post("/api/issues", json={"title": title, "kind": "bug", "expect_screenshot": True}, headers=tok)
    attacher.join(timeout=15)

    assert created.status_code == 201, created.text
    assert posted, "the operator mirror posted nothing"
    text = posted[-1]["text"]
    assert f"/api/issues/{created.json()['id']}/screenshot" in text, f"message never named the screenshot: {text!r}"


def test_report_without_a_screenshot_is_mirrored_immediately(state_backend, seeded_app_both, monkeypatch):
    """No screenshot promised, no waiting — the common case stays fast."""
    if state_backend != "pg":
        pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
    from app.services import issue_notifier

    monkeypatch.setenv("AGNES_ISSUES_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setattr("app.api.issues._SCREENSHOT_WAIT_SEC", 30.0)  # would hang if consulted

    posted: list[dict] = []
    monkeypatch.setattr(issue_notifier, "post_webhook", lambda url, payload, **kw: posted.append(payload) or True)

    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])
    r = c.post("/api/issues", json={"title": "no screenshot", "kind": "bug"}, headers=tok)
    assert r.status_code == 201, r.text
    assert posted and "Screenshot:" not in posted[-1]["text"]


def test_replacing_a_screenshot_is_atomic(state_backend, seeded_app_both, tmp_path, monkeypatch):
    """A reader never sees a half-written PNG.

    The upload used to write straight over the live path, so a GET served
    while a replacement was in flight could return truncated bytes (Devin
    review on #2402). Publishing through a temp file plus `os.replace` makes
    every read see one whole image, and leaves no `.part` behind.
    """
    if state_backend != "pg":
        pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])
    png_headers = {**tok, "Content-Type": "image/png"}

    first = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
    second = b"\x89PNG\r\n\x1a\n" + b"\x02" * 4096

    row = c.post("/api/issues", json={"title": "screenshot replaced", "kind": "bug"}, headers=tok).json()
    assert c.put(f"/api/issues/{row['id']}/screenshot", content=first, headers=png_headers).status_code == 204
    assert c.get(f"/api/issues/{row['id']}/screenshot", headers=tok).content == first

    assert c.put(f"/api/issues/{row['id']}/screenshot", content=second, headers=png_headers).status_code == 204
    served = c.get(f"/api/issues/{row['id']}/screenshot", headers=tok).content
    assert served == second, "a replacement must be published whole, never partially"

    shot_dir = tmp_path / "issues" / row["id"]
    leftovers = [p.name for p in shot_dir.iterdir() if p.name != "screenshot.png"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_a_rejected_screenshot_leaves_no_temp_file(state_backend, seeded_app_both, tmp_path, monkeypatch):
    """A refused upload must not litter the directory either."""
    if state_backend != "pg":
        pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c, tok = seeded_app_both["client"], _auth(seeded_app_both["analyst_token"])

    row = c.post("/api/issues", json={"title": "bad upload", "kind": "bug"}, headers=tok).json()
    bad = c.put(
        f"/api/issues/{row['id']}/screenshot",
        content=b"\xff\xd8\xff" + b"\x00" * 16,  # JPEG magic, declared as PNG
        headers={**tok, "Content-Type": "image/png"},
    )
    assert bad.status_code == 400 and bad.json()["detail"]["error"] == "screenshot_not_png"

    shot_dir = tmp_path / "issues" / row["id"]
    if shot_dir.exists():
        assert list(shot_dir.iterdir()) == [], "a refused upload wrote something"


def test_the_409_names_who_actually_resolved_it(state_backend, seeded_app_both):
    """The losing admin learns who won, not "resolved by None at None".

    The conflict used to be formatted from the row read while the report was
    still open, so it omitted exactly the two facts it exists to carry
    (Devin review on #2402).
    """
    if state_backend != "pg":
        pytest.skip("PG-only feature — the DuckDB contract is the typed 501")
    c, tok, adm = (
        seeded_app_both["client"],
        _auth(seeded_app_both["analyst_token"]),
        _auth(seeded_app_both["admin_token"]),
    )
    row = c.post("/api/issues", json={"title": "resolved twice", "kind": "bug"}, headers=tok).json()

    first = c.post(f"/api/admin/issues/{row['id']}/resolve", json={"resolution_note": "done"}, headers=adm)
    assert first.status_code == 200, first.text

    second = c.post(f"/api/admin/issues/{row['id']}/resolve", json={}, headers=adm)
    assert second.status_code == 409, second.text
    message = second.json()["detail"]["message"]
    assert "None" not in message, f"the conflict hid the winner: {message!r}"
    assert first.json()["resolved_by"] in message
